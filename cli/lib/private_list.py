"""Private session list — authoritative state for "do not upload".

A session is "private" iff its session_id appears (as a line) in
``<workspace>/.claude/agnes-sessions-private.txt``. The list is the single
source of truth — both ``agnes capture-session`` (queue writer) and
``agnes push`` (queue reader) consult it and skip private session IDs.

By making the list authoritative rather than treating queue removal as
authoritative, the slash-command / SessionStart-hook race condition is
impossible by construction:

- If ``mark-private`` writes the ID before ``capture-session`` runs,
  ``capture-session`` sees it on the list and skips the queue write.
- If ``capture-session`` runs first, ``mark-private`` adds the ID to the
  list. ``push`` re-checks the list before each upload — the list itself
  is the source of truth, queue removal is incidental.

File format: one session_id per line, UTF-8, LF terminators. The file
is never rewritten — only appended to (with dedup on add). The growth
rate is one line per privately-marked session, so manual cleanup (if
ever needed) is fine.
"""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock


_PRIVATE_LIST_FILENAME = "agnes-sessions-private.txt"


def _claude_dir_writable(workspace: Path) -> Path:
    """Return the workspace ``.claude/`` dir, creating it if needed.

    Use ONLY from write paths (``add_private``). Read paths must use
    ``_claude_dir_readonly`` so ``agnes statusline`` doesn't materialize
    ``.claude/`` directories in arbitrary working dirs (it gets called on
    every prompt redraw, and the user may be in `~/` or another non-Agnes
    directory at the time).
    """
    d = workspace / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claude_dir_readonly(workspace: Path) -> Path:
    """Return the workspace ``.claude/`` path WITHOUT mkdir.

    Read paths can compose this with ``Path.exists()`` to short-circuit
    cleanly when the directory doesn't exist — e.g. the user opened
    Claude Code in a non-Agnes directory; the statusline still calls
    ``is_private`` once per redraw and creating a ``.claude/`` shadow
    in every directory the user opens is hostile.
    """
    return workspace / ".claude"


def private_list_path(workspace: Path, *, writable: bool = False) -> Path:
    """Path to the private-session list.

    ``writable=True`` ensures the parent ``.claude/`` exists (use from
    ``add_private``); the read default keeps the function side-effect-
    free so ``statusline`` and other hot-path readers don't churn the
    filesystem.
    """
    parent = _claude_dir_writable(workspace) if writable else _claude_dir_readonly(workspace)
    return parent / _PRIVATE_LIST_FILENAME


# ---------------------------------------------------------------------------
# mtime-keyed read cache. ``agnes statusline`` is documented to run once per
# Claude Code prompt redraw (sub-second on an active session). The private
# list is append-only, so mtime is sufficient cache-keying: if the file's
# mtime hasn't moved, the cached set is still accurate. Cache keyed per
# absolute file path so multiple workspaces in the same process don't
# share state.
#
# The cache is process-local. statusline is invoked as a fresh subprocess
# per redraw (Claude Code's ``statusLine`` setting spawns the configured
# command), so the cache doesn't help the per-redraw path on its own —
# subprocess spawn cost dominates. What it DOES help is in-process callers
# (push doing one mtime stat per upload candidate, ``agnes diagnose``
# scanning workspaces) and prevents the previous behaviour of calling
# ``mkdir`` on every read (S2.7 from the PR review), which polluted
# arbitrary directories with stray ``.claude/`` shadows.
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, set[str]]] = {}
_CACHE_LOCK = Lock()


def _read_cached(path: Path) -> set[str]:
    key = os.fspath(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # File doesn't exist (or unreadable) — drop any cached entry and
        # return empty. Don't raise; statusline must never paint errors.
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return set()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    # Cache miss / stale — read outside the lock so concurrent readers
    # don't serialize on disk I/O. Last-writer-wins on the cache write
    # is fine: both readers will store the same (mtime, set) pair.
    out: set[str] = set()
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s:
                out.add(s)
    except OSError:
        return set()
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, out)
    return out


def read_all_private(workspace: Path) -> set[str]:
    """Return all private session IDs as a set for O(1) membership checks.

    Returns an empty set if the file doesn't exist. Whitespace-only lines
    are skipped. Cached by file mtime — repeated calls within the same
    process return the cached set when the file hasn't changed.
    """
    path = private_list_path(workspace)
    return _read_cached(path)


def is_private(workspace: Path, session_id: str) -> bool:
    """True iff ``session_id`` is on the private list. Empty session_id → False."""
    if not session_id:
        return False
    return session_id in read_all_private(workspace)


def add_private(workspace: Path, session_id: str) -> bool:
    """Append ``session_id`` to the private list if not already present.

    Returns True if the entry was added, False if it was already present
    (idempotent re-mark). Empty session_id → no-op, returns False.
    """
    if not session_id:
        return False
    if is_private(workspace, session_id):
        return False
    line = session_id.rstrip("\n") + "\n"
    with open(private_list_path(workspace, writable=True), "a", encoding="utf-8") as f:
        f.write(line)
    # Explicit cache eviction prevents a sub-second window where add+
    # is_private from the same process disagrees (mtime granularity on
    # some filesystems is 1s).
    with _CACHE_LOCK:
        _CACHE.pop(os.fspath(private_list_path(workspace)), None)
    return True
