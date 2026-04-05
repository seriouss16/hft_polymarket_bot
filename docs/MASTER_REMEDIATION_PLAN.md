# 🚀 MASTER REMEDIATION PLAN: HFT BOT REFACTORING

This plan is based on the 25 audit reports located in `reports/debug/`.

## 📌 WORKFLOW PROTOCOL
- **Main Branch**: `debug_refactor`
- **Critical Branch**: `debug_refactor_critical`
- **Non-Critical Branch**: `debug_refactor_non_critical`
- **Execution**: `uv run python ...`
- **Verification**: `pytest`, `pylint`, `flake8`, `mypy`, `vulture`, `pip-audit` after each fix.
- **Testing Policy**: Existing tests must pass. **New tests will be added for every fix** to verify correctness and prevent regressions. The total number of tests will increase as needed throughout the refactoring process.

---

## 🔴 PHASE 1: CRITICAL SAFETY & STABILITY
**Goal**: Eliminate blockers for live trading.
**Branch**: `debug_refactor_critical`

### 1.1 Security: Secret Management & Validation
- **Problem**: `PRIVATE_KEY` in plaintext `.env`; no NaN/Infinity checks.
- **Action**: Move key to secure storage; implement `_validate_order_params()`.
- **Files**: `.env`, `core/live_engine.py`, `utils/env_config.py`.
- **Report**: `22_live_security.md`

### 1.2 Safety: Kill-Switch & Anti-Doubling
- **Problem**: No emergency stop; risk of duplicate positions.
- **Action**: Implement FastAPI `/kill` endpoint; add `can_enter_position` check.
- **Files**: `bot_main_loop.py`, `core/live_engine.py`, `core/kill_switch_server.py` (new).
- **Report**: `20_live_safety_gates.md`, `14_risk_layer.md`

### 1.3 Stability: Exception Handling
- **Problem**: Silent crashes in background tasks.
- **Action**: Wrap all coroutines in `safe_task` with full traceback logging.
- **Files**: `bot_main_loop.py`, `core/live_engine.py`, `data/clob_market_ws.py`.
- **Report**: `17_error_handling.md`

### 1.4 Latency: Blocking I/O Removal
- **Problem**: Sync file writes and HTTP calls in hot path.
- **Action**: Move logging to background queue; make balance cache mandatory.
- **Files**: `core/engine.py`, `bot_main_loop.py`, `data/balance_cache.py`.
- **Report**: `02_async_hft.md`, `13_profiling.md`

### 1.5 Data Integrity: Sequence Protection
- **Problem**: Market data lacks out-of-order protection.
- **Action**: Implement sequence tracking for `ClobMarketBookCache`.
- **Files**: `data/clob_market_ws.py`.
- **Report**: `04_orderbook_freshness.md`

---

## 🟡 PHASE 2: NON-CRITICAL IMPROVEMENTS
**Goal**: Code quality, observability, and optimization.
**Branch**: `debug_refactor_non_critical`

### 2.1 Code Cleanup
- **Action**: Remove 27 unused attributes and 7 dead methods.
- **Report**: `11_code_cleanup.md`

### 2.2 Style & Documentation
- **Action**: PEP 8 (line length, whitespace) and PEP 257 (docstrings).
- **Report**: `12_style_documentation.md`

### 2.3 Metrics & Tracing
- **Action**: Implement Sharpe Ratio and nanosecond lifecycle tracing.
- **Report**: `18_metrics_tracing.md`

### 2.4 Config Versioning
- **Action**: Implement SHA256 config hashing and version metadata.
- **Report**: `21_config_hot_reload.md`

### 2.5 Dependency Pinning
- **Action**: Exact version pinning in `pyproject.toml`.
- **Report**: `24_dependencies_security.md`

### 2.6 Simulation Enhancements
- **Action**: Implement lag injection framework for stress testing.
- **Report**: `15_simulation.md`

---

## ✅ FINAL VERIFICATION
- [ ] All tests pass (including newly added ones).
- [ ] Static analysis (pylint/flake8/mypy) clean.
- [ ] No dead code (vulture).
- [ ] No vulnerabilities (pip-audit).
- [ ] Performance targets met (Signal->Fill < 200ms p99).
