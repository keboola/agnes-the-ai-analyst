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
- If ``capture-session`` runs first, ``mark-private`` removes the entry
  from the queue AND adds the ID to the list. ``push`` also re-checks
  the list before each upload as a third-layer safety net.

File format: one session_id per line, UTF-8, LF terminators. The file
is never rewritten — only appended to (with dedup on add). The growth
rate is one line per privately-marked session, so manual cleanup (if
ever needed) is fine.
"""

from __future__ import annotations

from pathlib import Path


_PRIVATE_LIST_FILENAME = "agnes-sessions-private.txt"


def _claude_dir(workspace: Path) -> Path:
    d = workspace / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def private_list_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _PRIVATE_LIST_FILENAME


def read_all_private(workspace: Path) -> set[str]:
    """Return all private session IDs as a set for O(1) membership checks.

    Returns an empty set if the file doesn't exist. Whitespace-only lines
    are skipped.
    """
    path = private_list_path(workspace)
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s:
            out.add(s)
    return out


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
    with open(private_list_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)
    return True
