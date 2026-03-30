"""Tests for trade journal schema, migration, and entry composers."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from utils.stats import StatsCollector
from utils.trade_journal import (
    JOURNAL_FIELDNAMES,
    JournalEntryComposer,
    TradeJournal,
    migrate_journal_schema_if_needed,
)
from core.executor import PnLTracker


# Legacy header (before exit_rsi_raw + row_kind) — matches pre-refactor _FIELDNAMES
_LEGACY_HEADER = (
    "ts,side,entry_edge,exit_edge,duration_sec,entry_trend,entry_speed,entry_depth,"
    "entry_imbalance,latency_ms,pnl,exit_reason,exit_rsi,rsi_band_lower,rsi_band_upper,"
    "rsi_slope,entry_book_px,entry_exec_px,exit_book_px,exit_exec_px,shares_bought,"
    "shares_sold,cost_usd,cost_basis_usd,proceeds_usd,entry_up_bid,entry_up_ask,"
    "entry_down_bid,entry_down_ask,exit_up_bid,exit_up_ask,exit_down_bid,exit_down_ask,"
    "strategy_name,entry_profile,performance_key\n"
)


def test_journal_fieldnames_include_row_kind_and_exit_rsi_raw():
    assert "row_kind" in JOURNAL_FIELDNAMES
    assert "exit_rsi_raw" in JOURNAL_FIELDNAMES
    assert JOURNAL_FIELDNAMES.index("exit_rsi_raw") == JOURNAL_FIELDNAMES.index("exit_rsi") + 1


def test_migrate_old_header_adds_new_columns(tmp_path: Path):
    p = tmp_path / "j.csv"
    p.write_text(_LEGACY_HEADER + "1.0,UP,0.1,,,,,,,0.5,1.2,TP,50,,,,0.4,0.41,,,10,10,4,,4,,,,,,,,,lat,prof,key\n", encoding="utf-8")
    migrate_journal_schema_if_needed(p, JOURNAL_FIELDNAMES)
    first = p.read_text(encoding="utf-8").splitlines()[0]
    assert "row_kind" in first
    assert "exit_rsi_raw" in first
    with p.open(encoding="utf-8", newline="") as f:
        r = next(csv.DictReader(f))
    assert r.get("row_kind") == "close"
    assert r.get("exit_rsi_raw") == ""


def test_trade_journal_record_close_writes_row_kind_close(tmp_path: Path):
    j = TradeJournal(path=str(tmp_path / "tj.csv"))
    dec = {
        "side": "UP",
        "entry_edge": 0.02,
        "exit_edge": 0.01,
        "duration_sec": 12.0,
        "entry_trend": "UP",
        "entry_speed": 1.0,
        "entry_depth": 0.5,
        "entry_imbalance": 0.4,
        "latency_ms": 30.0,
        "reason": "TP",
        "entry_book_px": 0.4,
        "entry_exec_px": 0.41,
        "exit_book_px": 0.45,
        "exit_exec_px": 0.44,
        "shares_bought": 10.0,
        "shares_sold": 10.0,
        "cost_usd": 4.1,
        "cost_basis_usd": 4.1,
        "proceeds_usd": 4.4,
        "entry_up_bid": 0.39,
        "entry_up_ask": 0.41,
        "entry_down_bid": 0.58,
        "entry_down_ask": 0.6,
        "exit_up_bid": 0.44,
        "exit_up_ask": 0.46,
        "exit_down_bid": 0.52,
        "exit_down_ask": 0.54,
        "strategy_name": "latency_arbitrage",
        "entry_profile": "day",
        "performance_key": "latency:foo",
    }
    rsi = {"rsi": 55.0, "rsi_raw": 54.2, "lower": 30.0, "upper": 70.0, "slope": 0.1}
    j.record_close(decision=dec, live_pnl=0.35, rsi_state=rsi)
    with Path(j.path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["row_kind"] == "close"
    assert rows[0]["exit_rsi_raw"] == "54.2"
    assert float(rows[0]["pnl"]) == pytest.approx(0.35)


def test_trade_journal_record_open_writes_row_kind_open(tmp_path: Path):
    j = TradeJournal(path=str(tmp_path / "tj2.csv"))
    dec = {
        "side": "DOWN",
        "entry_edge": 0.03,
        "entry_trend": "DOWN",
        "entry_speed": 0.0,
        "entry_depth": 0.0,
        "entry_imbalance": 0.5,
        "latency_ms": 25.0,
        "strategy_name": "latency_arbitrage",
        "entry_profile": "night",
        "performance_key": "",
        "trade": {"book_px": 0.55, "exec_px": 0.56, "amount_usd": 10.0},
    }
    book = {
        "bid": 0.44,
        "ask": 0.46,
        "down_bid": 0.53,
        "down_ask": 0.55,
    }
    rsi = {"rsi": 48.0, "rsi_raw": 47.5, "lower": 30.0, "upper": 70.0, "slope": -0.2}
    j.record_open(
        decision=dec,
        filled_shares=18.0,
        avg_price=0.555,
        amount_usd=9.99,
        rsi_state=rsi,
        book_snapshot=book,
    )
    with Path(j.path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["row_kind"] == "open"
    assert rows[0]["shares_bought"] == "18.0"
    assert rows[0]["entry_exec_px"] == "0.555"
    assert float(rows[0]["cost_usd"] or 0) == pytest.approx(9.99)
    assert rows[0]["exit_reason"] in ("", None) or rows[0]["exit_reason"] == ""


def test_journal_entry_composer_close_includes_row_kind():
    row = JournalEntryComposer.close_row(
        decision={"side": "UP", "reason": "SL", "pnl": 0.0},
        live_pnl=-1.5,
        rsi_state={"rsi": 40.0, "rsi_raw": 39.0, "lower": 30.0, "upper": 70.0, "slope": 0.0},
    )
    assert row["row_kind"] == "close"
    assert row["exit_rsi_raw"] == 39.0
    assert row["pnl"] == -1.5


def test_stats_journal_aggregates_skip_open_rows(tmp_path: Path):
    p = tmp_path / "mix.csv"
    # One open (pnl 0) and one close — stats must only count close for PnL
    j = TradeJournal(path=str(p))
    j.record_open(
        decision={"side": "UP", "latency_ms": 1.0, "strategy_name": "x"},
        filled_shares=5.0,
        avg_price=0.5,
        amount_usd=2.5,
        rsi_state=None,
        book_snapshot=None,
    )
    j.record_close(
        decision={"side": "UP", "reason": "TP"},
        live_pnl=0.5,
        rsi_state={"rsi": 50.0, "rsi_raw": 50.0, "lower": 30.0, "upper": 70.0, "slope": 0.0},
    )
    stats = StatsCollector(PnLTracker(live_mode=False))
    js = stats._journal_aggregates(p)
    assert js.rows == 1
    assert js.pnl_sum == pytest.approx(0.5)
