"""Acceptance tests for #622 Slice 2 — install-prompt overlay regression locks
+ base_sha per-file blob-sha divergence detection and badge.

Two independent pieces:

  1b. The install-prompt editor override already wins on the served /setup
      script (wired in Slice 1 via resolve_prompt). These tests LOCK that
      behavior (T1/T2) and guard the DB-free purity of resolve_lines (T3).

  2.  base_sha switches from the IWT HEAD-commit sha (Slice 1) to a per-file
      git BLOB sha, and GET /api/admin/prompts/{kind} computes a `diverged`
      flag by comparing the live blob sha to the stored base. The
      initial_workspace.sync audit gains a `diverged_prompts` param.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_git_iwt(root: Path, *, install_body: str, claude_md: str = "# WS\n") -> Path:
    """Create a real git working tree at ``root`` with an install-prompt
    template + workspace/CLAUDE.md, plus a second unrelated file so the
    HEAD-commit sha and the per-file blob sha are guaranteed to differ.
    Returns ``root`` (the IWT clone dir)."""
    (root / "install-prompt").mkdir(parents=True, exist_ok=True)
    (root / "install-prompt" / "template.md.tmpl").write_text(
        install_body, encoding="utf-8"
    )
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    # A second file in the same commit → HEAD-commit sha != any blob sha.
    (root / "README.md").write_text("# admin docs\n", encoding="utf-8")
    _git("init", "-b", "main", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-m", "initial", cwd=root)
    return root


# ===========================================================================
# Piece 1b — install-prompt overlay regression locks
# ===========================================================================


@pytest.fixture
def admin_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/auth/bootstrap",
        json={"email": "admin@example.com", "name": "A", "password": "TestPass123!"},
    )
    if resp.status_code == 403:
        pytest.skip("admin already bootstrapped")
    assert resp.status_code == 200, resp.text
    return client, resp.json()["access_token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _setup_page_context(client, token):
    """Drive the /setup page handler and return its TemplateResponse context.

    Going through the real handler (not the rendered HTML) avoids HTML-escaping
    noise and gives us the exact ``setup_script_text`` the page will embed."""
    import asyncio

    from app.web import router as web_router
    from src.db import get_system_db

    # Resolve the bootstrapped admin into the user dict the handler expects.
    conn = get_system_db()
    try:
        from src.repositories.users import UserRepository

        urow = UserRepository(conn).get_by_email("admin@example.com")
    finally:
        conn.close()
    user = {
        "id": urow["id"],
        "email": urow["email"],
        "name": urow.get("name"),
        "is_admin": True,
        "groups": ["Everyone"],
    }

    class _FakeURL:
        def __str__(self):
            return "http://testserver/"

    class _FakeRequest:
        base_url = _FakeURL()
        app = client.app

    conn2 = get_system_db()
    try:
        resp = asyncio.run(
            web_router.setup_page(_FakeRequest(), user=user, conn=conn2)
        )
    finally:
        conn2.close()
    # TemplateResponse stores the render context.
    return resp.context


def test_t1_setup_serves_install_editor_override(admin_client):
    """T1: an install editor override (source_mode='editor', non-empty content)
    is served verbatim by GET /setup. Expected to pass on Slice-1 code — this
    is a regression guard against losing the resolve_prompt wiring."""
    client, token = admin_client
    marker = "INSTALL-OVERRIDE-MARKER-T1-plain-text"
    r = client.put(
        "/api/admin/prompts/install", headers=_hdr(token), json={"content": marker}
    )
    assert r.status_code == 200, r.text

    ctx = _setup_page_context(client, token)
    assert ctx["setup_script_text"] == marker, (
        "install editor override must be served verbatim by /setup"
    )
    assert ctx["setup_instructions_lines"] == marker.split("\n")


def test_t2_setup_byte_identical_without_override(admin_client):
    """T2: with NO install override (content IS NULL, editor mode), the /setup
    script is byte-identical to compute_default_agent_prompt — the overlay is
    invisible when unset. Locks the 'git-mode/no-override stays byte-identical
    to today' requirement."""
    client, token = admin_client
    from src.db import get_system_db
    from src.welcome_template import compute_default_agent_prompt

    ctx = _setup_page_context(client, token)

    conn = get_system_db()
    try:
        from src.repositories.users import UserRepository

        urow = UserRepository(conn).get_by_email("admin@example.com")
        user = {
            "id": urow["id"],
            "email": urow["email"],
            "name": urow.get("name"),
            "is_admin": True,
            "groups": ["Everyone"],
        }
        default = compute_default_agent_prompt(
            conn, user=user, server_url="http://testserver"
        )
    finally:
        conn.close()

    assert ctx["setup_script_text"] == default, (
        "with no override, /setup must serve compute_default_agent_prompt "
        "byte-for-byte — the overlay is invisible when unset"
    )


