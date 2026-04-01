"""Bootstrap: layered env files and optional uvloop asyncio policy."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from utils.env_merge import merge_env_file
from utils.env_unify import apply_sim_live_unify
from utils.workspace_root import get_workspace_root


def load_runtime_env() -> None:
    """Load layered runtime configuration files (defaults, then runtime, then .env).

    ``sim_slippage.env`` is merged first so SIM slippage defaults apply when
    ``runtime.env`` omits those keys; ``runtime.env`` then overwrites (see merge_env_file).
    Finally :func:`utils.env_unify.apply_sim_live_unify` aligns ``LIVE_ORDER_SIZE`` /
    ``LIVE_MAX_SPREAD`` with ``HFT_*`` when the former are unset.
    """
    root = get_workspace_root()
    merge_env_file(root / "config" / "sim_slippage.env", overwrite=False)
    merge_env_file(root / "config" / "runtime.env", overwrite=True)
    merge_env_file(root / ".env", overwrite=True)
    apply_sim_live_unify()


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
