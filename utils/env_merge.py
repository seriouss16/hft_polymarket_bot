"""Merge KEY=VALUE lines from a file into ``os.environ`` (same rules as ``bot._load_env_file``)."""

from __future__ import annotations

import os
from pathlib import Path


def strip_env_inline_comment(val: str) -> str:
    """Remove trailing ``# …`` from a value when ``#`` is preceded by whitespace (outside quotes).

    Matches common ``KEY=value  # note`` style in ``*.env`` files. Unquoted ``#`` in the
    middle of a token is left intact (e.g. ``foo#bar``).
    """
    s = val.strip()
    n = len(s)
    i = 0
    in_quote: str | None = None
    while i < n:
        ch = s[i]
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in "\"'":
            in_quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or s[i - 1].isspace()):
            return s[:i].rstrip()
        i += 1
    return s


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
        val = strip_env_inline_comment(val)
        val = val.strip('"').strip("'")
        if not key:
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = val
