"""Tests for cli/lib/pull.py:run_pull."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import run_pull, PullResult


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Sandbox `cli.config` reads/writes into the test's tmp_path so a
    leftover ~/.config/agnes/sync_state.json from a prior run doesn't
    short-circuit the hash-comparison logic in run_pull."""
    cfg_dir = tmp_path / "_agnes_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))


@pytest.fixture
def fake_server(monkeypatch):
    """Mock api_get to return canned manifest + memory bundle."""
    canned = {
        "/api/sync/manifest": {"tables": {}},
        "/api/memory/bundle": {"mandatory": [], "approved": []},
    }

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        body = canned.get(path, {})
        resp.json.return_value = body
        resp.iter_bytes = lambda chunk_size=65536: iter([b""])
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    return canned


def test_run_pull_empty_manifest_no_parquet_dir(tmp_path, fake_server):
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert isinstance(result, PullResult)
    assert result.tables_updated == 0
    assert not (tmp_path / "server" / "parquet").exists(), \
        "lazy mkdir: empty manifest must not create server/parquet/"


def test_run_pull_empty_memory_no_rules_dir(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert not (tmp_path / ".claude" / "rules").exists(), \
        "lazy mkdir: empty bundle must not create .claude/rules/"


def test_run_pull_creates_duckdb_unconditionally(tmp_path, fake_server):
    """Even with zero data, the DuckDB file is opened (it's the load-bearing
    artifact and other readers expect its parent dir to exist)."""
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()


def test_run_pull_with_one_table(tmp_path, monkeypatch):
    """Manifest with one table -> server/parquet/ created, parquet downloaded."""
    canned_manifest = {"tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}}}
    canned_memory = {"mandatory": [], "approved": []}
    parquet_bytes = b"PAR1" + b"\x00" * 1000 + b"PAR1"

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    def _stream_download(path, target_path, progress_callback=None):
        # Simulate writing parquet file to disk (caller has already mkdir'd).
        from pathlib import Path as _P
        _P(target_path).write_bytes(parquet_bytes)
        return len(parquet_bytes)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    # md5 will mismatch ('abc' != real); short-circuit with empty hash flow:
    # easiest: monkeypatch _file_md5 to return 'abc' so verification passes.
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "abc", raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "server" / "parquet").exists()
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").exists()
    assert result.tables_updated == 1


def test_run_pull_dry_run_writes_nothing(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path, dry_run=True)
    assert not (tmp_path / "server").exists()
    assert not (tmp_path / "user" / "duckdb").exists()
    # No user-home state file either — dry_run must be hermetic.
    # The autouse fixture sandboxes AGNES_CONFIG_DIR to tmp_path/_agnes_cfg.
    assert not (tmp_path / "_agnes_cfg" / "sync_state.json").exists()
