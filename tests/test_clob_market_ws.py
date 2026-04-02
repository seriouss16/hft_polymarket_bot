"""Unit tests for CLOB market WebSocket book cache (no network)."""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.clob_market_ws import (  # noqa: E402
    ClobMarketBookCache,
    sync_poly_book_from_cache,
)


def test_book_event_official_doc_shape() -> None:
    """Schema from Polymarket docs (book: bids/asks with string .48 / size)."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "65818619657568813474341868652308942079804919287380422192892211131408793125422",
            "market": "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af",
            "bids": [
                {"price": ".48", "size": "30"},
                {"price": ".49", "size": "20"},
                {"price": ".50", "size": "15"},
            ],
            "asks": [
                {"price": ".52", "size": "25"},
                {"price": ".53", "size": "60"},
                {"price": ".54", "size": "10"},
            ],
            "timestamp": "123456789000",
            "hash": "0x0",
        }
    )
    tid = "65818619657568813474341868652308942079804919287380422192892211131408793125422"
    s = c.snapshot(tid, 5)
    assert s is not None
    # Best bid = highest bid price (.50); best ask = lowest ask price (.52).
    assert abs(float(s["best_bid"]) - 0.50) < 1e-9
    assert abs(float(s["best_ask"]) - 0.52) < 1e-9
    assert abs(float(s["bid_size_top"]) - 15.0) < 1e-9
    assert abs(float(s["ask_size_top"]) - 25.0) < 1e-9


def test_apply_book_snapshot_matches_snapshot_from_levels() -> None:
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "tid-up",
            "bids": [{"price": ".48", "size": "30"}, {"price": ".47", "size": "10"}],
            "asks": [{"price": ".52", "size": "25"}, {"price": ".53", "size": "5"}],
        }
    )
    s = c.snapshot("tid-up", 5)
    assert s is not None
    assert abs(float(s["best_bid"]) - 0.48) < 1e-9
    assert abs(float(s["best_ask"]) - 0.52) < 1e-9
    assert c.is_fresh("tid-up")


def test_price_change_updates_level() -> None:
    """Removing the top bid via price_change (size 0) drops best bid below 0.5 or to empty (0)."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "tid",
            "bids": [{"price": "0.5", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }
    )
    c._apply_price_change(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "tid",
                    "price": "0.5",
                    "size": "0",
                    "side": "BUY",
                }
            ],
        }
    )
    s = c.snapshot("tid", 5)
    assert s is not None
    assert float(s["best_bid"]) < 0.5 or float(s["best_bid"]) == 0.0


def test_best_bid_ask_official_doc_shape() -> None:
    """``best_bid_ask`` with custom_feature — seed book when empty."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_best_bid_ask(
        {
            "event_type": "best_bid_ask",
            "market": "0x0005c0d312de0be897668695bae9f32b624b4a1ae8b140c49f08447fcc74f442",
            "asset_id": "85354956062430465315924116860125388538595433819574542752031640332592237464430",
            "best_bid": "0.73",
            "best_ask": "0.77",
            "spread": "0.04",
            "timestamp": "1766789469958",
        }
    )
    tid = "85354956062430465315924116860125388538595433819574542752031640332592237464430"
    s = c.snapshot(tid, 5)
    assert s is not None
    assert abs(float(s["best_bid"]) - 0.73) < 1e-9
    assert abs(float(s["best_ask"]) - 0.77) < 1e-9


def test_best_bid_ask_updates_top_when_book_exists() -> None:
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "x",
            "bids": [{"price": "0.70", "size": "40"}],
            "asks": [{"price": "0.80", "size": "50"}],
        }
    )
    c._apply_best_bid_ask(
        {
            "event_type": "best_bid_ask",
            "asset_id": "x",
            "best_bid": "0.73",
            "best_ask": "0.77",
            "spread": "0.04",
        }
    )
    s = c.snapshot("x", 5)
    assert s is not None
    assert abs(float(s["best_bid"]) - 0.73) < 1e-9
    assert abs(float(s["best_ask"]) - 0.77) < 1e-9
    assert abs(float(s["bid_size_top"]) - 40.0) < 1e-9
    assert abs(float(s["ask_size_top"]) - 50.0) < 1e-9


def test_handle_raw_json_array_of_events() -> None:
    """Polymarket may send a JSON list of event objects; must not call .get on list."""
    c = ClobMarketBookCache()
    c.enabled = False
    raw = """[
      {"event_type":"book","asset_id":"a1","bids":[{"price":"0.4","size":"1"}],"asks":[{"price":"0.6","size":"1"}]},
      {"event_type":"book","asset_id":"a2","bids":[{"price":"0.3","size":"2"}],"asks":[{"price":"0.7","size":"2"}]}
    ]"""
    c._handle_raw(raw)
    assert c.snapshot("a1", 5) is not None
    assert c.snapshot("a2", 5) is not None


def test_sync_poly_book_from_cache() -> None:
    c = ClobMarketBookCache()
    c.enabled = False
    for tid in ("u", "d"):
        c._apply_book(
            {
                "event_type": "book",
                "asset_id": tid,
                "bids": [{"price": "0.4", "size": "10"}],
                "asks": [{"price": "0.6", "size": "10"}],
            }
        )
    pb: dict = {}
    ok = sync_poly_book_from_cache(pb, c, "u", "d", loop_ts=123.0)
    assert ok is True
    assert pb["ts"] == 123.0
    assert float(pb["bid"]) == 0.4
    assert float(pb["down_bid"]) == 0.4


def test_sync_poly_book_no_partial_merge_when_down_missing() -> None:
    """If DOWN is missing, do not write UP-only fields (all-or-nothing)."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "u",
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    pb = {"bid": 0.11, "ask": 0.88}
    ok = sync_poly_book_from_cache(pb, c, "u", "d", loop_ts=99.0)
    assert ok is False
    assert pb["bid"] == 0.11
    assert pb["ask"] == 0.88
    assert "ts" not in pb


def test_sync_poly_book_up_only_when_no_down_token() -> None:
    """With ``token_down_id is None``, only UP leg is required."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "u",
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    pb: dict = {}
    assert sync_poly_book_from_cache(pb, c, "u", None, loop_ts=1.0) is True
    assert float(pb["bid"]) == 0.4
    assert "down_bid" not in pb


def test_health_metrics_no_timestamps_last_message_age_inf() -> None:
    """Empty _last_ts must not report 0s age (would look falsely fresh)."""
    c = ClobMarketBookCache()
    c.enabled = False
    m = c.get_health_metrics()
    assert m["last_message_age_sec"] == math.inf
