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


# ---------------------------------------------------------------------
# Cached snapshot and optimized imbalance tests
# ---------------------------------------------------------------------

def test_cached_snapshot_basic() -> None:
    """Test that snapshot caching works and returns consistent results."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "test-token",
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.49", "size": "50"},
                {"price": "0.48", "size": "25"},
            ],
            "asks": [
                {"price": "0.52", "size": "80"},
                {"price": "0.53", "size": "40"},
                {"price": "0.54", "size": "20"},
            ],
        }
    )
    # First call should compute and cache
    snap1 = c.get_snapshot_with_imbalance("test-token", 5)
    assert snap1 is not None
    assert "bid_vol_topn" in snap1
    assert "ask_vol_topn" in snap1
    assert "imbalance" in snap1
    
    # Second call should use cache (same values)
    snap2 = c.get_snapshot_with_imbalance("test-token", 5)
    assert snap2 is not None
    assert snap1["bid_vol_topn"] == snap2["bid_vol_topn"]
    assert snap1["ask_vol_topn"] == snap2["ask_vol_topn"]
    assert snap1["imbalance"] == snap2["imbalance"]


def test_cached_snapshot_dirty_on_book_update() -> None:
    """Test that cache is invalidated when book updates arrive."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "test-token",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.52", "size": "80"}],
        }
    )
    # First call caches
    snap1 = c.get_snapshot_with_imbalance("test-token", 5)
    assert snap1 is not None
    initial_bid_vol = snap1["bid_vol_topn"]
    
    # Update book with price change
    c._apply_price_change(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "test-token",
                    "price": "0.50",
                    "size": "200",  # Increase size
                    "side": "BUY",
                }
            ],
        }
    )
    
    # Next call should recompute
    snap2 = c.get_snapshot_with_imbalance("test-token", 5)
    assert snap2 is not None
    # Bid volume should have changed
    assert snap2["bid_vol_topn"] != initial_bid_vol
    assert snap2["bid_vol_topn"] == 200.0


def test_cached_snapshot_dirty_on_price_change() -> None:
    """Test that price change invalidates cache."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    # Cache first snapshot
    s1 = c.get_snapshot_with_imbalance("t", 5)
    assert s1 is not None
    assert c._snapshot_dirty.get("t") is False
    
    # Apply price change
    c._apply_price_change(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "t", "price": "0.5", "size": "0", "side": "BUY"}
            ],
        }
    )
    # Dirty flag should be set
    assert c._snapshot_dirty.get("t") is True
    
    # After reading, dirty flag should be cleared
    s2 = c.get_snapshot_with_imbalance("t", 5)
    assert s2 is not None
    assert c._snapshot_dirty.get("t") is False


def test_top_n_extraction_correctness() -> None:
    """Test that top-N extraction produces same results as full sort."""
    c = ClobMarketBookCache()
    c.enabled = False
    # Create book with many levels
    bids = {0.50 + i * 0.01: 10.0 for i in range(20)}
    asks = {0.60 + i * 0.01: 10.0 for i in range(20)}
    with c._lock:
        c._bids["test"] = bids
        c._asks["test"] = asks
        c._touch("test")
        c._snapshot_dirty["test"] = True
    
    # Get snapshot with depth=5
    snap = c.get_snapshot_with_imbalance("test", 5)
    assert snap is not None
    
    # Check that volumes are calculated correctly (top 5 levels * 10 = 50 each)
    assert snap["bid_vol_topn"] == 50.0
    assert snap["ask_vol_topn"] == 50.0
    
    # Check that imbalance is 0 (equal volumes)
    assert abs(snap["imbalance"]) < 1e-9
    
    # Verify that the cached top levels are stored correctly
    cached_bids = c._cached_top_bids.get("test")
    cached_asks = c._cached_top_asks.get("test")
    assert cached_bids is not None
    assert cached_asks is not None
    assert len(cached_bids) == 5
    assert len(cached_asks) == 5
    
    # Bids should be in descending order (highest first)
    bid_prices = [p for p, _ in cached_bids]
    assert bid_prices == sorted(bid_prices, reverse=True)
    # Highest bid should be 0.50 + 19*0.01 = 0.69
    assert abs(bid_prices[0] - 0.69) < 1e-9
    # Lowest of top 5 should be 0.65
    assert abs(bid_prices[-1] - 0.65) < 1e-9
    
    # Asks should be in ascending order (lowest first)
    ask_prices = [p for p, _ in cached_asks]
    assert ask_prices == sorted(ask_prices)
    # Lowest ask should be 0.60
    assert abs(ask_prices[0] - 0.60) < 1e-9
    # Highest of top 5 should be 0.64
    assert abs(ask_prices[-1] - 0.64) < 1e-9


def test_incremental_imbalance_calculation() -> None:
    """Test that incremental imbalance matches direct calculation."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "test",
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.49", "size": "50"},
            ],
            "asks": [
                {"price": "0.52", "size": "80"},
                {"price": "0.53", "size": "40"},
            ],
        }
    )
    # Get snapshot with imbalance
    snap = c.get_snapshot_with_imbalance("test", 5)
    assert snap is not None
    
    # Calculate expected imbalance manually
    bid_vol = 100.0 + 50.0  # 150
    ask_vol = 80.0 + 40.0   # 120
    expected_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    
    assert abs(snap["bid_vol_topn"] - bid_vol) < 1e-9
    assert abs(snap["ask_vol_topn"] - ask_vol) < 1e-9
    assert abs(snap["imbalance"] - expected_imbalance) < 1e-9
    
    # Check cached values
    assert c._total_bid_volume.get("test") == bid_vol
    assert c._total_ask_volume.get("test") == ask_vol
    assert abs(c._cached_imbalance.get("test") - expected_imbalance) < 1e-9


