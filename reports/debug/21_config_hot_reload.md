# 📋 CONFIG & HOT RELOAD AUDIT REPORT (Section 21)

## Executive Summary
The system loads configuration from layered env files but lacks versioning, hashing, and true hot-reload capabilities for most parameters.

**Findings:**
- ❌ **Config Versioning**: No hash or version metadata saved at startup.
- ⚠️ **A/B Testing**: Parallel execution exists, but no capital splitting or isolated PnL tracking.
- ⚠️ **Exponential Backoff**: Implemented but caps at 30s (spec requires 60s).

---

## 🛠️ Improvement Plan
1. **Config Hashing**: Implement SHA256 fingerprinting of effective configuration at startup.
2. **Capital Splitting**: Extend `PnLTracker` to support fractional balance allocation per strategy.
3. **Backoff Alignment**: Update reconnection sequence to exactly match `[1, 2, 4, 8, 16, 32, 60]`.
4. **Hot Reload**: Add a file watcher to `runtime.env` to update non-structural parameters without restart.
