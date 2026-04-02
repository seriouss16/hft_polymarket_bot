"""Bootstrap: layered env files and optional uvloop asyncio policy."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from utils.env_merge import merge_env_file
from utils.env_unify import apply_sim_live_unify
from utils.workspace_root import get_workspace_root

_log = logging.getLogger(__name__)


def load_runtime_env() -> None:
    """Load layered runtime configuration files (weakest → strongest).

    Hierarchy (each layer overwrites the previous for keys it defines):

    1. ``config/runtime.env``         — base defaults (weakest)
    2. ``config/runtime_live.env``   — ``LIVE_*`` / CLOB execution defaults
    3. Day/Night session profile     — ``config/runtime_day.env`` or ``config/runtime_night.env``
    4. ``config/sim_slippage.env``   — simulation slippage defaults
    5. ``.env``                       — local overrides (strongest)

    After all layers are merged, :func:`utils.env_unify.apply_sim_live_unify` aligns
    ``LIVE_ORDER_SIZE`` / ``LIVE_MAX_SPREAD`` with ``HFT_*`` when the former are unset.
    Finally, sizing parameters are logged for startup diagnostics.
    """
    root = get_workspace_root()

    # 1. Base defaults (weakest)
    merge_env_file(root / "config" / "runtime.env", overwrite=True)

    # 2. Live / CLOB execution defaults (LIVE_*)
    merge_env_file(root / "config" / "runtime_live.env", overwrite=True)

    # 3. Day/Night session profile (applied at startup based on UTC time)
    from core.session_profile import apply_profile
    apply_profile(force=True)

    # 4. SIM slippage defaults
    merge_env_file(root / "config" / "sim_slippage.env", overwrite=True)

    # 5. Local .env overrides (strongest)
    merge_env_file(root / ".env", overwrite=True)

    # 6. Unify SIM/LIVE params (fills LIVE_ORDER_SIZE from HFT_DEFAULT_TRADE_USD if unset)
    apply_sim_live_unify()

    # 7. Startup logging for diagnostics — confirms the effective sizing params
    _log.info(
        "Startup sizing: LIVE_ORDER_SIZE=%s HFT_DEFAULT_TRADE_USD=%s HFT_MAX_POSITION_USD=%s LIVE_ACCOUNT_BALANCE=%s",
        os.environ.get("LIVE_ORDER_SIZE"),
        os.environ.get("HFT_DEFAULT_TRADE_USD"),
        os.environ.get("HFT_MAX_POSITION_USD"),
        os.environ.get("LIVE_ACCOUNT_BALANCE"),
    )


UVLOOP_ACTIVE = False


def install_uvloop_policy() -> None:
    """Prefer libuv-backed asyncio loop on Linux/macOS when uvloop is available."""
    global UVLOOP_ACTIVE
    if os.getenv("HFT_USE_UVLOOP") == "0":
        return
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        UVLOOP_ACTIVE = True
    except ImportError:
        pass
