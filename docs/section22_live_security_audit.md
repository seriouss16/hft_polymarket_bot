# Section 22 Audit: Live Security — Findings & Improvement Plan

**Auditor:** hft-devops
**Date:** 2026-04-05
**Scope:** docs/DEBUG_REFACTOR.md Section 22 (Безопасность в LIVE)

---

## 1. Pre-Send Order Validation (Section 22.1)

### Requirement
Before sending an order to Polymarket CLOB, validate:
- size > 0 and ≤ max
- price > 0
- order_type is valid
- no NaN / Infinity

### Findings

| Check | Status | Location | Details |
|-------|--------|----------|---------|
| size > 0 | PARTIAL | [`live_engine.py:1207`](core/live_engine.py:1207) | `_place_limit_raw` receives size but never validates it before creating `OrderArgs` |
| size ≤ max | PARTIAL | [`engine_sizing.py:103`](core/engine_sizing.py:103) | `calc_dynamic_amount` caps at `dynamic_amount_max_usd` but no NaN guard |
| price > 0 | PARTIAL | [`live_engine.py:1231`](core/live_engine.py:1231) | Price passed directly to `OrderArgs` without validation |
| order_type valid | OK | [`live_common.py:336`](core/live_common.py:336) | `OrderType.GTC` is hardcoded constant |
| NaN check | **MISSING** | All order paths | No `math.isfinite()` checks on size, price, or any numeric order param |
| Infinity check | **MISSING** | All order paths | No guard against `float('inf')` values |

### Critical Gaps

1. **No NaN/Infinity validation** — If `best_ask` or `best_bid` returns NaN from a stale WebSocket, the bot will attempt to place an order with NaN price. The CLOB will reject it, but this wastes time and creates noise.

2. **No pre-flight validation function** — Order parameters flow through multiple layers (`engine.py` → `executor.py` → `live_engine.py`) without a single validation gate.

3. **Implicit checks only** — [`executor.py:366`](core/executor.py:366) has `exec_price > 0` check for share calculation, but this is in the SIM path, not the live path.

### Risk Level: **HIGH**
A NaN or Infinity value in price or size could cause:
- CLOB API errors (best case)
- Unexpected order sizes (worst case, if CLOB interprets NaN as 0)
- Unhandled exceptions in the event loop

---

## 2. API Key Rotation (Section 22.2)

### Requirement
If an API key is compromised:
- Save current positions
- Disable old key (blacklist)
- Switch to backup key
- Continue without restart

### Findings

| Capability | Status | Details |
|------------|--------|---------|
| Key derivation at startup | OK | [`live_engine.py:326`](core/live_engine.py:326) — `create_or_derive_api_creds()` |
| Hot-swap without restart | **MISSING** | No mechanism to refresh credentials at runtime |
| Backup key support | **MISSING** | No secondary key configuration |
| Key blacklist | **MISSING** | No mechanism to revoke/rotate |
| Position persistence | PARTIAL | Positions tracked in-memory only; no DB/file persistence |

### Critical Gaps

1. **Credentials derived once at `__init__`** — The `_api_creds` object is set at line 328 and never refreshed. If Polymarket rotates the API key (e.g., user regenerates on the website), the bot will continue using stale credentials until restart.

2. **No `reload_credentials()` method** — The `LiveExecutionEngine` has no method to re-derive or update API credentials at runtime.

3. **No backup key configuration** — Environment variables support only one `PRIVATE_KEY` and one `FUNDER`. No `PRIVATE_KEY_BACKUP` or similar.

4. **Comment acknowledges the problem** — Line 325: "Explicit API keys in env may be stale (rotated/re-generated on Polymarket)."

### Risk Level: **CRITICAL**
If an API key is compromised or rotated:
- Bot must be manually restarted (downtime = missed trades + potential orphaned positions)
- No graceful degradation
- Positions are in-memory only — restart loses state

---

## 3. Audit Logging (Section 22.3)

### Requirement
For every order attempt (even rejected), log structured JSON:
```json
{
  "timestamp": "...",
  "action": "ORDER_SEND",
  "market": "BTC-25MAR",
  "side": "BUY",
  "size": 10,
  "price": 65000.50,
  "result": "SUCCESS",
  "server_order_id": "polymarket_123456"
}
```

### Findings

