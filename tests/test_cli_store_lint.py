"""Tests for `agnes admin store lint-{findings,audit,dismiss}` (v89, #687).

Network calls are mocked — these assert the CLI wires the right path/payload
and renders the response, not the server behavior (covered by
test_store_lint_api.py).
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

import cli.commands.admin_store as admin_store_mod
from cli.commands.admin_store import admin_store_app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_help_lists_lint_subcommands():
    r = runner.invoke(admin_store_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for cmd in ("lint-findings", "lint-audit", "lint-dismiss"):
        assert cmd in out


def test_lint_findings_calls_endpoint_and_renders(monkeypatch):
    captured = {}

    def _get(path, **params):
        captured["path"] = path
        captured["params"] = params
        return {
            "findings": [{"entity_id": "e1", "severity": "warn", "rule_id": "SL002", "message": "Too big."}],
            "last_run": None,
        }

    monkeypatch.setattr(admin_store_mod, "api_get_json", _get)
    r = runner.invoke(admin_store_app, ["lint-findings"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/admin/store/lint-findings"
    assert captured["params"] == {"include_dismissed": "false"}
    out = _clean(r.output)
    assert "SL002" in out and "Too big." in out


def test_lint_findings_include_dismissed_flag(monkeypatch):
    captured = {}

    def _get(path, **params):
        captured["params"] = params
        return {"findings": [], "last_run": None}

    monkeypatch.setattr(admin_store_mod, "api_get_json", _get)
    r = runner.invoke(admin_store_app, ["lint-findings", "--include-dismissed"])
    assert r.exit_code == 0, r.output
    assert captured["params"] == {"include_dismissed": "true"}
    assert "No advisory findings." in _clean(r.output)


def test_lint_audit_posts_force_and_renders_stats(monkeypatch):
    captured = {}

    def _post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"entities_linted": 3, "entities_skipped": 1, "findings_count": 2}

    monkeypatch.setattr(admin_store_mod, "api_post_json", _post)
    r = runner.invoke(admin_store_app, ["lint-audit", "--force"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/admin/store/lint-audit"
    assert captured["payload"] == {"force": True}
    out = _clean(r.output)
    assert "Audited 3 skills" in out and "2 findings" in out


def test_lint_audit_reports_skipped(monkeypatch):
    monkeypatch.setattr(admin_store_mod, "api_post_json", lambda path, payload: {"skipped": True})
    r = runner.invoke(admin_store_app, ["lint-audit"])
    assert r.exit_code == 0, r.output
    assert "Skipped" in _clean(r.output)


def test_lint_dismiss_posts_entity_and_rule(monkeypatch):
    captured = {}

    def _post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"dismissed": True}

    monkeypatch.setattr(admin_store_mod, "api_post_json", _post)
    r = runner.invoke(admin_store_app, ["lint-dismiss", "e1", "SL002"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/admin/store/lint-dismiss"
    assert captured["payload"] == {"entity_id": "e1", "rule_id": "SL002"}
    assert "Dismissed SL002 on e1." in _clean(r.output)
