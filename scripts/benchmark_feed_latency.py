#!/usr/bin/env python3
"""Compare how Polymarket RTDS oracle price tracks CEX — not just socket timing.

Same WSS as ``hft_bot`` (``providers.py``, ``poly_clob.py``). Reports:

1. **Signal staleness** — when Binance/Coinbase emit a tick, how old is the last Polymarket
   oracle update (ms). Large values = Poly signal is stale vs fresh CEX.

2. **Price gap (USD)** — ``Poly_mid − CEX_mid`` at Poly ticks and at CEX ticks (oracle vs spot).

3. **Catch-up delay** — after Binance mid moves by at least ``--move-threshold`` USD vs the
   previous tick, time (ms) until the **next** Polymarket oracle tick (proxy for how fast
   Poly “catches” the move).

Optional: ``--kraken``, ``--http-clob``. Raw inter-arrival stats kept as supplementary.

  uv run python hft_bot/scripts/benchmark_feed_latency.py --duration 30
  uv run python hft_bot/scripts/benchmark_feed_latency.py --duration 60 --move-threshold 10

Requires: websockets.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import math
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
import websockets
try:
    from tqdm.auto import tqdm
except Exception:  # pragma: no cover - optional dependency for UX only
    tqdm = None
try:
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover - optional dependency for plotting only
    plt = None

# --- Match hft_bot/data/providers.py + bot_main_loop ---


def binance_bookticker_uri(symbol: str = "BTC") -> str:
    stream_symbol = symbol.lower()
    if not stream_symbol.endswith("usdt"):
        stream_symbol = f"{stream_symbol}usdt"
    return f"wss://stream.binance.com:9443/stream?streams={stream_symbol}@bookTicker"


COINBASE_WS_BASE = "wss://ws-feed.exchange.coinbase.com"


def coinbase_ticker_product_id(symbol: str = "BTC-USD") -> str:
    product = symbol.upper()
    if "-" not in product:
        product = f"{product}-USD"
    return product


POLY_RTDS_URI = "wss://ws-live-data.polymarket.com"
KRAKEN_URI = "wss://ws.kraken.com"
CLOB_HTTPS = "https://clob.polymarket.com/"
DEFAULT_REPORT_DIR = "hft_bot/reports/banch_lag"
_WS_CONNECT_KWARGS = {"ping_interval": 10, "ping_timeout": 5}


@dataclass
class FeedSeries:
    """Monotonic (perf_counter, mid_usd) samples per feed."""

    name: str
    t_connect_start: float = 0.0
    t_first_msg: float | None = None
    events: list[tuple[float, float]] = field(default_factory=list)

    def record(self, t: float, mid: float) -> None:
        if self.t_first_msg is None:
            self.t_first_msg = t
        self.events.append((t, mid))

    def first_tick_ms(self) -> float | None:
        if self.t_first_msg is None:
            return None
        return (self.t_first_msg - self.t_connect_start) * 1000.0

    def inter_arrival_ms(self) -> list[float]:
        if len(self.events) < 2:
            return []
        return [(self.events[i][0] - self.events[i - 1][0]) * 1000.0 for i in range(1, len(self.events))]


def _gap_stats_ms(gaps: list[float]) -> tuple[float | None, float | None, float | None]:
    if not gaps:
        return None, None, None
    return min(gaps), statistics.fmean(gaps), max(gaps)


def _usd_stats(xs: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    if not xs:
        return None, None, None, None
    ax = [abs(v) for v in xs]
    return min(ax), statistics.fmean(ax), statistics.median(ax), max(ax)


def _fmt_usd_stats(label: str, xs: list[float]) -> str:
    if not xs:
        return f"{label}: no samples"
    lo, mean_abs, med, hi = _usd_stats(xs)
    signed_mean = statistics.fmean(xs)
    signed_med = statistics.median(xs)
    return (
        f"{label}: n={len(xs)}  mean signed = {signed_mean:+.2f} (median {signed_med:+.2f}) USD; "
        f"|gap| min/mean/median/max = {lo:.2f} / {mean_abs:.2f} / {med:.2f} / {hi:.2f} USD"
    )


def _fmt_ms_stats(label: str, xs: list[float]) -> str:
    if not xs:
        return f"{label}: no samples"
    lo, mean, med, hi = min(xs), statistics.fmean(xs), statistics.median(xs), max(xs)
    return f"{label}: n={len(xs)}  min/mean/median/max = {lo:.1f} / {mean:.1f} / {med:.1f} / {hi:.1f} ms"


def _fmt_triplet(
    lo: float | None, mid: float | None, hi: float | None, suffix: str = ""
) -> str:
    if lo is None:
        return "n/a"
    return f"{lo:.1f}{suffix} / {mid:.1f}{suffix} / {hi:.1f}{suffix}"


def _pearson_corr(xs: list[float], ys: list[float]) -> float | None:
    """Return Pearson correlation or None for degenerate vectors."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    x = xs[:n]
    y = ys[:n]
    mx = statistics.fmean(x)
    my = statistics.fmean(y)
    vx = [v - mx for v in x]
    vy = [v - my for v in y]
    sx = math.sqrt(sum(v * v for v in vx))
    sy = math.sqrt(sum(v * v for v in vy))
    if sx <= 1e-12 or sy <= 1e-12:
        return None
    return sum(a * b for a, b in zip(vx, vy)) / (sx * sy)


