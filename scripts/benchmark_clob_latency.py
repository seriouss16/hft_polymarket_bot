#!/usr/bin/env python3
"""Simple HTTPS latency benchmark to Polymarket CLOB.

Measures raw HTTPS latency to clob.polymarket.com without VPN.
Use this to verify your current location's latency before migration.

Usage:
    python hft_bot/scripts/benchmark_clob_latency.py --runs 10 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import time
import statistics
from pathlib import Path
from datetime import datetime

try:
    import aiohttp
except ImportError:
    print("Installing aiohttp...")
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp"])
    import aiohttp


CLOB_URL = "https://clob.polymarket.com/"


async def measure_latency(session: aiohttp.ClientSession) -> float:
    """Measure single HTTPS request latency in seconds."""
    start = time.perf_counter()
    try:
        async with session.get(CLOB_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await resp.text()  # Consume response
            elapsed = time.perf_counter() - start
            return elapsed
    except Exception as e:
        print(f"Error: {e}")
        return float('inf')


async def benchmark(
    runs: int = 10,
    duration_sec: float = 60.0,
    output_dir: str = "hft_bot/reports/banch_lag"
) -> dict:
    """Run latency benchmark and return statistics."""
    
    print(f"🎯 Target: {CLOB_URL}")
    print(f"📊 Runs: {runs}")
    print(f"⏱️  Duration: {duration_sec}s")
    print("-" * 50)
    
    results = []
    timestamps = []
    
    async with aiohttp.ClientSession() as session:
        start_time = time.perf_counter()
        run_count = 0
        
        pbar = None
        try:
            from tqdm import tqdm
            pbar = tqdm(total=runs, desc="Benchmarking")
        except ImportError:
            pass
        
        while run_count < runs or (time.perf_counter() - start_time) < duration_sec:
            latency = await measure_latency(session)
            run_count += 1
            
            if latency == float('inf'):
                print(f"❌ Run {run_count}: FAILED")
                continue
            
            results.append(latency * 1000)  # Convert to ms
            timestamps.append(datetime.now().isoformat())
            
            if pbar:
                pbar.update(1)
            else:
                print(f"✅ Run {run_count}: {latency*1000:.1f}ms")
        
        if pbar:
            pbar.close()
    
    if not results:
        print("❌ No successful measurements!")
        return {}
    
    # Calculate statistics
    min_ms = min(results)
    max_ms = max(results)
    mean_ms = statistics.mean(results)
    median_ms = statistics.median(results)
    std_ms = statistics.stdev(results) if len(results) > 1 else 0.0
    
    # Percentiles
    sorted_results = sorted(results)
    p95_idx = int(len(sorted_results) * 0.95)
    p99_idx = int(len(sorted_results) * 0.99)
    p95_ms = sorted_results[p95_idx] if p95_idx < len(sorted_results) else max_ms
    p99_ms = sorted_results[p99_idx] if p99_idx < len(sorted_results) else max_ms
    
    stats = {
        "min_ms": min_ms,
        "max_ms": max_ms,
        "mean_ms": mean_ms,
        "median_ms": median_ms,
        "std_ms": std_ms,
        "p95_ms": p95_ms,
        "p99_ms": p99_ms,
        "count": len(results),
        "timestamps": timestamps,
    }
    
    # Print results
    print("\n" + "=" * 50)
    print("📈 LATENCY RESULTS")
    print("=" * 50)
    print(f"Samples:    {stats['count']}")
    print(f"Min:        {stats['min_ms']:.1f} ms")
    print(f"Max:        {stats['max_ms']:.1f} ms")
    print(f"Mean:       {stats['mean_ms']:.1f} ms")
    print(f"Median:     {stats['median_ms']:.1f} ms")
    print(f"Std Dev:    {stats['std_ms']:.1f} ms")
    print(f"P95:        {stats['p95_ms']:.1f} ms")
    print(f"P99:        {stats['p99_ms']:.1f} ms")
    print("=" * 50)
    
    # Save to file
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    md_file = output_path / f"clob_latency_{timestamp}.md"
    csv_file = output_path / f"clob_latency_{timestamp}.csv"
    
    # Write markdown report
    with open(md_file, "w") as f:
        f.write(f"# CLOB HTTPS Latency Report\n\n")
        f.write(f"- Timestamp: {datetime.now().isoformat()}\n")
        f.write(f"- Target: {CLOB_URL}\n")
        f.write(f"- Samples: {stats['count']}\n\n")
        f.write("## Statistics\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Min | {stats['min_ms']:.1f} ms |\n")
        f.write(f"| Max | {stats['max_ms']:.1f} ms |\n")
        f.write(f"| Mean | {stats['mean_ms']:.1f} ms |\n")
        f.write(f"| Median | {stats['median_ms']:.1f} ms |\n")
        f.write(f"| Std Dev | {stats['std_ms']:.1f} ms |\n")
        f.write(f"| P95 | {stats['p95_ms']:.1f} ms |\n")
        f.write(f"| P99 | {stats['p99_ms']:.1f} ms |\n")
        f.write(f"\n## Interpretation\n\n")
        
        if stats['mean_ms'] < 15:
            f.write("✅ **Excellent** - You're close to Polymarket servers. Ready for migration to Ireland/London.\n")
        elif stats['mean_ms'] < 30:
            f.write("✅ **Good** - Acceptable latency. Ireland/London migration would add ~5-10ms improvement.\n")
        elif stats['mean_ms'] < 50:
            f.write("⚠️ **Moderate** - Consider migration to Ireland/London for <15ms latency.\n")
        else:
            f.write("❌ **High** - Significant latency. Migration to Ireland/London strongly recommended.\n")
    
    # Write CSV
    with open(csv_file, "w") as f:
        f.write("run,timestamp,latency_ms\n")
        for i, ts in enumerate(timestamps):
            f.write(f"{i+1},{ts},{results[i]}\n")
    
    print(f"\n📁 Reports saved to:")
    print(f"   {md_file}")
    print(f"   {csv_file}")
    
    return stats


def main():
    parser = argparse.ArgumentParser(description="Benchmark HTTPS latency to Polymarket CLOB")
    parser.add_argument("--runs", type=int, default=10, help="Number of benchmark runs")
    parser.add_argument("--duration", type=float, default=60.0, help="Minimum duration in seconds")
    parser.add_argument("--output", type=str, default="hft_bot/reports/banch_lag", help="Output directory")
    
    args = parser.parse_args()
    
    print(f"🚀 Starting CLOB Latency Benchmark")
    print(f"📅 {datetime.now().isoformat()}")
    print()
    
    stats = asyncio.run(benchmark(
        runs=args.runs,
        duration_sec=args.duration,
        output_dir=args.output
    ))
    
    if stats and stats['count'] > 0:
        print(f"\n✅ Benchmark complete!")
        return 0
    else:
        print(f"\n❌ Benchmark failed!")
        return 1


if __name__ == "__main__":
    exit(main())
