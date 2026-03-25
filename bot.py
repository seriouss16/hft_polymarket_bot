import os
import sys
import asyncio
import logging
from pathlib import Path


def _load_dotenv_if_present() -> None:
    """Merge key=value lines from hft_bot/.env into os.environ (existing keys are not overwritten)."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_if_present()

_UVLOOP_ACTIVE = False


def _install_uvloop_policy() -> None:
    """Prefer libuv-backed asyncio loop on Linux/macOS when uvloop is available."""
    global _UVLOOP_ACTIVE
    if os.getenv("HFT_USE_UVLOOP", "1") == "0":
        return
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _UVLOOP_ACTIVE = True
    except ImportError:
        pass


_install_uvloop_policy()
import traceback
import time
from datetime import datetime, timezone

# --- Форсируем вывод и отключаем мусор TF ---
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

print(">>> Инициализация HFT системы...", flush=True)

# Импорты после настройки окружения
import tensorflow as tf
from core.selector import MarketSelector
from core.executor import PnLTracker
from core.engine import HFTEngine
from core.live_engine import LiveExecutionEngine, LiveRiskManager
from core.risk_engine import RiskEngine
from data.aggregator import FastPriceAggregator
from data.providers import FastExchangeProvider
from data.poly_clob import PolyOrderBook
from ml.model import AsyncLSTMPredictor
from utils.stats import StatsCollector
from utils.trade_journal import TradeJournal

def _setup_logging() -> None:
    """Configure stdout logging and per-run file logs with retention."""
    log_dir = Path(os.getenv("HFT_LOG_DIR", str(Path(__file__).resolve().parent / "reports" / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    keep_files = int(os.getenv("HFT_LOG_KEEP_FILES", "5"))
    start_tag = datetime.now().strftime("%d%m%y_%H%M%S")
    log_basename = f"bot_{start_tag}.log"
    log_path = log_dir / log_basename
    existing = sorted(
        log_dir.glob("bot_*.log"),
        key=lambda p: p.stat().st_mtime,
    )
    while len(existing) >= keep_files:
        old = existing.pop(0)
        try:
            old.unlink()
        except OSError:
            break
    fmt = "%(asctime)s | %(levelname)s | %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    logging.info("File logging initialized: %s (retention=%s)", log_path.name, keep_files)


_setup_logging()

async def main():
    if _UVLOOP_ACTIVE:
        logging.info("asyncio: uvloop event loop policy active")

    # --- Конфигурация ---
    BYPASS_META_GATE = os.getenv("HFT_BYPASS_META_GATE", "1") == "1"
    TEST_MODE = True
    LIVE_MODE = os.getenv("LIVE_MODE", "0") == "1"
    USE_SMART_FAST = os.getenv("USE_SMART_FAST", "0") == "1"
    SYMBOL = "BTC"
    STATS_INTERVAL = float(os.getenv("STATS_INTERVAL_SEC", "120"))
    # PULSE_INTERVAL_SEC>0: at most one Fast: line per N seconds. When 0, use HFT_FAST_LOG_MIN_SEC.
    PULSE_INTERVAL = float(os.getenv("PULSE_INTERVAL_SEC", "0"))
    FAST_LOG_MIN_SEC = float(os.getenv("HFT_FAST_LOG_MIN_SEC", "0.25"))
    pulse_log_period = PULSE_INTERVAL if PULSE_INTERVAL > 0.0 else FAST_LOG_MIN_SEC
    MAIN_LOOP_SLEEP = float(os.getenv("HFT_LOOP_SLEEP_SEC", "0"))
    CLOB_PULL_INTERVAL = float(os.getenv("CLOB_BOOK_PULL_SEC", "0"))
    LSTM_MIN_INTERVAL = float(os.getenv("LSTM_INFERENCE_SEC", "0"))
    ENABLE_LSTM = os.getenv("HFT_ENABLE_LSTM", "0") == "1"
    SLOT_POLL_SEC = float(os.getenv("HFT_SLOT_POLL_SEC", "0"))
    MIN_SLOT_POLL_SEC = 1.0
    
    # --- Инициализация компонентов ---
    selector = MarketSelector(asset=SYMBOL)
    aggregator = FastPriceAggregator()
    pnl = PnLTracker()
    stats = StatsCollector(pnl)
    engine = HFTEngine(pnl, is_test_mode=TEST_MODE)
    lstm = AsyncLSTMPredictor(history_len=100)
    live_exec = LiveExecutionEngine(
        private_key=os.getenv("PRIVATE_KEY"),
        funder=os.getenv("FUNDER"),
        test_mode=not LIVE_MODE,
        min_order_size=float(os.getenv("LIVE_ORDER_SIZE", "10")),
        max_spread=float(os.getenv("LIVE_MAX_SPREAD", "1.0")),
    )
    live_risk = LiveRiskManager(max_daily_loss=float(os.getenv("LIVE_MAX_DAILY_LOSS", "-1e12")))
    risk = RiskEngine(
        max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.99")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "1.0")),
        loss_cooldown_sec=float(os.getenv("LOSS_COOLDOWN_SEC", "0")),
    )
    journal = TradeJournal(path=os.getenv("TRADE_JOURNAL_PATH", "reports/trade_journal.csv"))
    
    # Отключаем GPU для предсказаний
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

    token_up_id = None
    token_down_id = None
    current_slug = None
    poly_book = None
    last_stats_time = asyncio.get_event_loop().time()
    last_pulse_time = 0
    last_lstm_time = 0
    last_book_pull_time = 0
    forecast = 0.0
    last_slot_check_time = 0.0
    last_skew_warn_time = 0.0
    last_high_latency_warn_time = 0.0

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
            
            # 1. Авто-переключение слота (без фиксированной паузы: чаще = быстрее реакция на новый рынок).
            slot_poll = SLOT_POLL_SEC if SLOT_POLL_SEC > 0.0 else MIN_SLOT_POLL_SEC
            slot_poll = max(slot_poll, MIN_SLOT_POLL_SEC)
            if (now - last_slot_check_time) >= slot_poll:
                last_slot_check_time = now
                ts = selector.get_current_slot_timestamp()
                slug = selector.format_slug(ts)
                current_slug = slug
                up_id, down_id, question = await selector.fetch_up_down_token_ids(slug)

                if up_id and (up_id != token_up_id or down_id != token_down_id):
                    logging.info(f"🎯 Смена рынка: {question}")
                    token_up_id = up_id
                    token_down_id = down_id
                    engine.reset_for_new_market()
                    if poly_connect_task is not None and not poly_connect_task.done():
                        poly_connect_task.cancel()
                    poly_book = PolyOrderBook(symbol="bitcoin")
                    poly_connect_task = asyncio.create_task(poly_book.connect())

            # 2. Получение данных
            _net_dbg = os.getenv("HFT_NETWORK_TIMING_DEBUG", "0") == "1"
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
                    except Exception:
                        pass
                    last_book_pull_time = now
                    if _net_dbg:
                        _nw_t3 = time.perf_counter()
                        logging.info(
                            "NetworkCheck read_fast=%.1fms clob_roundtrip=%.1fms",
                            (_nw_t1 - _nw_t0) * 1000.0,
                            (_nw_t3 - _nw_t2) * 1000.0,
                        )

                aggregator.add_history(fast_price)
                zscore = aggregator.get_zscore()
                _ft = aggregator.feed_timing(float(poly_book.book.get("ts", 0.0)))
                latency_ms = float(_ft["staleness_ms"])
                skew_ms = float(_ft["skew_ms"])
                if (
                    engine.entry_max_latency_ms > 0.0
                    and latency_ms > engine.entry_max_latency_ms
                    and (now - last_high_latency_warn_time) >= 30.0
                ):
                    logging.info(
                        "Feed staleness %.0f ms above entry_max_latency_ms=%.0f (engine may block entries).",
                        latency_ms,
                        engine.entry_max_latency_ms,
                    )
                    last_high_latency_warn_time = now
                if (
                    abs(skew_ms) > 800.0
                    and (now - last_skew_warn_time) >= 120.0
                ):
                    logging.warning(
                        "Large cross-feed skew skew_ms=%.0f (cb_age=%.0f poly_age=%.0f ms); "
                        "not wall-clock NTP — local recv order of WS messages.",
                        skew_ms,
                        float(_ft["coinbase_age_ms"]),
                        float(_ft["poly_age_ms"]),
                    )
                    last_skew_warn_time = now
                equity = pnl.balance + pnl.get_unrealized_pnl(poly_book.book)
                risk.update_equity(equity)
                trade_allowed = risk.can_trade(time.time(), equity)

                decision = await engine.process_tick(
                    fast_price=fast_price,
                    poly_orderbook=poly_book.book,
                    price_history=primary_data if primary_data else [],
                    lstm_forecast=forecast,
                    zscore=zscore,
                    latency_ms=latency_ms,
                    recent_pnl=pnl.last_realized_pnl,
                    meta_enabled=trade_allowed or BYPASS_META_GATE,
                    seconds_to_expiry=selector.seconds_to_slot_end(),
                )
                if (now - last_pulse_time) >= pulse_log_period:
                    diff = fast_price - poly_btc
                    trend = engine.get_trend_state()
                    bid_size = float(poly_book.book.get("bid_size_top", 1.0))
                    ask_size = float(poly_book.book.get("ask_size_top", 1.0))
                    db_sz = float(poly_book.book.get("down_bid_size_top", 0.0))
                    da_sz = float(poly_book.book.get("down_ask_size_top", 0.0))
                    if trend["trend"] == "DOWN" and db_sz + da_sz > 0.0:
                        imbalance = db_sz / (db_sz + da_sz + 1e-9)
                    else:
                        imbalance = bid_size / (bid_size + ask_size + 1e-9)
                    upnl = pnl.get_unrealized_pnl(poly_book.book)
                    rsi_st = engine.get_rsi_v5_state()
                    cb_px = aggregator.get_coinbase_price()
                    bn_px = aggregator.get_binance_price()
                    cb_s = f"{cb_px:.2f}" if cb_px else "n/a"
                    bn_s = f"{bn_px:.2f}" if bn_px else "n/a"
                    up_bid = float(poly_book.book.get("bid", 0.0))
                    up_ask = float(poly_book.book.get("ask", 0.0))
                    d_bid = float(poly_book.book.get("down_bid", 0.0))
                    d_ask = float(poly_book.book.get("down_ask", 0.0))
                    if not (0.0 < d_bid < d_ask <= 1.0):
                        d_bid = max(0.01, min(0.99, 1.0 - up_ask))
                        d_ask = max(0.01, min(0.99, 1.0 - up_bid))
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
                        f"RSI: {rsi_st['rsi']:.1f} [{rsi_st['lower']:.0f}-{rsi_st['upper']:.0f}] "
                        f"Δ={rsi_st['slope']:+.2f} | "
                        f"Imb: {imbalance:.2f} | uPnL: {upnl:+.2f}$ | "
                        f"Stale: {latency_ms:.0f}ms skew: {skew_ms:+.0f} "
                        f"(cb {float(_ft['coinbase_age_ms']):.0f} "
                        f"poly {float(_ft['poly_age_ms']):.0f} "
                        f"bn {float(_ft['binance_age_ms']):.0f}) | "
                        f"DD: {risk.drawdown_pct(equity)*100:.2f}% | Gate: {'ON' if trade_allowed else 'OFF'} | "
                        f"Forecast: {forecast:.2f}",
                    )
                    last_pulse_time = now
                if isinstance(decision, dict) and decision.get("event") == "CLOSE":
                    risk.on_trade_closed(float(decision.get("pnl", 0.0)), time.time())
                    _rs = engine.get_rsi_v5_state()
                    journal.append(
                        {
                            "ts": time.time(),
                            "side": decision.get("side"),
                            "entry_edge": decision.get("entry_edge"),
                            "exit_edge": decision.get("exit_edge"),
                            "duration_sec": decision.get("duration_sec"),
                            "entry_trend": decision.get("entry_trend"),
                            "entry_speed": decision.get("entry_speed"),
                            "entry_depth": decision.get("entry_depth"),
                            "entry_imbalance": decision.get("entry_imbalance"),
                            "latency_ms": decision.get("latency_ms"),
                            "pnl": decision.get("pnl"),
                            "exit_reason": decision.get("reason"),
                            "exit_rsi": _rs.get("rsi"),
                            "rsi_band_lower": _rs.get("lower"),
                            "rsi_band_upper": _rs.get("upper"),
                            "rsi_slope": _rs.get("slope"),
                            "entry_book_px": decision.get("entry_book_px"),
                            "entry_exec_px": decision.get("entry_exec_px"),
                            "exit_book_px": decision.get("exit_book_px"),
                            "exit_exec_px": decision.get("exit_exec_px"),
                            "shares_bought": decision.get("shares_bought"),
                            "shares_sold": decision.get("shares_sold"),
                            "cost_usd": decision.get("cost_usd"),
                            "cost_basis_usd": decision.get("cost_basis_usd"),
                            "proceeds_usd": decision.get("proceeds_usd"),
                            "entry_up_bid": decision.get("entry_up_bid"),
                            "entry_up_ask": decision.get("entry_up_ask"),
                            "entry_down_bid": decision.get("entry_down_bid"),
                            "entry_down_ask": decision.get("entry_down_ask"),
                            "exit_up_bid": decision.get("exit_up_bid"),
                            "exit_up_ask": decision.get("exit_up_ask"),
                            "exit_down_bid": decision.get("exit_down_bid"),
                            "exit_down_ask": decision.get("exit_down_ask"),
                        }
                    )
                if LIVE_MODE and token_up_id and live_risk.can_trade():
                    live_signal = engine.generate_live_signal(
                        fast_price,
                        poly_btc,
                        zscore,
                        price_history=primary_data if primary_data else [],
                        recent_pnl=pnl.last_realized_pnl,
                        latency_ms=latency_ms,
                    )
                    if live_signal:
                        live_tid = token_up_id if live_signal == "BUY_UP" else (token_down_id or token_up_id)
                        await live_exec.execute(live_signal, live_tid)
            elif (now - last_pulse_time) >= pulse_log_period:
                # logging.debug("⏳ Ожидание полной синхронизации данных (Coinbase/Poly)...")
                last_pulse_time = now

            # 5. Промежуточный отчёт (STATS_INTERVAL_SEC<=0 отключает).
            if STATS_INTERVAL > 0.0 and (now - last_stats_time >= STATS_INTERVAL):
                stats.show_report()
                logging.info("Intermediate stats report (STATS_INTERVAL_SEC=%s).", STATS_INTERVAL)
                last_stats_time = now

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
            stats.show_final_report(
                journal_path=journal.path,
                shutdown_reason=shutdown_reason,
            )
        except Exception as exc:
            logging.error("Final report failed: %s", exc)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass