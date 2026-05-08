"""CLI surface for `agnes admin news ...` — talks to the live server via
the `/api/admin/news/*` endpoints.

The tests monkey-patch `cli.client.api_get/api_put/api_post` so each
typer.testing invocation dispatches into a single in-process FastAPI
TestClient with an authenticated admin session cookie. That mirrors
what a real PAT-authed CLI call does in production.
"""

from __future__ import annotations

import json
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


def _make_admin_test_client():
    """Return a FastAPI TestClient with an admin session cookie set."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@cli.test", name="admin")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
            "VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )
        token = create_access_token(user_id=uid, email="admin@cli.test")
    finally:
        conn.close()
        close_system_db()

    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


@pytest.fixture
def cli(monkeypatch, fresh_db):
    """Yield (CliRunner, admin_news_app) wired so api_* calls hit a
    TestClient with an admin session. Returns the same shape every test
    expects (`(runner, app)`)."""
    test_client = _make_admin_test_client()

    def _patched_get(path, **kw):
        return test_client.get(path)

    def _patched_post(path, **kw):
        return test_client.post(path, json=kw.get("json"))

    def _patched_put(path, **kw):
        return test_client.put(path, json=kw.get("json"))

    import cli.client
    monkeypatch.setattr(cli.client, "api_get", _patched_get)
    monkeypatch.setattr(cli.client, "api_post", _patched_post)
    monkeypatch.setattr(cli.client, "api_put", _patched_put)
    # Also patch the names that admin_news.py imported at module load.
    import cli.commands.admin_news as mod
    monkeypatch.setattr(mod, "api_get", _patched_get)
    monkeypatch.setattr(mod, "api_post", _patched_post)
    monkeypatch.setattr(mod, "api_put", _patched_put)

    return CliRunner(), mod.admin_news_app


def test_show_empty(cli):
    runner, app = cli
    r = runner.invoke(app, ["show"])
    assert r.exit_code == 0
    assert "none" in r.output


def test_edit_creates_draft(cli):
    runner, app = cli
    r = runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "<h1>V1</h1>"])
    assert r.exit_code == 0, r.output
    assert "saved draft v1" in r.output

    r = runner.invoke(app, ["draft"])
    assert r.exit_code == 0
    assert "version    : 1" in r.output
    assert "<p>v1</p>" in r.output


def test_publish_then_unpublish(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "<p>V1</p>"])
    r = runner.invoke(app, ["publish"])
    assert r.exit_code == 0, r.output
    assert "published v1" in r.output

    r = runner.invoke(app, ["show"])
    assert "version    : 1" in r.output
    assert "status     : published" in r.output

    r = runner.invoke(app, ["unpublish", "1"])
    assert r.exit_code == 0
    assert "unpublished v1" in r.output


def test_publish_with_no_draft_errors(cli):
    runner, app = cli
    r = runner.invoke(app, ["publish"])
    assert r.exit_code == 1
    assert "no active draft" in r.output


def test_unpublish_unknown_version_errors(cli):
    runner, app = cli
    r = runner.invoke(app, ["unpublish", "99"])
    assert r.exit_code == 1
    assert "not found" in r.output


def test_versions_table_lists_drafts_and_published(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    runner.invoke(app, ["publish"])
    runner.invoke(app, ["edit", "--intro", "<p>v2 draft</p>", "--content", "V2", "--force"])

    r = runner.invoke(app, ["versions"])
    assert r.exit_code == 0
    assert "published" in r.output
    assert "draft" in r.output


def test_edit_from_yaml_file(cli, tmp_path):
    runner, app = cli
    spec = tmp_path / "news.yaml"
    spec.write_text(
        "intro: <p>From file</p>\n"
        "content: <h1>From file body</h1>\n",
        encoding="utf-8",
    )
    r = runner.invoke(app, ["edit", "--from", str(spec)])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["draft"])
    assert "From file" in r.output


def test_edit_from_stdin(cli):
    runner, app = cli
    payload = json.dumps({"intro": "<p>stdin</p>", "content": "<p>S</p>"})
    r = runner.invoke(app, ["edit", "--from", "-"], input=payload)
    assert r.exit_code == 0, r.output
    assert "saved draft v1" in r.output


def test_export_dumps_yaml(cli, tmp_path):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    runner.invoke(app, ["publish"])
    out = tmp_path / "out.yaml"
    r = runner.invoke(app, ["export", str(out)])
    assert r.exit_code == 0, r.output
    text = out.read_text(encoding="utf-8")
    assert "intro:" in text
    assert "v1" in text


def test_publish_with_matching_version_succeeds(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    r = runner.invoke(app, ["publish", "--version", "1"])
    assert r.exit_code == 0, r.output
    assert "published v1" in r.output


def test_publish_with_mismatching_version_refuses(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    r = runner.invoke(app, ["publish", "--version", "5"])
    assert r.exit_code == 2
    assert "version conflict" in r.output


def test_edit_refuses_overwriting_existing_draft_without_force(cli):
    """The CLI's local soft-warning fires when a draft exists; it does
    not check authorship vs the calling PAT (we don't know that locally)."""
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "A"])
    r = runner.invoke(app, ["edit", "--intro", "<p>overwrite</p>", "--content", "B"])
    assert r.exit_code == 2
    assert "--expect-version" in r.output or "expect-version" in r.output


def test_edit_force_overrides_collision(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "A"])
    r = runner.invoke(app, ["edit", "--intro", "<p>force</p>", "--content", "B", "--force"])
    assert r.exit_code == 0


def test_edit_with_matching_expect_version_succeeds(cli):
    runner, app = cli
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "A"])
    r = runner.invoke(
        app,
        ["edit", "--intro", "<p>v1 updated</p>", "--content", "A2", "--expect-version", "1"],
    )
    assert r.exit_code == 0, r.output
    assert "saved draft v1" in r.output
