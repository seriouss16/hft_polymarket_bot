import os
import sys
import asyncio
import logging
import threading
from pathlib import Path


def _load_env_file(path: Path, overwrite: bool = False) -> None:
    """Merge key=value pairs from path into process environment."""
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
        if not key:
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = val


def _load_runtime_env() -> None:
    """Load layered runtime configuration files."""
    root = Path(__file__).resolve().parent
    _load_env_file(root / "config" / "runtime.env", overwrite=False)
    _load_env_file(root / ".env", overwrite=True)


_load_runtime_env()

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

os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

print(">>> Инициализация HFT системы...", flush=True)

from core.selector import MarketSelector
from core.executor import PnLTracker, mark_price_for_side
from core.live_engine import LiveExecutionEngine, LiveRiskManager
from core.risk_engine import RiskEngine
from core.session_profile import apply_profile, maybe_switch_profile
from core.market_regime import MarketRegimeDetector
from core.strategies import LatencyArbitrageStrategy, PhaseRouterStrategy
from core.strategy_hub import StrategyHub
from data.aggregator import FastPriceAggregator
from data.providers import FastExchangeProvider
from data.poly_clob import PolyOrderBook
from ml.model import AsyncLSTMPredictor
from utils.log_dedupe import SameMessageDedupeFilter
from utils.stats import StatsCollector
from utils.trade_journal import TradeJournal