def _resample_last_per_second(events: list[tuple[float, float]]) -> list[float]:
    """1 Hz series via last-value-per-second (LOCF)."""
    if len(events) < 2:
        return []
    t0 = int(events[0][0])
    t1 = int(events[-1][0])
    if t1 <= t0:
        return []
    out: list[float] = []
    j = 0
    last_px = events[0][1]
    for sec in range(t0, t1 + 1):
        while j < len(events) and events[j][0] <= float(sec):
            last_px = events[j][1]
            j += 1
        out.append(float(last_px))
    return out


def _first_diff(xs: list[float]) -> list[float]:
    if len(xs) < 2:
        return []
    return [xs[i] - xs[i - 1] for i in range(1, len(xs))]


def _shift_forward(xs: list[float], lag_sec: int) -> list[float]:
    """Shift series forward in time: y[t] = x[t-lag]."""
    if lag_sec <= 0:
        return list(xs)
    out = [float("nan")] * len(xs)
    for i in range(lag_sec, len(xs)):
        out[i] = xs[i - lag_sec]
    return out


def _write_alignment_csv(
    path: str,
    *,
    bn_1hz: list[float],
    cb_1hz: list[float],
    poly_1hz: list[float],
    bn_lag_sec: int,
    cb_lag_sec: int,
) -> None:
    n = min(len(bn_1hz), len(cb_1hz), len(poly_1hz))
    if n <= 0:
        return
    poly_for_bn = _shift_forward(poly_1hz[:n], bn_lag_sec)
    poly_for_cb = _shift_forward(poly_1hz[:n], cb_lag_sec)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "sec_idx",
                "binance_mid",
                "coinbase_mid",
                "poly_mid",
                "poly_shifted_for_binance",
                "poly_shifted_for_coinbase",
            ]
        )
        for i in range(n):
            w.writerow(
                [
                    i,
                    f"{bn_1hz[i]:.8f}",
                    f"{cb_1hz[i]:.8f}",
                    f"{poly_1hz[i]:.8f}",
                    "" if math.isnan(poly_for_bn[i]) else f"{poly_for_bn[i]:.8f}",
                    "" if math.isnan(poly_for_cb[i]) else f"{poly_for_cb[i]:.8f}",
                ]
            )


def _sanitize_tag_part(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "."):
            out.append("_")
    cleaned = "".join(out).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned or "unknown"


def _geo_by_ip_tag() -> tuple[str, str]:
    """Return (country, city) from public IP geo lookup."""
    urls = (
        "https://ipapi.co/json/",
        "https://ipinfo.io/json",
    )
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "curl/8.5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            country = (
                data.get("country_name")
                or data.get("country")
                or data.get("countryCode")
                or ""
            )
            city = data.get("city") or data.get("region") or ""
            ctry = _sanitize_tag_part(str(country))
            cty = _sanitize_tag_part(str(city))
            if ctry != "unknown" or cty != "unknown":
                return ctry, cty
        except Exception:
            continue
    return "unknown", "unknown"


def _build_timestamped_report_paths(base_csv: str) -> tuple[str, str]:
    """Build CSV/PNG paths with timestamp + geo suffix."""
    base = Path(base_csv)
    stem = base.stem
    if not stem:
        stem = "feed_lag_alignment"
    ts = datetime.now(timezone.utc).strftime("%y%m%d_%H%M%S")
    country, city = _geo_by_ip_tag()
    tagged_stem = f"{stem}_{ts}_{country}_{city}"
    csv_path = str(base.with_name(f"{tagged_stem}.csv"))
    png_path = str(base.with_name(f"{tagged_stem}.png"))
    return csv_path, png_path


def _resolve_export_base_path(export_csv_arg: str | None) -> str:
    """Resolve export destination; supports file path or directory path.

    If empty, defaults to DEFAULT_REPORT_DIR/feed_lag_alignment.csv.
    If path ends with '/' or has no suffix, treat it as a directory.
    """
    raw = (export_csv_arg or "").strip()
    if not raw:
        return str(Path(DEFAULT_REPORT_DIR) / "feed_lag_alignment.csv")
    p = Path(raw)
    if raw.endswith("/") or p.suffix == "":
        return str(p / "feed_lag_alignment.csv")
    return str(p)


