"""StatsCollector per-slot close aggregation."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.executor import PnLTracker  # noqa: E402
from utils.stats import StatsCollector  # noqa: E402


def test_record_slot_close_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_STATS_SLOT_TABLE", "1")
    pnl = PnLTracker(initial_balance=100.0, live_mode=False)
    st = StatsCollector(pnl)
    t0 = 1_705_318_800  # fixed UTC instant for stable strftime in table
    st.record_slot_close(0.5, t0)
    st.record_slot_close(-0.2, t0)
    st.record_slot_close(0.1, t0 + 300)
    lines = st._slot_performance_lines()
    joined = "\n".join(lines)
    assert "Σ" in joined
    assert "+0.40" in joined or "+0.4" in joined
    assert "  2 " in joined or " 2-" in joined  # two closes in first slot


def test_slot_table_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_STATS_SLOT_TABLE", "0")
    pnl = PnLTracker(initial_balance=100.0, live_mode=False)
    st = StatsCollector(pnl)
    st.record_slot_close(1.0, 1_000_000_000)
    assert st._slot_performance_lines() == []
