"""Tests for WorkdirManager — per-user workspace + per-session dir + reinit.

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents (same pattern as
tests/test_chat_persistence.py) are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""

from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.persistence import ChatRepository
from app.chat.workdir import WorkdirManager


@pytest.fixture
def workdir_mgr(tmp_path: Path) -> WorkdirManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,  # no override template
    )


def test_user_workspace_path_isolated(workdir_mgr: WorkdirManager):
    a = workdir_mgr.user_workspace("a@x")
    b = workdir_mgr.user_workspace("b@x")
    assert a != b
    assert a.name == "workspace"
    assert b.name == "workspace"


def test_ensure_user_workdir_initializes_once(workdir_mgr: WorkdirManager, tmp_path: Path):
    ws = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"
    assert (ws / ".claude/init-complete").exists()
    # second call is a no-op (marketplace SHA unchanged, sentinel present)
    (ws / "CLAUDE.md").write_text("edited")
    ws2 = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws2 / "CLAUDE.md").read_text() == "edited"  # not clobbered


def test_needs_reinit_on_marketplace_sha_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    assert workdir_mgr.needs_reinit("u@x") is False
    workdir_mgr._get_marketplace_sha = lambda: "mkt-sha-2"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_needs_reinit_on_agnes_version_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    workdir_mgr._agnes_version = "0.56.0"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_session_dir_creates_subtree(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_abc")
    assert sdir.is_dir()
    assert sdir.name == "chat_abc"
    # sessions sit under <user>/sessions/<chat_id>/
    assert sdir.parent.name == "sessions"


def test_regular_session_includes_claude_local_md(workdir_mgr: WorkdirManager):
    """Regression: a regular per-user session must symlink the analyst's
    personal CLAUDE.local.md by default (include_personal_override defaults
    to True). Co-sessions exclude it via prepare_ephemeral_session_dir."""
    workdir_mgr.ensure_user_workdir("u@x")
    ws = workdir_mgr.user_workspace("u@x")
    (ws / "CLAUDE.local.md").write_text("# personal override\n")

    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_local")

    link = sdir / "CLAUDE.local.md"
    assert link.exists(), "regular session must include CLAUDE.local.md"
    assert link.is_symlink()
    assert link.resolve() == (ws / "CLAUDE.local.md").resolve()


def test_prepare_session_dir_materializes_profile(workdir_mgr: WorkdirManager):
    """An authoring profile overrides the session CLAUDE.md with its persona
    and injects a read-only knowledge skill — WITHOUT mutating the shared
    workspace (.claude is copied, not symlinked-through)."""
    from app.chat.profiles import get_profile

    workdir_mgr.ensure_user_workdir("admin@x")
    sdir = workdir_mgr.prepare_session_dir("admin@x", "chat_prof", profile=get_profile("data-package-builder"))

    claude_md = (sdir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Data Package Builder" in claude_md  # profile persona, not "default"
    assert not (sdir / "CLAUDE.md").is_symlink()
    skill = sdir / ".claude" / "skills" / "agnes-data-package" / "SKILL.md"
    assert skill.exists()
    assert "data_packages" in skill.read_text(encoding="utf-8")
    # the shared workspace must NOT have gained the profile skill
    ws = workdir_mgr.user_workspace("admin@x")
    assert not (ws / ".claude" / "skills" / "agnes-data-package").exists()


def test_prepare_session_dir_without_profile_symlinks_claude_md(workdir_mgr: WorkdirManager):
    """Regression: with no profile the session still symlinks the workspace
    CLAUDE.md (unchanged behaviour)."""
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_noprof")
    assert (sdir / "CLAUDE.md").is_symlink()


def test_purge_user_removes_root(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    n = workdir_mgr.purge_user("u@x")
    assert n >= 2
    assert not workdir_mgr.user_workspace("u@x").exists()
    assert workdir_mgr._repo.get_workdir("u@x") is None


# ---------------------------------------------------------------------------
# render_workspace_prompt hook — sandbox CLAUDE.md == server-rendered analyst
# prompt (admin Workspace Prompt / default), matching a laptop `agnes init`.
# ---------------------------------------------------------------------------


def _mgr_with_render(tmp_path: Path, render) -> WorkdirManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,
        render_workspace_prompt=render,
    )


def test_run_init_overwrites_claude_md_with_rendered_prompt(tmp_path: Path):
    seen: list[str] = []

    def render(email: str):
        seen.append(email)
        return f"# Rendered for {email}"

    ws = _mgr_with_render(tmp_path, render).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "# Rendered for u@x"
    assert seen == ["u@x"]  # called with the user's email for RBAC context


def test_run_init_keeps_static_claude_md_when_render_returns_none(tmp_path: Path):
    ws = _mgr_with_render(tmp_path, lambda email: None).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"


def test_run_init_keeps_static_claude_md_when_render_raises(tmp_path: Path):
    def boom(email: str):
        raise RuntimeError("render failed")

    # Must not propagate — best-effort, static CLAUDE.md stays.
    ws = _mgr_with_render(tmp_path, boom).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"


def test_run_init_git_template_keeps_repo_claude_md_not_rendered(tmp_path: Path):
    """Override mode: when an admin git initial-workspace template is active,
    the repo's CLAUDE.md is authoritative (verbatim) — the Workspace Prompt
    render must NOT overwrite it. Mirrors `agnes init`, which skips
    /api/welcome in override mode. (The two are mutually exclusive by design.)
    """
    import io
    import zipfile

    from src.initial_workspace import TemplateStatus

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CLAUDE.md", "# FROM GIT REPO")
        zf.writestr(".claude/settings.json", "{}")
    zip_bytes = buf.getvalue()

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.59.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: TemplateStatus(
            configured=True,
            synced=True,
            template_source="git",
            template_sha="abc",
        ),
        fetch_template_zip=lambda: zip_bytes,
        render_workspace_prompt=lambda email: "RENDERED WORKSPACE PROMPT",  # must NOT win
    )
    ws = mgr.ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "# FROM GIT REPO"
