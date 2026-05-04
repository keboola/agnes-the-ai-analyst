"""Locate Claude Code session transcripts on disk.

Claude Code writes session jsonls to ``~/.claude/projects/<encoded-cwd>/``,
where the cwd encoding is **version-dependent**:

- **Older versions**: replace ``/`` with ``-``, preserve everything else
  (spaces, tildes, dots, underscores).  This is what we observe on macOS
  with iCloud paths today.

- **Newer versions** (and likely the default on Windows): replace every
  non-alphanumeric character with ``-``, then collapse runs of consecutive
  ``-``.  This matches "slugify"-style encoding used by recent Claude
  Code releases.

We try both encodings and return whichever directory exists.  This is
forward-compatible: if Claude Code adds a third encoding scheme later,
extend the variant list.

Cross-platform notes:
- ``~/.claude/projects/`` resolves via ``Path.home()``, which honors
  ``$HOME`` on POSIX and ``%USERPROFILE%`` on Windows.
- On Windows, the cwd will look like ``C:\\Users\\foo\\workspace``; the
  variant-B (non-alphanumeric -> ``-``) encoding handles drive letters
  and backslashes naturally.  Variant A is POSIX-flavored but harmless
  on Windows (it just won't match anything).

The legacy ``<workspace>/user/sessions/`` directory is preserved as a
fallback for setups that explicitly mirror sessions there (e.g. a
custom hook).  The new code tries the Claude Code path first; if no
sessions are found there, falls back to the legacy directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator


_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _encode_variant_a(cwd: str) -> str:
    """Older Claude Code: replace ``/`` with ``-``.  Preserves spaces, tildes,
    dots, underscores, etc.  Observed in production on macOS with iCloud paths.
    """
    return cwd.replace("/", "-")


def _encode_variant_b(cwd: str) -> str:
    """Newer Claude Code: replace every non-alphanumeric with ``-``, then
    collapse consecutive ``-`` to a single one.  Matches slugify-style
    encoding used by recent releases.
    """
    s = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    # Collapse runs of `-` to a single `-`.  Some Claude Code versions
    # leave the runs alone; others collapse.  We collapse defensively.
    return re.sub(r"-+", "-", s)


def _candidate_encodings(cwd: str) -> Iterator[str]:
    """Yield candidate encoded directory names for *cwd*, ordered by
    expected frequency.

    Both variants are emitted regardless of platform — Claude Code's
    encoding is a function of its release version, not the host OS.
    """
    yield _encode_variant_a(cwd)
    yield _encode_variant_b(cwd)


def find_claude_sessions_dirs(workspace: Path) -> list[Path]:
    """Return every ``~/.claude/projects/<encoded>/`` directory that exists
    for *workspace* — usually one, but **two** when the user has run both
    older and newer Claude Code versions in the same cwd (each version
    writes to its own encoded dir).  Returns an empty list when nothing
    matches.

    Reading all matching dirs is the correct default: if we picked only
    one, the picker would either miss the newest sessions (if it picks
    the older variant) or miss historical sessions still in the older
    variant's dir.
    """
    cwd = str(workspace.resolve())

    found: list[Path] = []
    seen: set[str] = set()
    for encoded in _candidate_encodings(cwd):
        if encoded in seen:
            continue
        seen.add(encoded)
        candidate = _PROJECTS_DIR / encoded
        if candidate.is_dir():
            found.append(candidate)

    return found


def find_claude_sessions_dir(workspace: Path) -> Path | None:
    """Return the first matching ``~/.claude/projects/<encoded>/`` directory
    or ``None``.  Kept for callers that only need a yes/no answer; prefer
    :func:`find_claude_sessions_dirs` when listing files.
    """
    dirs = find_claude_sessions_dirs(workspace)
    return dirs[0] if dirs else None


def list_session_files(workspace: Path) -> list[Path]:
    """Return ``*.jsonl`` files under **all** Claude Code project directories
    matching *workspace*, plus the legacy ``<workspace>/user/sessions/``
    fallback.

    Dedup rule when the same filename appears in multiple sources:
    - Among the Claude project dirs, the **most recently modified** copy
      wins.  This handles the rare case of the same session-id surfacing
      under both encoding variants — pick the live writer's version.
    - The legacy dir is only consulted for filenames absent from the
      Claude dirs.  It exists for back-compat with hook-managed mirrors
      (which haven't run since this rewrite landed, but on-disk state may
      linger).

    Result is sorted by filename for deterministic upload order.
    """
    files: dict[str, Path] = {}

    for claude_dir in find_claude_sessions_dirs(workspace):
        for f in claude_dir.glob("*.jsonl"):
            existing = files.get(f.name)
            if existing is None or f.stat().st_mtime > existing.stat().st_mtime:
                files[f.name] = f

    legacy_dir = workspace / "user" / "sessions"
    if legacy_dir.exists():
        for f in legacy_dir.glob("*.jsonl"):
            files.setdefault(f.name, f)

    return sorted(files.values(), key=lambda p: p.name)
