"""Resolve the checkout root so paths work when code is imported from site-packages."""

from __future__ import annotations

from pathlib import Path

_RUNTIME_ENV_REL = Path("config") / "runtime.env"


def get_workspace_root() -> Path:
    """Directory containing ``config/runtime.env`` (the real project tree).

    When top-level modules live under ``site-packages`` but the process was started
    with ``cwd`` set to the repo (``cd project && uv run ...``), prefer ``cwd``.
    Walking from ``utils/`` covers tests and imports from a source checkout.
    """
    cwd = Path.cwd()
    try:
        if (cwd / _RUNTIME_ENV_REL).is_file():
            return cwd.resolve()
    except OSError:
        pass
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, *here.parents):
        try:
            if (base / _RUNTIME_ENV_REL).is_file():
                return base.resolve()
        except OSError:
            continue
    return cwd.resolve()
