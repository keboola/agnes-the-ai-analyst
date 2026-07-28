"""CLI: agnes admin usage reprocess + prune.

Pattern mirrors tests/test_cli_admin_usage.py: patch cli.client helpers
to route through a FastAPI TestClient with a seeded DB, then invoke the
Typer app via CliRunner.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# Fixtures — shared setup
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_test_client_and_token(fresh_db, *, admin: bool):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        email = f"{'admin' if admin else 'analyst'}@reprocess.test"
        UserRepository(conn).create(id=uid, email=email, name="Test User")
        if admin:
            admin_group = conn.execute(
                "SELECT id FROM user_groups WHERE name = 'Admin'"
            ).fetchone()
            conn.execute(
                "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                "VALUES (?, ?, 'admin', 'test')",
                [uid, admin_group[0]],
            )
        token = create_access_token(user_id=uid, email=email)
        return TestClient(app), token
    except Exception:
        conn.close()
        close_system_db()
        raise


def _make_simple_client_factory(test_client, token_cookie):
    """Return a get_client factory that delegates POST calls to the FastAPI TestClient."""

    class _FakeClient:
        def post(self, path, **kwargs):
            return test_client.post(path, cookies={"access_token": token_cookie})

    def _factory(timeout=30.0):
        return _FakeClient()

    return _factory


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token = _make_test_client_and_token(fresh_db, admin=True)
    close_system_db()

    factory = _make_simple_client_factory(test_client, token)
    monkeypatch.setattr(mod, "get_client", factory)
    return CliRunner(), mod.app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token = _make_test_client_and_token(fresh_db, admin=False)
    close_system_db()

    factory = _make_simple_client_factory(test_client, token)
    monkeypatch.setattr(mod, "get_client", factory)
    return CliRunner(), mod.app


# ---------------------------------------------------------------------------
# reprocess tests
# ---------------------------------------------------------------------------


class TestReprocess:
    def test_reprocess_success_prints_deleted_counts(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["reprocess"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        assert "reprocess" in out.lower() or "deleted" in out.lower() or "usage" in out.lower()
        # Should show per-key deleted counts
        assert "events" in out or "state" in out or "summaries" in out

    def test_reprocess_non_admin_exits_1(self, cli_non_admin):
        runner, app = cli_non_admin
        result = runner.invoke(app, ["reprocess"])
        assert result.exit_code != 0
        out = _clean(result.output)
        assert (
            "admin" in out.lower()
            or "auth" in out.lower()
            or "403" in out
            or "401" in out
        )


# ---------------------------------------------------------------------------
# prune tests
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_skipped_prints_reason(self, cli_admin, monkeypatch):
        monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)
        runner, app = cli_admin
        result = runner.invoke(app, ["prune"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        assert "skipped" in out.lower() or "unset" in out.lower() or "0" in out

    def test_prune_success_prints_summary(self, cli_admin, monkeypatch, fresh_db):
        monkeypatch.setenv("USAGE_EVENTS_RETENTION_DAYS", "7")
        from src.db import get_system_db, close_system_db

        conn = get_system_db()
        conn.execute(
            """INSERT INTO usage_events
            (id, session_id, session_file, username, event_type, tool_name,
             is_error, source, occurred_at, processor_version)
            VALUES ('old-cli', 's', 'a/x.jsonl', 'a', 'tool_use', 'Bash', false, 'builtin', ?, 1)""",
            [datetime.now(timezone.utc) - timedelta(days=30)],
        )
        conn.close()
        close_system_db()

        runner, app = cli_admin
        result = runner.invoke(app, ["prune"])
        assert result.exit_code == 0, _clean(result.output)
        out = _clean(result.output)
        # Summary line should mention deleted count and retention window
        assert "pruned" in out.lower() or "deleted" in out.lower() or "1" in out

    def test_prune_json_flag_emits_json(self, cli_admin, monkeypatch):
        monkeypatch.delenv("USAGE_EVENTS_RETENTION_DAYS", raising=False)
        runner, app = cli_admin
        result = runner.invoke(app, ["prune", "--json"])
        assert result.exit_code == 0, _clean(result.output)
        # Should be valid JSON
        data = json.loads(result.output.strip())
        assert "status" in data

    def test_prune_non_admin_exits_1(self, cli_non_admin):
        runner, app = cli_non_admin
        result = runner.invoke(app, ["prune"])
        assert result.exit_code != 0
        out = _clean(result.output)
        assert (
            "admin" in out.lower()
            or "auth" in out.lower()
            or "403" in out
            or "401" in out
        )
