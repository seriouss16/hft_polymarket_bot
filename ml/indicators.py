"""Technical indicators for fast-price series (Coinbase-first history)."""

from __future__ import annotations

import math

import numpy as np


def compute_adx_last(prices, period: int = 14) -> float:
    """Wilder ADX (0–100) at the last bar of ``prices`` (close-only mid series).

    Callers should pass **actual feed ticks** (e.g. last ~60 Coinbase prices for ~12–15s),
    not a shorter RSI window — see ``HFTEngine`` ``px_adx`` vs ``px``.

    Synthetic OHLC uses a **rolling** high/low over the last ``period`` closes (bar ``i``:
    high = max(c[i-period+1:i+1]), low = min(...)).  Pairwise adjacent highs/lows inflate
    +DM/−DM one-sided on long monotonic trends and peg ADX at ~100; rolling range fixes
    that for close-only BTC feeds.
    """
    arr = np.asarray(prices, dtype=np.float64)
    n = int(arr.size)
    if n < 2 * period + 1:
        return float("nan")
    c = arr
    high = np.empty(n)
    low = np.empty(n)
    for i in range(n):
        j0 = max(0, i - period + 1)
        seg = c[j0 : i + 1]
        high[i] = float(np.max(seg))
        low[i] = float(np.min(seg))
    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        tr_ = max(
            high[i] - low[i],
            abs(high[i] - c[i - 1]),
            abs(low[i] - c[i - 1]),
        )
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        pdm = float(up_move) if up_move > down_move and up_move > 0 else 0.0
        mdm = float(down_move) if down_move > up_move and down_move > 0 else 0.0
        tr.append(tr_)
        plus_dm.append(pdm)
        minus_dm.append(mdm)
    m = len(tr)

    def _wilder_smooth(x: list[float]) -> list[float]:
        """Wilder RMA: first value = mean(x[:period]); then (prev*(n-1)+x)/n."""
        out = [0.0] * m
        out[period - 1] = float(sum(x[:period])) / float(period)
        n = float(period)
        for i in range(period, m):
            out[i] = (out[i - 1] * (n - 1.0) + x[i]) / n
        return out

    atr = _wilder_smooth(tr)
    pdm_s = _wilder_smooth(plus_dm)
    mdm_s = _wilder_smooth(minus_dm)
    eps = 1e-12
    dx = [0.0] * m
    for i in range(period - 1, m):
        ai = atr[i]
        if ai <= eps:
            continue
        pdi = 100.0 * pdm_s[i] / ai
        mdi = 100.0 * mdm_s[i] / ai
        denom = pdi + mdi
        if denom > eps:
            dx[i] = 100.0 * abs(pdi - mdi) / denom
    adx_s = _wilder_smooth(dx)
    val = float(adx_s[-1])
    if not math.isfinite(val):
        return float("nan")
    return float(np.clip(val, 0.0, 100.0))


def compute_rsi(prices, period=14):
    """Return Wilder-style RSI for the last point of prices."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100. / (1. + rs)

    for i in range(period, len(prices)):
        delta = deltas[i - 1]
        if delta > 0:
            upval = delta
            downval = 0.
        else:
            upval = 0.
            downval = -delta

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down
        rsi[i] = 100. - 100. / (1. + rs)
    return rsi[-1]


def dynamic_rsi_bands(
    prices,
    base_upper=70.0,
    base_lower=30.0,
    k=0.08,
    vol_window=50,
):
    """Widen overbought/oversold RSI exit bands when relative volatility rises."""
    if prices is None or len(prices) < 15:
        return float(base_upper), float(base_lower)
    arr = np.array(prices[-vol_window:], dtype=np.float64)
    mean = float(np.mean(arr)) + 1e-9
    vol_rel = float(np.std(arr) / mean)
    shift = min(12.0, k * vol_rel * 500.0)
    upper = min(95.0, base_upper + shift)
    lower = max(5.0, base_lower - shift)
    return upper, lower


def ema_series(prices, period: int) -> np.ndarray:
    """Return exponential moving average series (same length as prices)."""
    arr = np.asarray(prices, dtype=np.float64)
    n = len(arr)
    out = np.empty(n, dtype=np.float64)
    if n == 0:
        return out
    k = 2.0 / (float(period) + 1.0)
    out[0] = float(arr[0])
    for i in range(1, n):
        out[i] = k * float(arr[i]) + (1.0 - k) * float(out[i - 1])
    return out


def compute_ema_last(prices, period: int) -> float:
    """Return the last EMA value for the price series."""
    arr = np.asarray(prices, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    if arr.size < 2:
        return float(arr[-1])
    return float(ema_series(arr, period)[-1])


def compute_macd_last(
    prices,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """Return (macd_line, signal_line, histogram) at the last bar.

    MACD line = EMA(fast) - EMA(slow) on the close series; signal = EMA of the
    MACD line; histogram = MACD - signal.  All in the same units as ``prices``.
    """
    arr = np.asarray(prices, dtype=np.float64)
    n = int(arr.size)
    if n < max(slow + signal, fast + 1, 3):
        return 0.0, 0.0, 0.0
    ema_f = ema_series(arr, fast)
    ema_s = ema_series(arr, slow)
    macd_line = ema_f - ema_s
    sig = ema_series(macd_line, signal)
    hist = macd_line - sig
    return float(macd_line[-1]), float(sig[-1]), float(hist[-1])


def compute_reaction_score(
    rsi: float,
    price: float,
    ema_fast: float,
    macd_hist: float,
    *,
    ma_rel_scale: float = 0.0008,
    macd_hist_scale: float = 25.0,
    w_rsi: float = 0.45,
    w_ma: float = 0.30,
    w_macd: float = 0.25,
) -> float:
    """Blend RSI, price-vs-EMA, and MACD histogram into one 0–100 oscillator.

    The output uses the same 0–100 scale as RSI so it can replace RSI in entry
    and exit bands without changing threshold env-vars.  ``ma_rel_scale`` is a
    typical fractional distance (price - EMA) / EMA at which the MA term is
    half-saturated toward 0 or 100.  ``macd_hist_scale`` scales histogram in
    price units (e.g. USD for BTC).
    """
    w_sum = float(w_rsi + w_ma + w_macd)
    if w_sum <= 0.0:
        return float(np.clip(rsi, 0.0, 100.0))
    em = max(abs(float(ema_fast)), 1e-9)
    rel = (float(price) - float(ema_fast)) / em
    ma_score = 50.0 + 50.0 * math.tanh(rel / max(float(ma_rel_scale), 1e-12))
    hs = max(abs(float(macd_hist_scale)), 1e-9)
    macd_score = 50.0 + 50.0 * math.tanh(float(macd_hist) / hs)
    out = (
        float(w_rsi) * float(np.clip(rsi, 0.0, 100.0))
        + float(w_ma) * float(np.clip(ma_score, 0.0, 100.0))
        + float(w_macd) * float(np.clip(macd_score, 0.0, 100.0))
    ) / w_sum
    return float(np.clip(out, 0.0, 100.0))