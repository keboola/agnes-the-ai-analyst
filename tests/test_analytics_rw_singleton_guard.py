"""Static guard: nothing outside `src/db.py` may call `get_analytics_db()`.

`get_analytics_db()` (`src/db.py`) is a process-wide **read-write** DuckDB
singleton that nothing closes except `close_analytics_db()` /
`close_singleton_connections()` at shutdown. While it is open, DuckDB
refuses to open a second connection to the same file with a different
configuration — so every subsequent `get_analytics_db_readonly()` call in
the process raises "Can't open a connection to same database file with a
different configuration than existing connections" until restart.

That is exactly the outage a per-table MCP endpoint caused by reusing this
singleton to dodge the read-only/read-write conflict; the fix moved it onto
`get_analytics_db_readonly()` (see
`tests/test_analytics_db_singleton.py::TestReadonlyOnFreshDataDir`), which
closed the surface — but the constraint was never enforced, only implicit.
A future request-path caller would silently reintroduce the same
process-wide outage.

This is a ratchet in the spirit of `tests/test_backend_split_guard.py`: it
scans production code for call sites and fails the build the moment a new
one appears outside `src/db.py` itself (the only file allowed to own the
singleton's write lifecycle).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "cli", "services", "src", "connectors")

# The only file allowed to reference `get_analytics_db()` as a call target —
# its own definition. Every other production file must go through
# `get_analytics_db_readonly()` instead.
_ALLOWED_CALLERS: frozenset[str] = frozenset({"src/db.py"})


def _rel(p: Path) -> str:
    p = p.resolve()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _production_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            rp = p.as_posix()
            if "/tests/" in rp:
                continue
            out.append(p)
    return out


def scan_get_analytics_db_callers(files) -> dict[str, int]:
    """``{relpath: call_count}`` for ``get_analytics_db()`` call sites.

    Matches both the bare-name form (``get_analytics_db()``) and the
    attribute form (``db.get_analytics_db()`` / ``src.db.get_analytics_db()``)
    so an import-aliased call site can't dodge the guard.
    """
    found: dict[str, int] = {}
    for p in files:
        p = Path(p)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "get_analytics_db":
                count += 1
            elif isinstance(fn, ast.Attribute) and fn.attr == "get_analytics_db":
                count += 1
        if count:
            found[_rel(p)] = count
    return found


def test_no_get_analytics_db_callers_outside_db_module():
    """`get_analytics_db()` (the read-write singleton) must have exactly one
    call site: its own definition file. Any other caller reintroduces the
    process-wide `get_analytics_db_readonly()` poisoning outage — route
    through `get_analytics_db_readonly()` instead."""
    found = scan_get_analytics_db_callers(_production_files())
    new = {f: n for f, n in found.items() if f not in _ALLOWED_CALLERS}
    assert not new, (
        "New get_analytics_db() call site(s) detected outside src/db.py — this "
        "is a process-wide read-write singleton; holding it open poisons every "
        "subsequent get_analytics_db_readonly() call for the life of the "
        "process. Use get_analytics_db_readonly() instead:\n"
        + "\n".join(f"  {f}: {n} call(s)" for f, n in sorted(new.items()))
    )


def test_detector_flags_a_planted_violation(tmp_path):
    """A synthetic module that calls `get_analytics_db()` must be flagged —
    guards against the detector silently matching nothing."""
    planted = tmp_path / "planted.py"
    planted.write_text("from src.db import get_analytics_db\ndef handler():\n    return get_analytics_db()\n")
    found = scan_get_analytics_db_callers([planted])
    assert found.get(_rel(planted)) == 1, "detector failed to flag a planted get_analytics_db() call"


def test_detector_flags_attribute_form(tmp_path):
    """The `db.get_analytics_db()` / module-attribute call form must also be
    flagged — a caller can't dodge the guard by importing the module instead
    of the name."""
    planted = tmp_path / "planted_attr.py"
    planted.write_text("from src import db\ndef handler():\n    return db.get_analytics_db()\n")
    found = scan_get_analytics_db_callers([planted])
    assert found.get(_rel(planted)) == 1, "detector failed to flag a planted db.get_analytics_db() call"


def test_detector_ignores_readonly_variant(tmp_path):
    """`get_analytics_db_readonly()` — the sanctioned per-call accessor —
    must NOT be flagged."""
    clean = tmp_path / "clean.py"
    clean.write_text(
        "from src.db import get_analytics_db_readonly\ndef handler():\n    return get_analytics_db_readonly()\n"
    )
    found = scan_get_analytics_db_callers([clean])
    assert not found, f"get_analytics_db_readonly() wrongly flagged: {found}"
