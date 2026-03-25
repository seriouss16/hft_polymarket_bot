"""Resolve Polymarket market slugs to CLOB token ids via the Gamma API."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import requests


def _parse_json_field(val):
    """Decode a JSON string field from Gamma payloads into a Python value."""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return val


def normalize_clob_token_ids(raw) -> list[str]:
    """Return a list of CLOB token id strings; tolerate Gamma quirks and bad types."""
    if raw is None:
        return []
    parsed = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return [s]
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed if x is not None]
    if isinstance(parsed, bool):
        return []
    if isinstance(parsed, int):
        return [str(parsed)]
    if isinstance(parsed, float):
        if not parsed.is_integer():
            return []
        return [str(int(parsed))]
    if isinstance(parsed, str):
        return [parsed]
    return []


def _outcome_side_label(name: str) -> str | None:
    """Map Gamma outcome label to UP or DOWN for up/down markets."""
    low = str(name).strip().lower()
    if low.startswith("up"):
        return "UP"
    if low.startswith("down"):
        return "DOWN"
    return None


def _parse_float_list(field, n: int) -> list[float]:
    """Parse per-outcome numeric values that Gamma may return as list, JSON string, or scalar."""
    if n <= 0:
        return []

    def _numpy_scalar_to_float(val):
        """Coerce numpy or Python scalars to float; return None if not applicable."""
        if isinstance(val, (int, float)):
            return float(val)
        if hasattr(val, "item") and callable(val.item):
            try:
                item = val.item()
            except Exception:
                return None
            if isinstance(item, (int, float)):
                return float(item)
        return None

    def _fill_from_scalar(val: float, out: list[float]) -> list[float]:
        """Write a single scalar into the first outcome slot."""
        out[0] = float(val)
        return out

    raw = _parse_json_field(field)
    if raw is None:
        raw = field
    out = [0.0] * n

    v0 = _numpy_scalar_to_float(raw)
    if v0 is not None:
        return _fill_from_scalar(v0, out)

    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return out
        try:
            return _fill_from_scalar(float(s), out)
        except ValueError:
            parsed = _parse_json_field(s)
            if parsed is None:
                return out
            raw = parsed
            v1 = _numpy_scalar_to_float(raw)
            if v1 is not None:
                return _fill_from_scalar(v1, out)

    if isinstance(raw, (list, tuple)):
        for i in range(min(n, len(raw))):
            if raw[i] is None:
                continue
            try:
                out[i] = float(raw[i])
            except (TypeError, ValueError):
                out[i] = 0.0
    return out


def _fill_quote_from_mid(mid: float, half_spread: float = 0.01) -> tuple[float, float]:
    """Return synthetic bid/ask around mid when the book leg is missing."""
    if not (0.0 < mid < 1.0):
        return 0.0, 0.0
    b = max(0.01, min(0.99, mid - half_spread))
    a = max(0.01, min(0.99, mid + half_spread))
    if a <= b:
        a = min(0.99, b + 0.02)
    return b, a


class MarketSelector:
    """Pick active 5m up/down slots and map outcomes to CLOB token ids."""

    def __init__(self, asset="btc", interval=300):
        self.asset = asset
        self.interval = interval

    def get_current_slot_timestamp(self):
        """Return Unix timestamp at the start of the current slot window."""
        from datetime import datetime, timezone

        now = int(datetime.now(timezone.utc).timestamp())
        return (now // self.interval) * self.interval

    def seconds_to_slot_end(self) -> float:
        """Return approximate seconds until the current slot window ends."""
        slot_start = float(self.get_current_slot_timestamp())
        return max(0.0, slot_start + float(self.interval) - time.time())

    def format_slug(self, timestamp):
        """Return Gamma slug for this asset and slot start timestamp."""
        return f"{self.asset.lower()}-updown-5m-{timestamp}"

    async def _fetch_gamma_json(self, url: str, timeout: float = 5.0):
        """Return parsed JSON for a Gamma API URL without blocking the event loop."""

        def _do_request():
            resp = requests.get(url, timeout=timeout)
            return resp.json()

        return await asyncio.to_thread(_do_request)

    async def fetch_up_down_token_ids(self, slug):
        """Return (up_token_id, down_token_id, question) for a market slug."""
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        try:
            data = await self._fetch_gamma_json(url, timeout=5.0)
            if not data:
                return None, None, slug
            m = data[0]
            question = m.get("question", slug)
            raw_tids = m.get("clobTokenIds")
            tids = normalize_clob_token_ids(raw_tids)
            if len(tids) < 1:
                return None, None, question
            outcomes_raw = _parse_json_field(m.get("outcomes")) or []
            if isinstance(outcomes_raw, (list, tuple)):
                outcomes = [str(x) for x in outcomes_raw]
            else:
                outcomes = []
            up_id = None
            down_id = None
            for i, name in enumerate(outcomes):
                if i >= len(tids):
                    break
                label = _outcome_side_label(str(name))
                if label == "UP":
                    up_id = str(tids[i])
                elif label == "DOWN":
                    down_id = str(tids[i])
            if up_id is None and len(tids) >= 1:
                up_id = str(tids[0])
            if down_id is None and len(tids) >= 2:
                down_id = str(tids[1])
            return up_id, down_id, question
        except Exception as e:
            logging.error("Selector error for slug=%s: %s", slug, e)
            return None, None, slug

    async def fetch_token_id(self, slug):
        """Return the UP outcome token id and question (backward compatible)."""
        up_id, _, question = await self.fetch_up_down_token_ids(slug)
        return up_id, question

    async def fetch_up_down_quotes(self, slug: str, up_id: str | None, down_id: str | None) -> dict:
        """Return UP/DOWN bid/ask quotes from Gamma payload for a market slug."""
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        try:
            data = await self._fetch_gamma_json(url, timeout=5.0)
            if not data:
                return {}
            market = data[0]
            token_ids = normalize_clob_token_ids(market.get("clobTokenIds"))
            n = len(token_ids)
            if n == 0:
                return {}
            outcomes_raw = _parse_json_field(market.get("outcomes")) or []
            if isinstance(outcomes_raw, (list, tuple)):
                outcomes = [str(x) for x in outcomes_raw]
            else:
                outcomes = []
            mids = _parse_float_list(market.get("outcomePrices"), n)
            bids = _parse_float_list(market.get("bestBid"), n)
            asks = _parse_float_list(market.get("bestAsk"), n)

            quote_map: dict[str, dict[str, float]] = {}
            for i, tid in enumerate(token_ids):
                tid_s = str(tid)
                mid = mids[i] if i < len(mids) else 0.0
                b = bids[i] if i < len(bids) else 0.0
                a = asks[i] if i < len(asks) else 0.0
                if not (0.0 < b < 1.0 and 0.0 < a <= 1.0 and a > b):
                    if 0.0 < mid < 1.0:
                        b, a = _fill_quote_from_mid(mid, half_spread=0.01)
                    else:
                        b, a = 0.0, 0.0
                quote_map[tid_s] = {"bid": b, "ask": a}

            if n == 2:
                t0, t1 = str(token_ids[0]), str(token_ids[1])
                q0 = quote_map.get(t0) or {}
                q1 = quote_map.get(t1) or {}
                b0, a0 = float(q0.get("bid", 0.0)), float(q0.get("ask", 0.0))
                b1, a1 = float(q1.get("bid", 0.0)), float(q1.get("ask", 0.0))
                leg0_ok = 0.0 < b0 < a0 <= 1.0
                leg1_ok = 0.0 < b1 < a1 <= 1.0
                if leg0_ok and not leg1_ok:
                    quote_map[t1] = {
                        "bid": max(0.01, min(0.99, 1.0 - a0)),
                        "ask": max(0.01, min(0.99, 1.0 - b0)),
                    }
                elif leg1_ok and not leg0_ok:
                    quote_map[t0] = {
                        "bid": max(0.01, min(0.99, 1.0 - a1)),
                        "ask": max(0.01, min(0.99, 1.0 - b1)),
                    }

            up_quote = quote_map.get(str(up_id)) if up_id else None
            down_quote = quote_map.get(str(down_id)) if down_id else None
            result: dict[str, float] = {}
            if up_quote:
                result["up_bid"] = float(up_quote.get("bid", 0.0))
                result["up_ask"] = float(up_quote.get("ask", 0.0))
            if down_quote:
                result["down_bid"] = float(down_quote.get("bid", 0.0))
                result["down_ask"] = float(down_quote.get("ask", 0.0))

            if "up_bid" not in result or "up_ask" not in result:
                for i, outcome_name in enumerate(outcomes):
                    if i >= len(token_ids):
                        break
                    if _outcome_side_label(str(outcome_name)) == "UP":
                        q = quote_map.get(str(token_ids[i])) or {}
                        result["up_bid"] = float(q.get("bid", 0.0))
                        result["up_ask"] = float(q.get("ask", 0.0))
                        break
            if "down_bid" not in result or "down_ask" not in result:
                for i, outcome_name in enumerate(outcomes):
                    if i >= len(token_ids):
                        break
                    if _outcome_side_label(str(outcome_name)) == "DOWN":
                        q = quote_map.get(str(token_ids[i])) or {}
                        result["down_bid"] = float(q.get("bid", 0.0))
                        result["down_ask"] = float(q.get("ask", 0.0))
                        break
            return result
        except Exception as e:
            logging.error("Selector quote fetch error for slug=%s: %s", slug, e)
            return {}