def test_t3_resolve_lines_is_db_free(tmp_path, monkeypatch):
    """T3: resolve_lines must NOT take a `conn` param — override resolution
    stays one level up at resolve_prompt('install', conn). Guards against
    accidentally threading the DB into the pure renderer."""
    from app.web import setup_instructions

    sig = inspect.signature(setup_instructions.resolve_lines)
    assert "conn" not in sig.parameters, (
        "resolve_lines must stay DB-free — it is called from no-conn / anonymous "
        "paths; override resolution belongs at resolve_prompt one level up."
    )
    # And it must actually run with no DB / DATA_DIR available.
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "nonexistent"))
    lines = setup_instructions.resolve_lines("agnes.whl", connector_manifest=[])
    assert isinstance(lines, list) and lines


# ===========================================================================
# Piece 2 — blob_sha helper
# ===========================================================================


def _point_iwt(monkeypatch, iwt_root: Path):
    """Force is_configured()->True and the IWT snapshot dir to ``iwt_root``."""
    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)
    from app import utils as app_utils

    monkeypatch.setattr(app_utils, "get_initial_workspace_dir", lambda: iwt_root)
    # _iwt_snapshot reads get_initial_workspace_dir from the iw module namespace.
    monkeypatch.setattr(iw, "get_initial_workspace_dir", lambda: iwt_root)


def test_t4_blob_sha_happy_path_and_edges(tmp_path, monkeypatch):
    """T4: blob_sha returns the canonical `git rev-parse HEAD:<path>` blob sha;
    None for absent path, when unconfigured, and for `..` escapes."""
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    expected = _git("rev-parse", "HEAD:install-prompt/template.md.tmpl", cwd=iwt)
    assert iw.blob_sha("install-prompt/template.md.tmpl") == expected
    assert len(expected) == 40

    # absent path
    assert iw.blob_sha("install-prompt/missing.md") is None
    # `..` escape blocked by containment
    assert iw.blob_sha("../escape.txt") is None

    # not configured → None
    monkeypatch.setattr(iw, "is_configured", lambda: False)
    assert iw.blob_sha("install-prompt/template.md.tmpl") is None


# ===========================================================================
# Piece 2 — bind stamps blob sha + GET divergence
# ===========================================================================


def test_t5_bind_stamps_blob_sha(admin_client, tmp_path, monkeypatch):
    """T5: POST bind-git stores base_sha == blob_sha(git_path), NOT the HEAD
    commit sha."""
    client, token = admin_client
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    r = client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/template.md.tmpl"},
    )
    assert r.status_code == 200, r.text

    from src.repositories import welcome_template_repo

    meta = welcome_template_repo().get_meta()
    blob = _git("rev-parse", "HEAD:install-prompt/template.md.tmpl", cwd=iwt)
    head_commit = _git("rev-parse", "HEAD", cwd=iwt)
    assert meta["base_sha"] == blob
    assert meta["base_sha"] != head_commit, (
        "base_sha must be the per-file blob sha, not the HEAD commit sha"
    )


