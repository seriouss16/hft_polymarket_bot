import pytest
import time
from data.clob_market_ws import ClobMarketBookCache

def test_sequence_protection_apply_book():
    cache = ClobMarketBookCache()
    asset_id = "0x123"
    
    # 1. Initial book snapshot
    msg1 = {
        "event_type": "book",
        "asset_id": asset_id,
        "sequence": 100,
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}]
    }
    cache._apply_book(msg1)
    assert cache._last_sequence.get(asset_id) == 100
    assert len(cache._bids.get(asset_id, {})) == 1
    
    # 2. Stale book snapshot (seq <= last_seq)
    msg2 = {
        "event_type": "book",
        "asset_id": asset_id,
        "sequence": 99,
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.41", "size": "10"}]
    }
    cache._apply_book(msg2)
    assert cache._last_sequence.get(asset_id) == 100
    # Bids should still be from msg1
    assert cache._bids[asset_id][0.50] == 10.0
    
    # 3. Future book snapshot (seq > last_seq)
    msg3 = {
        "event_type": "book",
        "asset_id": asset_id,
        "sequence": 105,
        "bids": [{"price": "0.52", "size": "20"}],
        "asks": [{"price": "0.53", "size": "20"}]
    }
    cache._apply_book(msg3)
    assert cache._last_sequence.get(asset_id) == 105
    assert cache._bids[asset_id][0.52] == 20.0
    assert cache._sequence_gaps == 4 # 105 - 100 - 1

def test_sequence_protection_price_change():
    cache = ClobMarketBookCache()
    asset_id = "0x123"
    
    # Seed initial state
    cache._apply_book({
        "event_type": "book",
        "asset_id": asset_id,
        "sequence": 100,
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": []
    })
    
    # 1. Valid price change
    msg1 = {
        "event_type": "price_change",
        "sequence": 101,
        "price_changes": [{
            "asset_id": asset_id,
            "price": "0.50",
            "size": "15",
            "side": "BUY"
        }]
    }
    cache._apply_price_change(msg1)
    assert cache._last_sequence.get(asset_id) == 101
    assert cache._bids[asset_id][0.50] == 15.0
    
    # 2. Stale price change
    msg2 = {
        "event_type": "price_change",
        "sequence": 101, # Same as last
        "price_changes": [{
            "asset_id": asset_id,
            "price": "0.50",
            "size": "20",
            "side": "BUY"
        }]
    }
    cache._apply_price_change(msg2)
    assert cache._last_sequence.get(asset_id) == 101
    assert cache._bids[asset_id][0.50] == 15.0 # Unchanged

def test_sequence_protection_best_bid_ask():
    cache = ClobMarketBookCache()
    asset_id = "0x123"
    
    # Seed initial state
    cache._apply_book({
        "event_type": "book",
        "asset_id": asset_id,
        "sequence": 100,
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.55", "size": "10"}]
    })
    
    # 1. Valid best_bid_ask
    msg1 = {
        "event_type": "best_bid_ask",
        "asset_id": asset_id,
        "sequence": 102,
        "best_bid": "0.51",
        "best_ask": "0.54"
    }
    cache._apply_best_bid_ask(msg1)
    assert cache._last_sequence.get(asset_id) == 102
    assert 0.51 in cache._bids[asset_id]
    
    # 2. Stale best_bid_ask
    msg2 = {
        "event_type": "best_bid_ask",
        "asset_id": asset_id,
        "sequence": 101,
        "best_bid": "0.52",
        "best_ask": "0.53"
    }
    cache._apply_best_bid_ask(msg2)
    assert cache._last_sequence.get(asset_id) == 102
    assert 0.52 not in cache._bids[asset_id]

def test_sequence_protection_multi_asset():
    cache = ClobMarketBookCache()
    a1 = "0x1"
    a2 = "0x2"
    
    cache._apply_book({"asset_id": a1, "sequence": 10, "bids": [], "asks": []})
    cache._apply_book({"asset_id": a2, "sequence": 20, "bids": [], "asks": []})
    
    assert cache._last_sequence[a1] == 10
    assert cache._last_sequence[a2] == 20
    
    # a1 update with seq 15 is valid
    cache._apply_book({"asset_id": a1, "sequence": 15, "bids": [], "asks": []})
    assert cache._last_sequence[a1] == 15
    
    # a2 update with seq 15 is stale
    cache._apply_book({"asset_id": a2, "sequence": 15, "bids": [], "asks": []})
    assert cache._last_sequence[a2] == 20
