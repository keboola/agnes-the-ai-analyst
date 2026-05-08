"""CLI surface for `agnes admin news ...` — direct DB access, no API.

Each test runs against a fresh DATA_DIR so the bootstrap migration runs
on first connect and the news_template table starts empty.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _runner_app():
    from cli.commands.admin_news import admin_news_app
    return CliRunner(), admin_news_app


def test_show_empty(fresh_db):
    runner, app = _runner_app()
    r = runner.invoke(app, ["show"])
    assert r.exit_code == 0
    assert "none" in r.output


def test_edit_creates_draft(fresh_db):
    runner, app = _runner_app()
    r = runner.invoke(
        app,
        ["edit", "--intro", "<p>v1</p>", "--content", "<h1>V1</h1>", "--by", "alice@x"],
    )
    assert r.exit_code == 0
    assert "saved draft v1" in r.output

    r = runner.invoke(app, ["draft"])
    assert r.exit_code == 0
    assert "version    : 1" in r.output
    assert "<p>v1</p>" in r.output


def test_publish_then_unpublish(fresh_db):
    runner, app = _runner_app()
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "<p>V1</p>"])
    r = runner.invoke(app, ["publish"])
    assert r.exit_code == 0
    assert "published v1" in r.output

    r = runner.invoke(app, ["show"])
    assert "version    : 1" in r.output
    assert "status     : published" in r.output

    r = runner.invoke(app, ["unpublish", "1"])
    assert r.exit_code == 0
    assert "unpublished v1" in r.output


def test_publish_with_no_draft_errors(fresh_db):
    runner, app = _runner_app()
    r = runner.invoke(app, ["publish"])
    assert r.exit_code == 1
    assert "no active draft" in r.output


def test_unpublish_unknown_version_errors(fresh_db):
    runner, app = _runner_app()
    r = runner.invoke(app, ["unpublish", "99"])
    assert r.exit_code == 1
    assert "not found" in r.output


def test_versions_table_lists_drafts_and_published(fresh_db):
    runner, app = _runner_app()
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    runner.invoke(app, ["publish"])
    runner.invoke(app, ["edit", "--intro", "<p>v2 draft</p>", "--content", "V2"])

    r = runner.invoke(app, ["versions"])
    assert r.exit_code == 0
    assert "published" in r.output
    assert "draft" in r.output


def test_edit_from_yaml_file(fresh_db, tmp_path):
    runner, app = _runner_app()
    spec = tmp_path / "news.yaml"
    spec.write_text(
        "intro: <p>From file</p>\n"
        "content: <h1>From file body</h1>\n",
        encoding="utf-8",
    )
    r = runner.invoke(app, ["edit", "--from", str(spec)])
    assert r.exit_code == 0
    assert "saved draft v1" in r.output

    r = runner.invoke(app, ["draft"])
    assert "From file" in r.output


def test_edit_from_stdin(fresh_db):
    runner, app = _runner_app()
    payload = json.dumps({"intro": "<p>stdin</p>", "content": "<p>S</p>"})
    r = runner.invoke(app, ["edit", "--from", "-"], input=payload)
    assert r.exit_code == 0
    assert "saved draft v1" in r.output


def test_export_dumps_yaml(fresh_db, tmp_path):
    runner, app = _runner_app()
    runner.invoke(app, ["edit", "--intro", "<p>v1</p>", "--content", "V1"])
    runner.invoke(app, ["publish"])
    out = tmp_path / "out.yaml"
    r = runner.invoke(app, ["export", str(out)])
    assert r.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "intro:" in text
    assert "v1" in text