def test_t6_not_diverged_right_after_bind(admin_client, tmp_path, monkeypatch):
    """T6: immediately after bind, GET reports diverged=False and
    current_blob_sha == base_sha."""
    client, token = admin_client
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/template.md.tmpl"},
    )
    g = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()
    assert g["diverged"] is False
    assert g["current_blob_sha"] == g["base_sha"]
    assert g["current_blob_sha"] is not None


def test_t7_diverged_after_repo_edit(admin_client, tmp_path, monkeypatch):
    """T7: editing + committing the bound file in the clone makes GET report
    diverged=True and current_blob_sha != base_sha."""
    client, token = admin_client
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/template.md.tmpl"},
    )
    base = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()[
        "base_sha"
    ]

    # Simulate a sync landing new content for that file.
    (iwt / "install-prompt" / "template.md.tmpl").write_text(
        "INSTALL BODY CHANGED", encoding="utf-8"
    )
    _git("add", ".", cwd=iwt)
    _git("commit", "-m", "edit install prompt", cwd=iwt)

    g = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()
    assert g["diverged"] is True
    assert g["current_blob_sha"] != base
    assert g["current_blob_sha"] is not None


def test_t8_legacy_commit_sha_base_is_diverged(admin_client, tmp_path, monkeypatch):
    """T8: a binding stamped under Slice-1 with a 40-char COMMIT sha (!= blob
    sha) reports diverged=True — the loud default for pre-Slice-2 bindings."""
    client, token = admin_client
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    head_commit = _git("rev-parse", "HEAD", cwd=iwt)
    blob = _git("rev-parse", "HEAD:install-prompt/template.md.tmpl", cwd=iwt)
    assert head_commit != blob

    # Seed a Slice-1-style binding directly: git mode, base_sha = commit sha.
    from src.repositories import welcome_template_repo

    welcome_template_repo().bind_git(
        "install-prompt/template.md.tmpl",
        base_sha=head_commit,
        updated_by="admin@example.com",
    )

    g = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()
    assert g["source_mode"] == "git"
    assert g["base_sha"] == head_commit
    assert g["current_blob_sha"] == blob
    assert g["diverged"] is True


def test_t9_file_deleted_from_repo_is_diverged(admin_client, tmp_path, monkeypatch):
    """T9: after bind, removing the file from the clone HEAD makes GET report
    diverged=True with current_blob_sha None."""
    client, token = admin_client
    import src.initial_workspace as iw

    iwt = _make_git_iwt(tmp_path / "iwt", install_body="INSTALL BODY")
    _point_iwt(monkeypatch, iwt)

    client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/template.md.tmpl"},
    )

    _git("rm", "install-prompt/template.md.tmpl", cwd=iwt)
    _git("commit", "-m", "drop install prompt", cwd=iwt)

    g = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()
    assert g["current_blob_sha"] is None
    assert g["diverged"] is True


def test_t11_editor_mode_no_git_path_not_diverged(admin_client):
    """T11: editor mode with no git_path → diverged=False, current_blob_sha
    None (no git probe runs)."""
    client, token = admin_client
    g = client.get("/api/admin/prompts/install", headers=_hdr(token)).json()
    assert g["source_mode"] == "editor"
    assert g["git_path"] is None
    assert g["diverged"] is False
    assert g["current_blob_sha"] is None


# ===========================================================================
# Piece 2 — sync audit param
# ===========================================================================