| Requirement | Status | Details |
|-------------|--------|---------|
| Structured JSON logging | **MISSING** | All logging uses `logging.info/warning/error` with format strings |
| Every order attempt logged | PARTIAL | Successful orders logged; rejected orders logged inconsistently |
| Rejected orders logged | PARTIAL | Some rejections logged (e.g., slippage abort), but not all |
| Unique trade_id | **MISSING** | No UUID per order/trade |
| server_order_id captured | PARTIAL | Order ID captured on success, not on failure |
| Timestamp | PARTIAL | `asctime` in log format, but not ISO 8601 in structured form |

### Current Logging Coverage

| Event | Logged? | Format | Location |
|-------|---------|--------|----------|
| Order placed (success) | Yes | Format string | [`live_engine.py:1252`](core/live_engine.py:1252) |
| Order placed (failure) | Yes | Format string | [`live_engine.py:1258`](core/live_engine.py:1258) |
| Order filled | Yes | Format string | Via `live_open`/`live_close` |
| Order cancelled | Yes | Format string | [`live_engine.py:979`](core/live_engine.py:979) |
| Order stale/repriced | Yes | Format string | [`live_engine.py:157`](core/live_engine.py:157) |
| BUY rejected (slippage) | Yes | Format string | [`live_engine.py:172`](core/live_engine.py:172) |
| BUY rejected (ask cap) | Yes | Counter only | `_entry_stats["skip_ask_cap"]` |
| BUY rejected (spread) | Yes | Counter only | `_entry_stats["skip_spread"]` |
| BUY rejected (signal) | Yes | Counter only | `_entry_stats["skip_signal"]` |
| Emergency exit | Yes | Format string | [`live_engine.py:1364`](core/live_engine.py:1364) |

### Risk Level: **MEDIUM**
- No machine-parseable audit trail
- Difficult to reconstruct order lifecycle for post-mortem
- Rejected orders tracked as counters, not individual events

---

## 4. Secret Masking in Logs (Section 22.4)

### Requirement
Never log:
- Full API keys (only last 4 chars)
- Wallet addresses (only last 4 chars)
- Sensitive data in stdout

### Findings

| Secret Type | Masked? | Details |
|-------------|---------|---------|
| PRIVATE_KEY in config log | YES | [`bot_config_log.py:78`](bot_config_log.py:78) — `_format_config_value` returns `<redacted>` |
| API_KEY in config log | YES | Same mechanism |
| SECRET/PASSWORD in config log | YES | Same mechanism |
| API key in live engine log | **NO** | [`live_engine.py:330`](core/live_engine.py:330) — logs `derived.api_key` with `key=%.8s...` (exposes first 8 chars!) |
| Funder address in logs | **NO** | Funder address passed to `ClobClient` but not systematically masked |
| Wallet addresses | **NO** | No masking function for addresses |

### Critical Issues

1. **API key partially exposed** — Line 330: `logging.info("[LIVE] ClobClient credentials derived from private key (key=%.8s...).", derived.api_key)` — This logs the first 8 characters of the API key. While not the full key, this is still sensitive information that should be masked to last 4 chars only.

2. **No address masking utility** — No function like `mask_address(addr)` exists. Wallet addresses and funder addresses may appear in logs unmasked.

3. **Config logging is good** — The `_is_sensitive_config_key()` and `_format_config_value()` functions in [`bot_config_log.py`](bot_config_log.py:62) are well-implemented for startup config logging.

### Risk Level: **MEDIUM**
- First 8 chars of API key exposed in startup logs
- No systematic address masking
- Config file logging is properly secured

---

## 5. Improvement Plan

### Phase 1: Pre-Send Validation (Priority: HIGH)

**File:** `core/live_engine.py`
**New function:** `_validate_order_params(side, price, size)`

```python
import math

def _validate_order_params(side: str, price: float, size: float) -> tuple[bool, str]:
    """Validate order parameters before sending to CLOB.
    
    Returns (is_valid, error_message).
    """
    if not math.isfinite(price):
        return False, f"price is not finite: {price}"
    if not math.isfinite(size):
        return False, f"size is not finite: {size}"
    if price <= 0.0:
        return False, f"price must be > 0, got {price}"
    if size <= 0.0:
        return False, f"size must be > 0, got {size}"
    if price > 1.0:
        return False, f"price must be <= 1.0 for Polymarket, got {price}"
    if side not in ("BUY", "SELL"):
        return False, f"invalid side: {side}"
    return True, ""
```

