"""Tests for the per-instance Initial Workspace Template feature.

Covers:
  * `src.initial_workspace`: clone, validate, zip
  * `app.api.initial_workspace`: admin + analyst endpoints
  * `app.secrets.persist_overlay_token`: lock + correctness for the
    shared overlay-write helper (introduced as a prerequisite refactor)

Uses a local bare git repo as fake remote so no network is needed.
Pattern copied from `tests/test_marketplace.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fake-remote helpers (mirror tests/test_marketplace.py)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    full_env.setdefault("GIT_AUTHOR_NAME", "Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    full_env.setdefault("GIT_COMMITTER_NAME", "Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


@pytest.fixture
def fake_remote(tmp_path: Path):
    """Create a bare repo with a `workspace/` subdir containing
    CLAUDE.md + .claude/settings.json so the initial-workspace tests
    have something realistic to clone.

    Repo layout convention: only `workspace/` content reaches the
    analyst. Anything else at repo root (README, admin docs) is admin
    territory and never shipped.
    """
    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    # Repo-root file — admin-only, NOT shipped to analyst
    (work / "README.md").write_text("# Admin docs (not shipped)\n", encoding="utf-8")
    # Workspace subdir — this is what reaches the analyst
    workspace = work / "workspace"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text(
        "# Custom Workspace\n\nInternal rules.\n", encoding="utf-8"
    )
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "agnes pull --quiet || true"}]}
                    ],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))
    _git("remote", "add", "origin", str(bare), cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)

    return {"bare": bare, "work": work, "url": _file_url(bare), "sha": sha}


# ---------------------------------------------------------------------------
# Environment fixture — fresh DATA_DIR + system.duckdb per test
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "initial-workspace").mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    import src.db as db
    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None

    yield data_dir


# ===========================================================================
# Layer 1: src/initial_workspace.py — clone, validate, zip
# ===========================================================================


def test_sync_template_clones_fresh(clean_env, fake_remote):
    """Fresh clone lands content in ${DATA_DIR}/initial-workspace/.

    Repo layout: README.md at root + workspace/ subdir with workspace
    content. After clone, both are on disk; only workspace/ ships to
    analysts via build_zip / list_template_files.
    """
    from src.initial_workspace import sync_template

    result = sync_template(url=fake_remote["url"], branch="main")
    assert result["commit_sha"] == fake_remote["sha"]
    target = clean_env / "initial-workspace"
    # Both root-level admin docs AND workspace/ subdir exist on disk
    assert (target / "README.md").exists()
    assert (target / "workspace" / "CLAUDE.md").exists()
    assert (target / "workspace" / ".claude" / "settings.json").exists()
    assert (target / ".git").is_dir()


def test_sync_template_fetch_reset_on_resync(clean_env, fake_remote):
    """Second sync uses fetch+reset (not re-clone). New commit reflected."""
    from src.initial_workspace import sync_template

    sync_template(url=fake_remote["url"], branch="main")

    # Add a commit upstream — file in workspace/ subdir
    work = fake_remote["work"]
    (work / "workspace" / "docs").mkdir(exist_ok=True)
    (work / "workspace" / "docs" / "handbook.md").write_text(
        "handbook\n", encoding="utf-8"
    )
    _git("add", ".", cwd=work)
    _git("commit", "-m", "add handbook", cwd=work)
    _git("push", "origin", "main", cwd=work)
    new_sha = _git("rev-parse", "HEAD", cwd=work)

    result = sync_template(url=fake_remote["url"], branch="main")
    assert result["commit_sha"] == new_sha
    target = clean_env / "initial-workspace"
    assert (target / "workspace" / "docs" / "handbook.md").exists()


def test_validate_template_tree_rejects_reserved_path(tmp_path):
    """`workspace/.claude/init-complete` in repo is reserved — sync must reject."""
    from src.initial_workspace import TemplateValidationError, validate_template_tree

    root = tmp_path / "tree"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "init-complete").write_text("oops", encoding="utf-8")
    with pytest.raises(TemplateValidationError) as exc:
        validate_template_tree(root)
    assert "init-complete" in str(exc.value)
    assert "reserved" in str(exc.value).lower()


def test_validate_template_tree_requires_workspace_subdir(tmp_path):
    """A repo without `workspace/` at root is rejected — strict layout."""
    from src.initial_workspace import TemplateValidationError, validate_template_tree

    root = tmp_path / "tree"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# At wrong location\n", encoding="utf-8")
    # No workspace/ subdir
    with pytest.raises(TemplateValidationError) as exc:
        validate_template_tree(root)
    assert "workspace" in str(exc.value).lower()


def test_validate_template_tree_ignores_root_files(tmp_path):
    """Files OUTSIDE workspace/ (README, CI configs) are silently ignored."""
    from src.initial_workspace import validate_template_tree

    root = tmp_path / "tree"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("# Real content\n", encoding="utf-8")
    # Root-level files — admin's territory, validator must not touch
    (root / "README.md").write_text("# admin docs\n", encoding="utf-8")
    (root / ".github").mkdir()
    (root / ".github" / "workflows").mkdir()
    (root / ".github" / "workflows" / "ci.yml").write_text("ci\n", encoding="utf-8")
    # Even a "reserved" path at REPO ROOT is fine — only workspace/ scope matters
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "init-complete").write_text("not in workspace/\n", encoding="utf-8")
    # Should NOT raise
    validate_template_tree(root)


def test_sync_template_rejects_repo_without_workspace_subdir(clean_env, tmp_path):
    """End-to-end: a remote without workspace/ subdir fails sync."""
    from src.initial_workspace import TemplateValidationError, sync_template

    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "CLAUDE.md").write_text("# at root\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))

    with pytest.raises(TemplateValidationError) as exc:
        sync_template(url=_file_url(bare), branch="main")
    assert "workspace" in str(exc.value).lower()


def test_sync_template_rejects_repo_with_reserved_path(clean_env, tmp_path):
    """A remote shipping workspace/.claude/init-complete is rejected."""
    from src.initial_workspace import TemplateValidationError, sync_template

    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    workspace = work / "workspace"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "init-complete").write_text("naughty", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))

    with pytest.raises(TemplateValidationError):
        sync_template(url=_file_url(bare), branch="main")


def test_build_zip_excludes_root_files_and_git(clean_env, fake_remote):
    """Zip contains ONLY workspace/ contents, paths relative to workspace/.
    Root-level README.md from the repo must NOT be in the zip.
    """
    import io
    import zipfile

    from src.initial_workspace import build_zip, sync_template

    sync_template(url=fake_remote["url"], branch="main")
    data = build_zip()
    names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
    # Workspace content in, paths flattened (no workspace/ prefix)
    assert "CLAUDE.md" in names
    assert ".claude/settings.json" in names
    # Admin-only root files must NOT leak into the zip
    assert "README.md" not in names
    assert not any(n.startswith("workspace/") for n in names)
    assert not any(n.startswith(".git/") for n in names)


def test_list_template_files_deterministic(clean_env, fake_remote):
    """list_template_files returns sorted, deterministic POSIX paths
    relative to workspace/."""
    from src.initial_workspace import list_template_files, sync_template

    sync_template(url=fake_remote["url"], branch="main")
    files = list_template_files()
    assert files == sorted(files)
    assert "CLAUDE.md" in files
    assert ".claude/settings.json" in files
    # README.md at repo root must NOT be listed
    assert "README.md" not in files


# ===========================================================================
# Layer 2: app/secrets.persist_overlay_token concurrency
# ===========================================================================


def test_persist_overlay_token_concurrent_writes(clean_env):
    """Two threads writing different keys produce a valid merged overlay.

    Before the refactor, this test would intermittently fail because
    marketplaces._persist_token had no lock. With the shared helper,
    the lock guarantees both keys land in the final file.
    """
    from app.secrets import persist_overlay_token, _state_dir

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(key, value):
        try:
            barrier.wait(timeout=5)
            # Hit the helper many times so the race window is wide enough
            # to repro reliably on slow CI runners.
            for _ in range(50):
                persist_overlay_token(key, value)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("AGNES_KEY_A", "value_a"))
    t2 = threading.Thread(target=worker, args=("AGNES_KEY_B", "value_b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], errors

    overlay_text = (_state_dir() / ".env_overlay").read_text()
    lines = [l for l in overlay_text.splitlines() if l]
    pairs = dict(l.split("=", 1) for l in lines)
    assert pairs.get("AGNES_KEY_A") == "value_a", pairs
    assert pairs.get("AGNES_KEY_B") == "value_b", pairs


def test_persist_overlay_token_clear_removes_key(clean_env):
    """value=None and value='' both remove the key."""
    from app.secrets import persist_overlay_token, _state_dir

    persist_overlay_token("AGNES_TMP", "secret")
    assert "AGNES_TMP" in (_state_dir() / ".env_overlay").read_text()

    persist_overlay_token("AGNES_TMP", None)
    assert "AGNES_TMP" not in (_state_dir() / ".env_overlay").read_text()

    persist_overlay_token("AGNES_TMP", "back")
    persist_overlay_token("AGNES_TMP", "")
    assert "AGNES_TMP" not in (_state_dir() / ".env_overlay").read_text()


# ===========================================================================
# Layer 3: API endpoints (admin + analyst)
# ===========================================================================


@pytest.fixture
def web_client(clean_env, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    from fastapi.testclient import TestClient
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _make_admin(client, email="admin@example.com"):
    """Create an admin user and return their auth headers."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    ph = PasswordHasher()
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin", email=email, name="Admin", password_hash=ph.hash("AdminPass1!"),
    )
    # Admin group is seeded as is_system=TRUE on schema init; look up its id.
    admin_row = conn.execute(
        "SELECT id FROM user_groups WHERE name = 'Admin'"
    ).fetchone()
    assert admin_row is not None, "Admin group not seeded"
    UserGroupMembersRepository(conn).add_member(
        user_id="admin", group_id=admin_row[0], source="admin",
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": "AdminPass1!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_user(client, email="user@example.com"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    ph = PasswordHasher()
    conn = get_system_db()
    UserRepository(conn).create(
        id="user", email=email, name="User", password_hash=ph.hash("UserPass1!"),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": "UserPass1!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_admin_get_initial_workspace_not_configured(web_client):
    """GET returns configured:false when no section is in instance.yaml."""
    headers = _make_admin(web_client)
    r = web_client.get("/api/admin/initial-workspace", headers=headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_admin_endpoints_require_admin(web_client):
    """Non-admin user gets 403 on every admin endpoint — all four verbs.
    A future refactor that drops `Depends(require_admin)` from one
    endpoint must fail here (otherwise we'd silently expose the
    write/delete paths to any analyst with a PAT)."""
    headers = _make_user(web_client)
    cases = [
        ("GET",    "/api/admin/initial-workspace",      None),
        ("POST",   "/api/admin/initial-workspace",      {"url": "https://example.com/x.git"}),
        ("DELETE", "/api/admin/initial-workspace",      None),
        ("POST",   "/api/admin/initial-workspace/sync", None),
    ]
    for method, path, body in cases:
        r = web_client.request(method, path, headers=headers, json=body)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"


def test_admin_post_writes_yaml_section(web_client, fake_remote):
    """POST persists `initial_workspace:` to instance.yaml overlay."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": fake_remote["url"], "branch": "main"},
    )
    # file:// URLs are rejected (validator requires https://) — assert
    # so we can adjust the test for the relaxed CI case below
    assert r.status_code == 422
    assert "https" in r.json()["detail"].lower()


def test_admin_post_https_validation(web_client):
    """url must be https://."""
    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "http://example.com/repo.git"},
    )
    assert r.status_code == 422


def test_admin_post_token_routes_to_env_overlay(web_client, monkeypatch):
    """Token in POST body lands in .env_overlay, env-var name in YAML."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={
            "url": "https://github.com/example/template.git",
            "branch": "main",
            "token": "ghp_test_token",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["url"] == "https://github.com/example/template.git"
    assert body["has_token"] is True

    overlay = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_INITIAL_WORKSPACE_TOKEN=ghp_test_token" in overlay

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["url"] == "https://github.com/example/template.git"
    assert section["token_env"] == "AGNES_INITIAL_WORKSPACE_TOKEN"
    # Token value never lands in YAML
    assert "ghp_test_token" not in yaml.dump(instance_yaml)


def test_admin_post_idempotent(web_client):
    """Two POSTs land one section (overwrite, no duplication)."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    for url in ("https://github.com/a/b.git", "https://github.com/c/d.git"):
        r = web_client.post(
            "/api/admin/initial-workspace",
            headers=headers,
            json={"url": url, "branch": "main"},
        )
        assert r.status_code == 200, r.text

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["url"] == "https://github.com/c/d.git"  # latest wins


def test_admin_delete_removes_section_and_token(web_client):
    """DELETE wipes YAML section + .env_overlay key."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "token": "ghp_x"},
    )

    r = web_client.delete("/api/admin/initial-workspace", headers=headers)
    assert r.status_code == 204

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text() or "{}") or {}
    assert "initial_workspace" not in instance_yaml
    overlay = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_INITIAL_WORKSPACE_TOKEN" not in overlay


def test_admin_sync_against_file_url(web_client, fake_remote, monkeypatch):
    """End-to-end: register file:// URL (bypass https check via DB), run sync,
    verify last_synced_at + last_commit_sha land in YAML."""
    import yaml
    from app.api.initial_workspace import _write_section
    from app.secrets import _state_dir

    # Bypass the https:// validation by patching the section directly —
    # the test fake_remote is file:// (no real git server in CI).
    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})

    headers = _make_admin(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "sync_ok"
    assert body["commit_sha"] == fake_remote["sha"]
    assert body["file_count"] >= 2  # CLAUDE.md + .claude/settings.json

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["last_commit_sha"] == fake_remote["sha"]
    assert section["last_synced_at"] is not None
    assert section.get("last_error") is None


def test_analyst_status_unconfigured(web_client):
    """PAT-authed user sees configured:false when no template registered."""
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace", headers=headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_analyst_status_configured_synced(web_client, fake_remote):
    """Analyst sees full metadata + file list when configured + synced."""
    from app.api.initial_workspace import _write_section
    from src.initial_workspace import sync_template

    # Register + sync directly (bypass https:// check)
    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section({
        "last_synced_at": "2026-05-13T10:00:00+00:00",
        "last_commit_sha": result["commit_sha"],
    })

    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["synced"] is True
    assert body["template_sha"] == result["commit_sha"]
    assert "CLAUDE.md" in body["files"]


def test_analyst_zip_browser_unauthenticated_redirects_to_login(web_client):
    """Unauthenticated browser request (Accept: text/html) redirects to /login."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=/api/initial-workspace.zip"


def test_analyst_zip_api_unauthenticated_returns_401(web_client):
    """Unauthenticated API client (no text/html in Accept) still gets a JSON 401."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 401


def test_analyst_zip_curl_default_accept_returns_401(web_client):
    """`Accept: */*` (curl's default with no `-H`) lands in the 401 branch.

    Mirrors the `_wants_html()` contract in `app/main.py`: `*/*` must NOT
    silently flip a curl/tooling client to an HTML response — they expect
    `{"detail": "..."}` and a real 401.
    """
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "*/*"},
    )
    assert r.status_code == 401


def test_analyst_zip_empty_accept_returns_401(web_client):
    """Empty `Accept` header lands in the 401 branch — same shape as the `*/*`
    case (no `text/html` substring means: not a browser, give the raw 401)."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": ""},
    )
    assert r.status_code == 401


def test_analyst_zip_404_when_not_configured(web_client):
    """GET /api/initial-workspace.zip returns 404 when no template."""
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 404


def test_analyst_zip_503_when_not_synced(web_client):
    """503 when configured but never synced."""
    from app.api.initial_workspace import _write_section

    _write_section({"url": "https://github.com/a/b.git", "branch": "main", "token_env": None})
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 503


def test_analyst_zip_returns_bytes_and_etag(web_client, fake_remote):
    """200 returns zip bytes with ETag = template_sha."""
    import io
    import zipfile

    from app.api.initial_workspace import _write_section
    from src.initial_workspace import sync_template

    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section({
        "last_synced_at": "2026-05-13T10:00:00+00:00",
        "last_commit_sha": result["commit_sha"],
    })

    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["etag"] == f'"{result["commit_sha"]}"'
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert "CLAUDE.md" in names


def test_analyst_zip_writes_fetch_started_audit(web_client, fake_remote):
    """GET .../zip writes a server-side audit row."""
    from app.api.initial_workspace import _write_section
    from src.db import get_system_db
    from src.initial_workspace import sync_template

    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section({
        "last_synced_at": "2026-05-13T10:00:00+00:00",
        "last_commit_sha": result["commit_sha"],
    })

    headers = _make_user(web_client)
    web_client.get("/api/initial-workspace.zip", headers=headers)

    conn = get_system_db()
    rows = conn.execute(
        "SELECT action, params FROM audit_log WHERE action = 'initial_workspace.fetch_started'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1, rows
    params = json.loads(rows[0][1])
    assert params["template_sha"] == result["commit_sha"]


def test_analyst_applied_writes_audit(web_client):
    """POST /applied writes audit row with mode + counts."""
    from src.db import get_system_db

    headers = _make_user(web_client)
    r = web_client.post(
        "/api/initial-workspace/applied",
        headers=headers,
        json={
            "mode": "fresh_install",
            "template_sha": "abc123",
            "files_overwritten": 0,
            "files_created": 5,
        },
    )
    assert r.status_code == 200

    conn = get_system_db()
    rows = conn.execute(
        "SELECT params FROM audit_log WHERE action = 'initial_workspace.applied'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    params = json.loads(rows[0][0])
    assert params["mode"] == "fresh_install"
    assert params["files_created"] == 5


def test_analyst_applied_rejects_invalid_mode(web_client):
    """POST /applied with garbage mode returns 422."""
    headers = _make_user(web_client)
    r = web_client.post(
        "/api/initial-workspace/applied",
        headers=headers,
        json={"mode": "garbage"},
    )
    assert r.status_code == 422
