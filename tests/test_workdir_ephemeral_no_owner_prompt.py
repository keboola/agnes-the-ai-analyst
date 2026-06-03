"""FIX 4: prepare_ephemeral_session_dir must NOT render owner-scoped CLAUDE.md.

If the operator's render_workspace_prompt template iterates {{tables}},
an owner-only table name must NOT appear in the co-session CLAUDE.md.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema
from app.chat.persistence import ChatRepository
from app.chat.workdir import WorkdirManager


def _make_repo(tmp_path: Path) -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def _make_workdir_mgr(
    tmp_path: Path, repo: ChatRepository, *, render_fn=None
) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("bundled")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
        render_workspace_prompt=render_fn,
    )


def test_ephemeral_dir_does_not_render_owner_workspace_prompt(tmp_path):
    """render_workspace_prompt is NOT called for the ephemeral co-drive path.

    The fixture uses a render function that emits OWNER_ONLY_TABLE_SECRET
    so any call to it would leave a detectable trace in the CLAUDE.md.
    After prepare_ephemeral_session_dir, the CLAUDE.md must NOT contain
    that token.
    """
    SECRET = "OWNER_ONLY_TABLE_SECRET"

    render_calls: list[str] = []

    def spy_render(email: str) -> str:
        render_calls.append(email)
        # Simulate a template that leaks owner-specific table names
        return f"# Workspace\n\n{SECRET}\n\nTable: owner_private_table\n"

    repo = _make_repo(tmp_path)
    mgr = _make_workdir_mgr(tmp_path, repo, render_fn=spy_render)

    session_dir = mgr.prepare_ephemeral_session_dir(
        chat_id="chat_test",
        participant_emails=["owner@x.com", "collab@x.com"],
        intersection={},
    )

    claude_md = (session_dir / "CLAUDE.md").read_text()

    # The owner-scoped template content must NOT appear
    assert SECRET not in claude_md, (
        f"Owner-scoped workspace prompt leaked into co-session CLAUDE.md. "
        f"Content: {claude_md!r}"
    )
    # Fallback content used
    assert "Co-drive session" in claude_md or claude_md.strip()

    # render_workspace_prompt should NOT have been called for the ephemeral path
    assert render_calls == [], (
        f"render_workspace_prompt was called with: {render_calls!r}; "
        "should not be invoked for ephemeral co-sessions"
    )


def test_ephemeral_dir_uses_static_codrive_header(tmp_path):
    """Without a render_workspace_prompt, CLAUDE.md is the static header."""
    repo = _make_repo(tmp_path)
    mgr = _make_workdir_mgr(tmp_path, repo, render_fn=None)

    session_dir = mgr.prepare_ephemeral_session_dir(
        chat_id="chat_test2",
        participant_emails=["a@x.com", "b@x.com"],
        intersection={},
    )
    claude_md = (session_dir / "CLAUDE.md").read_text()
    assert "Co-drive session" in claude_md
