"""Tests for `agnes connectors` — the on-demand connector setup surface.

The install prompt references connectors by name (`agnes connectors list`
/ `agnes connectors show <slug>`) instead of inlining every SKILL.md body;
these tests pin the CLI half of that contract.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from cli.commands.connectors import connectors_app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()


_MANIFEST = {
    "schema_version": 2,
    "source": "bundled",
    "connectors": [
        {
            "slug": "connector-asana",
            "display_name": "Asana",
            "short_summary": "Read tasks and projects.",
            "estimated_minutes": 3,
            "vendor_url": None,
            "requires_oauth_app": False,
            "required": False,
        },
        {
            "slug": "connector-xtool",
            "display_name": "XTool",
            "short_summary": "Mandatory org tool.",
            "estimated_minutes": 2,
            "vendor_url": None,
            "requires_oauth_app": False,
            "required": True,
        },
    ],
}

_PROMPT = {
    "schema_version": 2,
    "slug": "connector-asana",
    "display_name": "Asana",
    "short_summary": "Read tasks and projects.",
    "estimated_minutes": 3,
    "required": False,
    "prompt": "Set up an Asana PAT for Claude Code. Walk me through it.",
    "source": "bundled",
}


def test_list_renders_table_with_origin(monkeypatch):
    monkeypatch.setattr("cli.commands.connectors.api_get_json", lambda path, **kw: _MANIFEST)
    result = runner.invoke(connectors_app, ["list"])
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "connector-asana" in out
    assert "Asana" in out
    assert "required" in out  # the mandatory marker is visible
    # Origin labeling per the command-UX standard.
    assert "bundled" in out


def test_list_json(monkeypatch):
    monkeypatch.setattr("cli.commands.connectors.api_get_json", lambda path, **kw: _MANIFEST)
    result = runner.invoke(connectors_app, ["list", "--json"])
    assert result.exit_code == 0
    import json

    body = json.loads(result.output)
    assert [c["slug"] for c in body["connectors"]] == [
        "connector-asana",
        "connector-xtool",
    ]


def test_show_prints_prompt_body(monkeypatch):
    calls = []

    def fake_get(path, **kw):
        calls.append(path)
        return _PROMPT

    monkeypatch.setattr("cli.commands.connectors.api_get_json", fake_get)
    result = runner.invoke(connectors_app, ["show", "connector-asana"])
    assert result.exit_code == 0, result.output
    assert calls == ["/api/connectors/connector-asana/prompt"]
    assert "Set up an Asana PAT" in result.output


def test_show_unknown_slug_hints_list(monkeypatch):
    from cli.v2_client import V2ClientError

    def fake_get(path, **kw):
        raise V2ClientError(
            404,
            {
                "detail": {
                    "kind": "unknown_connector",
                    "hint": "Run `agnes connectors list`.",
                }
            },
        )

    monkeypatch.setattr("cli.commands.connectors.api_get_json", fake_get)
    result = runner.invoke(connectors_app, ["show", "connector-nope"])
    assert result.exit_code == 1
    assert "agnes connectors list" in _clean(result.output)


def test_registered_in_main_app():
    from cli.main import app

    names = {
        getattr(g, "name", None) or (g.typer_instance.info.name if hasattr(g, "typer_instance") else None)
        for g in app.registered_groups
    }
    assert "connectors" in {n for n in names if n}
