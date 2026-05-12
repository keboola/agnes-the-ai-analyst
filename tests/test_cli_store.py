"""Tests for `agnes store` (creator-side) and `agnes my-stack` Typer wrappers.

Smoke + happy-path. Network calls are mocked so tests don't depend on a
running server. Consumer-side browse/install ops (list, show, install,
uninstall) moved to `agnes marketplace` — see test_cli_marketplace.py.
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
    for cmd in ("upload", "update", "delete", "mine"):
        assert cmd in out, f"missing subcommand {cmd!r} in help"


def test_admin_store_help_lists_subcommands():
    from cli.commands.admin_store import admin_store_app
    r = runner.invoke(admin_store_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for cmd in ("pull", "push", "info"):
        assert cmd in out


def test_my_stack_help_lists_subcommands():
    r = runner.invoke(my_stack_app, ["--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    assert "show" in out
    assert "toggle" not in out


# ---------------------------------------------------------------------------
# Happy-path mocked tests.
# ---------------------------------------------------------------------------



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

    r = runner.invoke(my_stack_app, [])
    assert r.exit_code == 0, r.output
    out = _clean(r.output)
    assert "Curated" in out and "alpha" in out
    assert "From Flea Market" in out and "code-review-by-alice" in out



# ---------------------------------------------------------------------------
# `agnes store update`
# ---------------------------------------------------------------------------


def test_store_update_help_lists_options():
    r = runner.invoke(store_app, ["update", "--help"])
    assert r.exit_code == 0
    out = _clean(r.output)
    for opt in ("--description", "--category", "--video-url", "--photo", "--zip"):
        assert opt in out


def test_store_update_no_fields_exit_2():
    r = runner.invoke(store_app, ["update", "abc123"])
    assert r.exit_code == 2
    assert "Nothing to update" in _clean(r.output)


def test_store_update_sends_put_multipart(monkeypatch):
    captured: dict = {}

    def _put(path, *, files, data):
        captured["path"] = path
        captured["files"] = files
        captured["data"] = data
        return {"id": "abc", "version": "newhash01234567"}

    import cli.commands.store as store_mod
    monkeypatch.setattr(store_mod, "api_put_multipart", _put)

    r = runner.invoke(store_app, ["update", "abc", "--description", "new desc"])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/entities/abc"
    assert captured["data"] == {"description": "new desc"}
    assert captured["files"] is None
    assert "Updated" in _clean(r.output)


# ---------------------------------------------------------------------------
# `agnes store pull` / `agnes store info`
# ---------------------------------------------------------------------------


def test_admin_store_pull_writes_zip(monkeypatch, tmp_path):
    """Bulk pull of all Store entities lives under `agnes admin store pull`."""
    from cli.commands.admin import admin_app
    from cli.commands import admin_store as admin_store_mod

    captured: dict = {}

    def _stream(path, dest, **params):
        captured["path"] = path
        captured["params"] = params
        with open(dest, "wb") as f:
            f.write(b"PK\x03\x04fakezip")
        return 9

    monkeypatch.setattr(admin_store_mod, "api_get_stream", _stream)

    out = tmp_path / "store.zip"
    r = runner.invoke(admin_app, ["store", "pull", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/bundle.zip"
    # `mine` uses owner=me; bulk pull does NOT.
    assert "owner" not in captured["params"]
    assert "Wrote 9 bytes" in _clean(r.output)
    assert out.exists()


def test_admin_store_pull_unpack(monkeypatch, tmp_path):
    """`agnes admin store pull --unpack DIR` streams + extracts."""
    import zipfile
    from cli.commands.admin import admin_app
    from cli.commands import admin_store as admin_store_mod

    fake_zip_path = tmp_path / "_fake.zip"
    with zipfile.ZipFile(fake_zip_path, "w") as zf:
        zf.writestr("manifest.json", '{"format":1,"entries":[]}')
        zf.writestr("entities/abc/plugin/.claude-plugin/plugin.json", '{}')

    def _stream(path, dest, **params):
        from pathlib import Path as _P
        with open(dest, "wb") as fh:
            fh.write(_P(fake_zip_path).read_bytes())
        return _P(dest).stat().st_size

    monkeypatch.setattr(admin_store_mod, "api_get_stream", _stream)

    target = tmp_path / "unpacked"
    r = runner.invoke(admin_app, ["store", "pull", "--unpack", str(target)])
    assert r.exit_code == 0, r.output
    assert (target / "manifest.json").is_file()
    assert (target / "entities/abc/plugin/.claude-plugin/plugin.json").is_file()


def test_store_mine_uses_owner_me_param(monkeypatch, tmp_path):
    """`agnes store mine` is the user-facing variant — same endpoint with
    `?owner=me` so server can scope to caller's own entities."""
    captured: dict = {}

    def _stream(path, dest, **params):
        captured["path"] = path
        captured["params"] = params
        with open(dest, "wb") as f:
            f.write(b"PK\x03\x04mine")
        return 7

    import cli.commands.store as store_mod
    monkeypatch.setattr(store_mod, "api_get_stream", _stream)

    out = tmp_path / "mine.zip"
    r = runner.invoke(store_app, ["mine", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/bundle.zip"
    assert captured["params"] == {"owner": "me"}
    assert out.exists()


def test_admin_store_info_summarizes(monkeypatch):
    from cli.commands.admin import admin_app
    from cli.commands import admin_store as admin_store_mod

    page1 = {
        "items": [
            {"type": "skill", "file_size": 1024},
            {"type": "skill", "file_size": 512},
            {"type": "agent", "file_size": 256},
        ],
        "total": 3, "skip": 0, "limit": 100,
    }
    empty = {"items": [], "total": 3, "skip": 100, "limit": 100}
    pages = [page1, empty]

    monkeypatch.setattr(admin_store_mod, "api_get_json", lambda *a, **kw: pages.pop(0))

    r = runner.invoke(admin_app, ["store", "info"])
    assert r.exit_code == 0, r.output
    out = _clean(r.output)
    assert "3 entit" in out
    assert "skill" in out and "2" in out
    assert "agent" in out and "1" in out


def test_admin_store_info_json(monkeypatch):
    from cli.commands.admin import admin_app
    from cli.commands import admin_store as admin_store_mod
    one = {
        "items": [{"type": "plugin", "file_size": 999}],
        "total": 1, "skip": 0, "limit": 100,
    }
    pages = [one, {"items": [], "total": 1, "skip": 100, "limit": 100}]
    monkeypatch.setattr(admin_store_mod, "api_get_json", lambda *a, **kw: pages.pop(0))

    r = runner.invoke(admin_app, ["store", "info", "--json"])
    assert r.exit_code == 0, r.output
    import json as _json
    body = _json.loads(_clean(r.output))
    assert body["total_entities"] == 1
    assert body["by_type"] == {"plugin": 1}


# ---------------------------------------------------------------------------
# `agnes admin store push`
# ---------------------------------------------------------------------------


def test_admin_store_push_help():
    from cli.commands.admin_store import admin_store_app
    r = runner.invoke(admin_store_app, ["--help"])
    assert r.exit_code == 0
    assert "push" in _clean(r.output)


def test_admin_store_push_invalid_mode_exit_2(tmp_path):
    """Single-command Typer app — invoke via parent so the `push` token
    actually routes to the subcommand (otherwise Typer collapses the lone
    command and treats `push` as the SOURCE positional)."""
    from cli.commands.admin import admin_app
    bundle = tmp_path / "x.zip"
    bundle.write_bytes(b"PK\x03\x04")
    r = runner.invoke(admin_app, ["store", "push", str(bundle), "--mode", "wat"])
    assert r.exit_code == 2
    assert "merge|replace|skip" in _clean(r.output)


def test_admin_store_push_zips_directory(monkeypatch, tmp_path):
    """When source is a directory, CLI must zip it client-side and POST."""
    import zipfile as _zf

    captured: dict = {}

    def _post(path, *, files, data):
        captured["path"] = path
        captured["data"] = data
        zip_bytes = files["file"][1]
        with _zf.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
            captured["names"] = sorted(zf.namelist())
        return {
            "imported": 1, "replaced": 0, "skipped": 0,
            "stub_users_created": 0, "errors": [],
        }

    from cli.commands import admin_store as admin_store_mod
    from cli.commands.admin import admin_app
    monkeypatch.setattr(admin_store_mod, "api_post_multipart", _post)

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "entities" / "abc" / "plugin").mkdir(parents=True)
    (bundle_dir / "manifest.json").write_text('{"format":1,"entries":[]}')
    (bundle_dir / "entities" / "abc" / "plugin" / "marker.txt").write_text("x")

    r = runner.invoke(
        admin_app, ["store", "push", str(bundle_dir), "--mode", "merge", "--yes"],
    )
    assert r.exit_code == 0, r.output
    assert captured["path"] == "/api/store/import-bundle"
    assert captured["data"] == {"mode": "merge"}
    assert "manifest.json" in captured["names"]
    assert "entities/abc/plugin/marker.txt" in captured["names"]
    assert "imported=1" in _clean(r.output)


def test_admin_store_push_directory_without_manifest_exit_2(tmp_path):
    from cli.commands.admin import admin_app
    empty_dir = tmp_path / "no_manifest"
    empty_dir.mkdir()
    r = runner.invoke(
        admin_app, ["store", "push", str(empty_dir), "--yes"],
    )
    assert r.exit_code == 2
    assert "manifest.json" in _clean(r.output)
