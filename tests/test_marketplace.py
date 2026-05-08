"""Tests for marketplace registry + sync.

Uses a local bare git repo as a fake remote so no network is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: local bare repo as a fake "remote"
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    # Minimal identity so commits work in CI sandboxes without global config.
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
    # git accepts file:// URLs and plain absolute paths as "URLs" for clone/fetch.
    # A file:// URL keeps things OS-agnostic.
    return path.resolve().as_uri()


@pytest.fixture
def fake_remote(tmp_path: Path):
    """Create a bare repo + seed one commit. Returns (bare_path, url, first_sha)."""
    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))
    # Wire the work tree to push back to the bare remote so we can seed
    # additional commits during tests via _add_commit().
    _git("remote", "add", "origin", str(bare), cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)

    return {"bare": bare, "work": work, "url": _file_url(bare), "sha": sha}


def _add_commit(fake_remote: dict, filename: str, content: str) -> str:
    """Add a new commit to the fake remote via the working clone + push."""
    work = fake_remote["work"]
    (work / filename).write_text(content, encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", f"add {filename}", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


# ---------------------------------------------------------------------------
# Environment — fresh DATA_DIR + fresh system.duckdb per test
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "marketplaces").mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    # Reset the shared system DB connection so it picks up the new DATA_DIR.
    import src.db as db

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None

    yield data_dir

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None


# ---------------------------------------------------------------------------
# Repository layer
# ---------------------------------------------------------------------------


def test_registry_crud(clean_env):
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        repo = MarketplaceRegistryRepository(conn)

        assert repo.list_all() == []
        assert repo.get("foo") is None

        repo.register(
            id="foo", name="Foo", url="https://example.com/foo.git",
            branch="main", token_env="FOO_TOKEN", description="demo",
            registered_by="admin@test.com",
        )
        row = repo.get("foo")
        assert row is not None
        assert row["url"] == "https://example.com/foo.git"
        assert row["branch"] == "main"
        assert row["token_env"] == "FOO_TOKEN"
        assert row["registered_by"] == "admin@test.com"
        assert row["last_synced_at"] is None

        # UPSERT: re-register with new name keeps row count at 1.
        repo.register(id="foo", name="Foo v2", url="https://example.com/foo.git")
        rows = repo.list_all()
        assert len(rows) == 1
        assert rows[0]["name"] == "Foo v2"

        from datetime import datetime, timezone
        repo.update_sync_status(
            "foo",
            commit_sha="abc123",
            synced_at=datetime.now(timezone.utc),
        )
        row = repo.get("foo")
        assert row["last_commit_sha"] == "abc123"
        assert row["last_synced_at"] is not None
        assert row["last_error"] is None

        # Error write
        repo.update_sync_status("foo", error="boom")
        assert repo.get("foo")["last_error"] == "boom"
        # Success after error clears it
        repo.update_sync_status("foo", commit_sha="def456", synced_at=datetime.now(timezone.utc))
        assert repo.get("foo")["last_error"] is None

        repo.unregister("foo")
        assert repo.get("foo") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sync_one — clone and update against local bare repo
# ---------------------------------------------------------------------------


def test_sync_one_clone_then_update(clean_env, fake_remote):
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="hello", name="Hello", url=fake_remote["url"], branch="main"
        )
    finally:
        conn.close()

    result = sync_one("hello")
    assert result["action"] == "clone"
    assert result["commit"] == fake_remote["sha"]
    target = Path(result["path"])
    assert target.is_dir()
    assert (target / "README.md").exists()

    # Registry row updated
    conn = get_system_db()
    try:
        row = MarketplaceRegistryRepository(conn).get("hello")
        assert row["last_commit_sha"] == fake_remote["sha"]
        assert row["last_error"] is None
    finally:
        conn.close()

    new_sha = _add_commit(fake_remote, "new.txt", "hello world")

    result2 = sync_one("hello")
    assert result2["action"] == "update"
    assert result2["commit"] == new_sha
    assert result2["commit"] != fake_remote["sha"]
    assert (Path(result2["path"]) / "new.txt").exists()


def test_sync_one_failure_redacts_token(clean_env, tmp_path, monkeypatch):
    """A bogus HTTPS URL + token should fail with the token redacted from the error."""
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    token = "ghp_supersecrettoken1234567890"
    monkeypatch.setenv("AGNES_MARKETPLACE_BOGUS_TOKEN", token)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="bogus",
            name="Bogus",
            # Non-routable IP + unlikely port → git fails fast without real network.
            url="https://127.0.0.1:1/does-not-exist.git",
            token_env="AGNES_MARKETPLACE_BOGUS_TOKEN",
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError) as ei:
        sync_one("bogus")

    assert token not in str(ei.value)

    conn = get_system_db()
    try:
        row = MarketplaceRegistryRepository(conn).get("bogus")
        assert row["last_error"]
        assert token not in row["last_error"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sync_marketplaces — collects errors per entry, empty registry = no-op
# ---------------------------------------------------------------------------


def test_sync_marketplaces_empty(clean_env):
    from src.marketplace import sync_marketplaces

    assert sync_marketplaces() == {"synced": [], "errors": []}


def test_sync_marketplaces_mixed(clean_env, fake_remote, monkeypatch):
    from src.db import get_system_db
    from src.marketplace import sync_marketplaces
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(os.environ["DATA_DIR"]) / "no-global"))
    conn = get_system_db()
    try:
        repo = MarketplaceRegistryRepository(conn)
        repo.register(id="good", name="Good", url=fake_remote["url"], branch="main")
        repo.register(id="bad", name="Bad", url="https://127.0.0.1:1/x.git")
    finally:
        conn.close()

    result = sync_marketplaces()
    assert len(result["synced"]) == 1
    assert result["synced"][0]["id"] == "good"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["id"] == "bad"


# ---------------------------------------------------------------------------
# URL auth helper
# ---------------------------------------------------------------------------


def test_authenticated_url():
    from src.marketplace import _authenticated_url

    # No token → identity
    assert _authenticated_url("https://example.com/x.git", "") == "https://example.com/x.git"
    # HTTPS + token → x-access-token scheme
    out = _authenticated_url("https://example.com/org/repo.git", "secret123")
    assert out == "https://x-access-token:secret123@example.com/org/repo.git"
    # With port
    out = _authenticated_url("https://host:8443/repo.git", "t")
    assert out == "https://x-access-token:t@host:8443/repo.git"
    # Non-HTTPS (file://) → unchanged
    assert _authenticated_url("file:///tmp/repo.git", "t") == "file:///tmp/repo.git"
    assert _authenticated_url("http://host/repo.git", "t") == "http://host/repo.git"


def test_is_valid_slug():
    from src.marketplace import is_valid_slug

    assert is_valid_slug("foo")
    assert is_valid_slug("foo-bar")
    assert is_valid_slug("foo_bar_99")
    assert is_valid_slug("a")
    assert not is_valid_slug("")
    assert not is_valid_slug("Foo")
    assert not is_valid_slug("../etc")
    assert not is_valid_slug("foo/bar")
    assert not is_valid_slug("-foo")
    assert not is_valid_slug("a" * 65)


# ---------------------------------------------------------------------------
# Admin API — CRUD + token persistence in .env_overlay
# ---------------------------------------------------------------------------


def test_api_create_with_token_persists_to_overlay(seeded_app, fake_remote):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    data_dir = Path(seeded_app["env"]["data_dir"])

    pat = "ghp_testsecret_abcdef1234567890"
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Hello",
            "slug": "hello",
            "url": fake_remote["url"].replace("file://", "https://") if False else "https://example.com/hello.git",
            "token": pat,
            "curator_name": "Test Curator",
            "curator_email": "curator@example.com",
        },
    )
    # URL must start with https:// per our validator — the placeholder above
    # is a plain https URL; it's only persisted, not hit by this endpoint.
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "hello"
    assert body["has_token"] is True
    assert "token" not in body  # response never echoes the secret

    overlay = (data_dir / "state" / ".env_overlay").read_text()
    assert f"AGNES_MARKETPLACE_HELLO_TOKEN={pat}" in overlay
    assert os.environ.get("AGNES_MARKETPLACE_HELLO_TOKEN") == pat

    # GET list includes it
    r = client.get("/api/marketplaces", headers=token_headers)
    assert r.status_code == 200
    entries = r.json()
    assert any(e["id"] == "hello" and e["has_token"] for e in entries)


def test_api_rejects_bad_slug_and_non_https(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}

    # Bad slug
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "../etc", "url": "https://example.com/x.git", **curator},
    )
    assert r.status_code == 400

    # Non-https URL
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "xy", "url": "http://example.com/x.git", **curator},
    )
    assert r.status_code == 400


def test_api_create_requires_curator(seeded_app):
    """v32: curator_name + curator_email are mandatory at create time.

    Three failure shapes are checked: missing both, missing email, malformed
    email. Each must surface a 400 with a curator-specific message so the
    admin form can render a useful toast.
    """
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Both missing
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "cnone", "url": "https://example.com/x.git"},
    )
    assert r.status_code == 400
    assert "curator_name" in r.text

    # Email missing
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X", "slug": "cmail", "url": "https://example.com/x.git",
            "curator_name": "Test",
        },
    )
    assert r.status_code == 400
    assert "curator_email" in r.text

    # Email malformed
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X", "slug": "cbad", "url": "https://example.com/x.git",
            "curator_name": "Test", "curator_email": "not-an-email",
        },
    )
    assert r.status_code == 400


def test_api_curator_round_trip(seeded_app):
    """Curator fields persist through create + GET list, and a PATCH edit
    updates them. Empty-string curator inputs on PATCH leave the existing
    values unchanged (per the help text on the edit modal)."""
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Curator Test", "slug": "curator-rt",
            "url": "https://example.com/x.git",
            "curator_name": "Alice Original",
            "curator_email": "alice@example.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["curator_name"] == "Alice Original"
    assert body["curator_email"] == "alice@example.com"

    # Update curator name only
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_name": "Bob New"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["curator_name"] == "Bob New"
    assert body["curator_email"] == "alice@example.com"  # unchanged

    # Empty-string PATCH leaves the value alone (not a clear)
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_name": "", "curator_email": "  "},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["curator_name"] == "Bob New"
    assert body["curator_email"] == "alice@example.com"

    # Malformed email rejected on PATCH too
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_email": "not-an-email"},
    )
    assert r.status_code == 400


def test_api_delete_clears_overlay_binding(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    pat = "ghp_another_test_token"
    client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Temp", "slug": "temp",
            "url": "https://example.com/temp.git", "token": pat,
            "curator_name": "Curator", "curator_email": "c@example.com",
        },
    )
    assert os.environ.get("AGNES_MARKETPLACE_TEMP_TOKEN") == pat

    r = client.delete("/api/marketplaces/temp?purge=false", headers=token_headers)
    assert r.status_code == 204
    assert os.environ.get("AGNES_MARKETPLACE_TEMP_TOKEN") in (None, "")


def test_refresh_plugin_cache_drops_missing_internal_assets(clean_env, monkeypatch):
    """v32 enrichment drop semantics — when agnes-metadata references files
    that don't exist on disk (or external URLs that fail to mirror), those
    entries are removed from the served metadata so the UI never shows a
    broken link / image. We exercise the missing-internal-file branch
    directly because it's deterministic without network.
    """
    from src.db import get_system_db
    from src.marketplace import _refresh_plugin_cache, is_valid_slug
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    slug = "drop-test"
    assert is_valid_slug(slug)

    # Stand up a minimal cloned marketplace tree by hand. No git involved —
    # the helper reads from disk directly.
    repo_root = clean_env / "marketplaces" / slug
    (repo_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (repo_root / "plugins" / "demo").mkdir(parents=True, exist_ok=True)

    # Real marketplace.json — single plugin
    (repo_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({
            "name": "drop-test", "owner": {"name": "T"},
            "plugins": [{
                "name": "demo", "description": "test",
                "version": "1.0", "source": "./plugins/demo",
            }],
        }),
        encoding="utf-8",
    )
    # agnes-metadata referencing a mix of valid + missing internal paths
    (repo_root / ".claude-plugin" / "agnes-metadata.json").write_text(
        json.dumps({
            "version": 1,
            "plugins": {
                "demo": {
                    # cover_photo points at a file that does NOT exist
                    "cover_photo": ".agnes/missing-cover.png",
                    "doc_links": [
                        # Internal path that exists → should survive
                        {"name": "ok-doc", "path": "docs/ok.md"},
                        # Internal path that doesn't exist → dropped
                        {"name": "missing-doc", "path": "docs/missing.md"},
                    ],
                },
            },
        }),
        encoding="utf-8",
    )
    # Create the file referenced by the surviving doc_link
    (repo_root / "docs").mkdir(exist_ok=True)
    (repo_root / "docs" / "ok.md").write_text("# ok\n", encoding="utf-8")

    # Register the marketplace so the cache write has a parent row to point at.
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=slug, name="drop test", url="https://example.com/x.git",
            curator_name="C", curator_email="c@example.com",
        )
    finally:
        conn.close()

    written = _refresh_plugin_cache(slug)
    assert written == 1

    # Assert the DB row reflects the drops:
    #   cover_photo_url is NULL (missing internal file)
    #   doc_links carries only the surviving entry (ok-doc)
    conn = get_system_db()
    try:
        row = conn.execute(
            "SELECT cover_photo_url, doc_links "
            "FROM marketplace_plugins "
            "WHERE marketplace_id = ? AND name = ?",
            [slug, "demo"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    cover_url, doc_links_json = row
    assert cover_url is None, f"missing internal cover should be dropped, got {cover_url}"

    import json as _json
    doc_links = _json.loads(doc_links_json) if isinstance(doc_links_json, str) else doc_links_json
    assert isinstance(doc_links, list) and len(doc_links) == 1
    assert doc_links[0]["name"] == "ok-doc"


def test_api_sync_endpoint(seeded_app, fake_remote):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Register marketplace pointing at our fake remote.
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Hello",
            "slug": "sync-hello",
            "url": "https://example.com/placeholder.git",  # URL in DB (not dialed here)
            "curator_name": "Curator",
            "curator_email": "c@example.com",
        },
    )
    assert r.status_code == 201

    # Patch the URL to the local file:// one. PATCH requires https://, so we
    # go around it by writing directly via the repo — simulates an admin
    # that registered then later rotated to a real URL behind a reverse proxy.
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="sync-hello", name="Hello", url=fake_remote["url"], branch="main"
        )
    finally:
        conn.close()

    r = client.post("/api/marketplaces/sync-hello/sync", headers=token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commit"] == fake_remote["sha"]
    assert body["action"] == "clone"


def test_api_sync_nonexistent_returns_404(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post("/api/marketplaces/missing/sync", headers=token_headers)
    assert r.status_code == 404


def test_api_requires_admin(seeded_app):
    client = seeded_app["client"]
    analyst_headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    r = client.get("/api/marketplaces", headers=analyst_headers)
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# delete_marketplace_dir helper
# ---------------------------------------------------------------------------


def test_delete_marketplace_dir(clean_env):
    from src.marketplace import delete_marketplace_dir
    from app.utils import get_marketplaces_dir

    target = get_marketplaces_dir() / "foo"
    target.mkdir(parents=True)
    (target / "a.txt").write_text("x")

    assert delete_marketplace_dir("foo") is True
    assert not target.exists()

    # Idempotent: deleting twice returns False, no exception
    assert delete_marketplace_dir("foo") is False

    with pytest.raises(ValueError):
        delete_marketplace_dir("../etc")
