"""Tests for `agnes collections` CLI commands.

All network calls are monkeypatched — no running server required.
Covers: create, list, show, upload (multipart), rm.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from cli.commands.collections import collections_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Help smoke test
# ---------------------------------------------------------------------------


def test_collections_help_lists_subcommands():
    r = runner.invoke(collections_app, ["--help"])
    assert r.exit_code == 0, r.output
    for cmd in ("create", "list", "show", "upload", "rm"):
        assert cmd in r.output, f"missing subcommand {cmd!r} in help"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_prints_id(monkeypatch):
    body = {
        "id": "col_abc123",
        "slug": "my-corpus",
        "name": "My Corpus",
        "description": "test",
        "created_by": "admin",
        "created_at": "2026-06-15T00:00:00",
        "updated_at": None,
    }
    with patch("cli.commands.collections.api_post_json", return_value=body):
        r = runner.invoke(collections_app, ["create", "--name", "My Corpus", "--description", "test"])
    assert r.exit_code == 0, r.output
    assert "col_abc123" in r.output
    assert "My Corpus" in r.output


def test_create_json_flag(monkeypatch):
    body = {
        "id": "col_xyz",
        "slug": "s",
        "name": "N",
        "description": None,
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
    }
    with patch("cli.commands.collections.api_post_json", return_value=body):
        r = runner.invoke(collections_app, ["create", "--name", "N", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["id"] == "col_xyz"


def test_create_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_post_json",
        side_effect=V2ClientError(status_code=403, body={"detail": "Forbidden"}),
    ):
        r = runner.invoke(collections_app, ["create", "--name", "X"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_items(monkeypatch):
    body = {
        "items": [
            {
                "id": "col_1",
                "slug": "first",
                "name": "First",
                "description": None,
                "created_by": "u",
                "created_at": None,
                "updated_at": None,
            },
        ]
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["list"])
    assert r.exit_code == 0, r.output
    assert "col_1" in r.output
    assert "First" in r.output


def test_list_json_flag(monkeypatch):
    body = {"items": []}
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == body


def test_list_empty_table(monkeypatch):
    with patch("cli.commands.collections.api_get_json", return_value={"items": []}):
        r = runner.invoke(collections_app, ["list"])
    assert r.exit_code == 0
    assert "no collections" in r.output.lower()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_detail(monkeypatch):
    body = {
        "id": "col_1",
        "slug": "first",
        "name": "First",
        "description": "desc",
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
        "files": [
            {
                "file_id": "cf_a",
                "filename": "notes.txt",
                "processing_status": "pending",
                "size_bytes": 11,
                "file_type": "txt",
                "sha256": "abc",
                "corpus_id": "col_1",
                "created_at": None,
                "processing_detail": None,
            }
        ],
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["show", "col_1"])
    assert r.exit_code == 0, r.output
    assert "First" in r.output
    assert "notes.txt" in r.output


def test_show_json_flag(monkeypatch):
    body = {
        "id": "col_1",
        "slug": "s",
        "name": "N",
        "description": None,
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
        "files": [],
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["show", "col_1", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["id"] == "col_1"


def test_show_not_found_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_get_json",
        side_effect=V2ClientError(status_code=404, body={"detail": "collection_not_found"}),
    ):
        r = runner.invoke(collections_app, ["show", "col_missing"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_sends_multipart_per_file(monkeypatch, tmp_path):
    """Each file in the path list is sent as a separate multipart POST."""
    calls: list[dict] = []

    def _fake_post(path, *, files, data=None):
        calls.append({"path": path, "files": files})
        return [{"file_id": "cf_new", "filename": "a.txt", "processing_status": "pending", "size_bytes": 3}]

    f1 = tmp_path / "a.txt"
    f1.write_bytes(b"abc")
    f2 = tmp_path / "b.pdf"
    f2.write_bytes(b"pdf")

    with patch("cli.commands.collections.api_post_multipart", _fake_post):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), str(f2)])

    assert r.exit_code == 0, r.output
    # Two separate POST calls (one per file)
    assert len(calls) == 2
    assert calls[0]["path"] == "/api/collections/col_1/files"
    assert "pending" in r.output


def test_upload_with_path_sends_paths_form_field(monkeypatch, tmp_path):
    """`--path` is forwarded as the `paths` multipart form field for upsert."""
    calls: list[dict] = []

    def _fake_post(path, *, files, data=None):
        calls.append({"path": path, "data": data})
        return [{"file_id": "cf_new", "filename": "a.md", "processing_status": "pending", "path": "docs/a.md"}]

    f1 = tmp_path / "a.md"
    f1.write_bytes(b"alpha")

    with patch("cli.commands.collections.api_post_multipart", _fake_post):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), "--path", "docs/a.md"])

    assert r.exit_code == 0, r.output
    assert len(calls) == 1
    assert calls[0]["data"] == {"paths": "docs/a.md"}


def test_upload_path_with_multiple_files_errors(tmp_path):
    """`--path` is single-file only — reject an ambiguous multi-file upload."""
    f1 = tmp_path / "a.md"
    f1.write_bytes(b"a")
    f2 = tmp_path / "b.md"
    f2.write_bytes(b"b")

    called: list = []
    with patch("cli.commands.collections.api_post_multipart", lambda *a, **k: called.append(1) or []):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), str(f2), "--path", "docs/x.md"])
    assert r.exit_code != 0
    assert not called  # never hit the network


def test_upload_server_error_exits_nonzero(monkeypatch, tmp_path):
    from cli.v2_client import V2ClientError

    f = tmp_path / "bad.dwg"
    f.write_bytes(b"bin")
    with patch(
        "cli.commands.collections.api_post_multipart",
        side_effect=V2ClientError(status_code=422, body=[{"filename": "bad.dwg", "processing_status": "rejected"}]),
    ):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f)])
    assert r.exit_code != 0


def test_upload_missing_file_exits_nonzero(tmp_path):
    """Typer argument validation: path must exist."""
    r = runner.invoke(collections_app, ["upload", "col_1", str(tmp_path / "no_such.txt")])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def test_rm_with_yes_flag(monkeypatch):
    called: list[str] = []

    def _fake_delete(path):
        called.append(path)
        return {}

    with patch("cli.commands.collections.api_delete", _fake_delete):
        r = runner.invoke(collections_app, ["rm", "col_1", "--yes"])
    assert r.exit_code == 0, r.output
    assert called == ["/api/collections/col_1"]
    assert "deleted" in r.output.lower()


def test_rm_prompts_without_yes(monkeypatch):
    """Without --yes the command should ask for confirmation."""
    called: list[str] = []

    with patch("cli.commands.collections.api_delete", lambda p: called.append(p) or {}):
        # Simulate user answering "n" to the confirmation prompt
        runner.invoke(collections_app, ["rm", "col_1"], input="n\n")
    # User said no — nothing deleted
    assert not called


def test_rm_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_delete",
        side_effect=V2ClientError(status_code=404, body={"detail": "collection_not_found"}),
    ):
        r = runner.invoke(collections_app, ["rm", "col_missing", "--yes"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# reingest
# ---------------------------------------------------------------------------


def test_collections_reingest_posts_to_endpoint(monkeypatch):
    calls = {}

    def fake_post(path, payload):
        calls["path"] = path
        return {"file_id": "cf_1", "processing_status": "pending"}

    with patch("cli.commands.collections.api_post_json", fake_post):
        r = runner.invoke(collections_app, ["reingest", "col_1", "cf_1"])
    assert r.exit_code == 0, r.output
    assert calls["path"] == "/api/collections/col_1/files/cf_1/reingest"
    assert "pending" in r.output


def test_collections_reingest_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_post_json",
        side_effect=V2ClientError(status_code=409, body={"detail": "reingest_in_progress"}),
    ):
        r = runner.invoke(collections_app, ["reingest", "col_1", "cf_1"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Registration check — `agnes collections` must exist in the top-level app
# ---------------------------------------------------------------------------


def test_collections_registered_in_main():
    """The `collections` sub-app is registered in cli/main.py."""
    from cli.main import app

    group_names = {g.name for g in app.registered_groups if g.name}
    assert "collections" in group_names, (
        "collections_app not registered in cli/main.py — add `app.add_typer(collections_app, name='collections')`"
    )
