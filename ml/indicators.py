"""Technical indicators for fast-price series (Coinbase-first history)."""

import numpy as np


def compute_rsi(prices, period=14):
    """Return Wilder-style RSI for the last point of prices."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0: return 100.0
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


def compute_ma(prices, period):
    """Return simple moving average at the series end."""
    if len(prices) < period:
        return prices[-1]
    return np.mean(prices[-period:])