"""Tests for `agnes admin ask` CLI command.

Pattern: patch cli.commands.admin_ask.get_client to route through a FastAPI
TestClient with an admin session, then invoke the Typer app via CliRunner.

Covers:
- ask prints SQL + result table on success
- --json flag prints raw JSON only
- server returns 200 with rejected field → CLI exits 1 + prints both SQL and rejection
"""

from __future__ import annotations

import json
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# DB + client helpers
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
        UserRepository(conn).create(id=uid, email="admin@ask.test", name="admin")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
            "VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )
        token = create_access_token(user_id=uid, email="admin@ask.test")
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
        UserRepository(conn).create(id=uid, email="analyst@ask.test", name="analyst")
        token = create_access_token(user_id=uid, email="analyst@ask.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


def _make_post_factory(test_client, monkeypatch_env_key="sk-fake"):
    """Return a get_client factory whose .post() delegates to the FastAPI TestClient."""

    class _FakeClient:
        def post(self, path, **kw):
            return test_client.post(path, **kw)

    def _factory(timeout=120):
        return _FakeClient()

    return _factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    """CliRunner + admin_ask app wired to an admin TestClient (mocked LLM)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    test_client = _make_admin_test_client()
    factory = _make_post_factory(test_client)

    import cli.commands.admin_ask as mod
    monkeypatch.setattr(mod, "get_client", factory)

    return CliRunner(), mod.app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    """CliRunner + admin_ask app wired to a non-admin TestClient."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    test_client = _make_non_admin_test_client()
    factory = _make_post_factory(test_client)

    import cli.commands.admin_ask as mod
    monkeypatch.setattr(mod, "get_client", factory)

    return CliRunner(), mod.app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAskPrintsTable:
    def test_ask_cli_prints_sql_and_rows(self, cli_admin, monkeypatch):
        """Happy path: patched LLM returns valid SQL; CLI prints SQL + table."""
        runner, app = cli_admin

        with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
            mock_cls.return_value.extract_json.return_value = {
                "sql": "SELECT 42 AS answer",
                "rationale": "Returns the answer.",
            }
            result = runner.invoke(app, ["top tools today"])

        out = _clean(result.output)
        assert result.exit_code == 0, out
        assert "SELECT 42 AS answer" in out
        assert "Returns the answer." in out
        # Column header and value should be present
        assert "answer" in out
        assert "42" in out

    def test_ask_cli_shows_no_rows_message(self, cli_admin, monkeypatch):
        """When query returns empty result, CLI prints '(no rows)'."""
        runner, app = cli_admin

        with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
            mock_cls.return_value.extract_json.return_value = {
                "sql": "SELECT 1 AS x WHERE 1=0",
                "rationale": "Always empty.",
            }
            result = runner.invoke(app, ["show empty"])

        out = _clean(result.output)
        assert result.exit_code == 0, out
        assert "(no rows)" in out


class TestAskJsonFlag:
    def test_ask_cli_json_flag_prints_raw_json(self, cli_admin, monkeypatch):
        """--json emits raw JSON only (parseable, contains sql + rows)."""
        runner, app = cli_admin

        with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
            mock_cls.return_value.extract_json.return_value = {
                "sql": "SELECT 7 AS n",
                "rationale": "Seven.",
            }
            result = runner.invoke(app, ["--json", "how many"])

        out = _clean(result.output)
        assert result.exit_code == 0, out
        data = json.loads(out.strip())
        assert data["sql"] == "SELECT 7 AS n"
        assert "rows" in data
        assert data["rows"][0]["n"] == 7


class TestAskRejectedSql:
    def test_ask_cli_handles_rejected_sql(self, cli_admin, monkeypatch):
        """Server returns 200 with rejected field — CLI prints SQL and rejection, exits 1."""
        runner, app = cli_admin

        with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
            mock_cls.return_value.extract_json.return_value = {
                "sql": "DROP TABLE usage_events",
                "rationale": "Dangerous.",
            }
            result = runner.invoke(app, ["delete all data"])

        out = _clean(result.output)
        assert result.exit_code != 0
        assert "DROP TABLE usage_events" in out
        # Error message with rejection reason should appear
        assert "forbidden" in out.lower() or "rejected" in out.lower()
