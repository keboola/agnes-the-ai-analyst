"""Tests for `agnes store` and `agnes my-stack` Typer wrappers.

Smoke + happy-path. Network calls are mocked so tests don't depend on a
running server.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from cli.commands.my_stack import my_stack_app
from cli.commands.store import store_app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ---------------------------------------------------------------------------
# Help-text smoke tests — guard against accidental command renames.
# ---------------------------------------------------------------------------


def test_store_help_lists_subcommands():
    r = runner.invoke(store_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for cmd in ("list", "show", "install", "uninstall", "upload", "delete"):
        assert cmd in out, f"missing subcommand {cmd!r} in help"


def test_my_stack_help_lists_subcommands():
    r = runner.invoke(my_stack_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for cmd in ("show", "toggle"):
        assert cmd in out


def test_store_list_default_help():
    r = runner.invoke(store_app, ["list", "--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for opt in ("--type", "--category", "--search", "--owner", "--limit", "--skip", "--json"):
        assert opt in out


# ---------------------------------------------------------------------------
# Happy-path mocked tests.
# ---------------------------------------------------------------------------


def test_store_list_renders_table(monkeypatch):
    sample = {
        "items": [
            {
                "id": "abc123",
                "type": "skill",
                "name": "code-review",
                "owner_username": "alice",
                "install_count": 5,
                "version": "deadbeef00000000",
            },
        ],
        "total": 1,
        "skip": 0,
        "limit": 24,
    }
    import cli.commands.store as store_mod
    monkeypatch.setattr(store_mod, "api_get_json", lambda *a, **kw: sample)

    r = runner.invoke(store_app, ["list"])
    assert r.exit_code == 0, r.output
    out = _clean(r.output)
    assert "1 entit" in out
    assert "code-review" in out
    assert "alice" in out


def test_store_install_calls_api(monkeypatch):
    captured: dict = {}

    def _post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"entity_id": "xyz", "installed": True}

    import cli.commands.store as store_mod
    monkeypatch.setattr(store_mod, "api_post_json", _post)

    r = runner.invoke(store_app, ["install", "xyz"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/entities/xyz/install"
    assert "Installed" in _clean(r.output)


def test_store_upload_sends_multipart(monkeypatch, tmp_path):
    captured: dict = {}

    def _multipart(path, *, files, data):
        captured["path"] = path
        captured["data"] = data
        captured["files_keys"] = list(files.keys())
        return {
            "id": "new-id",
            "name": data.get("name", "fallback"),
            "invocation_name": "fallback-by-someone",
            "version": "abcd1234",
        }

    import cli.commands.store as store_mod
    monkeypatch.setattr(store_mod, "api_post_multipart", _multipart)

    zip_path = tmp_path / "skill.zip"
    zip_path.write_bytes(b"PK\x03\x04fake-zip-content")

    r = runner.invoke(
        store_app,
        ["upload", "skill", str(zip_path), "--name", "my-skill", "--description", "d"],
    )
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/entities"
    assert captured["data"]["type"] == "skill"
    assert captured["data"]["name"] == "my-skill"
    assert captured["data"]["description"] == "d"
    assert captured["files_keys"] == ["file"]


def test_my_stack_show_renders(monkeypatch):
    sample = {
        "curated": [
            {
                "marketplace_id": "official",
                "marketplace_slug": "official",
                "plugin_name": "alpha",
                "manifest_name": "alpha",
                "version": "1.0",
                "enabled": True,
            },
        ],
        "store": [
            {
                "entity_id": "e1",
                "type": "skill",
                "name": "code-review",
                "owner_username": "alice",
                "version": "abcd",
                "invocation_name": "code-review-by-alice",
                "install_count": 1,
            },
        ],
    }
    import cli.commands.my_stack as ms_mod
    monkeypatch.setattr(ms_mod, "api_get_json", lambda *a, **kw: sample)

    r = runner.invoke(my_stack_app, ["show"])
    assert r.exit_code == 0, r.output
    out = _clean(r.output)
    assert "Curated" in out and "alpha" in out
    assert "From Store" in out and "code-review-by-alice" in out


def test_my_stack_toggle_requires_on_or_off():
    r = runner.invoke(my_stack_app, ["toggle", "official", "alpha"])
    assert r.exit_code == 2
    assert "exactly one" in _clean(r.output) or "exactly one" in _clean(r.stderr or "")


def test_my_stack_toggle_writes_put(monkeypatch):
    captured: dict = {}

    def _put(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    import cli.commands.my_stack as ms_mod
    monkeypatch.setattr(ms_mod, "api_put_json", _put)

    r = runner.invoke(my_stack_app, ["toggle", "official", "alpha", "--off"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/my-stack/curated/official/alpha"
    assert captured["payload"] == {"enabled": False}
    assert "DISABLED" in _clean(r.output)
