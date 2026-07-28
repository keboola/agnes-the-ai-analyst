"""CLI surface for `agnes onboarded {on,off,status}` — talks to the
running server via /api/me/onboarded (PAT-authed).

Same TestClient-based monkey-patch pattern as test_cli_admin_news.py:
each typer.testing invocation dispatches into a single in-process
FastAPI TestClient with an authenticated session cookie.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from typer.testing import CliRunner


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_test_client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="user@cli.test", name="user")
        token = create_access_token(user_id=uid, email="user@cli.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


@pytest.fixture
def cli(monkeypatch, fresh_db):
    test_client = _make_test_client()

    def _patched_get(path, **kw):
        return test_client.get(path)

    def _patched_post(path, **kw):
        return test_client.post(path, json=kw.get("json"))

    import cli.client
    import cli.commands.onboarded as mod
    for name in ("api_get", "api_post"):
        if hasattr(cli.client, name):
            monkeypatch.setattr(cli.client, name, locals()[f"_patched_{name.split('_')[1]}"])
    monkeypatch.setattr(mod, "api_get", _patched_get)
    monkeypatch.setattr(mod, "api_post", _patched_post)

    return CliRunner(), mod.onboarded_app


def test_status_starts_false(cli):
    runner, app = cli
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "onboarded: False" in r.output


def test_on_flips_true(cli):
    runner, app = cli
    r = runner.invoke(app, ["on"])
    assert r.exit_code == 0, r.output
    assert "onboarded: True" in r.output

    r = runner.invoke(app, ["status"])
    assert "onboarded: True" in r.output


def test_off_flips_false(cli):
    runner, app = cli
    runner.invoke(app, ["on"])
    r = runner.invoke(app, ["off"])
    assert r.exit_code == 0, r.output
    assert "onboarded: False" in r.output

    r = runner.invoke(app, ["status"])
    assert "onboarded: False" in r.output


def test_on_off_idempotent(cli):
    runner, app = cli
    runner.invoke(app, ["on"])
    r = runner.invoke(app, ["on"])
    assert r.exit_code == 0
    assert "onboarded: True" in r.output

    runner.invoke(app, ["off"])
    r = runner.invoke(app, ["off"])
    assert r.exit_code == 0
    assert "onboarded: False" in r.output


def test_audit_log_records_source(cli, fresh_db):
    """Each toggle writes an audit_log row with the requested source."""
    import json
    from src.db import get_system_db, close_system_db

    runner, app = cli
    runner.invoke(app, ["on", "--source", "agnes_init"])
    runner.invoke(app, ["off", "--source", "self_unmark"])

    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT action, params FROM audit_log "
            "WHERE action IN ('user_onboarded', 'user_offboarded') "
            "ORDER BY timestamp"
        ).fetchall()
    finally:
        conn.close()
        close_system_db()

    actions = [r[0] for r in rows]
    assert "user_onboarded" in actions
    assert "user_offboarded" in actions

    sources = []
    for _, p in rows:
        params = json.loads(p) if isinstance(p, str) else p
        sources.append(params.get("source"))
    assert "agnes_init" in sources
    assert "self_unmark" in sources
