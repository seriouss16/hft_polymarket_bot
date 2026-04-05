import statistics
import time

from core.live_common import is_fresh_for_trading


class MockCache:
    def __init__(self, fresh=True):
        self._fresh = fresh

    def is_fresh(self, *args):
        return self._fresh


def benchmark():
    market_cache = MockCache(True)
    user_cache = MockCache(True)
    token_id = "0x123"

    latencies = []
    for _ in range(10000):
        start = time.perf_counter_ns()
        is_fresh_for_trading(token_id, market_cache, user_cache)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    avg_ns = statistics.mean(latencies)
    p99_ns = statistics.quantiles(latencies, n=100)[98]

    print(f"Average latency: {avg_ns:.2f} ns ({avg_ns/1000000:.4f} ms)")
    print(f"P99 latency: {p99_ns:.2f} ns ({p99_ns/1000000:.4f} ms)")


if __name__ == "__main__":
    benchmark()
