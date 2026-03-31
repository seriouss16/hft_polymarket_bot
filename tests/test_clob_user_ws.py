"""Unit tests for ClobUserOrderCache message parsing (no network)."""

from __future__ import annotations

import time

import pytest

from data.clob_user_ws import ClobUserOrderCache


class _Creds:
    api_key = "k"
    api_secret = "s"
    api_passphrase = "p"


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setenv("CLOB_USER_WS_ENABLED", "0")
    c = ClobUserOrderCache(lambda: _Creds())
    return c


def test_handle_order_update_partial(cache: ClobUserOrderCache):
    cache._handle_raw(
        '{"event_type":"order","type":"UPDATE","id":"0xabc","size_matched":"3",'
        '"original_size":"10"}'
    )
    st, filled = cache.get_order_fill("0xabc")
    assert st == "partially_matched"
    assert abs(filled - 3.0) < 1e-9


def test_handle_order_update_full(cache: ClobUserOrderCache):
    cache._handle_raw(
        '{"event_type":"order","type":"UPDATE","id":"0xabc","size_matched":"10",'
        '"original_size":"10"}'
    )
    st, filled = cache.get_order_fill("0xabc")
    assert st == "matched"
    assert abs(filled - 10.0) < 1e-9


def test_handle_order_without_event_type_uses_inner_type(cache: ClobUserOrderCache):
    cache._handle_raw('{"type":"PLACEMENT","id":"0xdef","size_matched":"0","original_size":"5"}')
    st, filled = cache.get_order_fill("0xdef")
    assert st == "live"
    assert filled == 0.0


def test_stale_row_returns_none(cache: ClobUserOrderCache, monkeypatch):
    monkeypatch.setenv("CLOB_USER_WS_MAX_STALE_SEC", "0.001")
    cache._max_stale_sec = 0.001
    cache._handle_raw(
        '{"event_type":"order","type":"UPDATE","id":"0xold","size_matched":"1","original_size":"2"}'
    )
    time.sleep(0.05)
    assert cache.get_order_fill("0xold") is None


def test_trade_without_event_type(cache: ClobUserOrderCache):
    cache._handle_raw(
        '{"type":"TRADE","status":"MATCHED","size":"2","taker_order_id":"0xt"}'
    )
    st, filled = cache.get_order_fill("0xt")
    assert st == "matched"
    assert abs(filled - 2.0) < 1e-9
