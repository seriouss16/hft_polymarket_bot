"""HFT bot entrypoint: env bootstrap, optional uvloop, TensorFlow env, then main loop."""

from __future__ import annotations

import asyncio
import os
import threading

from bot_runtime import install_uvloop_policy, load_runtime_env
from utils.config_validation import validate_config
from utils.config_version import ConfigVersioner

load_runtime_env()
ConfigVersioner().auto_snapshot()
validate_config()
install_uvloop_policy()

os.environ["PYTHONUNBUFFERED"] = "1"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

print(">>> Initializing HFT system...", flush=True)

from bot_config_log import setup_logging  # noqa: E402

setup_logging()

from bot_main_loop import main  # noqa: E402


def _suppress_uvloop_shutdown_error(args: threading.ExceptHookArgs) -> None:
    """Silence the benign RuntimeError from uvloop cleanup thread on Ctrl+C.

    uvloop's internal shutdown thread calls call_soon_threadsafe after the loop
    is already closed when the user sends multiple SIGINT signals. This is a
    known uvloop issue and does not indicate data loss or corruption.
    """
    if args.exc_type is RuntimeError and "Event loop is closed" in str(args.exc_value):
        return
    threading.__excepthook__(args)


def run_cli() -> None:
    """Console entry for \`uv run hft-bot\` / \`hft-bot\`."""
    threading.excepthook = _suppress_uvloop_shutdown_error
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_cli()