def test_t10_sync_audit_diverged_prompts(tmp_path, monkeypatch):
    """T10: admin_sync stamps `diverged_prompts` into the
    initial_workspace.sync audit params — populated when a bound prompt's file
    moved, [] when nothing bound diverged. Driven against a real file:// repo."""
    # Build the harness identically to test_initial_workspace_api's web_client.
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "initial-workspace").mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")

    import src.db as db

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None
    db.close_system_db()

    # --- build a fake remote (bare repo) with workspace/CLAUDE.md ---
    work = tmp_path / "src-work"
    ws = work / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# Custom Workspace\nv1\n", encoding="utf-8")
    (work / "README.md").write_text("# admin docs\n", encoding="utf-8")
    _git("init", "-b", "main", cwd=work)
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)
    url = bare.resolve().as_uri()

    from app.main import create_app

    app = create_app()
    client = TestClient(app)

    # admin
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    ph = PasswordHasher()
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin", email="admin@example.com", name="Admin",
        password_hash=ph.hash("AdminPass1!"),
    )
    admin_row = conn.execute(
        "SELECT id FROM user_groups WHERE name = 'Admin'"
    ).fetchone()
    UserGroupMembersRepository(conn).add_member(
        user_id="admin", group_id=admin_row[0], source="admin",
    )
    conn.close()
    tok = client.post(
        "/auth/token", json={"email": "admin@example.com", "password": "AdminPass1!"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    from app.api.initial_workspace import _write_section

    _write_section({"url": url, "branch": "main", "token_env": None})

    # First sync — clone lands.
    r = client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 200, r.text

    # Bind the workspace prompt to its clone file → base_sha = current blob.
    rb = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=headers,
        json={"git_path": "workspace/CLAUDE.md"},
    )
    assert rb.status_code == 200, rb.text

    # --- case A: nothing changed upstream → re-sync → diverged_prompts == [] ---
    r = client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 200, r.text

    def _last_sync_audit_params():
        c = get_system_db()
        try:
            row = c.execute(
                "SELECT params FROM audit_log WHERE action = 'initial_workspace.sync' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        finally:
            c.close()
        import json as _json

        params = row[0]
        if isinstance(params, str):
            params = _json.loads(params)
        return params

    params_a = _last_sync_audit_params()
    assert "diverged_prompts" in params_a
    assert params_a["diverged_prompts"] == []

    # --- case B: upstream edits the bound file → re-sync → ['workspace'] ---
    (ws / "CLAUDE.md").write_text("# Custom Workspace\nv2 CHANGED\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "edit claude_md", cwd=work)
    _git("push", "origin", "main", cwd=work)

    r = client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 200, r.text

    params_b = _last_sync_audit_params()
    assert params_b["diverged_prompts"] == ["workspace"], params_b

    db.close_system_db()


def test_admin_prompts_page_has_divergence_badge(admin_client):
    """The /admin/prompts page ships the divergence badge slot + the re-bind
    reconcile hint so the Slice-2 JS has its DOM targets."""
    client, token = admin_client
    r = client.get("/admin/prompts", headers=_hdr(token), follow_redirects=False)
    assert r.status_code == 200, r.text
    assert 'data-role="divergence"' in r.text
    assert "prompt-badge" in r.text
    # the re-bind-to-reaccept reconcile hint
    assert "re-click" in r.text or "re-stamp" in r.text


# ===========================================================================
# Piece 2 — cross-backend signature parity (T12, cheap variant)
# ===========================================================================


def test_t12_repo_signature_parity_duckdb_pg():
    """T12: bind_git / get_meta signatures match across the DuckDB and PG repos
    for both managed-prompt kinds (mirrors Slice 1's de-vacuous parity assert)
    so the blob-sha base flows through both backends identically."""
    from src.repositories.claude_md_template import ClaudeMdTemplateRepository
    from src.repositories.claude_md_template_pg import (
        ClaudeMdTemplatePgRepository,
    )
    from src.repositories.welcome_template import WelcomeTemplateRepository
    from src.repositories.welcome_template_pg import WelcomeTemplatePgRepository

    pairs = [
        (ClaudeMdTemplateRepository, ClaudeMdTemplatePgRepository),
        (WelcomeTemplateRepository, WelcomeTemplatePgRepository),
    ]
    for duck, pg in pairs:
        for method in ("bind_git", "get_meta"):
            ds = inspect.signature(getattr(duck, method))
            ps = inspect.signature(getattr(pg, method))
            assert list(ds.parameters) == list(ps.parameters), (
                f"{duck.__name__}.{method} vs {pg.__name__}.{method} param mismatch: "
                f"{list(ds.parameters)} != {list(ps.parameters)}"
            )