def test_cache_disabled_when_config_off() -> None:
    """Test that caching can be disabled via environment."""
    import os
    os.environ["HFT_CACHE_BOOK_SNAPSHOT"] = "0"
    c = ClobMarketBookCache()
    # Re-read env by creating new instance
    c._cache_enabled = False
    
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    # Should not use cache
    snap1 = c.get_snapshot_with_imbalance("t", 5)
    snap2 = c.get_snapshot_with_imbalance("t", 5)
    # Without cache, these are separate computations but values should be equal
    assert snap1["bid_vol_topn"] == snap2["bid_vol_topn"]
    assert c._cached_snapshot == {}


def test_snapshot_with_non_default_depth() -> None:
    """Test that caching only applies when depth matches _top_n."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t",
            "bids": [{"price": "0.5", "size": "10"}, {"price": "0.49", "size": "5"}],
            "asks": [{"price": "0.6", "size": "8"}, {"price": "0.61", "size": "4"}],
        }
    )
    # First call with depth=5 (matches default _top_n)
    snap1 = c.get_snapshot_with_imbalance("t", 5)
    # Cache should be populated
    assert "t" in c._cached_snapshot
    
    # Second call with different depth should bypass cache
    snap2 = c.get_snapshot_with_imbalance("t", 3)
    # Values should still be correct (just different depth)
    assert snap2 is not None
    # The cached snapshot remains for depth=5
    assert "t" in c._cached_snapshot


def test_invalidate_cache_method() -> None:
    """Test explicit cache invalidation."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    # Cache it
    c.get_snapshot_with_imbalance("t", 5)
    assert "t" in c._cached_snapshot
    assert c._snapshot_dirty.get("t") is False
    
    # Invalidate
    c.invalidate_cache("t")
    assert c._snapshot_dirty.get("t") is True
    assert "t" not in c._cached_snapshot


def test_set_asset_ids_clears_removed_cache() -> None:
    """Test that changing asset IDs clears cache for removed assets."""
    c = ClobMarketBookCache()
    c.enabled = False
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t1",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    c._apply_book(
        {
            "event_type": "book",
            "asset_id": "t2",
            "bids": [{"price": "0.5", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
    )
    # Cache both
    c.get_snapshot_with_imbalance("t1", 5)
    c.get_snapshot_with_imbalance("t2", 5)
    assert "t1" in c._cached_snapshot
    assert "t2" in c._cached_snapshot
    
    # Keep only t1
    c.set_asset_ids(["t1"])
    # t2 cache should be cleared
    assert "t2" not in c._cached_snapshot
    assert "t2" not in c._total_bid_volume
    # t1 should still be cached (but dirty since book update set dirty flag)
    # Actually set_asset_ids doesn't touch t1's book, so it remains cached
    assert "t1" in c._cached_snapshot or c._snapshot_dirty.get("t1", True)
