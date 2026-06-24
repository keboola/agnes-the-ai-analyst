"""Locate the current workspace's Claude Code session transcripts on disk.

Claude Code writes session jsonls to ``<projects_root>/<encoded-workspace>/``
where:

- ``projects_root`` is ``$CLAUDE_CONFIG_DIR/projects`` when
  ``CLAUDE_CONFIG_DIR`` is set, otherwise ``~/.claude/projects``.
- the workspace path is encoded by replacing **every** non-alphanumeric
  character with ``-`` and **without** collapsing consecutive dashes. e.g.
  ``C:\\Users\\me\\FoundryAI`` -> ``C--Users-me-FoundryAI`` (the ``:`` and
  the first ``\\`` each become a ``-``, so ``C:\\`` yields ``C--``).

This is the encoding recent Claude Code releases use on every platform. An
earlier collapse-runs variant (and a POSIX-normalizing shell shim) produced
the *wrong* folder name on Windows, which is why session upload was
unreliable. We anchor on the workspace root recorded in the Agnes config
(``workspace_root``) rather than reverse-engineering it from the current
working directory, so the scan is stable regardless of where ``agnes push``
is invoked from.

Only the workspace-root folder is scanned. Sessions started in nested
subfolders of the workspace are intentionally **not** uploaded.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def projects_root() -> Path:
    """Directory Claude Code keeps per-project session folders under."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg) / "projects"
    return Path.home() / ".claude" / "projects"


def encode_workspace(workspace_root: str | Path) -> str:
    """Encode a workspace path to its Claude Code projects-dir folder name.

    Replaces every non-alphanumeric character with ``-``. Does NOT collapse
    runs of ``-`` — Claude Code keeps them, so collapsing would point at a
    non-existent folder on Windows (``C:\\x`` must stay ``C--x``). A single
    trailing path separator is stripped first so ``/a/b`` and ``/a/b/``
    encode identically.
    """
    s = str(workspace_root)
    stripped = s.rstrip("/\\")
    if stripped:
        s = stripped
    return re.sub(r"[^A-Za-z0-9]", "-", s)


def session_dir(workspace_root: str | Path) -> Path:
    """The single ``<projects_root>/<encoded-workspace>/`` folder for *workspace_root*."""
    return projects_root() / encode_workspace(workspace_root)


def list_session_files(workspace_root: str | Path) -> list[Path]:
    """Return the ``*.jsonl`` transcripts in the workspace's session folder.

    Sorted by filename for deterministic upload order. Returns an empty list
    when the folder doesn't exist. Each file's stem is the Claude Code
    ``session_id`` (Claude Code names transcripts ``<session-id>.jsonl``).
    """
    d = session_dir(workspace_root)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.name)
