"""Anchored workspace resolution for data commands (spec §5.1).

Order: ``AGNES_LOCAL_DIR`` env (explicit override, always wins even when
the target is not workspace-shaped — the sandbox/runner contract) →
cwd, if workspace-shaped (preserves pre-existing behaviour for anyone
standing inside a workspace) → ``workspace_root`` config, if
workspace-shaped (the global fallback; a stale anchor degrades to None,
never to reads against a bogus path) → ``None``.

Deliberately DIFFERENT from ``cli/commands/update.py::_resolve_workspace``
(env → anchor → cwd-if-initialised): convergence must target the anchor
even when run from inside some other initialised folder, while data reads
prefer the workspace you are standing in. Pinned by
``tests/test_workspace_resolve.py::test_precedence_differs_from_update_resolver``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cli.config import get_workspace_root


def is_workspace_shaped(p: Path) -> bool:
    """True when ``p`` looks like an Agnes workspace: the init sentinel,
    a local analytics DuckDB, or a parquet tree."""
    try:
        return (
            (p / ".claude" / "init-complete").exists()
            or (p / "user" / "duckdb" / "analytics.duckdb").exists()
            or (p / "server" / "parquet").is_dir()
        )
    except OSError:
        return False


def resolve_data_workspace() -> Optional[Path]:
    env_dir = os.environ.get("AGNES_LOCAL_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    cwd = Path.cwd()
    if is_workspace_shaped(cwd):
        return cwd.resolve()
    root = get_workspace_root()
    if root:
        anchor = Path(root)
        if is_workspace_shaped(anchor):
            return anchor.resolve()
    return None