def _silence_http_client_loggers() -> None:
    """Lower noise from urllib3/requests (used by MarketSelector and HTTP helpers)."""
    raw = os.getenv("HFT_HTTP_CLIENT_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, raw, logging.WARNING)
    for name in (
        "urllib3",
        "urllib3.connectionpool",
        "urllib3.util",
        "requests",
        "requests.packages.urllib3",
        "charset_normalizer",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(level)


def _parse_env_file_keys(path: Path) -> list[str]:
    """Return keys from env file in order of appearance (non-comment lines with =)."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    keys: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key:
            keys.append(key)
    return keys


def _is_sensitive_config_key(key: str) -> bool:
    """Return True if env value must not appear verbatim in logs."""
    u = key.upper()
    if u in ("PRIVATE_KEY", "CLOB_PRIVATE_KEY"):
        return True
    if "SECRET" in u or "PASSPHRASE" in u or "PASSWORD" in u:
        return True
    if u.endswith("_API_KEY") or u == "API_KEY":
        return True
    return False


def _format_config_value(key: str, value: str | None) -> str:
    """Return value safe for logging, or a placeholder when unset or sensitive."""
    if value is None:
        return "<unset>"
    if _is_sensitive_config_key(key):
        return "<redacted>" if value else "<unset>"
    return value


_LIVE_REQUIRED_KEYS: tuple[str, ...] = (
    "LIVE_MAX_DAILY_LOSS",
    "MAX_DRAWDOWN_PCT",
    "MAX_POSITION_PCT",
    "LOSS_COOLDOWN_SEC",
    "LIVE_MAX_SPREAD",
    "LIVE_ORDER_SIZE",
    "LIVE_ORDER_FILL_POLL_SEC",
    "LIVE_ORDER_STALE_SEC",
    "LIVE_ORDER_MAX_REPRICE",
    "PRIVATE_KEY",
    "POLY_FUNDER_ADDRESS",
    "POLY_SIGNATURE_TYPE",
)

_SIM_REQUIRED_KEYS: tuple[str, ...] = (
    "HFT_DEPOSIT_USD",
    "HFT_BUY_EDGE",
    "HFT_SELL_EDGE_ABS",
    "HFT_MIN_HOLD_SEC",
    "HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC",
    "HFT_REGIME_FILTER_ENABLED",
    "HFT_TRAILING_TP_ENABLED",
    "HFT_TRAILING_SL_ENABLED",
    "STATS_INTERVAL_SEC",
)


def _validate_required_config(live_mode: bool) -> None:
    """Abort startup if required parameters are missing from the environment.

    Checks sim-critical keys always, then additionally checks live-critical keys
    when live_mode is True. Raises SystemExit listing every missing key so the
    operator can fix them all at once.
    """
    missing: list[str] = []
    for key in _SIM_REQUIRED_KEYS:
        if not os.environ.get(key, "").strip():
            missing.append(f"  {key}  (required for sim and live)")
    if live_mode:
        for key in _LIVE_REQUIRED_KEYS:
            if not os.environ.get(key, "").strip():
                missing.append(f"  {key}  (required for LIVE_MODE=1)")
    if missing:
        lines = "\n".join(missing)
        raise SystemExit(
            f"\n{'='*60}\n"
            f"🛑  STARTUP ABORTED — missing required config keys:\n"
            f"{lines}\n"
            f"\nAdd the missing keys to hft_bot/config/runtime.env\n"
            f"or to hft_bot/.env (overrides runtime.env).\n"
            f"{'='*60}\n"
        )


def _runtime_configuration_keys(root: Path) -> list[str]:
    """Build ordered unique keys from layered env files plus related process env."""
    seen: set[str] = set()
    ordered: list[str] = []
    for path in (root / "config" / "runtime.env", root / ".env"):
        for k in _parse_env_file_keys(path):
            if k not in seen:
                seen.add(k)
                ordered.append(k)
    extra_prefixes = (
        "HFT_",
        "LIVE_",
        "CLOB_",
        "STATS_",
        "PULSE_",
        "LSTM_",
        "USE_SMART",
        "POLY_",
        "POLIMARKET_",
        "POLYMARKET_",
    )
    extra_exact = frozenset(
        {
            "MAX_DRAWDOWN_PCT",
            "MAX_POSITION_PCT",
            "LOSS_COOLDOWN_SEC",
            "TRADE_JOURNAL_PATH",
            "FUNDER",
            "WALLET",
            "WALLET_ADDRESS",
        }
    )
    for k in sorted(os.environ):
        if k in seen:
            continue
        if k in extra_exact or k.startswith(extra_prefixes):
            ordered.append(k)
            seen.add(k)
    return ordered


def _log_runtime_configuration() -> None:
    """Log effective configuration from env files and matching process variables."""
    root = Path(__file__).resolve().parent
    keys = _runtime_configuration_keys(root)
    logging.info("--- Runtime configuration (effective env, %s keys) ---", len(keys))
    for k in keys:
        logging.info("  %s=%s", k, _format_config_value(k, os.environ.get(k)))
    logging.info("--- End runtime configuration ---")


def _setup_logging() -> None:
    """Configure stdout logging and per-run file logs with retention."""
    log_dir = Path(os.getenv("HFT_LOG_DIR", str(Path(__file__).resolve().parent / "reports" / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    keep_files = int(os.getenv("HFT_LOG_KEEP_FILES", "20"))
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
    fmt = "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s "
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    _dedupe = SameMessageDedupeFilter()
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(fmt))
    sh.addFilter(_dedupe)
    root.addHandler(sh)
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    fh.addFilter(_dedupe)
    root.addHandler(fh)
    _silence_http_client_loggers()
    logging.info("File logging initialized: %s (retention=%s)", log_path.name, keep_files)
    _log_runtime_configuration()


_setup_logging()

async def main():
    if _UVLOOP_ACTIVE:
        logging.info("asyncio: uvloop event loop policy active")

    # --- Конфигурация ---
    LIVE_MODE = os.getenv("LIVE_MODE", "0") == "1"
    _validate_required_config(LIVE_MODE)

    # Apply day/night session profile before any strategy objects read env vars.
    apply_profile(force=True)

    BYPASS_META_GATE = os.getenv("HFT_BYPASS_META_GATE", "1") == "1"
    TEST_MODE = not LIVE_MODE
    USE_SMART_FAST = os.getenv("USE_SMART_FAST", "0") == "1"
    SYMBOL = "BTC"
    STATS_INTERVAL = float(os.environ["STATS_INTERVAL_SEC"])
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
    pnl = PnLTracker(live_mode=LIVE_MODE)
    stats = StatsCollector(pnl)
    regime_detector = MarketRegimeDetector()
    strategy_hub = StrategyHub()
    strategy_hub.register(LatencyArbitrageStrategy(pnl, is_test_mode=TEST_MODE))
    if os.getenv("HFT_ENABLE_PHASE_ROUTING", "0") == "1":
        strategy_hub.register(PhaseRouterStrategy(pnl, is_test_mode=TEST_MODE))
    default_strategy = os.getenv(
        "HFT_ACTIVE_STRATEGY",
        "phase_router" if os.getenv("HFT_ENABLE_PHASE_ROUTING", "0") == "1" else "latency_arbitrage",
    )
    live_signal_strategy = os.getenv("HFT_LIVE_SIGNAL_STRATEGY", "latency_arbitrage").strip()
    if default_strategy in strategy_hub.list_strategies():
        strategy_hub.set_active(default_strategy)
    strategy_hub.enable_parallel(os.getenv("HFT_PARALLEL_STRATEGIES", "0") == "1")
    lstm = AsyncLSTMPredictor(history_len=100)
    live_exec = LiveExecutionEngine(
        private_key=os.getenv("PRIVATE_KEY"),
        funder=os.getenv("FUNDER") or os.getenv("POLY_FUNDER_ADDRESS"),
        test_mode=not LIVE_MODE,
        min_order_size=float(os.environ["LIVE_ORDER_SIZE"]),
        max_spread=float(os.environ["LIVE_MAX_SPREAD"]),
    )
    live_risk = LiveRiskManager(max_daily_loss=float(os.environ["LIVE_MAX_DAILY_LOSS"]))
    risk = RiskEngine(
        max_drawdown_pct=float(os.environ["MAX_DRAWDOWN_PCT"]),
        max_position_pct=float(os.environ["MAX_POSITION_PCT"]),
        loss_cooldown_sec=float(os.environ["LOSS_COOLDOWN_SEC"]),
    )
    journal = TradeJournal(path=os.getenv("TRADE_JOURNAL_PATH", "reports/trade_journal.csv"))

    # Validate session deposit against real account balance in live mode.
    _session_deposit = float(os.environ["HFT_DEPOSIT_USD"])
    if LIVE_MODE:
        # Refresh USDC and CTF conditional token allowances so SELL orders are accepted.
        # Without CTF allowance the CLOB rejects every SELL with "not enough balance".
        await asyncio.to_thread(live_exec.ensure_allowances)
        _live_account_balance_limit = float(os.getenv("LIVE_ACCOUNT_BALANCE", "0") or "0")
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
        async def _run_heartbeat() -> None:
            """Send Polymarket CLOB heartbeat every 5 s to keep open orders alive.

            Without a valid heartbeat every ≤15 s the CLOB cancels all open orders.
            Errors are logged but do not stop the loop.
            """
            _hb_id = ""
            while True:
                try:
                    resp = await asyncio.to_thread(live_exec.client.post_heartbeat, _hb_id)
                    _hb_id = resp.get("heartbeat_id", "") if isinstance(resp, dict) else getattr(resp, "heartbeat_id", "")
                except Exception as _hb_exc:
                    logging.debug("[LIVE] Heartbeat failed: %s", _hb_exc)
                await asyncio.sleep(5.0)

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
    _live_skip_cooldown_sec = float(os.getenv("LIVE_SKIP_COOLDOWN_SEC", "30.0"))
    last_lstm_time = 0
    last_book_pull_time = 0
    forecast = 0.0
    last_slot_check_time = 0.0
    last_slot_ts: int | None = None
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

            # Periodic stats before any await: slot/orderbook/strategy work must not delay the report.
            if STATS_INTERVAL > 0.0 and (now - last_stats_time >= STATS_INTERVAL):
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
                        logging.info(f"🎯 Смена рынка: {question}")
                        token_up_id = up_id
                        token_down_id = down_id
                        strategy_hub.reset_for_new_market()
                        if os.getenv("HFT_PERF_RESET_ON_NEW_MARKET", "0") == "1":
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
                    except Exception as exc:
                        logging.warning("CLOB book pull failed: %s", exc)
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

                # Block engine entries during live-skip cooldown to prevent phantom
                # sim positions from accumulating when the CLOB rejects every BUY.
                _skip_cooldown_active = LIVE_MODE and (now < _live_skip_until)

                decision = await strategy_hub.process_tick(
                    fast_price=fast_price,
                    poly_orderbook=poly_book.book,
                    price_history=primary_data if primary_data else [],
                    lstm_forecast=forecast,
                    zscore=zscore,
                    latency_ms=latency_ms,
                    recent_pnl=pnl.last_realized_pnl,
                    meta_enabled=(trade_allowed or BYPASS_META_GATE) and not _skip_cooldown_active,
                    seconds_to_expiry=selector.seconds_to_slot_end(),
                    skew_ms=skew_ms,
                )
                if (now - last_pulse_time) >= pulse_log_period:
                    diff = fast_price - poly_btc
                    trend = strategy_hub.get_trend_state()
                    profile_suffix = ""
                    if os.getenv("HFT_LOG_MARKET_PROFILE", "0") == "1":
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
                        f"Regime: {regime_detector.get_regime()} | "
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
                        _close_tid = (
                            token_up_id if _close_side in ("BUY_UP", None)
                            else (token_down_id or token_up_id)
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
                                "[LIVE] Close skipped: no live-filled shares for token=%s "
                                "(phantom position — engine state desync).",
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
                            "pnl": _live_pnl,
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
                            "strategy_name": decision.get("strategy_name"),
                            "entry_profile": decision.get("entry_profile"),
                            "performance_key": decision.get("performance_key"),
                        }
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
                            _cost_usd = (
                                float(_trade_info.get("amount_usd") or 0.0)
                                or float(os.environ["LIVE_ORDER_SIZE"])
                            )
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
                            # Verify capped budget can still buy CLOB minimum shares.
                            # Use entry ask price from decision if available to estimate shares.
                            _poly_min_sh = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))
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
                                # Blocks until CLOB confirms fill or timeout.
                                _filled_sh, _filled_px = await live_exec.execute(
                                    _open_signal, _live_tid, budget_usd=_cost_usd
                                )
                                if _filled_sh > 0:
                                    # Record confirmed CLOB fill into PnL tracker.
                                    _live_skip_until = 0.0
                                    pnl.live_open(
                                        _open_signal, _filled_sh, _filled_px,
                                        _filled_sh * _filled_px,
                                        strategy_name=decision.get("strategy_name") or "",
                                    )
                                    # Refresh CTF allowance so the subsequent SELL is accepted.
                                    await asyncio.to_thread(
                                        live_exec.ensure_conditional_allowance, _live_tid
                                    )
                                else:
                                    # BUY not filled — impose cooldown to avoid retry spam.
                                    _live_skip_until = now + _live_skip_cooldown_sec
                                    logging.info(
                                        "[LIVE] Skip cooldown active for %.0fs (until %.1f).",
                                        _live_skip_cooldown_sec, _live_skip_until,
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
                _exit_tid = (
                    token_up_id if _exit_side_name in ("BUY_UP", None)
                    else (token_down_id or token_up_id)
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
            stats.show_final_report(
                journal_path=journal.path,
                shutdown_reason=shutdown_reason,
            )
        except Exception as exc:
            logging.error("Final report failed: %s", exc)

def _suppress_uvloop_shutdown_error(args: threading.ExceptHookArgs) -> None:
    """Silence the benign RuntimeError from uvloop cleanup thread on Ctrl+C.

    uvloop's internal shutdown thread calls call_soon_threadsafe after the loop
    is already closed when the user sends multiple SIGINT signals. This is a
    known uvloop issue and does not indicate data loss or corruption.
    """
    if args.exc_type is RuntimeError and "Event loop is closed" in str(args.exc_value):
        return
    threading.__excepthook__(args)


if __name__ == "__main__":
    threading.excepthook = _suppress_uvloop_shutdown_error
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass