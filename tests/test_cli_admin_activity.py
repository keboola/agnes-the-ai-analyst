"""Tests for `agnes admin activity` subcommands.

Pattern mirrors tests/test_cli_admin_news.py: monkey-patch cli.client api_*
helpers to route through a FastAPI TestClient with an admin session, then
invoke the Typer app via CliRunner.

Covers:
- timeline (default callback): success, --json, --since, --action filter, admin-only enforcement
- health: success, --json
- sync: success, --json, --since
"""

from __future__ import annotations

import json
import tempfile
import uuid

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_admin_test_client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@activity.test", name="admin")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
            "VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )
        token = create_access_token(user_id=uid, email="admin@activity.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


def _make_non_admin_test_client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="analyst@activity.test", name="analyst")
        token = create_access_token(user_id=uid, email="analyst@activity.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    """CliRunner + activity_app wired to an admin TestClient."""
    test_client = _make_admin_test_client()

    def _get(path, **kw):
        params = kw.get("params") or {}
        return test_client.get(path, params=params)

    import cli.client
    monkeypatch.setattr(cli.client, "api_get", _get)

    import cli.commands.admin_activity as mod
    monkeypatch.setattr(mod, "api_get", _get)

    return CliRunner(), mod.activity_app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    """CliRunner + activity_app wired to a non-admin TestClient."""
    test_client = _make_non_admin_test_client()

    def _get(path, **kw):
        params = kw.get("params") or {}
        return test_client.get(path, params=params)

    import cli.client
    monkeypatch.setattr(cli.client, "api_get", _get)

    import cli.commands.admin_activity as mod
    monkeypatch.setattr(mod, "api_get", _get)

    return CliRunner(), mod.activity_app


# ---------------------------------------------------------------------------
# Timeline tests
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_timeline_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, [])
        assert r.exit_code == 0, _clean(r.output)
        # Should print column headers
        out = _clean(r.output)
        assert "TIME" in out or "ACTION" in out or "rows" in out.lower() or "No activity" in out

    def test_timeline_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_timeline_since_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--since", "1h", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        # since=1h → since_minutes=60; server accepts and returns rows key
        assert "rows" in data

    def test_timeline_action_prefix_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--action", "sync.", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_timeline_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["--json"])
        assert r.exit_code != 0
        out = _clean(r.output)
        assert "auth" in out.lower() or "403" in out or "401" in out or "forbidden" in out.lower()

    def test_timeline_since_7d(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["--since", "7d", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data


# ---------------------------------------------------------------------------
# Health tests
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["health"])
        assert r.exit_code == 0, _clean(r.output)
        out = _clean(r.output)
        # Should include at least the status or one field key
        assert any(k in out for k in ("green", "yellow", "red", "scheduler", "sync_24h", "STATUS"))

    def test_health_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["health", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "status" in data
        assert data["status"] in ("green", "yellow", "red")
        assert "fields" in data
        assert "sentence" in data

    def test_health_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["health"])
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSync:
    def test_sync_success_table_output(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync"])
        assert r.exit_code == 0, _clean(r.output)
        out = _clean(r.output)
        assert "TABLE" in out or "No sync" in out or "rows" in out.lower()

    def test_sync_json_is_valid(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_sync_since_filter(self, cli_admin):
        runner, app = cli_admin
        r = runner.invoke(app, ["sync", "--since", "7d", "--json"])
        assert r.exit_code == 0, _clean(r.output)
        data = json.loads(r.output)
        assert "rows" in data

    def test_sync_admin_only(self, cli_non_admin):
        runner, app = cli_non_admin
        r = runner.invoke(app, ["sync"])
        assert r.exit_code != 0
