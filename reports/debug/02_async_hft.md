# Detailed Audit Report: Async & HFT Behavior (Section 2 of docs/DEBUG_REFACTOR.md)

## 1. Findings: Blocking Operations
**Status: CRITICAL ISSUES FOUND**

*   **`time.sleep()` in Async Paths:**
    *   [`data/clob_user_ws.py:58`](data/clob_user_ws.py:58) (in tests) uses `time.sleep(0.05)`, which is acceptable for tests but should be avoided in production code.
    *   [`core/live_engine.py`](core/live_engine.py) contains several `await asyncio.sleep()` calls (e.g., lines 565, 1732, 1831, 2307). While these are non-blocking for the event loop, they introduce significant wall-clock latency in the execution path.
*   **Blocking I/O (Sync Requests/File Ops):**
    *   [`core/engine.py:73`](core/engine.py:73) and [`core/engine_rsi_exit.py:76`](core/engine_rsi_exit.py:76) perform synchronous `open().write()` operations in the hot path for debug logging. This is a major latency bottleneck.
    *   [`bot_main_loop.py:394`](bot_main_loop.py:394) calls `live_exec.fetch_usdc_balance()` synchronously during startup.
    *   [`core/live_engine.py`](core/live_engine.py) uses `asyncio.to_thread()` for many API calls (e.g., lines 163, 413, 566, 1012). While this prevents blocking the event loop, the overhead of thread creation and context switching is high for HFT.

## 2. Findings: Thread/Task Separation
**Status: PARTIALLY IMPLEMENTED**

*   **Market Data:** Handled by `FastExchangeProvider` in [`data/providers.py`](data/providers.py) and `ClobMarketBookCache` in [`data/clob_market_ws.py`](data/clob_market_ws.py). These run as independent `asyncio` tasks.
*   **Execution:** The main loop in [`bot_main_loop.py`](bot_main_loop.py) handles signal generation and execution sequentially. However, `LiveExecutionEngine` uses an internal `_event_worker` task for FSM transitions, creating a split between signal logic and state management.
*   **Logging:** `TradeJournal` in [`utils/trade_journal.py`](utils/trade_journal.py) implements an async queue-based writer, which is good. However, `HFTEngine` debug logging is still synchronous.

## 3. Findings: Event Queue Implementation
**Status: INCOMPLETE / INCONSISTENT**

*   `LiveExecutionEngine` has an `_event_queue` ([`core/live_engine.py:304`](core/live_engine.py:304)) used for `WsOrderEvent`, `RestResponseEvent`, and `TimerEvent`.
*   **Issue:** The main trading loop in [`bot_main_loop.py`](bot_main_loop.py) does **not** use a unified event queue. It polls price aggregators and caches directly. This can lead to "race-to-signal" issues where a price update is processed while an execution is still pending in a background thread.

## 4. Findings: FSM Audit (Order Lifecycle)
**Status: IMPLEMENTED BUT FRAGILE**

*   `OrderFSM` in [`core/live_engine.py:61`](core/live_engine.py:61) manages states: `PENDING`, `PARTIAL`, `FILLED`, `CANCELLED`, `FAILED`, `STALE`.
*   **Issue:** The FSM transition logic ([`core/live_engine.py:69`](core/live_engine.py:69)) allows transitions from any non-terminal state. There is no strict enforcement of the sequence (e.g., `PLACING` -> `PENDING` -> `FILLED`).
*   **Issue:** `OrderStatus.PLACING` is used in `_handle_ws_order` but not explicitly defined in the `OrderStatus` enum in `core/live_common.py` (needs verification).

---

## Improvement Plan (Step-by-Step)

### Phase 1: Eliminate Hot-Path Blocking (Immediate)
1.  **Refactor Debug Logging:** Move `DEBUG_LOG_PATH` writes in [`core/engine.py`](core/engine.py) and [`core/engine_rsi_exit.py`](core/engine_rsi_exit.py) to a non-blocking background queue (similar to `TradeJournal`).
2.  **Nanosecond Timestamps:** Replace `time.time()` with `time.time_ns()` for all latency-critical measurements to avoid float precision issues.

### Phase 2: Unified Event Loop
1.  **Implement Central Event Bus:** Create a unified `PriorityQueue` in `bot_main_loop.py`.
2.  **Convert Providers to Events:** `FastExchangeProvider` and `ClobMarketBookCache` should push events to the central bus instead of updating shared state directly.
3.  **Sequential Processing:** The main loop should `await event_queue.get()` and process one event at a time to guarantee deterministic behavior.

### Phase 3: FSM Hardening
1.  **Strict Transitions:** Update `OrderFSM.transition` to validate `(old_state, new_state)` pairs against a valid transition matrix.
2.  **Atomic State Updates:** Ensure all state changes and PnL updates happen within the same task to prevent race conditions between WS fills and strategy logic.

### Phase 4: Latency Optimization
1.  **Remove `asyncio.to_thread`:** Replace synchronous `py_clob_client` calls with a truly async HTTP client (e.g., `httpx` or `aiohttp`) to eliminate thread overhead.
2.  **Pre-calculate Signatures:** Move EIP-712 signing out of the execution path if possible (pre-sign common order types).
