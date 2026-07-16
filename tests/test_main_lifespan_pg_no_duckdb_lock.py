"""Regression net for the production incident that motivated the
``_GRANDFATHERED_GET_SYSTEM_DB`` cleanup in ``tests/test_backend_split_guard.py``:
a request-serving process held a persistent exclusive OS-level lock on
``system.duckdb`` even though the instance was configured with
``database.backend: cloud`` (Postgres) — traced to unconditional
``get_system_db()`` calls inside ``app/main.py``'s boot sequence
(``lifespan()``).

Full end-to-end lifespan execution against a live Postgres engine is out of
scope for the unit suite, so this locks in the two boot-time call sites via a
source-level check: both must sit behind a ``use_pg()`` guard so a future
edit can't silently reintroduce the unconditional call. This mirrors the
existing static-scan pattern used by
``test_no_real_secret_in_sandbox_spawn_env`` in the guard test module.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "app" / "main.py"


def _lifespan_source() -> str:
    tree = ast.parse(MAIN_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            seg = ast.get_source_segment(MAIN_PY.read_text(), node)
            assert seg is not None
            return seg
    raise AssertionError("expected an async def lifespan(...) in app/main.py")


def test_fts_index_rebuild_gated_on_use_pg():
    """The boot-time FTS BM25 index rebuild (issue #121) is DuckDB-only —
    it must not force-open the process-singleton DuckDB connection on a
    Postgres-backed instance."""
    seg = _lifespan_source()
    idx = seg.index("ensure_knowledge_fts_index(get_system_db())")
    preceding = seg[:idx]
    # The nearest preceding `if` guard on that line's block must test use_pg().
    guard_idx = preceding.rfind("if not _use_pg_fts()")
    assert guard_idx != -1, (
        "ensure_knowledge_fts_index(get_system_db()) must be gated behind "
        "`if not use_pg():` so it never runs on a Postgres-backed instance"
    )
    # No unrelated `get_system_db()` call may sit between the guard and the
    # FTS call (that would defeat the gate).
    between = preceding[guard_idx:]
    assert "get_system_db()" not in between


def test_chat_repo_conn_gated_on_use_pg():
    """ChatRepository delegates every method to the Postgres *_pg repos when
    use_pg() is true — the DuckDB conn must only be opened on the DuckDB
    path."""
    seg = _lifespan_source()
    idx = seg.index("app.state.chat_repo = ChatRepository(_chat_conn)")
    preceding = seg[:idx]
    guard_idx = preceding.rfind("if _use_pg_chat():")
    assert guard_idx != -1, (
        "ChatRepository(_chat_conn) construction must branch on use_pg() so "
        "_chat_conn is None (no get_system_db() call) on the Postgres backend"
    )


def _nested_function_source(name: str) -> str:
    """Source of a function nested inside lifespan() (e.g. the CHAT-INIT
    closures `_fetch_local_template_zip` / `_render_workspace_prompt`)."""
    src = MAIN_PY.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            for inner in ast.walk(node):
                if isinstance(inner, ast.FunctionDef) and inner.name == name:
                    seg = ast.get_source_segment(src, inner)
                    assert seg is not None
                    return seg
    raise AssertionError(f"expected a nested def {name}(...) inside lifespan()")


def test_fetch_local_template_zip_gated_on_use_pg():
    """`_fetch_local_template_zip` must not open `get_system_db()` on the
    Postgres backend — `build_zip(None, resolve_overlay=True)` preserves the
    admin CLAUDE.md overlay without a DuckDB connection."""
    seg = _nested_function_source("_fetch_local_template_zip")
    assert "if use_pg():" in seg
    assert "build_zip(None, resolve_overlay=True)" in seg
    pg_branch = seg[seg.index("if use_pg():") :]
    duckdb_branch_idx = pg_branch.index("get_system_db()")
    # get_system_db() must only appear AFTER the use_pg() branch has
    # returned — i.e. on the `else` (DuckDB) path.
    assert "return build_zip(None, resolve_overlay=True)" in pg_branch[:duckdb_branch_idx]


def test_render_workspace_prompt_never_opens_duckdb():
    """`_render_workspace_prompt` must not call `get_system_db()` at all —
    `render_claude_md` resolves everything through the backend-aware
    factory regardless of `conn`, so opening a DuckDB connection here was
    pure overhead (and a force-open on the Postgres backend)."""
    seg = _nested_function_source("_render_workspace_prompt")
    assert "get_system_db()" not in seg
    assert "render_claude_md(None, user=u, server_url=_server_url)" in seg
