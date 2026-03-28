"""Tests for live CLOSE token routing in bot.py."""

from __future__ import annotations

from bot import _conditional_token_for_position_side


def test_position_side_up_maps_to_token_up():
    """UP must select the UP outcome token (regression: was wrongly using DOWN)."""
    up, down = "74817163694801044820", "92024492501678580392"
    assert _conditional_token_for_position_side("UP", up, down) == up


def test_buy_up_maps_to_token_up():
    """BUY_UP selects the UP token."""
    up, down = "a", "b"
    assert _conditional_token_for_position_side("BUY_UP", up, down) == up


def test_position_side_down_maps_to_token_down():
    """DOWN selects the DOWN token."""
    up, down = "a", "b"
    assert _conditional_token_for_position_side("DOWN", up, down) == down


def test_none_defaults_to_token_up():
    """None falls through to UP token (legacy behaviour)."""
    assert _conditional_token_for_position_side(None, "a", "b") == "a"
