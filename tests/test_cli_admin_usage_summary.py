"""CLI: agnes admin telemetry summary (#410).

Routes the Typer command through a FastAPI TestClient with a seeded
audit_log, then asserts the rendered query-telemetry table + the --json mode.
Pattern mirrors tests/test_cli_admin_usage_reprocess.py.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _seed_query_audit(conn):
    now = datetime.now(timezone.utc)
    rows = [
        ("query.remote", "table:kbc.orders", {"bytes_scanned": 100}, now - timedelta(hours=1)),
        ("query.remote", "table:kbc.orders", {"bytes_scanned": 200}, now - timedelta(hours=2)),
        ("query.local", "table:kbc.orders", {}, now - timedelta(hours=3)),
        ("query.remote", "table:kbc.sessions", {"bytes_scanned": 50}, now - timedelta(hours=4)),
    ]
    for action, resource, params, ts in rows:
        conn.execute(
            """INSERT INTO audit_log (id, timestamp, user_id, action, resource, params, result)
               VALUES (?, ?, 'u1', ?, ?, ?, 'success')""",
            [str(uuid.uuid4()), ts, action, resource, json.dumps(params)],
        )


def _make_test_client_and_token(fresh_db, *, admin: bool, seed: bool):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        email = f"{'admin' if admin else 'analyst'}@summary.test"
        UserRepository(conn).create(id=uid, email=email, name="Test User")
        if admin:
            gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
            conn.execute(
                "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                "VALUES (?, ?, 'admin', 'test')",
                [uid, gid[0]],
            )
        if seed:
            _seed_query_audit(conn)
        token = create_access_token(user_id=uid, email=email)
        return TestClient(app), token
    except Exception:
        conn.close()
        close_system_db()
        raise


def _make_get_client_factory(test_client, token_cookie):
    class _FakeClient:
        def get(self, path, params=None, **kwargs):
            return test_client.get(path, params=params or {}, cookies={"access_token": token_cookie})

    def _factory(timeout=30.0):
        return _FakeClient()

    return _factory


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token = _make_test_client_and_token(fresh_db, admin=True, seed=True)
    close_system_db()
    monkeypatch.setattr(mod, "get_client", _make_get_client_factory(test_client, token))
    return CliRunner(), mod.app


@pytest.fixture
def cli_admin_empty(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token = _make_test_client_and_token(fresh_db, admin=True, seed=False)
    close_system_db()
    monkeypatch.setattr(mod, "get_client", _make_get_client_factory(test_client, token))
    return CliRunner(), mod.app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token = _make_test_client_and_token(fresh_db, admin=False, seed=False)
    close_system_db()
    monkeypatch.setattr(mod, "get_client", _make_get_client_factory(test_client, token))
    return CliRunner(), mod.app


class TestSummary:
    def test_renders_top_tables(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["summary", "--window", "7d"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        assert "kbc.orders" in out
        assert "kbc.sessions" in out
        assert "scan bytes" in out.lower()

    def test_json_mode_is_parseable(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["summary", "--window", "7d", "--json"])
        assert result.exit_code == 0, _clean(result.output)
        payload = json.loads(_clean(result.output))
        assert "top_tables" in payload
        ids = {t["table_id"] for t in payload["top_tables"]}
        assert "kbc.orders" in ids
        assert payload["remote_queries"] == 3  # orders x2 + sessions x1
        assert payload["local_queries"] == 1

    def test_empty_window_message(self, cli_admin_empty):
        runner, app = cli_admin_empty
        result = runner.invoke(app, ["summary", "--window", "7d"])
        assert result.exit_code == 0, _clean(result.output)
        assert "no query activity" in _clean(result.output).lower()

    def test_bogus_window_exits_1_client_side(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["summary", "--window", "bogus"])
        assert result.exit_code != 0
        assert "window" in _clean(result.output).lower()

    def test_non_admin_exits_1(self, cli_non_admin):
        runner, app = cli_non_admin
        result = runner.invoke(app, ["summary", "--window", "7d"])
        assert result.exit_code != 0
        out = _clean(result.output).lower()
        assert "admin" in out or "auth" in out or "403" in out or "401" in out
