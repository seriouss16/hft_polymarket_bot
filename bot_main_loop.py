"""Main async trading loop for the HFT bot."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback

from bot_config_log import validate_required_config
from bot_runtime import UVLOOP_ACTIVE
from core.executor import PnLTracker, mark_price_for_side
from core.live_common import reconcile_binary_outcome_books
from core.live_engine import LiveExecutionEngine, LiveRiskManager
from core.market_regime import MarketRegimeDetector
from core.risk_engine import RiskEngine
from core.selector import MarketSelector
from core.session_profile import apply_profile, maybe_switch_profile
from core.strategies import LatencyArbitrageStrategy, PhaseRouterStrategy
from core.strategy_hub import StrategyHub
from data.aggregator import FastPriceAggregator
from data.providers import FastExchangeProvider
from data.poly_clob import PolyOrderBook
from ml.model import AsyncLSTMPredictor
from utils.env_config import req_float, req_str
from utils.stats import StatsCollector
from utils.trade_journal import TradeJournal


def _conditional_token_for_position_side(
    position_side: str | None,
    token_up_id: str | None,
    token_down_id: str | None,
) -> str | None:
    """Return CLOB conditional token id for an open position.

    ``PnLTracker.position_side`` uses \"UP\"/\"DOWN\"; the engine may emit
    ``BUY_UP``/``BUY_DOWN``. All of these must map to the correct outcome token.
    """
    if position_side in ("BUY_UP", "UP", None):
        return token_up_id
    if position_side in ("BUY_DOWN", "DOWN"):
        return token_down_id or token_up_id
    logging.warning(
        "[LIVE] Unrecognized position side %r — using UP token.",
        position_side,
    )
    return token_up_id


async def main():
    if UVLOOP_ACTIVE:
        logging.info("asyncio: uvloop event loop policy active")

    # --- Конфигурация ---
    LIVE_MODE = os.getenv("LIVE_MODE", "0") == "1"
    validate_required_config(LIVE_MODE)

    # Apply day/night session profile before any strategy objects read env vars.
    apply_profile(force=True)

    BYPASS_META_GATE = os.getenv("HFT_BYPASS_META_GATE", "1") == "1"
    USE_SMART_FAST = os.getenv("USE_SMART_FAST", "0") == "1"
    SYMBOL = "BTC"
    STATS_INTERVAL = float(os.environ["STATS_INTERVAL_SEC"])
    # PULSE_INTERVAL_SEC>0: at most one Fast: line per N seconds. When 0, use HFT_FAST_LOG_MIN_SEC.
    PULSE_INTERVAL = req_float("PULSE_INTERVAL_SEC")
    FAST_LOG_MIN_SEC = req_float("HFT_FAST_LOG_MIN_SEC")
    pulse_log_period = PULSE_INTERVAL if PULSE_INTERVAL > 0.0 else FAST_LOG_MIN_SEC
    MAIN_LOOP_SLEEP = req_float("HFT_LOOP_SLEEP_SEC")
    CLOB_PULL_INTERVAL = req_float("CLOB_BOOK_PULL_SEC")
    LSTM_MIN_INTERVAL = req_float("LSTM_INFERENCE_SEC")
    ENABLE_LSTM = os.getenv("HFT_ENABLE_LSTM") == "1"
    SLOT_POLL_SEC = req_float("HFT_SLOT_POLL_SEC")
    # When HFT_SLOT_POLL_SEC=0, slot/market checks still run at most once per this interval.
    MIN_SLOT_POLL_SEC = req_float("HFT_MIN_SLOT_POLL_SEC")

    # --- Инициализация компонентов ---
    # Signal path: identical to SIM (process_tick + execute → log_trade). LIVE only
    # changes (1) PnLTracker.live_mode so log_trade suppresses ledger updates until
    # live_open/live_close, and (2) the block below that sends real CLOB orders.
    selector = MarketSelector(asset=SYMBOL)
    aggregator = FastPriceAggregator()
    pnl = PnLTracker(live_mode=LIVE_MODE)
    stats = StatsCollector(pnl)
    regime_detector = MarketRegimeDetector()
    strategy_hub = StrategyHub()
    strategy_hub.register(LatencyArbitrageStrategy(pnl))
    if os.getenv("HFT_ENABLE_PHASE_ROUTING") == "1":
        strategy_hub.register(PhaseRouterStrategy(pnl))
    default_strategy = os.getenv(
        "HFT_ACTIVE_STRATEGY",
        "phase_router" if os.getenv("HFT_ENABLE_PHASE_ROUTING") == "1" else "latency_arbitrage",
    )
    if default_strategy in strategy_hub.list_strategies():
        strategy_hub.set_active(default_strategy)
    strategy_hub.enable_parallel(os.getenv("HFT_PARALLEL_STRATEGIES") == "1")
    lstm = AsyncLSTMPredictor(history_len=100)
    live_exec = LiveExecutionEngine(
        private_key=os.getenv("PRIVATE_KEY"),
        funder=os.getenv("FUNDER") or os.getenv("POLY_FUNDER_ADDRESS"),
        test_mode=not LIVE_MODE,
        min_order_size=float(os.environ["LIVE_ORDER_SIZE"]),
        max_spread=float(os.environ["LIVE_MAX_SPREAD"]),
    )
    live_risk = LiveRiskManager(max_session_loss=float(os.environ["LIVE_MAX_SESSION_LOSS"]))
    risk = RiskEngine(
        max_drawdown_pct=float(os.environ["MAX_DRAWDOWN_PCT"]),
        max_position_pct=float(os.environ["MAX_POSITION_PCT"]),
        loss_cooldown_sec=float(os.environ["LOSS_COOLDOWN_SEC"]),
    )
    journal = TradeJournal(path=req_str("TRADE_JOURNAL_PATH"))

    # Validate session deposit against real account balance in live mode.
    _session_deposit = float(os.environ["HFT_DEPOSIT_USD"])
    if LIVE_MODE:
        # Refresh USDC and CTF conditional token allowances so SELL orders are accepted.
        # Without CTF allowance the CLOB rejects every SELL with "not enough balance".
        await asyncio.to_thread(live_exec.ensure_allowances)
        _live_account_balance_limit = req_float("LIVE_ACCOUNT_BALANCE")
        _account_balance = live_exec.fetch_usdc_balance()
        _effective_account = _account_balance if _account_balance is not None else _live_account_balance_limit
        if _effective_account > 0.0 and _session_deposit > _effective_account:
            raise SystemExit(
                f"\n{'='*60}\n"
                f"🛑  STARTUP ABORTED — session deposit exceeds account balance:\n"
                f"  HFT_DEPOSIT_USD = {_session_deposit:.2f} USD  (session budget)\n"
                f"  Account balance = {_effective_account:.2f} USD  (Polymarket USDC)\n"
                f"  Set HFT_DEPOSIT_USD <= {_effective_account:.2f} to proceed.\n"
                f"{'='*60}\n"
            )
        if _effective_account > 0.0:
            logging.info(
                "💰 Account balance check: session=%.2f USD  account=%.2f USD  margin=%.2f USD",
                _session_deposit, _effective_account, _effective_account - _session_deposit,
            )
        else:
            logging.warning(
                "⚠️  Could not verify Polymarket account balance. "
                "Proceeding with session deposit=%.2f USD. "
                "Set LIVE_ACCOUNT_BALANCE in config for offline validation.",
                _session_deposit,
            )
    
    if ENABLE_LSTM:
        import tensorflow as tf
        tf.config.set_visible_devices([], 'GPU')

    # --- Запуск провайдеров быстрых цен (Coinbase anchor + Binance lead) ---
    providers = [
        FastExchangeProvider("binance", "wss://stream.binance.com:9443", "BTC", aggregator.update),
        FastExchangeProvider("coinbase", "wss://ws-feed.exchange.coinbase.com", "BTC-USD", aggregator.update)
    ]
    provider_tasks: list[asyncio.Task] = [
        asyncio.create_task(p.connect()) for p in providers
    ]
    poly_connect_task: asyncio.Task | None = None
    _heartbeat_task: asyncio.Task | None = None

    if LIVE_MODE and live_exec.client is not None:
        _heartbeat_interval_sec = req_float("LIVE_HEARTBEAT_INTERVAL_SEC")

        async def _run_heartbeat() -> None:
            """Send Polymarket CLOB heartbeat periodically to keep open orders alive.

            Without a valid heartbeat every ≤15 s the CLOB cancels all open orders.
            Interval is ``LIVE_HEARTBEAT_INTERVAL_SEC`` (default 5 s). Errors are logged
            but do not stop the loop.
            """
            _hb_id = ""
            while True:
                try:
                    resp = await asyncio.to_thread(live_exec.client.post_heartbeat, _hb_id)
                    _hb_id = resp.get("heartbeat_id", "") if isinstance(resp, dict) else getattr(resp, "heartbeat_id", "")
                except Exception as _hb_exc:
                    logging.debug("[LIVE] Heartbeat failed: %s", _hb_exc)
                await asyncio.sleep(_heartbeat_interval_sec)

        _heartbeat_task = asyncio.create_task(_run_heartbeat())

    token_up_id = None
    token_down_id = None
    current_slug = None
    poly_book = None
    last_stats_time = asyncio.get_event_loop().time()
    last_pulse_time = 0
    _regime_last_price: float = 0.0
    _regime_last_ts: float = 0.0
    # Timestamp until which live OPEN entries are suppressed after a live BUY skip.
    # Prevents the engine from accumulating phantom sim positions when the CLOB
    # rejects every entry due to insufficient balance for the minimum share count.
    _live_skip_until: float = 0.0
    _live_skip_cooldown_sec = req_float("LIVE_SKIP_COOLDOWN_SEC")
    _live_inventory_reconcile_sec = float(os.getenv("LIVE_INVENTORY_RECONCILE_SEC", "5"))
    _last_live_reconcile_ts = 0.0
    last_lstm_time = 0
    last_book_pull_time = 0
    last_book_mismatch_warn_time = 0.0
    last_clob_pull_fail_log_time = 0.0
    forecast = 0.0
    last_slot_check_time = 0.0
    last_slot_ts: int | None = None
    last_skew_warn_time = 0.0
    last_high_latency_warn_time = 0.0
    # BTC Chainlink price recorded at the moment the slot opens.
    # The market resolves "Up" if BTC-end >= BTC-start, so this price acts as
    # the natural target/anchor for trader position bias within the slot.
    slot_anchor_price: float = 0.0

    async def _reconcile_live_inventory_maybe(poly_book_obj) -> None:
        """If chain or CLOB shows outcome shares but PnL is flat, sync live_open for EXIT."""
        if not LIVE_MODE or poly_book_obj is None:
            return
        if pnl.inventory > 1e-9:
            return
        # After a successful live_close, chain/CLOB can briefly still report the old
        # conditional balance — adopting here would debit balance again and corrupt
        # the session ledger (see false "inventory_reconcile" after SELL).
        _after_close = float(os.getenv("LIVE_INVENTORY_RECONCILE_AFTER_CLOSE_SEC", "45"))
        if _after_close > 0 and float(getattr(pnl, "last_close_ts", 0.0) or 0.0) > 0:
            if time.time() - float(pnl.last_close_ts) < _after_close:
                return
        _tup = token_up_id
        _tdn = token_down_id
        if not _tup:
            return
        _poly_min = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))
        _dust = float(os.getenv("LIVE_INVENTORY_DUST_SHARES", "0.02"))
        _floor = max(_dust, 0.01)
        _cands: list[tuple[str, str, float]] = []
        try:
            for _tid, _sig in ((_tup, "BUY_UP"), (_tdn or "", "BUY_DOWN")):
                if not _tid:
                    continue
                if _sig == "BUY_DOWN" and not _tdn:
                    continue
                if live_exec.has_pending_buy(_tid) or live_exec.has_pending_sell(_tid):
                    continue
                _mem = live_exec.filled_buy_shares(_tid)
                _ch = await asyncio.to_thread(live_exec.fetch_conditional_balance, _tid)
                _chv = float(_ch) if _ch is not None else 0.0
                # Chain truth beats stale in-memory FILLED rows in _active_orders after
                # clear_filled_buy() (mem can show 5.47 sh while chain is dust — phantom
                # adopt; see bot_300326_* logs).
                if (
                    _ch is not None
                    and _chv < _poly_min - 1e-6
                    and _mem >= _poly_min - 1e-6
                ):
                    logging.debug(
                        "[LIVE] inventory reconcile skip token=%s: mem=%.4f sh but "
                        "chain=%.4f — stale filled_buy_shares.",
                        _tid[:20],
                        _mem,
                        _chv,
                    )
                    continue
                if _chv >= _poly_min - 1e-6:
                    _sh = _chv if _mem < _poly_min - 1e-6 else min(_mem, _chv)
                elif _mem >= _poly_min - 1e-6:
                    _sh = _mem
                elif _dust < _chv < _poly_min - 1e-6:
                    _sh = _chv
                else:
                    continue
                if _sh < _floor:
                    continue
                _cands.append((_tid, _sig, _sh))
        except Exception as exc:
            logging.warning("[LIVE] inventory reconcile probe failed: %s", exc)
            return
        if len(_cands) >= 2:
            logging.warning(
                "🛟 [LIVE] INVENTORY RECONCILE: balance on both outcome tokens — "
                "using larger position.",
            )
            _cands.sort(key=lambda x: -x[2])
        if not _cands:
            return
        _tid, _sig, _sh = _cands[0]
        _book = poly_book_obj.book
        if _sig == "BUY_UP":
            _px = float(_book.get("ask", 0.0) or 0.0)
        else:
            _px = float(_book.get("down_ask", 0.0) or 0.0)
        if _px <= 0.0:
            _px = 0.5
        logging.warning(
            "🛟 [LIVE] INVENTORY RECONCILE: chain/CLOB vs PnL desync — "
            "adopting %.4f sh @ ~%.4f (token=%s). Bot EXIT can run.",
            _sh,
            _px,
            _tid[:20],
        )
        live_exec.sync_confirmed_fill(_tid, _sh)
        pnl.live_open(
            _sig,
            _sh,
            _px,
            _sh * _px,
            strategy_name="inventory_reconcile",
        )
        _hft_eng = getattr(
            strategy_hub.get_active_strategy(),
            "_engine",
            None,
        )
        if _hft_eng is not None and getattr(
            _hft_eng, "_live_entry_sync_pending", False
        ):
            _apply_fast = fast_price
            if USE_SMART_FAST:
                _nf = aggregator.get_weighted_price()
            else:
                _nf = aggregator.get_coinbase_price() or aggregator.get_weighted_price()
            if _nf is not None:
                _apply_fast = float(_nf)
            _book_px = float(
                _book.get("down_ask", 0.0)
                if _sig == "BUY_DOWN"
                else _book.get("ask", 0.0)
            )
            _hft_eng.apply_live_entry_after_fill(
                _book,
                _apply_fast,
                _book_px,
                float(_px),
                float(_sh),
                float(_sh * _px),
            )
        try:
            await asyncio.to_thread(live_exec.ensure_conditional_allowance, _tid)
        except Exception as _ea_exc:
            logging.debug("[LIVE] reconcile ensure_conditional_allowance: %s", _ea_exc)

    logging.info("🔥 Система запущена. Ожидание первого слота Polymarket...")
    if ENABLE_LSTM:
        logging.info("HFT_ENABLE_LSTM=1: TensorFlow LSTM inference on (higher CPU).")
    else:
        logging.info(
            "HFT_ENABLE_LSTM=0: LSTM off; forecast tracks spot. Set HFT_ENABLE_LSTM=1 to enable."
        )

    shutdown_reason = "shutdown"
    try:
        while True:
            now = asyncio.get_event_loop().time()

            if LIVE_MODE and live_risk.session_loss_breached():
                logging.error(
                    "🛑 Session loss limit reached — stopping bot "
                    "(session_pnl=%.4f, LIVE_MAX_SESSION_LOSS=%.4f).",
                    live_risk.pnl,
                    live_risk.max_session_loss,
                )
                shutdown_reason = "session_loss_limit"
                break

            # Periodic stats before any await: slot/orderbook/strategy work must not delay the report.
            if STATS_INTERVAL > 0.0 and (now - last_stats_time >= STATS_INTERVAL):
                if LIVE_MODE:
                    try:
                        _st_usdc = await asyncio.to_thread(live_exec.fetch_usdc_balance)
                        stats.set_live_wallet_usdc(_st_usdc)
                    except Exception as _st_exc:
                        logging.debug("fetch_usdc_balance for stats: %s", _st_exc)
                        stats.set_live_wallet_usdc(None)
                else:
                    stats.set_live_wallet_usdc(None)
                stats.show_report()
                logging.info(
                    "Intermediate stats (STATS_INTERVAL_SEC=%s, loop.now=%.3f).",
                    STATS_INTERVAL,
                    now,
                )
                last_stats_time = now

            # Check day/night session boundary and reapply profile if needed.
            _switched = maybe_switch_profile()
            if _switched is not None:
                # Re-read all session-profile-controlled params into running engines.
                strategy_hub.reload_profile_params()
                risk.reload_profile_params()

            # 1. Авто-переключение слота.
            # React immediately when UTC time crosses an exact 5m boundary.
            slot_poll = SLOT_POLL_SEC if SLOT_POLL_SEC > 0.0 else MIN_SLOT_POLL_SEC
            slot_poll = max(slot_poll, MIN_SLOT_POLL_SEC)
            current_slot_ts = selector.get_current_slot_timestamp()
            slot_boundary_crossed = last_slot_ts is not None and current_slot_ts != last_slot_ts
            should_check_slot = slot_boundary_crossed or (now - last_slot_check_time) >= slot_poll
            if should_check_slot:
                last_slot_check_time = now
                ts = current_slot_ts
                if last_slot_ts is None:
                    last_slot_ts = ts
                elif slot_boundary_crossed:
                    logging.info("🕒 Новый 5m-слот: UTC boundary ts=%s.", ts)
                    last_slot_ts = ts
                slug = selector.format_slug(ts)
                current_slug = slug
                up_id, down_id, question = await selector.fetch_up_down_token_ids(slug)

                if up_id and (up_id != token_up_id or down_id != token_down_id):
                    if pnl.inventory > 0:
                        # Never swap token IDs while a position is open — the CLOSE
                        # logic uses token_up_id/token_down_id to route the SELL order.
                        # A stale API response or slug re-parse returning tokens in
                        # a different order would send the SELL to the wrong contract.
                        logging.warning(
                            "⚠️ Token ID change detected while position open "
                            "(inventory=%.4f side=%s) — deferring until position closed. "
                            "Old up=%s down=%s | New up=%s down=%s",
                            pnl.inventory, pnl.position_side,
                            (token_up_id or "")[:16], (token_down_id or "")[:16],
                            up_id[:16], (down_id or "")[:16],
                        )
                    else:
                        _anchor_now = float(
                            poly_book.book.get("btc_oracle")
                            or poly_book.book.get("mid")
                            or 0.0
                        ) if poly_book is not None else 0.0
                        if _anchor_now > 0.0:
                            slot_anchor_price = _anchor_now
                        logging.info(
                            "🎯 Смена рынка: %s | anchor=%.2f",
                            question,
                            slot_anchor_price,
                        )
                        token_up_id = up_id
                        token_down_id = down_id
                        strategy_hub.reset_for_new_market()
                        # Reset orderbook cache to avoid stale data from previous market
                        if poly_book and hasattr(poly_book, 'book'):
                            poly_book.book.clear()
                            poly_book.book["bid"] = 0.0
                            poly_book.book["ask"] = 1.0
                            poly_book.book["down_bid"] = 1.0
                            poly_book.book["down_ask"] = 0.0
                        if os.getenv("HFT_PERF_RESET_ON_NEW_MARKET") == "1":
                            pnl.reset_strategy_performance()
                        if poly_connect_task is not None and not poly_connect_task.done():
                            poly_connect_task.cancel()
                            try:
                                await poly_connect_task
                            except asyncio.CancelledError:
                                pass
                            except Exception as exc:
                                logging.debug(
                                    "Poly RTDS task ended after market switch cancel: %s",
                                    exc,
                                )
                        poly_book = PolyOrderBook(symbol="bitcoin")
                        poly_connect_task = asyncio.create_task(poly_book.connect())

            # 2. Получение данных
            _net_dbg = os.getenv("HFT_NETWORK_TIMING_DEBUG") == "1"
            if _net_dbg:
                _nw_t0 = time.perf_counter()
            if USE_SMART_FAST:
                fast_price = aggregator.get_weighted_price()
            else:
                fast_price = aggregator.get_coinbase_price() or aggregator.get_weighted_price()
            primary_data = aggregator.get_primary_history()
            if _net_dbg:
                _nw_t1 = time.perf_counter()
            
            # 3. LSTM is optional: engine ignores forecast; keep off by default for lower CPU latency.
            if ENABLE_LSTM and primary_data and (
                LSTM_MIN_INTERVAL <= 0.0 or (now - last_lstm_time) >= LSTM_MIN_INTERVAL
            ):
                forecast = await lstm.predict(primary_data)
                last_lstm_time = now
            elif fast_price:
                forecast = float(fast_price)

            # Fast-start fallback: keep forecast on realistic price scale before warmup.
            if fast_price and (forecast <= 0 or abs(forecast - fast_price) > 0.2 * fast_price):
                forecast = float(fast_price)

            # 4. Анализ и "Пульс"
            poly_btc = 0.0
            if poly_book is not None:
                poly_btc = float(
                    poly_book.book.get("btc_oracle")
                    or poly_book.book.get("mid")
                    or 0.0
                )
            # RTDS connects asynchronously; at market switch btc_oracle is often still 0.
            # Latch anchor on the first Chainlink tick so Anchor/Δ and _anchor_gate see a real target.
            if (
                slot_anchor_price <= 0.0
                and token_up_id
                and poly_btc > 0.0
            ):
                slot_anchor_price = float(poly_btc)
                logging.info(
                    "🎯 Slot anchor latched from Chainlink RTDS: %.2f",
                    slot_anchor_price,
                )
            if fast_price and poly_book is not None and poly_btc > 0:
                if token_up_id and (
                    CLOB_PULL_INTERVAL <= 0.0
                    or (now - last_book_pull_time) >= CLOB_PULL_INTERVAL
                ):
                    if _net_dbg:
                        _nw_t2 = time.perf_counter()
                    try:
                        up_bid = 0.0
                        up_ask = 0.0
                        down_bid = 0.0
                        down_ask = 0.0

                        if token_down_id:
                            ob_up, ob_down = await asyncio.gather(
                                asyncio.to_thread(
                                    live_exec.get_orderbook_snapshot, token_up_id, 5
                                ),
                                asyncio.to_thread(
                                    live_exec.get_orderbook_snapshot, token_down_id, 5
                                ),
                            )
                        else:
                            ob_up = await asyncio.to_thread(
                                live_exec.get_orderbook_snapshot, token_up_id, 5
                            )
                            ob_down = {}
                        up_bid = float(ob_up.get("best_bid", 0.0))
                        up_ask = float(ob_up.get("best_ask", 0.0))
                        if token_down_id:
                            down_bid = float(ob_down.get("best_bid", 0.0))
                            down_ask = float(ob_down.get("best_ask", 0.0))

                        up_valid = 0.0 < up_bid < up_ask <= 1.0
                        down_valid = 0.0 < down_bid < down_ask <= 1.0
                        if (not up_valid or not down_valid) and current_slug:
                            q = await selector.fetch_up_down_quotes(current_slug, token_up_id, token_down_id)
                            if not up_valid:
                                up_bid = float(q.get("up_bid", 0.0))
                                up_ask = float(q.get("up_ask", 0.0))
                                up_valid = 0.0 < up_bid < up_ask <= 1.0
                            if not down_valid:
                                down_bid = float(q.get("down_bid", 0.0))
                                down_ask = float(q.get("down_ask", 0.0))
                                down_valid = 0.0 < down_bid < down_ask <= 1.0

                        if up_valid:
                            poly_book.book["bid"] = up_bid
                            poly_book.book["ask"] = up_ask
                            poly_book.book["bid_size_top"] = float(ob_up.get("bid_size_top", poly_book.book.get("bid_size_top", 1.0)))
                            poly_book.book["ask_size_top"] = float(ob_up.get("ask_size_top", poly_book.book.get("ask_size_top", 1.0)))
                        else:
                            # Reset stale UP data to force complement calculation
                            poly_book.book.pop("bid", None)
                            poly_book.book.pop("ask", None)
                        if down_valid:
                            poly_book.book["down_bid"] = down_bid
                            poly_book.book["down_ask"] = down_ask
                            if isinstance(ob_down, dict) and ob_down:
                                poly_book.book["down_bid_size_top"] = float(
                                    ob_down.get("bid_size_top", 0.0)
                                )
                                poly_book.book["down_ask_size_top"] = float(
                                    ob_down.get("ask_size_top", 0.0)
                                )
                        else:
                            # Reset stale DOWN data to force complement calculation
                            poly_book.book.pop("down_bid", None)
                            poly_book.book.pop("down_ask", None)
                    except Exception as exc:
                        if (now - last_clob_pull_fail_log_time) >= 90.0:
                            logging.warning("CLOB book pull failed: %s", exc)
                            last_clob_pull_fail_log_time = now
                        else:
                            logging.debug("CLOB book pull failed (rate-limited): %s", exc)
                    last_book_pull_time = now
                    if _net_dbg:
                        _nw_t3 = time.perf_counter()
                        logging.info(
                            "NetworkCheck read_fast=%.1fms clob_roundtrip=%.1fms",
                            (_nw_t1 - _nw_t0) * 1000.0,
                            (_nw_t3 - _nw_t2) * 1000.0,
                        )

                # Re-read fast anchor after CLOB awaits; feeds advance while the event loop is in thread/network work.
                _fp_before_refresh = fast_price
                if USE_SMART_FAST:
                    fast_price = aggregator.get_weighted_price()
                else:
                    fast_price = aggregator.get_coinbase_price() or aggregator.get_weighted_price()
                if fast_price is None:
                    fast_price = _fp_before_refresh
                if fast_price and (forecast <= 0 or abs(forecast - fast_price) > 0.2 * fast_price):
                    forecast = float(fast_price)

                aggregator.add_history(fast_price)
                zscore = aggregator.get_zscore()
                _ft = aggregator.feed_timing(float(poly_book.book.get("ts", 0.0)))
                latency_ms = float(_ft["staleness_ms"])
                skew_ms = float(_ft["skew_ms"])
                if (
                    strategy_hub.entry_max_latency_ms > 0.0
                    and latency_ms > strategy_hub.entry_max_latency_ms
                    and (now - last_high_latency_warn_time) >= 30.0
                ):
                    logging.info(
                        "Feed staleness %.0f ms above entry_max_latency_ms=%.0f (engine may block entries).",
                        latency_ms,
                        strategy_hub.entry_max_latency_ms,
                    )
                    last_high_latency_warn_time = now
                if (
                    abs(skew_ms) > 800.0
                    and (now - last_skew_warn_time) >= 300.0
                ):
                    logging.info(
                        "Cross-feed skew skew_ms=%.0f (cb_age=%.0f poly_age=%.0f ms); "
                        "not wall-clock NTP — local recv order of WS messages.",
                        skew_ms,
                        float(_ft["coinbase_age_ms"]),
                        float(_ft["poly_age_ms"]),
                    )
                    last_skew_warn_time = now
                # Feed regime detector using raw fast-price velocity (pts/s)
                # rather than edge_window speed which flips 0↔large every other tick.
                _regime_dt = now - _regime_last_ts if _regime_last_ts > 0 else 1.0
                _regime_speed = abs(fast_price - _regime_last_price) / max(_regime_dt, 0.05)
                _regime_last_price = fast_price
                _regime_last_ts = now
                _regime_changed = regime_detector.update(
                    speed=_regime_speed,
                    latency_ms=latency_ms,
                )
                if _regime_changed:
                    _regime_now = time.time()
                    if _regime_now - regime_detector._last_log_ts >= regime_detector._log_min_sec:
                        regime_detector._last_log_ts = _regime_now
                        logging.info(
                            "🔄 [REGIME] %s | speed_rms=%.3f stale_median=%.0fms",
                            regime_detector.get_regime(),
                            regime_detector.state.speed_rms,
                            regime_detector.state.stale_median_ms,
                        )

                mark_px = mark_price_for_side(poly_book.book, pnl.position_side)
                if pnl.inventory > 0 and mark_px > 0.0:
                    equity = pnl.balance + (pnl.inventory * mark_px)
                else:
                    equity = pnl.balance
                risk.update_equity(equity)
                trade_allowed = risk.can_trade(time.time(), equity)

                if (
                    LIVE_MODE
                    and token_up_id
                    and poly_book is not None
                    and (now - _last_live_reconcile_ts) >= _live_inventory_reconcile_sec
                ):
                    _last_live_reconcile_ts = now
                    await _reconcile_live_inventory_maybe(poly_book)

                # Live-only: after a failed/rejected CLOB BUY we suppress placing another
                # order until _live_skip_until (see OPEN block below). Do NOT fold this
                # into meta_enabled: that would make HFTEngine skip OPEN while paper
                # still evaluates the same tick — keep process_tick parity with paper.
                _skip_cooldown_active = LIVE_MODE and (now < _live_skip_until)

                # Validate binary market constraint (UP + DOWN ≈ 1.0) and fix
                # stale/wrong orderbook data before passing it to the engine.
                _ub = float(poly_book.book.get("bid", 0.0))
                _ua = float(poly_book.book.get("ask", 0.0))
                _db = float(poly_book.book.get("down_bid", 0.0))
                _da = float(poly_book.book.get("down_ask", 0.0))
                _up_mid_chk = (_ub + _ua) / 2.0 if (0.0 < _ub < _ua <= 1.0) else 0.5
                _dn_mid_chk = (_db + _da) / 2.0 if (0.0 < _db < _da <= 1.0) else 0.5
                if abs(_up_mid_chk + _dn_mid_chk - 1.0) > 0.05:
                    if (now - last_book_mismatch_warn_time) >= 90.0:
                        logging.info(
                            "Book mismatch corrected before engine: UP %.3f/%.3f + DOWN %.3f/%.3f sum=%.3f",
                            _ub, _ua, _db, _da, _up_mid_chk + _dn_mid_chk,
                        )
                        last_book_mismatch_warn_time = now
                    else:
                        logging.debug(
                            "Book mismatch corrected (suppressed repeat): UP %.3f/%.3f + DOWN %.3f/%.3f sum=%.3f",
                            _ub, _ua, _db, _da, _up_mid_chk + _dn_mid_chk,
                        )
                reconcile_binary_outcome_books(poly_book.book)

                decision = await strategy_hub.process_tick(
                    fast_price=fast_price,
                    poly_orderbook=poly_book.book,
                    price_history=primary_data if primary_data else [],
                    lstm_forecast=forecast,
                    zscore=zscore,
                    latency_ms=latency_ms,
                    recent_pnl=pnl.last_realized_pnl,
                    meta_enabled=(trade_allowed or BYPASS_META_GATE),
                    seconds_to_expiry=selector.seconds_to_slot_end(),
                    skew_ms=skew_ms,
                    slot_anchor_price=slot_anchor_price,
                )
                if (now - last_pulse_time) >= pulse_log_period:
                    diff = fast_price - poly_btc
                    trend = strategy_hub.get_trend_state()
                    profile_suffix = ""
                    if os.getenv("HFT_LOG_MARKET_PROFILE") == "1":
                        _gp = getattr(
                            strategy_hub.get_active_strategy(),
                            "get_active_profile",
                            None,
                        )
                        if callable(_gp):
                            profile_suffix = f" | Profile: {_gp()}"
                    bid_size = float(poly_book.book.get("bid_size_top", 1.0))
                    ask_size = float(poly_book.book.get("ask_size_top", 1.0))
                    db_sz = float(poly_book.book.get("down_bid_size_top", 0.0))
                    da_sz = float(poly_book.book.get("down_ask_size_top", 0.0))
                    if trend["trend"] == "DOWN" and db_sz + da_sz > 0.0:
                        imbalance = db_sz / (db_sz + da_sz + 1e-9)
                    else:
                        imbalance = bid_size / (bid_size + ask_size + 1e-9)
                    upnl = pnl.get_unrealized_pnl(poly_book.book)
                    rsi_st = strategy_hub.get_rsi_v5_state()
                    _rx_on = float(rsi_st.get("reaction_on", 0.0)) >= 0.5
                    _rsi_line = (
                        f"Rx {rsi_st['rsi']:.1f} raw {rsi_st.get('rsi_raw', rsi_st['rsi']):.1f} "
                        f"[{rsi_st['lower']:.0f}-{rsi_st['upper']:.0f}] "
                        f"Δ={rsi_st['slope']:+.2f} m={rsi_st.get('ma_fast', 0.0):.2f} mh={rsi_st.get('macd_hist', 0.0):+.2f}"
                        if _rx_on
                        else (
                            f"RSI: {rsi_st['rsi']:.1f} [{rsi_st['lower']:.0f}-{rsi_st['upper']:.0f}] "
                            f"Δ={rsi_st['slope']:+.2f}"
                        )
                    )
                    cb_px = aggregator.get_coinbase_price()
                    bn_px = aggregator.get_binance_price()
                    bn_bbo = aggregator.get_binance_bbo()
                    cb_s = f"{cb_px:.2f}" if cb_px else "n/a"
                    if bn_bbo:
                        bn_s = f"{bn_bbo[0]:.4f}/{bn_bbo[1]:.4f}"
                    elif bn_px is not None:
                        bn_s = f"{bn_px:.4f}"
                    else:
                        bn_s = "n/a"
                    up_bid = float(poly_book.book.get("bid", 0.0))
                    up_ask = float(poly_book.book.get("ask", 0.0))
                    d_bid = float(poly_book.book.get("down_bid", 0.0))
                    d_ask = float(poly_book.book.get("down_ask", 0.0))
                    if trend["trend"] == "UP":
                        book_focus = f"UP b/a {up_bid:.3f}/{up_ask:.3f}"
                    elif trend["trend"] == "DOWN":
                        book_focus = f"DOWN b/a {d_bid:.3f}/{d_ask:.3f}"
                    else:
                        book_focus = f"UP b/a {up_bid:.3f}/{up_ask:.3f} | DOWN b/a {d_bid:.3f}/{d_ask:.3f}"
                    
                    logging.info(
                        f"Fast: {fast_price:.2f} (CB {cb_s} BNC {bn_s} smart={USE_SMART_FAST}) | "
                        f"PolyRTDS: {poly_btc:.2f} | "
                        f"Diff: {diff:+.2f} | Z: {zscore:+.2f} | "
                        f"Trend: {trend['trend']} s={trend['speed']:+.2f} d={trend['depth']:.2f} a={trend['age']:.1f}s | "
                        f"Book: {book_focus} | "
                        f"{_rsi_line} | "
                        f"Imb: {imbalance:.2f} | uPnL: {upnl:+.2f}$ | "
                        f"Stale: {latency_ms:.0f}ms skew: {skew_ms:+.0f} "
                        f"(cb {float(_ft['coinbase_age_ms']):.0f} "
                        f"poly {float(_ft['poly_age_ms']):.0f} "
                        f"bn {float(_ft['binance_age_ms']):.0f}) | "
                        f"DD: {risk.drawdown_pct(equity)*100:.2f}% | Gate: {'ON' if trade_allowed else 'OFF'} | "
                        f"Regime: {regime_detector.get_regime()} | "
                        f"Anchor: {slot_anchor_price:.2f} Δ={fast_price - slot_anchor_price:+.2f} | "
                        f"Forecast: {forecast:.2f}{profile_suffix}",
                    )
                    last_pulse_time = now
                if isinstance(decision, dict) and decision.get("event") == "CLOSE":
                    _live_skip_until = 0.0
                    _live_pnl = 0.0  # Populated by live path; used for journal/attribution.
                    if LIVE_MODE and token_up_id:
                        # Use the side of the OPEN position, not the exit signal side.
                        # TREND_FLIP_EXIT changes decision["side"] to the new direction,
                        # which would select the wrong token and cause phantom close.
                        _close_side = pnl.position_side or decision.get("side")
                        _close_tid = _conditional_token_for_position_side(
                            _close_side, token_up_id, token_down_id
                        )
                        logging.info(
                            "[LIVE] CLOSE routing: side=%s token=%s (up=%s down=%s)",
                            _close_side,
                            (_close_tid or "")[:20],
                            (token_up_id or "")[:20],
                            (token_down_id or "")[:20],
                        )
                        _live_filled = live_exec.filled_buy_shares(_close_tid)
                        if _live_filled == 0 and live_exec.has_pending_buy(_close_tid):
                            logging.info(
                                "[LIVE] BUY still PENDING at close signal — waiting for fill "
                                "(token=%s).", _close_tid[:20],
                            )
                            _live_filled = await live_exec.wait_for_buy_fill(_close_tid, timeout_sec=5.0)
                        if _live_filled == 0:
                            await live_exec.wait_for_exit_readiness(_close_tid)
                            _live_filled = live_exec.filled_buy_shares(_close_tid)
                        if _live_filled == 0:
                            _probe = await live_exec.probe_chain_shares_for_close(_close_tid)
                            if _probe > 0:
                                logging.info(
                                    "[LIVE] Close: using chain-probed %.4f sh (lag or partial-fill "
                                    "remainder) token=%s",
                                    _probe,
                                    _close_tid[:20],
                                )
                                _live_filled = _probe
                        # PnL inventory is authoritative for how much is left to sell; filled_buy_shares
                        # can still sum stale FILLED rows in _active_orders after clear_filled_buy().
                        if _live_filled > 0 and pnl.inventory > 1e-9:
                            if _live_filled > pnl.inventory + 1e-6:
                                logging.warning(
                                    "[LIVE] Close size cap: engine/CLOB track %.4f sh > PnL "
                                    "inventory %.4f sh — selling remainder only.",
                                    _live_filled,
                                    pnl.inventory,
                                )
                            _live_filled = min(_live_filled, float(pnl.inventory))
                        if _live_filled > 0:
                            logging.info(
                                "[LIVE] Close: selling %.4f live-filled shares token=%s",
                                _live_filled, _close_tid[:20],
                            )
                            _sell_filled, _sell_px = await live_exec.close_position(
                                _close_tid, _live_filled
                            )
                            if _sell_filled > 0 and _sell_px > 0:
                                live_exec.clear_filled_buy(_close_tid)
                                _live_pnl = pnl.live_close(
                                    _sell_filled, _sell_px,
                                    strategy_name=decision.get("strategy_name") or "",
                                    performance_key=decision.get("performance_key"),
                                )
                                live_risk.update(_live_pnl)
                                live_risk.log_status()
                            else:
                                _live_pnl = 0.0
                                # SELL completely failed — force-clear the PnL position so the
                                # engine does not enter an infinite phantom EXIT loop.
                                if pnl.inventory > 0:
                                    logging.error(
                                        "🛑 [LIVE] SELL failed entirely for %.4f shares — "
                                        "force-clearing PnL state. Manual check required.",
                                        pnl.inventory,
                                    )
                                    live_exec.clear_filled_buy(_close_tid)
                                    pnl.inventory = 0.0
                                    pnl.entry_price = 0.0
                                    pnl.entry_ts = 0
                                    pnl.position_side = None
                            risk.on_trade_closed(_live_pnl, time.time())
                        else:
                            logging.info(
                                "[LIVE] Close skipped: no tracked or on-chain shares for token=%s "
                                "after pending/order wait and chain probe.",
                                _close_tid[:20],
                            )
                            # Phantom position: PnL state shows inventory but CLOB has none.
                            # Force-clear so the engine stops generating EXIT signals.
                            live_exec.clear_filled_buy(_close_tid)
                            if pnl.inventory > 0:
                                logging.warning(
                                    "[LIVE] Force-clearing phantom PnL position (%.4f sh).",
                                    pnl.inventory,
                                )
                                pnl.inventory = 0.0
                                pnl.entry_price = 0.0
                                pnl.entry_ts = 0
                                pnl.position_side = None
                            risk.on_trade_closed(0.0, time.time())
                    else:
                        # Paper mode: use engine-simulated PnL.
                        _live_pnl = float(decision.get("pnl", 0.0))
                        risk.on_trade_closed(_live_pnl, time.time())
                    _rs = strategy_hub.get_rsi_v5_state()
                    _perf_key = decision.get("performance_key")
                    # In live mode _live_pnl is the real CLOB PnL; in paper it is
                    # the engine-simulated value — use it for attribution in both cases.
                    if _perf_key:
                        _sl = pnl.strategy_performance.slices.get(str(_perf_key))
                        _cum = _sl.pnl_sum if _sl else 0.0
                        logging.info(
                            "📊 Close attribution: key=%s trade_pnl=%+.4f USD | cumulative_this_key=%+.4f",
                            _perf_key,
                            _live_pnl,
                            _cum,
                        )
                        _sc = pnl.strategy_performance.summary_compact()
                        if _sc:
                            logging.info("📊 session_slices: %s", _sc)
                    # Journal uses real CLOB PnL in live mode, sim PnL in paper mode.
                    journal.record_close(
                        decision=decision,
                        live_pnl=_live_pnl,
                        rsi_state=_rs,
                    )
                if LIVE_MODE and token_up_id and live_risk.can_trade():
                    # Engine signals OPEN intent — place real CLOB BUY and record
                    # position only after confirmed fill.  No sim BUY is written.
                    if isinstance(decision, dict) and decision.get("event") == "OPEN":
                        _raw_side = decision.get("side", "")
                        if _raw_side == "UP":
                            _open_signal = "BUY_UP"
                        elif _raw_side == "DOWN":
                            _open_signal = "BUY_DOWN"
                        else:
                            _open_signal = _raw_side
                        if _open_signal in ("BUY_UP", "BUY_DOWN"):
                            _trade_info = decision.get("trade") or {}
                            _live_order_cap = float(os.environ["LIVE_ORDER_SIZE"])
                            _cost_usd = (
                                float(_trade_info.get("amount_usd") or 0.0)
                                or _live_order_cap
                            )
                            # Engine dynamic sizing (_calc_dynamic_amount) can exceed
                            # LIVE_ORDER_SIZE; reprice/emergency do not add USD — only this
                            # path can overshoot if we trust amount_usd blindly.
                            if _cost_usd > _live_order_cap:
                                logging.info(
                                    "[LIVE] Capping engine notional %.4f USD → "
                                    "LIVE_ORDER_SIZE %.4f USD.",
                                    _cost_usd,
                                    _live_order_cap,
                                )
                                _cost_usd = _live_order_cap
                            _max_pos_usd = float(
                                os.getenv(
                                    "HFT_MAX_POSITION_USD",
                                    os.environ["LIVE_ORDER_SIZE"],
                                )
                            )
                            if _max_pos_usd > 0.0 and _cost_usd > _max_pos_usd:
                                logging.info(
                                    "[LIVE] Capping order %.4f USD → max position %.4f USD.",
                                    _cost_usd,
                                    _max_pos_usd,
                                )
                                _cost_usd = _max_pos_usd
                            # Cap order size to actual CLOB balance to prevent
                            # "not enough balance" rejections when account dropped
                            # below the configured LIVE_ORDER_SIZE after a loss.
                            _real_usdc = await asyncio.to_thread(live_exec.fetch_usdc_balance)
                            if _real_usdc is not None and _real_usdc < _cost_usd:
                                logging.warning(
                                    "⚠️ [LIVE] Real USDC balance %.4f < order size %.4f "
                                    "— capping to available balance.",
                                    _real_usdc, _cost_usd,
                                )
                                _cost_usd = _real_usdc
                            # Polymarket CLOB has a $1 minimum order size (USD).
                            # If the capped amount is below this, skip the trade to avoid rejection.
                            if _cost_usd < 1.0:
                                logging.warning(
                                    "⚠️ [LIVE] Capped order size %.4f USD < $1.00 minimum — skipping entry.",
                                    _cost_usd,
                                )
                                _live_skip_until = now + _live_skip_cooldown_sec
                            # Verify capped budget can still buy CLOB minimum shares.
                            # Use entry ask price from decision if available to estimate shares.
                            _poly_min_sh = float(os.getenv("POLY_CLOB_MIN_SHARES"))
                            _trade_dict = decision.get("trade") or {}
                            _entry_ask = float(
                                _trade_dict.get("exec_px")
                                or _trade_dict.get("book_px")
                                or 0.0
                            )
                            _budget_too_low = (
                                _entry_ask > 0.0
                                and (_cost_usd / _entry_ask) < _poly_min_sh
                            )
                            if _budget_too_low:
                                logging.warning(
                                    "⚠️ [LIVE] Budget %.4f USD @ %.4f = %.2f shares < "
                                    "CLOB min %.0f — skipping entry (insufficient balance).",
                                    _cost_usd, _entry_ask,
                                    _cost_usd / _entry_ask, _poly_min_sh,
                                )
                                _live_skip_until = now + _live_skip_cooldown_sec
                            if now < _live_skip_until:
                                logging.debug(
                                    "[LIVE] OPEN suppressed during skip-cooldown (%.1fs left).",
                                    _live_skip_until - now,
                                )
                            else:
                                _live_tid = (
                                    token_up_id if _open_signal == "BUY_UP"
                                    else (token_down_id or token_up_id)
                                )
                                _pending_buy = live_exec.filled_buy_shares(_live_tid)
                                if pnl.inventory > 1e-9:
                                    logging.warning(
                                        "⚠️ [LIVE] Skip OPEN: position already open "
                                        "(inventory=%.6f sh).",
                                        pnl.inventory,
                                    )
                                elif _pending_buy > 1e-9:
                                    # CLOB can confirm shares while PnL stayed flat (chain/API desync).
                                    await _reconcile_live_inventory_maybe(poly_book)
                                    if pnl.inventory > 1e-9:
                                        logging.info(
                                            "[LIVE] Orphan fill adopted before OPEN — "
                                            "position tracked for EXIT.",
                                        )
                                    else:
                                        logging.warning(
                                            "⚠️ [LIVE] Skip OPEN: BUY already pending or "
                                            "confirmed on token (%.4f sh).",
                                            _pending_buy,
                                        )
                                else:
                                    _live_bb = 0.0
                                    _live_ba = 0.0
                                    if poly_book is not None and hasattr(poly_book, "book"):
                                        if _open_signal == "BUY_UP":
                                            _live_bb = float(poly_book.book.get("bid", 0.0) or 0.0)
                                            _live_ba = float(poly_book.book.get("ask", 0.0) or 0.0)
                                        else:
                                            _live_bb = float(
                                                poly_book.book.get("down_bid", 0.0) or 0.0
                                            )
                                            _live_ba = float(
                                                poly_book.book.get("down_ask", 0.0) or 0.0
                                            )
                                    _exec_kw = {}
                                    if _live_bb > 0.0 and _live_ba > 0.0:
                                        _exec_kw = {
                                            "best_bid": _live_bb,
                                            "best_ask": _live_ba,
                                        }
                                    # Blocks until CLOB confirms fill or timeout.
                                    _filled_sh, _filled_px = await live_exec.execute(
                                        _open_signal,
                                        _live_tid,
                                        budget_usd=_cost_usd,
                                        **_exec_kw,
                                    )
                                    if _filled_sh > 0:
                                        # Record confirmed CLOB fill into PnL tracker. Use actual
                                        # notional (shares × CLOB avg) so balance/entry match wallet + UI;
                                        # execute() sizes orders so limit_price × shares ≤ budget.
                                        _buy_cash_usd = float(_filled_sh) * float(_filled_px)
                                        _live_skip_until = 0.0
                                        pnl.live_open(
                                            _open_signal, _filled_sh, _filled_px,
                                            _buy_cash_usd,
                                            strategy_name=decision.get("strategy_name") or "",
                                        )
                                        _hft_eng = getattr(
                                            strategy_hub.get_active_strategy(),
                                            "_engine",
                                            None,
                                        )
                                        if (
                                            _hft_eng is not None
                                            and getattr(
                                                _hft_eng, "_live_entry_sync_pending", False
                                            )
                                        ):
                                            _apply_fast = fast_price
                                            if USE_SMART_FAST:
                                                _nf = aggregator.get_weighted_price()
                                            else:
                                                _nf = (
                                                    aggregator.get_coinbase_price()
                                                    or aggregator.get_weighted_price()
                                                )
                                            if _nf is not None:
                                                _apply_fast = float(_nf)
                                            _book_px = float(
                                                poly_book.book.get("down_ask", 0.0)
                                                if _open_signal == "BUY_DOWN"
                                                else poly_book.book.get("ask", 0.0)
                                            )
                                            _hft_eng.apply_live_entry_after_fill(
                                                poly_book.book,
                                                _apply_fast,
                                                _book_px,
                                                float(_filled_px),
                                                float(_filled_sh),
                                                float(_filled_sh * _filled_px),
                                            )
                                        # Refresh CTF allowance so the subsequent SELL is accepted.
                                        await asyncio.to_thread(
                                            live_exec.ensure_conditional_allowance, _live_tid
                                        )
                                        journal.record_open(
                                            decision=decision,
                                            filled_shares=float(_filled_sh),
                                            avg_price=float(_filled_px),
                                            amount_usd=float(_buy_cash_usd),
                                            rsi_state=strategy_hub.get_rsi_v5_state(),
                                            book_snapshot=poly_book.book
                                            if poly_book is not None
                                            else None,
                                        )
                                    else:
                                        _hft_eng = getattr(
                                            strategy_hub.get_active_strategy(),
                                            "_engine",
                                            None,
                                        )
                                        if _hft_eng is not None:
                                            _hft_eng.rollback_live_open_signal()
                                        _buy_skip = getattr(
                                            live_exec, "_last_buy_skip_reason", None
                                        )
                                        _slip_abort = _buy_skip == "slippage_abort"
                                        _cooldown_on_slip = (
                                            os.getenv(
                                                "LIVE_SKIP_COOLDOWN_ON_SLIPPAGE_ABORT", "0"
                                            )
                                            == "1"
                                        )
                                        # Slippage guard: optional no-cooldown (existing behaviour).
                                        _apply_cd = not (
                                            _slip_abort and not _cooldown_on_slip
                                        )
                                        # Stale / emergency placement failure: liquidity/timing, not a
                                        # rejected bad trade — default is no cooldown so the next tick
                                        # can retry. Set LIVE_APPLY_COOLDOWN_ON_STALE_NO_FILL=1 to force.
                                        if _apply_cd and _buy_skip in (
                                            "stale_no_fill",
                                            "emergency_buy_failed",
                                        ):
                                            if os.getenv(
                                                "LIVE_APPLY_COOLDOWN_ON_STALE_NO_FILL", "0"
                                            ) != "1":
                                                _apply_cd = False
                                                logging.info(
                                                    "[LIVE] BUY not filled (%s) — no skip cooldown "
                                                    "(set LIVE_APPLY_COOLDOWN_ON_STALE_NO_FILL=1 to force).",
                                                    _buy_skip,
                                                )
                                        if _apply_cd:
                                            _live_skip_until = now + _live_skip_cooldown_sec
                                            logging.info(
                                                "[LIVE] Skip cooldown active for %.0fs (until %.1f).",
                                                _live_skip_cooldown_sec, _live_skip_until,
                                            )
                                        elif _slip_abort:
                                            logging.info(
                                                "[LIVE] BUY skipped (slippage guard) — no "
                                                "live-skip cooldown (set "
                                                "LIVE_SKIP_COOLDOWN_ON_SLIPPAGE_ABORT=1 to enable).",
                                            )
            elif (now - last_pulse_time) >= pulse_log_period:
                # logging.debug("⏳ Ожидание полной синхронизации данных (Coinbase/Poly)...")
                last_pulse_time = now

            # When MAIN_LOOP_SLEEP is 0, asyncio.sleep(0) only yields to the event loop (no wall delay).
            await asyncio.sleep(MAIN_LOOP_SLEEP if MAIN_LOOP_SLEEP > 0.0 else 0.0)

    except KeyboardInterrupt:
        print("\n🛑 Остановка пользователем...")
        shutdown_reason = "KeyboardInterrupt"
    except Exception:
        logging.error("💥 КРИТИЧЕСКАЯ ОШИБКА В ГЛАВНОМ ЦИКЛЕ")
        logging.error(traceback.format_exc())
        shutdown_reason = "exception"
        try:
            bp = aggregator.data.get("coinbase")
            pp = poly_book.book if poly_book else "None"
            logging.debug("DEBUG DATA AT CRASH -> Coinbase: %s | Poly: %s", bp, pp)
        except Exception:
            pass
    finally:
        if LIVE_MODE and token_up_id and pnl.inventory > 0:
            logging.warning(
                "🚨 Shutdown with open live position (%.4f shares) — emergency exit.",
                pnl.inventory,
            )
            try:
                _exit_side_name = pnl.position_side or "BUY_UP"
                _exit_tid = _conditional_token_for_position_side(
                    _exit_side_name, token_up_id, token_down_id
                )
                await live_exec.emergency_exit(_exit_tid, pnl.inventory)
                await asyncio.sleep(2.0)
            except Exception as _exc:
                logging.error("Emergency exit on shutdown failed: %s", _exc)
        if _heartbeat_task is not None and not _heartbeat_task.done():
            _heartbeat_task.cancel()
        for _t in provider_tasks:
            if not _t.done():
                _t.cancel()
        if poly_connect_task is not None and not poly_connect_task.done():
            poly_connect_task.cancel()
        _bg: list[asyncio.Task] = list(provider_tasks)
        if poly_connect_task is not None:
            _bg.append(poly_connect_task)
        if _bg:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*_bg, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logging.warning(
                    "Shutdown timeout while cancelling background tasks; exiting anyway."
                )
        try:
            if LIVE_MODE:
                try:
                    _fin_usdc = await asyncio.to_thread(live_exec.fetch_usdc_balance)
                    stats.set_live_wallet_usdc(_fin_usdc)
                except Exception as _fin_exc:
                    logging.debug("fetch_usdc_balance for final report: %s", _fin_exc)
                    stats.set_live_wallet_usdc(None)
            else:
                stats.set_live_wallet_usdc(None)
            stats.show_final_report(
                journal_path=journal.path,
                shutdown_reason=shutdown_reason,
            )
        except Exception as exc:
            logging.error("Final report failed: %s", exc)
