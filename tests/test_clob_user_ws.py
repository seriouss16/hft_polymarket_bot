"""Unit tests for ClobUserOrderCache message parsing (no network)."""

from __future__ import annotations

import time

import pytest

from data.clob_user_ws import ClobUserOrderCache, _extract_server_sequence


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


def test_extract_server_sequence_common_keys() -> None:
    assert _extract_server_sequence({"seq": "42"}) == 42
    assert _extract_server_sequence({"sequence_number": 7}) == 7
    assert _extract_server_sequence({"event_type": "order"}) is None


def test_server_sequence_gap_detected_on_handle_raw(cache: ClobUserOrderCache):
    cache._handle_raw(
        '{"event_type":"order","type":"UPDATE","id":"0xs1","seq":1,'
        '"size_matched":"0","original_size":"1"}'
    )
    cache._handle_raw(
        '{"event_type":"order","type":"UPDATE","id":"0xs2","seq":3,'
        '"size_matched":"0","original_size":"1"}'
    )
    m = cache.get_metrics()
    assert m["sequence_gaps_detected"] == 1
    assert m["sequence_number"] == 3


def test_reconnect_buffer_replays_after_stop(cache: ClobUserOrderCache):
    cache.start_reconnect_buffering()
    cache.handle_ws_message_with_sequence(
        '{"event_type":"order","type":"UPDATE","id":"0xbuf","size_matched":"1",'
        '"original_size":"10"}'
    )
    assert cache.get_order_fill("0xbuf") is None
    replayed = cache.stop_reconnect_buffering()
    assert replayed == 1
    st, filled = cache.get_order_fill("0xbuf")
    assert st == "partially_matched"
    assert abs(filled - 1.0) < 1e-9


def test_reconnect_buffer_json_array_replays_both_orders(cache: ClobUserOrderCache):
    cache.start_reconnect_buffering()
    cache.handle_ws_message_with_sequence(
        "["
        '{"event_type":"order","type":"UPDATE","id":"0xa1","size_matched":"1","original_size":"2"},'
        '{"event_type":"order","type":"UPDATE","id":"0xa2","size_matched":"0","original_size":"1"}'
        "]"
    )
    assert cache.get_order_fill("0xa1") is None
    assert cache.stop_reconnect_buffering() == 2
    assert cache.get_order_fill("0xa1") is not None
    assert cache.get_order_fill("0xa2") is not None


def test_order_cache_bounded(monkeypatch):
    monkeypatch.setenv("CLOB_USER_WS_ENABLED", "0")
    monkeypatch.setenv("CLOB_USER_WS_MAX_ORDER_ENTRIES", "3")
    c = ClobUserOrderCache(lambda: _Creds())
    for i in range(5):
        c._touch(f"0x{i:04x}", "live", 0.0)
    assert len(c._orders) == 3
    assert len(c._state_machine) == 3
