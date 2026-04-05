## 📊 HFT Performance Review: Section 13 - Profiling

## Executive Summary
Analyzed critical paths: **signal → order** and **order → fill**. Identified several latency bottlenecks in the hot path costing **10-100ms per tick**.

**Critical Findings:**
1. 🔴 **Blocking Calls**: Synchronous balance fetches in the critical path (20-1200ms).
2. 🟡 **Signing Latency**: EIP-712 signing blocks before network request (5-10ms).
3. 🟡 **Snapshot Overhead**: `heapq` sorting on every orderbook snapshot (2-10ms).
4. 🟡 **Strategy Hub**: Task creation overhead on every tick (1-3ms).

---

## 🛠️ Improvement Plan
1. **Phase 1: Eliminate Blocking Calls**: Make balance cache mandatory and pre-fetch in background.
2. **Phase 2: Optimize Order Placement**: Parallelize signing and HTTP preparation.
3. **Phase 3: Event Processing**: Implement parallel FSM processing for independent orders.
4. **Phase 4: Strategy Hub**: Reuse tasks across ticks instead of recreating them.
