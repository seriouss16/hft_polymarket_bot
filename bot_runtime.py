"""Bootstrap: layered env files and optional uvloop asyncio policy."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from utils.env_merge import merge_env_file


def load_runtime_env() -> None:
    """Load layered runtime configuration files (runtime.env then .env overrides)."""
    root = Path(__file__).resolve().parent
    merge_env_file(root / "config" / "runtime.env", overwrite=False)
    merge_env_file(root / ".env", overwrite=True)


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
