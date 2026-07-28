"""Tests for `agnes admin usage` CLI subcommands.

Pattern mirrors tests/test_cli_admin_activity.py: patch cli.client helpers
to route through a FastAPI TestClient with a seeded DB, then invoke the
Typer app via CliRunner.

Covers:
- export --format=csv succeeds, output begins with CSV header
- export --format=json succeeds, output is parseable NDJSON
- export --format=parquet --out writes a valid parquet file
- non-admin PAT exits 1 with auth error
- --format=bogus exits 1 client-side (validation)
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _seed_events(conn, n=3):
    for i in range(n):
        conn.execute(
            """INSERT INTO usage_events
            (id, session_id, session_file, username, event_uuid, parent_uuid,
             event_type, tool_name, skill_name, subagent_type, command_name,
             is_error, source, ref_id, model, cwd, occurred_at, processor_version)
            VALUES (?, 'sess-1', 'alice/file.jsonl', 'alice',
                    ?, NULL, 'tool_use', 'Bash', NULL, NULL, NULL,
                    false, 'builtin', NULL, 'claude-x', '/tmp', ?, 1)""",
            [
                f"cli-event-{uuid.uuid4().hex[:8]}",
                f"cli-uuid-{i}",
                datetime(2026, 5, 10 + i, 10, 0, tzinfo=timezone.utc),
            ],
        )


# ---------------------------------------------------------------------------
# Client shim
# ---------------------------------------------------------------------------


def _make_streaming_client_factory(test_client, token_cookie):
    """Return a factory (signature matches get_client) that produces an
    object whose .stream() context-manager delegates to the FastAPI TestClient.
    """

    @contextmanager
    def _stream_ctx(method, path, params=None):
        resp = test_client.get(path, params=params or {}, cookies={"access_token": token_cookie})

        # Wrap the TestClient response to look like an httpx streaming response.
        # Only iter_bytes() and status_code are used by the CLI command.
        class _FakeStreamResp:
            status_code = resp.status_code
            headers = resp.headers

            def iter_bytes(self, chunk_size=None):
                yield resp.content

            def read(self):
                return resp.content

        yield _FakeStreamResp()

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def stream(self, method, path, **kwargs):
            return _stream_ctx(method, path, params=kwargs.get("params"))

    def _factory(timeout=30.0):
        return _FakeClient()

    return _factory


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


def _make_admin_client_and_token(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@usage.test", name="admin")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )
        token = create_access_token(user_id=uid, email="admin@usage.test")
        return TestClient(app), token, conn, uid
    except Exception:
        conn.close()
        close_system_db()
        raise


def _make_analyst_client_and_token(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="analyst@usage.test", name="analyst")
        token = create_access_token(user_id=uid, email="analyst@usage.test")
        return TestClient(app), token, conn, uid
    except Exception:
        conn.close()
        close_system_db()
        raise


@pytest.fixture
def cli_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token, conn, uid = _make_admin_client_and_token(fresh_db)
    _seed_events(conn)
    conn.close()
    close_system_db()

    factory = _make_streaming_client_factory(test_client, token)
    monkeypatch.setattr(mod, "get_client", factory)

    return CliRunner(), mod.app


@pytest.fixture
def cli_non_admin(monkeypatch, fresh_db):
    from src.db import close_system_db
    import cli.commands.admin_usage as mod

    test_client, token, conn, uid = _make_analyst_client_and_token(fresh_db)
    conn.close()
    close_system_db()

    factory = _make_streaming_client_factory(test_client, token)
    monkeypatch.setattr(mod, "get_client", factory)

    return CliRunner(), mod.app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExportCsv:
    def test_csv_success_has_header(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["export", "--format", "csv"])
        assert result.exit_code == 0, _clean(result.output)
        lines = result.output.splitlines()
        # First line should be the CSV header row
        header = lines[0]
        assert "id" in header
        assert "tool_name" in header or "username" in header

    def test_csv_is_parseable(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["export", "--format", "csv"])
        assert result.exit_code == 0, _clean(result.output)
        rows = list(csv.reader(io.StringIO(result.output)))
        assert len(rows) >= 1  # at minimum the header row
        assert rows[0][0] == "id"


class TestExportJson:
    def test_json_success_ndjson_parseable(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["export", "--format", "json"])
        assert result.exit_code == 0, _clean(result.output)
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) >= 1
        rec = json.loads(lines[0])
        assert "id" in rec
        assert "tool_name" in rec


class TestExportParquet:
    def test_parquet_writes_valid_file(self, cli_admin, tmp_path):
        runner, app = cli_admin
        out_file = tmp_path / "usage.parquet"
        result = runner.invoke(app, ["export", "--format", "parquet", "--out", str(out_file)])
        assert result.exit_code == 0, _clean(result.output)
        assert out_file.exists()
        assert out_file.stat().st_size > 0
        # Validate parquet is readable
        import duckdb as ddb

        rows = ddb.connect().execute(f"SELECT * FROM read_parquet('{out_file}')").fetchall()
        assert isinstance(rows, list)


class TestExportAuthEnforcement:
    def test_non_admin_exits_1_with_auth_error(self, cli_non_admin):
        runner, app = cli_non_admin
        result = runner.invoke(app, ["export", "--format", "csv"])
        assert result.exit_code != 0
        out = _clean(result.output)
        assert (
            "auth" in out.lower()
            or "403" in out
            or "401" in out
            or "forbidden" in out.lower()
            or "admin" in out.lower()
        )


class TestExportValidation:
    def test_bogus_format_exits_1_client_side(self, cli_admin):
        runner, app = cli_admin
        result = runner.invoke(app, ["export", "--format", "bogus"])
        assert result.exit_code != 0
        out = _clean(result.output)
        assert "bogus" in out or "format" in out.lower() or "csv" in out.lower()
