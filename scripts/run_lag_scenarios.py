#!/usr/bin/env python3
"""Benchmark script to run the bot in simulation mode with different lag scenarios."""

import asyncio
import os
import subprocess
import time
from pathlib import Path

SCENARIOS = [0.0, 0.5, 1.0, 2.0]
DURATION_SEC = 60  # Run each scenario for 60 seconds
REPORT_DIR = Path("reports/lag_benchmarks")

async def run_scenario(delay: float):
    print(f"--- Running scenario: {delay}s delay ---")
    env = os.environ.copy()
    env["HFT_SIM_FEED_DELAY_SEC"] = str(delay)
    env["HFT_MODE"] = "simulation"
    
    # Ensure we use a unique journal for each run
    journal_path = REPORT_DIR / f"journal_lag_{delay}s.csv"
    env["HFT_TRADE_JOURNAL_PATH"] = str(journal_path)
    
    # Start the bot
    # Note: We use 'uv run python bot.py' as per global instructions
    cmd = ["uv", "run", "python", "bot.py"]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    try:
        # Wait for the specified duration
        await asyncio.sleep(DURATION_SEC)
    finally:
        # Terminate the bot
        process.terminate()
        await process.wait()
        print(f"--- Finished scenario: {delay}s delay ---")

def generate_report():
    print("--- Generating Comparison Report ---")
    report_file = REPORT_DIR / "comparison_report.md"
    with open(report_file, "w") as f:
        f.write("# Lag Simulation Comparison Report\n\n")
        f.write("| Delay (s) | Trades | Total PnL | Avg Latency (ms) |\n")
        f.write("|-----------|--------|-----------|------------------|\n")
        
        for delay in SCENARIOS:
            journal_path = REPORT_DIR / f"journal_lag_{delay}s.csv"
            if not journal_path.exists():
                f.write(f"| {delay} | N/A | N/A | N/A |\n")
                continue
            
            # Simple CSV parsing to get stats
            import csv
            trades = 0
            total_pnl = 0.0
            latencies = []
            
            with open(journal_path, "r") as jf:
                reader = csv.DictReader(jf)
                for row in reader:
                    if row.get("row_kind") == "close":
                        trades += 1
                        total_pnl += float(row.get("pnl") or 0.0)
                        latencies.append(float(row.get("latency_ms") or 0.0))
            
            avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
            f.write(f"| {delay} | {trades} | {total_pnl:.4f} | {avg_latency:.2f} |\n")
    
    print(f"Report generated at {report_file}")

async def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for delay in SCENARIOS:
        await run_scenario(delay)
    generate_report()

if __name__ == "__main__":
    asyncio.run(main())
