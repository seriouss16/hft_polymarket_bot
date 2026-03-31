"""Trend tracking and dynamic edge thresholds for HFTEngine."""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from core.engine_price import price_array_for_rsi


def micro_trend_metrics(
    edge_window: Any,
    *,
    now: float,
    window_sec: float,
) -> dict[str, Any]:
    """Short-window edge dynamics vs oracle (poly mid): slope, convergence, ETA to cross.

    ``edge_window`` holds ``(timestamp, edge)`` with edge = fast - oracle. Slope is OLS
    on samples in ``[now - window_sec, now]`` (edge points per second). ``toward_target``
    is True when edge is moving toward zero (possible cross of the target line).
    """
    empty = {
        "micro_slope": None,
        "micro_strength": None,
        "toward_target": None,
        "cross_eta_sec": None,
    }
    if not edge_window or len(edge_window) < 2:
        return empty
    t_cut = float(now) - float(window_sec)
    pts = [(float(t), float(e)) for t, e in edge_window if t >= t_cut]
    if len(pts) < 2:
        tail = list(edge_window)[-min(8, len(edge_window)) :]
        pts = [(float(t), float(e)) for t, e in tail]
    if len(pts) < 2:
        return empty
    t_arr = np.array([p[0] for p in pts], dtype=np.float64)
    e_arr = np.array([p[1] for p in pts], dtype=np.float64)
    t_rel = t_arr - t_arr[0]
    n = int(t_rel.size)
    if n < 2:
        return empty
    sum_t = float(t_rel.sum())
    sum_e = float(e_arr.sum())
    sum_t2 = float((t_rel * t_rel).sum())
    sum_te = float((t_rel * e_arr).sum())
    denom = n * sum_t2 - sum_t * sum_t
    if abs(denom) < 1e-24:
        slope = 0.0
    else:
        slope = float((n * sum_te - sum_t * sum_e) / denom)
    edge_last = float(pts[-1][1])
    eps = 1e-9
    if abs(edge_last) <= eps:
        toward = True
    else:
        toward = bool((edge_last * slope) < -eps)
    eta: float | None = None
    if toward and abs(slope) > eps and abs(edge_last) > eps:
        eta = abs(edge_last) / abs(slope)
        eta = min(float(eta), 3600.0)
    return {
        "micro_slope": float(slope),
        "micro_strength": float(abs(slope)),
        "toward_target": bool(toward),
        "cross_eta_sec": float(eta) if eta is not None else None,
    }


def update_trend(eng: Any, fast_price, poly_mid):
    """Track crossing of target price and estimate trend speed/depth (mutates ``eng``)."""
    now = time.time()
    edge = fast_price - poly_mid
    eng.edge_window.append((now, edge))

    sign = 1 if edge > 0 else -1 if edge < 0 else 0
    crossed = sign != 0 and eng.last_edge_sign != 0 and sign != eng.last_edge_sign
    if crossed:
        eng.trend_since_ts = now
        eng.trend_depth = abs(edge)
        eng.trend_dir = "UP" if sign > 0 else "DOWN"
        logging.info("🔁 Trend cross: %s edge=%.2f", eng.trend_dir, edge)
    elif sign != 0:
        if eng.trend_since_ts == 0.0:
            eng.trend_since_ts = now
            eng.trend_dir = "UP" if sign > 0 else "DOWN"
        eng.trend_depth = max(eng.trend_depth, abs(edge))
    else:
        eng.trend_dir = "FLAT"

    if sign != 0:
        eng.last_edge_sign = sign

    speed = 0.0
    if len(eng.edge_window) >= 2:
        t0, e0 = eng.edge_window[-2]
        t1, e1 = eng.edge_window[-1]
        dt = max(t1 - t0, 1e-6)
        speed = (e1 - e0) / dt
    age = now - eng.trend_since_ts if eng.trend_since_ts else 0.0
    return edge, speed, eng.trend_depth, age, eng.trend_dir


def dynamic_edge_threshold(
    eng: Any,
    price_history,
    recent_pnl=0.0,
    latency_ms=0.0,
    extra_mult=1.0,
):
    """Return adaptive edge threshold in price units from recent volatility."""
    if not price_history or len(price_history) < 30:
        be, se = eng.buy_edge, abs(eng.sell_edge)
        return be * extra_mult, se * extra_mult
    arr = price_array_for_rsi(price_history, 50)
    vol = float(np.std(arr))
    pnl_penalty = 1.15 if recent_pnl < 0 else 1.0
    lo = 0.0 if eng.no_entry_guards else 2.0
    edge = max(lo, min(20.0, vol * 0.6 * pnl_penalty))
    edge *= float(extra_mult)
    return edge, edge
