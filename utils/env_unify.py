"""Unify SIM (HFT_*) and LIVE execution params so one profile drives both modes."""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)


def apply_sim_live_unify() -> None:
    """If ``LIVE_ORDER_SIZE`` / ``LIVE_MAX_SPREAD`` are unset, copy from HFT_*.

    - ``LIVE_ORDER_SIZE`` ← ``HFT_DEFAULT_TRADE_USD``
    - ``LIVE_MAX_SPREAD`` ← ``HFT_MAX_ENTRY_SPREAD``

    Explicit ``LIVE_*`` values are never overwritten. Call after layered ``merge_env_file``
    (e.g. from :func:`bot_runtime.load_runtime_env` and pytest ``conftest``).
    """
    if not os.environ.get("LIVE_ORDER_SIZE", "").strip():
        src = os.environ.get("HFT_DEFAULT_TRADE_USD", "").strip()
        if src:
            os.environ["LIVE_ORDER_SIZE"] = src
            _log.info(
                "Config unify: LIVE_ORDER_SIZE unset → HFT_DEFAULT_TRADE_USD=%s",
                src,
            )
    if not os.environ.get("LIVE_MAX_SPREAD", "").strip():
        src = os.environ.get("HFT_MAX_ENTRY_SPREAD", "").strip()
        if src:
            os.environ["LIVE_MAX_SPREAD"] = src
            _log.info(
                "Config unify: LIVE_MAX_SPREAD unset → HFT_MAX_ENTRY_SPREAD=%s",
                src,
            )
