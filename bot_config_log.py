"""Runtime config validation and logging setup for the HFT bot.

SIM vs LIVE sizing/spread: set ``HFT_DEFAULT_TRADE_USD`` and ``HFT_MAX_ENTRY_SPREAD`` once.
If ``LIVE_ORDER_SIZE`` / ``LIVE_MAX_SPREAD`` are omitted, :func:`utils.env_unify.apply_sim_live_unify`
copies from those HFT keys (also run from :func:`bot_runtime.load_runtime_env`).
Explicit ``LIVE_*`` values still override.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from utils.env_config import req_str
from utils.env_unify import apply_sim_live_unify
from utils.log_dedupe import SameMessageDedupeFilter
from utils.workspace_root import get_workspace_root


def _silence_http_client_loggers() -> None:
    """Lower noise from urllib3/requests (used by MarketSelector and HTTP helpers)."""
    raw = req_str("HFT_HTTP_CLIENT_LOG_LEVEL")
    level = getattr(logging, raw.upper(), logging.WARNING)
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
    "LIVE_MAX_SESSION_LOSS",
    "MAX_DRAWDOWN_PCT",
    "MAX_POSITION_PCT",
    "LOSS_COOLDOWN_SEC",
    # LIVE_MAX_SPREAD / LIVE_ORDER_SIZE: filled from HFT_MAX_ENTRY_SPREAD / HFT_DEFAULT_TRADE_USD
    # by _unify_sim_live_trading_params() when unset; validated below.
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


def _migrate_legacy_live_max_session_loss() -> None:
    """If LIVE_MAX_SESSION_LOSS is unset, copy from deprecated LIVE_MAX_DAILY_LOSS."""
    if os.environ.get("LIVE_MAX_SESSION_LOSS", "").strip():
        return
    legacy = os.environ.get("LIVE_MAX_DAILY_LOSS", "").strip()
    if legacy:
        os.environ["LIVE_MAX_SESSION_LOSS"] = legacy


def validate_required_config(live_mode: bool) -> None:
    """Abort startup if required parameters are missing from the environment."""
    apply_sim_live_unify()
    if live_mode:
        _migrate_legacy_live_max_session_loss()
    missing: list[str] = []
    for key in _SIM_REQUIRED_KEYS:
        if not os.environ.get(key, "").strip():
            missing.append(f"  {key}  (required for sim and live)")
    if live_mode:
        for key in _LIVE_REQUIRED_KEYS:
            if not os.environ.get(key, "").strip():
                missing.append(f"  {key}  (required for LIVE_MODE=1)")
    if live_mode:
        if not os.environ.get("LIVE_ORDER_SIZE", "").strip():
            missing.append("  LIVE_ORDER_SIZE  (or set HFT_DEFAULT_TRADE_USD — unified at startup)")
        if not os.environ.get("LIVE_MAX_SPREAD", "").strip():
            missing.append("  LIVE_MAX_SPREAD  (or set HFT_MAX_ENTRY_SPREAD — unified at startup)")
    if missing:
        lines = "\n".join(missing)
        _abort = (
            f"\n{'=' * 60}\n"
            f"🛑  STARTUP ABORTED — missing required config keys:\n"
            f"{lines}\n"
            f"\nAdd the missing keys to hft_bot/config/runtime.env\n"
            f"or to hft_bot/.env (overrides runtime.env).\n"
            f"{'=' * 60}\n"
        )
        logging.critical("%s", _abort)
        raise SystemExit(1)


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
    root = get_workspace_root()
    keys = _runtime_configuration_keys(root)
    logging.info("--- Runtime configuration (effective env, %s keys) ---", len(keys))
    for k in keys:
        logging.info("  %s=%s", k, _format_config_value(k, os.environ.get(k)))
    logging.info("--- End runtime configuration ---")


def _resolve_log_dir() -> Path:
    """Log directory: ``HFT_LOG_DIR`` or ``<workspace>/reports/logs`` (relative paths vs workspace)."""
    root = get_workspace_root()
    raw = (os.getenv("HFT_LOG_DIR") or "").strip()
    if not raw:
        return root / "reports" / "logs"
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def setup_logging() -> None:
    """Configure stdout logging and per-run file logs with retention."""
    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    _raw_keep = (os.getenv("HFT_LOG_KEEP_FILES") or "100").strip()
    try:
        keep_files = int(_raw_keep) if _raw_keep else 100
    except ValueError:
        keep_files = 100
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
    logging.info(
        "File logging initialized: %s (retention=%s)",
        log_path,
        keep_files,
    )
    _log_runtime_configuration()
