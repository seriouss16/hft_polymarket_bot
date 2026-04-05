#!/usr/bin/env python3
"""Plot curve alignment from benchmark_feed_latency CSV output.

Input CSV columns (from benchmark_feed_latency.py):
  - sec_idx
  - binance_mid
  - coinbase_mid
  - poly_mid
  - poly_shifted_for_binance
  - poly_shifted_for_coinbase

Example:
  uv run python hft_bot/scripts/plot_feed_lag_alignment.py \
    --input hft_bot/reports/feed_lag_alignment.csv \
    --output hft_bot/reports/feed_lag_alignment.png
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


def _as_float(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="Plot feed lag alignment curves from CSV.")
    p.add_argument(
        "--input",
        default="hft_bot/reports/feed_lag_alignment.csv",
        help="Input CSV from benchmark_feed_latency.py",
    )
    p.add_argument(
        "--output",
        default="hft_bot/reports/feed_lag_alignment.png",
        help="Output PNG path",
    )
    args = p.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        raise SystemExit(f"input CSV not found: {in_path}")

    sec_idx: list[int] = []
    bn: list[float] = []
    cb: list[float] = []
    poly: list[float] = []
    poly_bn_shift: list[float | None] = []
    poly_cb_shift: list[float | None] = []

    with in_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        required = {
            "sec_idx",
            "binance_mid",
            "coinbase_mid",
            "poly_mid",
            "poly_shifted_for_binance",
            "poly_shifted_for_coinbase",
        }
        missing = required.difference(r.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV missing columns: {sorted(missing)}")
        for row in r:
            sec_idx.append(int(float(row["sec_idx"])))
            bn.append(float(row["binance_mid"]))
            cb.append(float(row["coinbase_mid"]))
            poly.append(float(row["poly_mid"]))
            poly_bn_shift.append(_as_float(row["poly_shifted_for_binance"]))
            poly_cb_shift.append(_as_float(row["poly_shifted_for_coinbase"]))

    if not sec_idx:
        raise SystemExit("CSV has no data rows")

    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)

    # Top: raw curves
    ax = axes[0]
    ax.plot(sec_idx, bn, label="Binance mid", color="tab:blue", linewidth=1.3)
    ax.plot(sec_idx, cb, label="Coinbase mid", color="tab:green", linewidth=1.3)
    ax.plot(sec_idx, poly, label="Poly RTDS", color="tab:red", linewidth=1.3, alpha=0.9)
    ax.set_title("Raw curves")
    ax.set_ylabel("Price (USD)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")

    # Bottom: aligned overlays
    ax2 = axes[1]
    ax2.plot(sec_idx, bn, label="Binance mid", color="tab:blue", linewidth=1.2)
    ax2.plot(sec_idx, cb, label="Coinbase mid", color="tab:green", linewidth=1.2)

    x_bn = [sec_idx[i] for i, v in enumerate(poly_bn_shift) if v is not None]
    y_bn = [v for v in poly_bn_shift if v is not None]
    x_cb = [sec_idx[i] for i, v in enumerate(poly_cb_shift) if v is not None]
    y_cb = [v for v in poly_cb_shift if v is not None]
    if x_bn:
        ax2.plot(
            x_bn,
            y_bn,
            label="Poly shifted for Binance",
            color="tab:orange",
            linewidth=1.4,
            alpha=0.95,
        )
    if x_cb:
        ax2.plot(
            x_cb,
            y_cb,
            label="Poly shifted for Coinbase",
            color="tab:purple",
            linewidth=1.4,
            alpha=0.95,
        )
    ax2.set_title("Aligned curves (using lag medians from benchmark)")
    ax2.set_xlabel("Second index")
    ax2.set_ylabel("Price (USD)")
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="best")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    print(f"saved plot: {out_path}")


if __name__ == "__main__":
    main()
