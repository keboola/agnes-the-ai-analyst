"""agnes pull — maintained digest delivery to `.claude/rules/ka_<slug>.md` (K4, #799).

Codes against the frozen Task-6 manifest contract for ``kind:"digest"``
entries: ``{kind, id, slug, title, status, status_reason, generated_at,
md5, url}`` (see ``app/api/sync.py::_digest_entries``). Reuses the
stub-server idiom of ``tests/test_lib_pull_knowledge.py`` verbatim.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import run_pull


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_agnes_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))


def _digest_entry(
    slug="architecture-overview",
    did="kd_abc123",
    title="Architecture overview",
    status="fresh",
    status_reason=None,
    generated_at="2026-07-11T00:00:00Z",
    md5="d1",
):
    return {
        "kind": "digest",
        "id": did,
        "slug": slug,
        "title": title,
        "status": status,
        "status_reason": status_reason,
        "generated_at": generated_at,
        "md5": md5,
        "url": f"/api/knowledge/digests/{did}/content",
    }


def _chunks_entry(cid="col_a", md5="m1"):
    return {
        "kind": "chunks",
        "corpus_id": cid,
        "name": "Handbook",
        "md5": md5,
        "size_bytes": 4,
        "chunks": 2,
        "built_at": "2026-07-10T00:00:00Z",
        "url": f"/api/knowledge/artifacts/{cid}/download",
    }


def _content_body(entry, output_md="Some maintained content."):
    return {
        "id": entry["id"],
        "slug": entry["slug"],
        "title": entry["title"],
        "output_md": output_md,
        "status": entry["status"],
        "status_reason": entry.get("status_reason"),
        "generated_at": entry.get("generated_at"),
    }


def _server(monkeypatch, manifest, digest_contents=None, content_error=None, stream_bytes=b"DUCK"):
    """Stub `api_get` (manifest + memory bundle + digest content JSON) and
    `stream_download` (binary knowledge artifacts), mirroring
    `tests/test_lib_pull_knowledge.py::_server`."""
    digest_contents = digest_contents or {}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
            resp.raise_for_status = lambda: None
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
            resp.raise_for_status = lambda: None
        elif path.startswith("/api/knowledge/digests/"):
            if content_error is not None and path == content_error:
                raise RuntimeError("boom: digest content fetch failed")
            resp.json.return_value = digest_contents.get(path, {})
            resp.raise_for_status = lambda: None
        else:
            resp.json.return_value = {}
            resp.raise_for_status = lambda: None
        return resp

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P

        _P(target_path).write_bytes(stream_bytes)
        return len(stream_bytes)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)


def test_writes_ka_file_with_title_and_content(tmp_path, monkeypatch):
    entry = _digest_entry()
    body = _content_body(entry, output_md="# Overview\n\nDetails here.")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [entry]}, {entry["url"]: body})

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    target = tmp_path / ".claude" / "rules" / "ka_architecture-overview.md"
    assert target.exists()
    text = target.read_text()
    assert "# Architecture overview" in text
    assert "Details here." in text
    assert "STALE" not in text
    assert result.digests_updated == 1


def test_stale_digest_gets_visible_banner(tmp_path, monkeypatch):
    entry = _digest_entry(status="stale", status_reason="LLM timeout", md5="d-stale")
    body = _content_body(entry, output_md="Old but good content.")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [entry]}, {entry["url"]: body})

    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    target = tmp_path / ".claude" / "rules" / "ka_architecture-overview.md"
    text = target.read_text()
    assert "STALE" in text
    assert "LLM timeout" in text
    assert "Old but good content." in text


def test_unchanged_md5_skips_fetch(tmp_path, monkeypatch):
    entry = _digest_entry(md5="stable-md5")
    body = _content_body(entry)
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [entry]}, {entry["url"]: body})
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    calls = []

    def _tracking_api_get(path, *args, **kwargs):
        calls.append(path)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        if path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}, "knowledge_artifacts": [entry]}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = body
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _tracking_api_get, raising=False)
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert entry["url"] not in calls
    assert result.digests_updated == 0


def test_staleness_flip_refetches(tmp_path, monkeypatch):
    entry = _digest_entry(status="fresh", md5="fresh-md5")
    body = _content_body(entry, output_md="Content.")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [entry]}, {entry["url"]: body})
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    stale_entry = _digest_entry(status="stale", status_reason="sources changed", md5="stale-md5")
    stale_body = _content_body(stale_entry, output_md="Content.")
    _server(
        monkeypatch,
        {"tables": {}, "knowledge_artifacts": [stale_entry]},
        {stale_entry["url"]: stale_body},
    )
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    target = tmp_path / ".claude" / "rules" / "ka_architecture-overview.md"
    text = target.read_text()
    assert "STALE" in text
    assert "sources changed" in text
    assert result.digests_updated == 1


def test_prunes_on_deauthorization(tmp_path, monkeypatch):
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "ka_gone.md").write_text("stale content")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": []})  # key PRESENT, empty

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert not (rules_dir / "ka_gone.md").exists()
    assert result.digests_removed == 1


def test_pre_k4_server_key_missing_leaves_tree_untouched(tmp_path, monkeypatch):
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "ka_keep.md").write_text("keep me")
    _server(monkeypatch, {"tables": {}})  # no knowledge_artifacts key at all

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert (rules_dir / "ka_keep.md").read_text() == "keep me"
    assert result.digests_removed == 0
    assert result.digests_updated == 0


def test_chunks_entries_ignored_by_digest_step_and_vice_versa(tmp_path, monkeypatch):
    chunks_entry = _chunks_entry()
    digest_entry = _digest_entry()
    body = _content_body(digest_entry, output_md="Mixed section content.")
    _server(
        monkeypatch,
        {"tables": {}, "knowledge_artifacts": [chunks_entry, digest_entry]},
        {digest_entry["url"]: body},
    )
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "m1", raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    # Chunks artifact downloaded via the K3 loop, unaffected by the digest kind.
    knowledge_target = tmp_path / "user" / "knowledge" / "col_a.duckdb"
    assert knowledge_target.exists()
    assert result.knowledge_updated == 1
    # Digest written via the new loop, unaffected by the chunks entry.
    ka_target = tmp_path / ".claude" / "rules" / "ka_architecture-overview.md"
    assert ka_target.exists()
    assert result.digests_updated == 1


def test_unsafe_slug_skipped(tmp_path, monkeypatch):
    entry = _digest_entry(slug="../evil", did="kd_bad")
    body = _content_body(entry, output_md="Should never land.")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [entry]}, {entry["url"]: body})

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    rules_dir = tmp_path / ".claude" / "rules"
    assert not (rules_dir / "ka_../evil.md").exists()
    assert not (tmp_path / ".claude" / "evil.md").exists()
    assert result.digests_updated == 0


def test_fetch_error_keeps_prior_file(tmp_path, monkeypatch):
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "ka_architecture-overview.md").write_text("# Architecture overview\n\nprior good content")

    entry = _digest_entry(md5="new-md5")
    _server(
        monkeypatch,
        {"tables": {}, "knowledge_artifacts": [entry]},
        content_error=entry["url"],
    )

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    text = (rules_dir / "ka_architecture-overview.md").read_text()
    assert "prior good content" in text
    assert any(e.get("stage") == "knowledge_digests" for e in result.errors)
    assert result.digests_updated == 0
