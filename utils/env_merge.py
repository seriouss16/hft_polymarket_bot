"""Merge KEY=VALUE lines from a file into ``os.environ`` (same rules as ``bot._load_env_file``)."""

from __future__ import annotations

import os
from pathlib import Path


def merge_env_file(path: Path, *, overwrite: bool = False) -> None:
    """Merge key=value pairs from path into process environment.

    Lines starting with ``#``, blanks, and lines without ``=`` are skipped.
    When ``overwrite`` is False, existing keys in ``os.environ`` are left unchanged.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = val
