#!/usr/bin/env python3
"""Pair LIVE 'BUY placed' limit prices with executor 'filled @ avg' to estimate entry slippage.

Usage (from ``hft_bot/``):

  # one file
  uv run python scripts/analyze_live_entry_slippage.py reports/logs/_bot_300326_121002_live_plus.log

  # whole directory (recursive): only logs matching ``*_live*.log`` by default
  uv run python scripts/analyze_live_entry_slippage.py reports/logs/

  # all ``*.log`` in tree
  uv run python scripts/analyze_live_entry_slippage.py reports/logs/ --glob '*.log'

  # non-recursive (this directory only)
  uv run python scripts/analyze_live_entry_slippage.py reports/logs/ --no-recursive

From repo root:

  uv run python hft_bot/scripts/analyze_live_entry_slippage.py hft_bot/reports/logs/ --glob '*_live*.log'

Requires log lines from live_engine (BUY placed @) and executor ([LIVE BUY_*] filled @ avg).
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

_PLACED = re.compile(
    r"BUY placed:\s+BUY_(UP|DOWN)\s+[\d.]+\s+sh\s+@\s+([\d.]+)",
)
_FILLED = re.compile(
    r"\[LIVE BUY_(UP|DOWN)\]\s+filled=[\d.]+\s+sh\s+@\s+avg\s+([\d.]+)",
)


def analyze(text: str) -> tuple[list[float], list[tuple[str, float, float]]]:
    placed: list[tuple[str, float]] = []
    for m in _PLACED.finditer(text):
        placed.append((m.group(1), float(m.group(2))))
    filled: list[tuple[str, float]] = []
    for m in _FILLED.finditer(text):
        filled.append((m.group(1), float(m.group(2))))
    n = min(len(placed), len(filled))
    rels: list[float] = []
    rows: list[tuple[str, float, float]] = []
    for i in range(n):
        side_p, px_p = placed[i]
        side_f, px_f = filled[i]
        if side_p != side_f:
            continue
        if px_p <= 0:
            continue
        rel = (px_f - px_p) / px_p
        rels.append(rel)
        rows.append((side_p, px_p, px_f))
    return rels, rows


def _iter_log_paths(root: Path, pattern: str, recursive: bool) -> list[Path]:
    if not root.is_dir():
        return []
    if recursive:
        paths = sorted(root.rglob(pattern))
    else:
        paths = sorted(root.glob(pattern))
    return [p for p in paths if p.is_file()]


def _print_aggregate(
    rels: list[float],
    rows: list[tuple[str, float, float]],
    *,
    label: str,
    show_samples: bool,
) -> None:
    print(f"\n=== {label} ===")
    print(f"Paired trades: {len(rels)}")
    print(
        f"Relative slippage (avg-placed)/placed: "
        f"mean={statistics.mean(rels):.4%} median={statistics.median(rels):.4%}"
    )
    if len(rels) > 1:
        print(f"  stdev={statistics.stdev(rels):.4%}")
    mean_rel = statistics.mean(rels)
    per_sec = 0.0005
    sec_hint = mean_rel / per_sec if per_sec > 0 else 0.0
    print(
        f"Suggested HFT_SIM_SLIPPAGE_EXTRA_FRACTION={mean_rel:.6f} "
        f"(direct +{mean_rel:.2%} vs book ask before SIM fee; caps in engine still apply)."
    )
    print(
        f"Alternatively HFT_SIM_ENTRY_SLIPPAGE_SEC≈{max(0.0, sec_hint):.2f} "
        f"with HFT_SIM_SLIPPAGE_EXTRA_FRACTION_PER_SEC={per_sec} (linear model; "
        f"use direct fraction when fills are not well modelled as per-second drift)."
    )
    if show_samples:
        print("Sample (side, placed, filled):")
        for r in rows[:15]:
            print(f"  {r[0]} placed={r[1]:.4f} filled={r[2]:.4f}")
        if len(rows) > 15:
            print(f"  ... +{len(rows) - 15} more")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "path",
        type=Path,
        help="Log file or directory of .log files",
    )
    ap.add_argument(
        "--glob",
        dest="glob_pattern",
        default="*_live*.log",
        help="Glob for files when PATH is a directory (default: *_live*.log). "
        "Use '*.log' to include every log in the tree.",
    )
    ap.add_argument(
        "--no-recursive",
        action="store_true",
        help="When PATH is a directory, only match files in that folder (no subfolders).",
    )
    args = ap.parse_args()
    target = args.path

    if target.is_file():
        text = target.read_text(encoding="utf-8", errors="replace")
        rels, rows = analyze(text)
        if not rels:
            print("No paired BUY placed / LIVE BUY filled lines found.", file=sys.stderr)
            return 1
        print(f"File: {target.resolve()}")
        _print_aggregate(rels, rows, label="single file", show_samples=True)
        return 0

    if not target.is_dir():
        print(f"Not a file or directory: {target}", file=sys.stderr)
        return 2

    files = _iter_log_paths(target, args.glob_pattern, recursive=not args.no_recursive)
    if not files:
        print(
            f"No files matching glob {args.glob_pattern!r} under {target.resolve()}",
            file=sys.stderr,
        )
        return 1

    print(f"Directory: {target.resolve()}")
    print(f"Glob: {args.glob_pattern!r}  recursive={not args.no_recursive}")
    print(f"Files matched: {len(files)}")

    all_rels: list[float] = []
    all_rows: list[tuple[str, float, float]] = []
    per_file: list[tuple[Path, int, float | None]] = []

    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  skip (read error): {fp} ({e})", file=sys.stderr)
            continue
        rels, rows = analyze(text)
        if not rels:
            per_file.append((fp, 0, None))
            continue
        m = statistics.mean(rels)
        per_file.append((fp, len(rels), m))
        all_rels.extend(rels)
        all_rows.extend(rows)

    print("\n--- per file ---")
    for fp, n_pair, mrel in per_file:
        rel_s = f"mean_slip={mrel:+.4%}" if mrel is not None else "no pairs"
        print(f"  {fp.name}  n={n_pair}  {rel_s}")

    if not all_rels:
        print("\nNo paired BUY placed / LIVE BUY filled lines in any file.", file=sys.stderr)
        return 1

    _print_aggregate(all_rels, all_rows, label="aggregate (all matched files)", show_samples=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