**Integration points:**
- Call in [`_place_limit_raw()`](core/live_engine.py:1207) before creating `OrderArgs`
- Call in [`_place_fak_sell()`](core/live_engine.py:1121) before market order
- Call in [`_fak_sell()`](core/live_engine.py:1261) wrapper
- Call in [`emergency_exit()`](core/live_engine.py:1474) before aggressive orders

### Phase 2: API Key Hot-Swap (Priority: CRITICAL)

**File:** `core/live_engine.py`
**New method:** `LiveExecutionEngine.reload_api_credentials()`

```python
def reload_api_credentials(self) -> bool:
    """Re-derive API credentials from private key without restart.
    
    Returns True on success, False on failure.
    Logs the result with masked key info.
    """
    if self.test_mode or self.client is None:
        return False
    try:
        derived = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(derived)
        self._api_creds = derived
        logging.info(
            "[LIVE] API credentials reloaded (key=...%s).",
            derived.api_key[-4:],
        )
        return True
    except Exception as exc:
        logging.error("[LIVE] API credential reload failed: %s", exc)
        return False
```

**Additional changes:**
- Add `POLY_API_KEY_BACKUP` and `POLY_API_SECRET_BACKUP` env var support
- Add `reload_api_credentials_from_backup()` method
- Add file watcher or signal handler for hot-reload trigger (SIGHUP)

### Phase 3: Structured Audit Logging (Priority: MEDIUM)

**New file:** `utils/audit_logger.py`

```python
import json
import logging
import time
import uuid
from typing import Any

_audit_logger = logging.getLogger("hft.audit")
_audit_logger.setLevel(logging.INFO)

def audit_log(action: str, **kwargs: Any) -> str:
    """Log a structured audit event. Returns trade_id."""
    trade_id = kwargs.pop("trade_id", str(uuid.uuid4()))
    record = {
        "trade_id": trade_id,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
        "action": action,
        **kwargs,
    }
    _audit_logger.info(json.dumps(record))
    return trade_id
```

**Integration points:**
- Call `audit_log("ORDER_SEND", ...)` before every order placement
- Call `audit_log("ORDER_REJECT", ...)` for every rejected order
- Call `audit_log("ORDER_FILL", ...)` on fill confirmation
- Call `audit_log("ORDER_CANCEL", ...)` on cancellation
- Call `audit_log("ORDER_STALE", ...)` on stale detection

### Phase 4: Secret Masking Fix (Priority: MEDIUM)

**File:** `core/live_engine.py` line 330
**Change:** Replace `key=%.8s...` with `key=...%s`

```python
# Before:
logging.info("[LIVE] ClobClient credentials derived from private key (key=%.8s...).", derived.api_key)

# After:
logging.info("[LIVE] ClobClient credentials derived from private key (key=...%s).", derived.api_key[-4:])
```

**New utility:** `utils/secrets_mask.py`

```python
def mask_api_key(key: str, visible: int = 4) -> str:
    """Mask API key, showing only last N characters."""
    if not key or len(key) <= visible:
        return "<redacted>"
    return f"...{key[-visible:]}"

def mask_address(addr: str, visible: int = 4) -> str:
    """Mask wallet/funder address, showing only last N characters."""
    if not addr or len(addr) <= visible:
        return "<redacted>"
    return f"...{addr[-visible:]}"
```

---

## Summary of Findings

| Section | Status | Risk | Effort |
|---------|--------|------|--------|
| 22.1 Pre-send validation | PARTIAL | HIGH | Low (1-2 hours) |
| 22.2 API key rotation | MISSING | CRITICAL | Medium (4-6 hours) |
| 22.3 Audit logging | PARTIAL | MEDIUM | Medium (3-4 hours) |
| 22.4 Secret masking | PARTIAL | MEDIUM | Low (30 min) |

## Recommended Priority Order

1. **Phase 4** (Secret masking fix) — Quick win, 30 min
2. **Phase 1** (Pre-send validation) — Safety critical, 1-2 hours
3. **Phase 3** (Audit logging) — Observability, 3-4 hours
4. **Phase 2** (API key rotation) — Complex, 4-6 hours

## Handover

**Handover to: hft-code** — This plan contains specific code changes, file locations, and implementation details ready for execution.
