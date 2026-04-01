#!/usr/bin/env python3
"""Run ``benchmark_vpn_clob.sh`` (nmcli VPN sweep). There is no separate Python implementation."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    sh = os.path.join(root, "benchmark_vpn_clob.sh")
    if not os.path.isfile(sh):
        print(f"error: missing {sh}", file=sys.stderr)
        sys.exit(1)
    env = os.environ.copy()
    raise SystemExit(subprocess.call(["/usr/bin/env", "bash", sh], cwd=root, env=env))


if __name__ == "__main__":
    main()
