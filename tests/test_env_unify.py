"""Tests for utils.env_unify.apply_sim_live_unify."""

import os

import pytest

from utils.env_unify import apply_sim_live_unify


def test_unify_fills_live_from_hft_when_absent(monkeypatch):
    monkeypatch.delenv("LIVE_ORDER_SIZE", raising=False)
    monkeypatch.delenv("LIVE_MAX_SPREAD", raising=False)
    monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "7.5")
    monkeypatch.setenv("HFT_MAX_ENTRY_SPREAD", "0.04")
    apply_sim_live_unify()
    assert os.environ["LIVE_ORDER_SIZE"] == "7.5"
    assert os.environ["LIVE_MAX_SPREAD"] == "0.04"


def test_unify_explicit_live_override(monkeypatch):
    monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "7.5")
    monkeypatch.setenv("HFT_MAX_ENTRY_SPREAD", "0.04")
    monkeypatch.setenv("LIVE_ORDER_SIZE", "99")
    monkeypatch.setenv("LIVE_MAX_SPREAD", "0.11")
    apply_sim_live_unify()
    assert os.environ["LIVE_ORDER_SIZE"] == "99"
    assert os.environ["LIVE_MAX_SPREAD"] == "0.11"
