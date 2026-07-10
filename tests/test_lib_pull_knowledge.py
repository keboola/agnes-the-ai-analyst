"""agnes pull — knowledge artifact lifecycle (K3, #798)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import run_pull


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_agnes_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))


def _server(monkeypatch, manifest, artifact_bytes=b"DUCK"):
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = {}
        resp.raise_for_status = lambda: None
        return resp

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P

        _P(target_path).write_bytes(artifact_bytes)
        return len(artifact_bytes)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)


def _entry(cid="col_a", md5="m1"):
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


def test_downloads_verifies_and_promotes(tmp_path, monkeypatch):
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [_entry()]})
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "m1", raising=False)
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    target = tmp_path / "user" / "knowledge" / "col_a.duckdb"
    assert target.exists() and result.knowledge_updated == 1
    assert not list(target.parent.glob("*.tmp"))


def test_hash_mismatch_keeps_prior_good_file(tmp_path, monkeypatch):
    kdir = tmp_path / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "col_a.duckdb").write_bytes(b"OLD")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [_entry(md5="expected")]})
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "WRONG", raising=False)
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (kdir / "col_a.duckdb").read_bytes() == b"OLD"
    assert any(e.get("stage") == "knowledge_artifacts" for e in result.errors)


def test_unchanged_hash_skips_download(tmp_path, monkeypatch):
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": [_entry()]})
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "m1", raising=False)
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    calls = []
    monkeypatch.setattr(
        "cli.lib.pull.stream_download",
        lambda *a, **kw: calls.append(a) or 0,
        raising=False,
    )
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert calls == [] and result.knowledge_updated == 0


def test_prunes_on_deauthorization(tmp_path, monkeypatch):
    kdir = tmp_path / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "col_gone.duckdb").write_bytes(b"OLD")
    _server(monkeypatch, {"tables": {}, "knowledge_artifacts": []})  # key PRESENT, empty
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert not (kdir / "col_gone.duckdb").exists()
    assert result.knowledge_removed == 1


def test_pre_k3_server_leaves_local_tree_untouched(tmp_path, monkeypatch):
    kdir = tmp_path / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "col_keep.duckdb").write_bytes(b"KEEP")
    _server(monkeypatch, {"tables": {}})  # no knowledge_artifacts key at all
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (kdir / "col_keep.duckdb").read_bytes() == b"KEEP"
    assert result.knowledge_removed == 0