def _plot_alignment_png_from_arrays(
    out_png: str,
    *,
    bn_1hz: list[float],
    cb_1hz: list[float],
    poly_1hz: list[float],
    bn_lag_sec: int,
    cb_lag_sec: int,
) -> bool:
    """Build a two-panel curve plot; returns False when matplotlib missing."""
    if plt is None:
        return False
    n = min(len(bn_1hz), len(cb_1hz), len(poly_1hz))
    if n <= 0:
        return False
    xs = list(range(n))
    poly_for_bn = _shift_forward(poly_1hz[:n], bn_lag_sec)
    poly_for_cb = _shift_forward(poly_1hz[:n], cb_lag_sec)

    # Stacked linewidths so overlapping curves remain visible (wider = drawn first / underneath).
    lw_bn, lw_cb, lw_poly = 3.0, 2.3, 1.5
    z_bn, z_cb, z_poly = 1, 2, 3

    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    ax = axes[0]
    ax.plot(
        xs,
        bn_1hz[:n],
        label="Binance mid",
        color="tab:blue",
        linewidth=lw_bn,
        zorder=z_bn,
        solid_capstyle="round",
    )
    ax.plot(
        xs,
        cb_1hz[:n],
        label="Coinbase mid",
        color="tab:green",
        linewidth=lw_cb,
        zorder=z_cb,
        solid_capstyle="round",
    )
    ax.plot(
        xs,
        poly_1hz[:n],
        label="Poly RTDS",
        color="tab:red",
        linewidth=lw_poly,
        alpha=0.95,
        zorder=z_poly,
        solid_capstyle="round",
    )
    ax.set_title("Raw curves")
    ax.set_ylabel("Price (USD)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")

    ax2 = axes[1]
    ax2.plot(
        xs,
        bn_1hz[:n],
        label="Binance mid",
        color="tab:blue",
        linewidth=lw_bn,
        zorder=z_bn,
        solid_capstyle="round",
    )
    ax2.plot(
        xs,
        cb_1hz[:n],
        label="Coinbase mid",
        color="tab:green",
        linewidth=lw_cb,
        zorder=z_cb,
        solid_capstyle="round",
    )
    x_bn = [i for i, v in enumerate(poly_for_bn) if not math.isnan(v)]
    y_bn = [v for v in poly_for_bn if not math.isnan(v)]
    x_cb = [i for i, v in enumerate(poly_for_cb) if not math.isnan(v)]
    y_cb = [v for v in poly_for_cb if not math.isnan(v)]
    # Orange under purple: thicker so both stay visible when lags match.
    lw_poly_bn_shift, lw_poly_cb_shift = 2.5, 1.55
    z_poly_bn, z_poly_cb = 4, 5
    if x_bn:
        ax2.plot(
            x_bn,
            y_bn,
            label="Poly shifted for Binance",
            color="tab:orange",
            linewidth=lw_poly_bn_shift,
            alpha=0.95,
            zorder=z_poly_bn,
            solid_capstyle="round",
        )
    if x_cb:
        ax2.plot(
            x_cb,
            y_cb,
            label="Poly shifted for Coinbase",
            color="tab:purple",
            linewidth=lw_poly_cb_shift,
            alpha=0.95,
            zorder=z_poly_cb,
            solid_capstyle="round",
        )
    ax2.set_title("Aligned curves (using lag medians from benchmark)")
    ax2.set_xlabel("Second index")
    ax2.set_ylabel("Price (USD)")
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="best")

    fig.tight_layout()
    out_path = Path(out_png)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def _write_markdown_report(
    md_path: str,
    *,
    duration_sec: float,
    move_threshold_usd: float,
    lag_window_sec: int,
    lag_max_sec: int,
    st_bin: str,
    st_cb: str,
    gap_poly_bn: str,
    gap_poly_cb: str,
    gap_last_bn: str,
    gap_last_cb: str,
    catchup_bn: str,
    curve_bn: str,
    curve_cb: str,
    skew_bn: str,
    skew_cb: str,
    inter_bn: str,
    inter_cb: str,
    inter_poly: str,
    csv_path: str,
    png_path: str | None,
) -> None:
    out = Path(md_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    png_rel = None
    if png_path:
        try:
            png_rel = Path(png_path).relative_to(out.parent).as_posix()
        except Exception:
            png_rel = png_path
    csv_rel = None
    try:
        csv_rel = Path(csv_path).relative_to(out.parent).as_posix()
    except Exception:
        csv_rel = csv_path

    lines = [
        "# Feed Lag Report",
        "",
        f"- Duration: `{duration_sec:.1f}s`",
        f"- Catch-up threshold: `Binance move >= {move_threshold_usd:.1f} USD`",
        f"- Curve lag window/search: `{lag_window_sec}s`, `0..{lag_max_sec}s`",
        f"- CSV: `{csv_rel}`",
    ]
    if png_rel:
        lines.append(f"- Plot: `{png_rel}`")
    lines.extend(
        [
            "",
            "## Polymarket Signal Staleness",
            f"- {st_bin}",
            f"- {st_cb}",
            "",
            "## Price Gap",
            f"- {gap_poly_bn}",
            f"- {gap_poly_cb}",
            f"- {gap_last_bn}",
            f"- {gap_last_cb}",
            "",
            "## Catch-up",
            f"- {catchup_bn}",
            "",
            "## Curve Lag",
            f"- {curve_bn}",
            f"- {curve_cb}",
            "",
            "## Supplement",
            f"- binance skew: {skew_bn}",
            f"- coinbase skew: {skew_cb}",
            f"- binance inter-arrival: {inter_bn}",
            f"- coinbase inter-arrival: {inter_cb}",
            f"- polymarket_rtds inter-arrival: {inter_poly}",
        ]
    )
    if png_rel:
        lines.extend(
            [
                "",
                "## Plot",
                "",
                f"![Feed lag alignment]({png_rel})",
            ]
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _window_lag_by_curve(
    lead_1hz: list[float],
    poly_1hz: list[float],
    *,
    win_sec: int,
    max_lag_sec: int,
) -> tuple[list[float], list[float]]:
    """Return per-window lag(sec) and corr where Poly best matches lead curve.

    Positive lag means Poly is delayed and must be shifted forward by lag seconds
    to align with the lead source.
    """
    n = min(len(lead_1hz), len(poly_1hz))
    if n < win_sec + max_lag_sec + 2:
        return [], []
    lag_out: list[float] = []
    corr_out: list[float] = []
    # Window on lead, search lag on poly.
    # i .. i+win_sec are level points, we correlate first differences (shape).
    for i in range(0, n - win_sec - max_lag_sec):
        lead_w = lead_1hz[i : i + win_sec]
        d_lead = _first_diff(lead_w)
        best_lag = None
        best_corr = None
        for lag in range(0, max_lag_sec + 1):
            poly_w = poly_1hz[i + lag : i + lag + win_sec]
            d_poly = _first_diff(poly_w)
            c = _pearson_corr(d_lead, d_poly)
            if c is None:
                continue
            if best_corr is None or c > best_corr:
                best_corr = c
                best_lag = lag
        if best_lag is not None and best_corr is not None:
            lag_out.append(float(best_lag))
            corr_out.append(float(best_corr))
    return lag_out, corr_out


async def _wait_with_progress(duration_sec: float) -> None:
    """Sleep for benchmark duration and render tqdm progress if available."""
    if duration_sec <= 0:
        return
    # Cursor/CI often runs with non-interactive stdio (isatty=False), where
    # tqdm carriage-return animation may be hidden. Use a plain-text fallback.
    interactive_tty = bool(sys.stderr.isatty() or sys.stdout.isatty())
    if tqdm is None or not interactive_tty:
        whole = max(1, int(round(duration_sec)))
        for i in range(whole):
            await asyncio.sleep(duration_sec / whole)
            print(f"[progress] {i + 1}/{whole}s", flush=True)
        return
    total_steps = max(1, int(duration_sec * 10))
    step_sec = duration_sec / total_steps
    with tqdm(
        total=total_steps,
        desc="Collecting feed samples",
        unit="tick",
        leave=False,
    ) as bar:
        for _ in range(total_steps):
            await asyncio.sleep(step_sec)
            bar.update(1)


class PriceBenchState:
    """Cross-feed price dynamics vs Polymarket (single-threaded asyncio)."""

    def __init__(
        self,
        *,
        move_threshold_usd: float,
        skew_keys: tuple[str, ...],
    ) -> None:
        self.move_threshold_usd = move_threshold_usd
        self.skew_keys = skew_keys
        self.skew_ms: dict[str, list[float]] = {k: [] for k in skew_keys}
        self.last_cex_recv: dict[str, float] = {}

        self.last_poly_t: float | None = None
        self.last_poly_px: float | None = None
        self.last_bn_px: float | None = None
        self.last_cb_px: float | None = None
        self.last_kr_px: float | None = None

        # When a CEX tick arrives: age of last Poly update (signal staleness).
        self.poly_stale_ms_on_binance: list[float] = []
        self.poly_stale_ms_on_coinbase: list[float] = []
        self.poly_stale_ms_on_kraken: list[float] = []

        # Poly − CEX in USD (signed) at Poly tick using last known CEX mids before Poly.
        self.gap_poly_minus_bn_at_poly: list[float] = []
        self.gap_poly_minus_cb_at_poly: list[float] = []
        self.gap_poly_minus_kr_at_poly: list[float] = []

        # At CEX tick: last_poly_px − cex_mid (how far oracle is from fresh CEX).
        self.gap_poly_minus_bn_at_bn: list[float] = []
        self.gap_poly_minus_cb_at_cb: list[float] = []

        self.prev_bn_px: float | None = None
        self._pending_bn_move_t: float | None = None

        # After Binance jump ≥ threshold: delay to next Poly tick (ms).
        self.catchup_bn_to_poly_ms: list[float] = []

    def _record_skew_poly(self, t: float) -> None:
        for key in self.skew_keys:
            prev = self.last_cex_recv.get(key)
            if prev is not None:
                self.skew_ms[key].append((t - prev) * 1000.0)

    def on_binance(self, t: float, mid: float) -> None:
        self.last_cex_recv["binance"] = t
        if self.last_poly_t is not None:
            self.poly_stale_ms_on_binance.append((t - self.last_poly_t) * 1000.0)
        if self.last_poly_px is not None:
            self.gap_poly_minus_bn_at_bn.append(self.last_poly_px - mid)

        if self.prev_bn_px is not None and abs(mid - self.prev_bn_px) >= self.move_threshold_usd:
            self._pending_bn_move_t = t
        self.prev_bn_px = mid
        self.last_bn_px = mid

    def on_coinbase(self, t: float, mid: float) -> None:
        self.last_cex_recv["coinbase"] = t
        if self.last_poly_t is not None:
            self.poly_stale_ms_on_coinbase.append((t - self.last_poly_t) * 1000.0)
        if self.last_poly_px is not None:
            self.gap_poly_minus_cb_at_cb.append(self.last_poly_px - mid)
        self.last_cb_px = mid

    def on_kraken(self, t: float, mid: float) -> None:
        self.last_cex_recv["kraken"] = t
        if self.last_poly_t is not None:
            self.poly_stale_ms_on_kraken.append((t - self.last_poly_t) * 1000.0)
        self.last_kr_px = mid

    def on_poly(self, t: float, px: float) -> None:
        if self.last_bn_px is not None:
            self.gap_poly_minus_bn_at_poly.append(px - self.last_bn_px)
        if self.last_cb_px is not None:
            self.gap_poly_minus_cb_at_poly.append(px - self.last_cb_px)
        if "kraken" in self.skew_keys and self.last_kr_px is not None:
            self.gap_poly_minus_kr_at_poly.append(px - self.last_kr_px)

        if self._pending_bn_move_t is not None:
            self.catchup_bn_to_poly_ms.append((t - self._pending_bn_move_t) * 1000.0)
            self._pending_bn_move_t = None

        self._record_skew_poly(t)
        self.last_poly_t = t
        self.last_poly_px = px


async def _run_binance(series: FeedSeries, state: PriceBenchState, stop: asyncio.Event) -> None:
    uri = binance_bookticker_uri("BTC")
    series.t_connect_start = time.perf_counter()
    try:
        async with websockets.connect(uri, **_WS_CONNECT_KWARGS) as ws:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                t = time.perf_counter()
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                tick = data.get("data", data) if isinstance(data, dict) else {}
                if isinstance(tick, dict) and "a" in tick and "b" in tick:
                    bid_px = float(tick["b"])
                    ask_px = float(tick["a"])
                    mid = (bid_px + ask_px) * 0.5
                    series.record(t, mid)
                    state.on_binance(t, mid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[binance] error: {exc}", flush=True)


async def _run_coinbase(series: FeedSeries, state: PriceBenchState, stop: asyncio.Event) -> None:
    series.t_connect_start = time.perf_counter()
    product = coinbase_ticker_product_id("BTC-USD")
    try:
        async with websockets.connect(COINBASE_WS_BASE, **_WS_CONNECT_KWARGS) as ws:
            sub = {
                "type": "subscribe",
                "channels": [{"name": "ticker", "product_ids": [product]}],
            }
            await ws.send(json.dumps(sub))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                t = time.perf_counter()
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if data.get("type") != "ticker" or "price" not in data:
                    continue
                px = float(data["price"])
                if "best_bid" in data and "best_ask" in data:
                    mid = (float(data["best_bid"]) + float(data["best_ask"])) * 0.5
                else:
                    mid = px
                series.record(t, mid)
                state.on_coinbase(t, mid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[coinbase] error: {exc}", flush=True)


async def _run_kraken(series: FeedSeries, state: PriceBenchState, stop: asyncio.Event) -> None:
    series.t_connect_start = time.perf_counter()
    sub_spread = {
        "event": "subscribe",
        "subscription": {"name": "spread"},
        "pair": ["XBT/USD"],
    }
    sub_trade = {
        "event": "subscribe",
        "subscription": {"name": "trade"},
        "pair": ["XBT/USD"],
    }
    kraken_kw = {"ping_interval": 20, "ping_timeout": 10}
    try:
        async with websockets.connect(KRAKEN_URI, **kraken_kw) as ws:
            await ws.send(json.dumps(sub_spread))
            await ws.send(json.dumps(sub_trade))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                t = time.perf_counter()
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(data, list) or len(data) < 3:
                    continue
                kind = data[2]
                mid: float | None = None
                if kind == "spread" and isinstance(data[1], list) and len(data[1]) >= 2:
                    b, a = float(data[1][0]), float(data[1][1])
                    mid = (b + a) * 0.5
                elif kind == "trade" and data[1]:
                    row = data[1][0]
                    mid = float(row[0]) if isinstance(row, (list, tuple)) else float(row)
                if mid is not None:
                    series.record(t, mid)
                    state.on_kraken(t, mid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[kraken] error: {exc}", flush=True)


async def _run_poly_rtds(series: FeedSeries, state: PriceBenchState, stop: asyncio.Event) -> None:
    series.t_connect_start = time.perf_counter()
    sub = {
        "action": "subscribe",
        "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
        ],
    }
    try:
        async with websockets.connect(POLY_RTDS_URI, **_WS_CONNECT_KWARGS) as ws:
            await ws.send(json.dumps(sub))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                t = time.perf_counter()
                try:
                    data = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if data.get("type") != "update":
                    continue
                payload = data.get("payload") or {}
                if str(payload.get("symbol", "")).lower() != "btc/usd":
                    continue
                px = float(payload["value"])
                series.record(t, px)
                state.on_poly(t, px)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[polymarket_rtds] error: {exc}", flush=True)


def _http_clob_ms(n: int) -> tuple[list[float], str | None]:
    out: list[float] = []
    last_err: str | None = None
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                CLOB_HTTPS,
                headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                _ = resp.status
                resp.read(64)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        out.append((time.perf_counter() - t0) * 1000.0)
    return out, last_err


async def _bench(
    duration_sec: float,
    http_clob: bool,
    kraken: bool,
    move_threshold_usd: float,
    lag_window_sec: int,
    lag_max_sec: int,
    export_csv: str | None,
) -> None:
    stop = asyncio.Event()
    skew_keys: tuple[str, ...] = ("binance", "coinbase", "kraken") if kraken else ("binance", "coinbase")
    state = PriceBenchState(move_threshold_usd=move_threshold_usd, skew_keys=skew_keys)
    series: dict[str, FeedSeries] = {
        "binance": FeedSeries("binance"),
        "coinbase": FeedSeries("coinbase"),
        "polymarket_rtds": FeedSeries("polymarket_rtds"),
    }
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_run_binance(series["binance"], state, stop)),
        asyncio.create_task(_run_coinbase(series["coinbase"], state, stop)),
        asyncio.create_task(_run_poly_rtds(series["polymarket_rtds"], state, stop)),
    ]
    if kraken:
        series["kraken"] = FeedSeries("kraken")
        tasks.append(asyncio.create_task(_run_kraken(series["kraken"], state, stop)))

    await _wait_with_progress(duration_sec)
    stop.set()
    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"task cleanup: {exc}", flush=True)

    poly_n = len(series["polymarket_rtds"].events)
    if poly_n < 3 and duration_sec < 15.0:
        print(
            f"Note: Polymarket RTDS only {poly_n} oracle tick(s) in {duration_sec:.1f}s — "
            "use longer --duration for gap/catch-up stats.",
            flush=True,
        )

    print()
    print(f"=== Price propagation vs Polymarket ({duration_sec:.1f}s) ===")
    print(
        "WSS = hft_bot (Binance bookTicker, Coinbase ticker, Polymarket RTDS"
        f"{', Kraken optional' if kraken else ''})."
    )
    print(
        f"Catch-up trigger: Binance mid move ≥ {move_threshold_usd:.1f} USD vs previous tick "
        "→ delay to next Poly oracle tick."
    )
    print()

    print("--- Polymarket signal staleness (when CEX ticks: ms since last Poly oracle update) ---")
    st_bin = _fmt_ms_stats("Binance tick -> Poly age", state.poly_stale_ms_on_binance)
    st_cb = _fmt_ms_stats("Coinbase tick -> Poly age", state.poly_stale_ms_on_coinbase)
    print(f"  {st_bin}")
    print(f"  {st_cb}")
    if kraken:
        print(_fmt_ms_stats("  Kraken tick → Poly age", state.poly_stale_ms_on_kraken))

    print()
    print("--- Price gap: Poly oracle − CEX mid (USD; at Poly tick, using last CEX mid) ---")
    gap_poly_bn = _fmt_usd_stats("Poly - Binance", state.gap_poly_minus_bn_at_poly)
    gap_poly_cb = _fmt_usd_stats("Poly - Coinbase", state.gap_poly_minus_cb_at_poly)
    print(f"  {gap_poly_bn}")
    print(f"  {gap_poly_cb}")
    if kraken:
        print("  " + _fmt_usd_stats("Poly − Kraken", state.gap_poly_minus_kr_at_poly))

    print()
    print("--- Same gap at CEX tick: last Poly oracle − fresh CEX mid (USD) ---")
    gap_last_bn = _fmt_usd_stats("last Poly - Binance", state.gap_poly_minus_bn_at_bn)
    gap_last_cb = _fmt_usd_stats("last Poly - Coinbase", state.gap_poly_minus_cb_at_cb)
    print(f"  {gap_last_bn}")
    print(f"  {gap_last_cb}")

    print()
    print(
        f"--- Catch-up: after Binance move ≥ {move_threshold_usd:.1f} USD, delay to next Poly tick ---"
    )
    catchup_bn = _fmt_ms_stats("Binance move -> next Poly", state.catchup_bn_to_poly_ms)
    print(f"  {catchup_bn}")

    # Curve-alignment lag (user goal): 1Hz windows, match curve shape by max corr.
    bn_1hz = _resample_last_per_second(series["binance"].events)
    cb_1hz = _resample_last_per_second(series["coinbase"].events)
    poly_1hz = _resample_last_per_second(series["polymarket_rtds"].events)
    bn_lags, bn_corrs = _window_lag_by_curve(
        bn_1hz,
        poly_1hz,
        win_sec=lag_window_sec,
        max_lag_sec=lag_max_sec,
    )
    cb_lags, cb_corrs = _window_lag_by_curve(
        cb_1hz,
        poly_1hz,
        win_sec=lag_window_sec,
        max_lag_sec=lag_max_sec,
    )
    print()
    print(
        f"--- Curve lag (1 Hz, window={lag_window_sec}s, search lag=0..{lag_max_sec}s) ---"
    )
    if bn_lags:
        curve_bn = (
            "Binance -> Poly lag(sec): "
            + _fmt_triplet(min(bn_lags), statistics.fmean(bn_lags), max(bn_lags))
            + f"; median={statistics.median(bn_lags):.1f}; windows={len(bn_lags)}; "
            + f"corr(mean/median)={statistics.fmean(bn_corrs):.3f}/{statistics.median(bn_corrs):.3f}"
        )
        print(f"  {curve_bn}")
    else:
        curve_bn = "Binance -> Poly lag(sec): no samples (increase --duration)"
        print(f"  {curve_bn}")
    if cb_lags:
        curve_cb = (
            "Coinbase -> Poly lag(sec): "
            + _fmt_triplet(min(cb_lags), statistics.fmean(cb_lags), max(cb_lags))
            + f"; median={statistics.median(cb_lags):.1f}; windows={len(cb_lags)}; "
            + f"corr(mean/median)={statistics.fmean(cb_corrs):.3f}/{statistics.median(cb_corrs):.3f}"
        )
        print(f"  {curve_cb}")
    else:
        curve_cb = "Coinbase -> Poly lag(sec): no samples (increase --duration)"
        print(f"  {curve_cb}")
    out_csv = ""
    out_png = None
    export_base = _resolve_export_base_path(export_csv)
    if export_base:
        out_csv, out_png = _build_timestamped_report_paths(export_base)
        bn_lag_sec = int(round(statistics.median(bn_lags))) if bn_lags else 0
        cb_lag_sec = int(round(statistics.median(cb_lags))) if cb_lags else 0
        _write_alignment_csv(
            out_csv,
            bn_1hz=bn_1hz,
            cb_1hz=cb_1hz,
            poly_1hz=poly_1hz,
            bn_lag_sec=bn_lag_sec,
            cb_lag_sec=cb_lag_sec,
        )
        print(
            f"  Alignment CSV saved: {out_csv} "
            f"(poly shift: binance={bn_lag_sec}s, coinbase={cb_lag_sec}s)"
        )
        if _plot_alignment_png_from_arrays(
            out_png,
            bn_1hz=bn_1hz,
            cb_1hz=cb_1hz,
            poly_1hz=poly_1hz,
            bn_lag_sec=bn_lag_sec,
            cb_lag_sec=cb_lag_sec,
        ):
            print(f"  Alignment plot saved: {out_png}")
        else:
            print("  Alignment plot skipped: matplotlib is not available")
        out_md = str(Path(out_csv).with_suffix(".md"))
        inter_bn = _fmt_triplet(
            *_gap_stats_ms(series["binance"].inter_arrival_ms())
        )
        inter_cb = _fmt_triplet(
            *_gap_stats_ms(series["coinbase"].inter_arrival_ms())
        )
        inter_poly = _fmt_triplet(
            *_gap_stats_ms(series["polymarket_rtds"].inter_arrival_ms())
        )
        skew_bn = _fmt_skew_line(state.skew_ms["binance"])
        skew_cb = _fmt_skew_line(state.skew_ms["coinbase"])
        _write_markdown_report(
            out_md,
            duration_sec=duration_sec,
            move_threshold_usd=move_threshold_usd,
            lag_window_sec=lag_window_sec,
            lag_max_sec=lag_max_sec,
            st_bin=st_bin,
            st_cb=st_cb,
            gap_poly_bn=gap_poly_bn,
            gap_poly_cb=gap_poly_cb,
            gap_last_bn=gap_last_bn,
            gap_last_cb=gap_last_cb,
            catchup_bn=catchup_bn,
            curve_bn=curve_bn,
            curve_cb=curve_cb,
            skew_bn=skew_bn,
            skew_cb=skew_cb,
            inter_bn=inter_bn,
            inter_cb=inter_cb,
            inter_poly=inter_poly,
            csv_path=out_csv,
            png_path=out_png if (out_png and Path(out_png).exists()) else None,
        )
        print(f"  Markdown report saved: {out_md}")

    print()
    print("--- Supplement: recv-order skew (Poly tick vs last CEX recv, ms) ---")
    print(f"  binance   {_fmt_skew_line(state.skew_ms['binance'])}")
    print(f"  coinbase  {_fmt_skew_line(state.skew_ms['coinbase'])}")
    if kraken:
        print(f"  kraken    {_fmt_skew_line(state.skew_ms['kraken'])}")

    print()
    print("--- Supplement: inter-arrival gap between ticks (ms) ---")
    feed_order = ("binance", "coinbase", "kraken", "polymarket_rtds") if kraken else (
        "binance",
        "coinbase",
        "polymarket_rtds",
    )
    for key in feed_order:
        s = series[key]
        gaps = s.inter_arrival_ms()
        gmin, gmean, gmax = _gap_stats_ms(gaps)
        ft = s.first_tick_ms()
        ft_s = f"{ft:.1f}" if ft is not None else "n/a"
        print(
            f"  {key:18s}  n={len(s.events):5d}  first_tick_ms={ft_s:>8}  "
            f"gaps min/mean/max = {_fmt_triplet(gmin, gmean, gmax)}"
        )

    if http_clob:
        print()
        print(f"HTTPS GET {CLOB_HTTPS} (3 requests):")
        times, last_err = await asyncio.to_thread(_http_clob_ms, 3)
        if times:
            print(
                f"  min/mean/max ms = {_fmt_triplet(min(times), statistics.fmean(times), max(times))}"
            )
        else:
            print(f"  (all failed){f' — {last_err}' if last_err else ''}")
    print()


def _fmt_skew_line(xs: list[float]) -> str:
    if not xs:
        return "no samples"
    lo, mid, hi = min(xs), statistics.fmean(xs), max(xs)
    p50 = statistics.median(xs)
    return f"n={len(xs)}  min/mean/median/max = {lo:.1f} / {mid:.1f} / {p50:.1f} / {hi:.1f} ms"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Polymarket oracle vs CEX: price gaps, staleness, catch-up delay (same WSS as hft_bot)."
    )
    p.add_argument(
        "--duration",
        type=float,
        default=float(os.environ.get("FEED_BENCH_SEC", "5")),
        help="Seconds to collect (default 5 or FEED_BENCH_SEC). Prefer ≥30 for Poly stats.",
    )
    p.add_argument(
        "--move-threshold",
        type=float,
        default=float(os.environ.get("FEED_BENCH_MOVE_USD", "5")),
        help="Binance USD move vs previous tick to start catch-up timer (default 5).",
    )
    p.add_argument(
        "--lag-window-sec",
        type=int,
        default=int(os.environ.get("FEED_BENCH_LAG_WINDOW_SEC", "20")),
        help="Window size in seconds for curve-based lag estimate (default 20).",
    )
    p.add_argument(
        "--lag-max-sec",
        type=int,
        default=int(os.environ.get("FEED_BENCH_LAG_MAX_SEC", "15")),
        help="Max lag to search for Poly delay in curve matching (default 15).",
    )
    p.add_argument(
        "--export-csv",
        type=str,
        default=os.environ.get("FEED_BENCH_EXPORT_CSV", str(Path(DEFAULT_REPORT_DIR) / "feed_lag_alignment.csv")),
        help=(
            "Export base CSV path (default: hft_bot/reports/banch_lag/feed_lag_alignment.csv). "
            "Can also be a directory (e.g. hft_bot/reports/banch_lag/)."
        ),
    )
    p.add_argument(
        "--http-clob",
        action="store_true",
        help="After bench, 3 HTTPS GETs to clob.polymarket.com (min/mean/max ms).",
    )
    p.add_argument(
        "--kraken",
        action="store_true",
        help="Also connect Kraken WS (not in hft_bot main loop).",
    )
    args = p.parse_args()
    if args.duration <= 0:
        raise SystemExit("duration must be positive")
    if args.move_threshold <= 0:
        raise SystemExit("--move-threshold must be positive")
    if args.lag_window_sec < 5:
        raise SystemExit("--lag-window-sec must be >= 5")
    if args.lag_max_sec < 1:
        raise SystemExit("--lag-max-sec must be >= 1")
    asyncio.run(
        _bench(
            args.duration,
            args.http_clob,
            args.kraken,
            args.move_threshold,
            args.lag_window_sec,
            args.lag_max_sec,
            args.export_csv,
        )
    )


if __name__ == "__main__":
    main()
