"""#607 — `agnes pull` must NOT download a `server_only` table's parquet,
but MUST still count it as listed (parquets_total) — mirroring the
"listed-but-skipped" behavior. A normal `local` table alongside it still
downloads.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import run_pull


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "_agnes_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))


def test_pull_skips_server_only_but_counts_it(tmp_path, monkeypatch):
    # Manifest: one normal local table (downloads) + one server_only table
    # (listed, not downloaded).
    canned_manifest = {
        "tables": {
            "normal_tbl": {
                "hash": "h_normal", "rows": 0, "size_bytes": 0,
                "query_mode": "local", "server_only": False,
            },
            "so_tbl": {
                "hash": "h_so", "rows": 0, "size_bytes": 0,
                "query_mode": "local", "server_only": True,
            },
        }
    }
    canned_memory = {"mandatory": [], "approved": []}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    downloaded_tids: list[str] = []

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P
        # path is the server URL path; capture which tid was requested.
        downloaded_tids.append(str(path))
        _P(target_path).write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")
        return 108

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    # Make md5 verification pass for whichever table is downloaded.
    monkeypatch.setattr(
        "cli.lib.pull._file_md5",
        lambda p: "h_normal" if "normal_tbl" in str(p) else "h_so",
        raising=False,
    )

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    # The server_only table is counted as listed alongside the normal one.
    assert result.parquets_total == 2, (
        f"both local tables must be listed in parquets_total, got {result.parquets_total}"
    )

    # Only the normal table's parquet lands on disk.
    parquet_dir = tmp_path / "server" / "parquet"
    assert (parquet_dir / "normal_tbl.parquet").exists(), "normal local table must download"
    assert not (parquet_dir / "so_tbl.parquet").exists(), (
        "server_only table must NOT be downloaded by agnes pull"
    )

    # No GET was issued for the server_only tid.
    assert not any("so_tbl" in p for p in downloaded_tids), (
        f"agnes pull must not GET the server_only parquet; downloads={downloaded_tids}"
    )
    assert any("normal_tbl" in p for p in downloaded_tids), (
        f"normal local table GET must fire; downloads={downloaded_tids}"
    )
    assert result.tables_updated == 1


def test_pull_prunes_stale_parquet_when_table_flips_to_server_only(
    tmp_path, monkeypatch,
):
    """#630 review: a table downloaded while server_only=false must lose its
    local parquet (and sync-state row) on the first pull after the admin
    flips server_only=true — otherwise the stale copy keeps a local view
    alive and the table stays locally queryable."""
    canned_manifest = {
        "tables": {
            "so_tbl": {
                "hash": "h_so", "rows": 0, "size_bytes": 0,
                "query_mode": "local", "server_only": True,
            },
        }
    }
    canned_memory = {"mandatory": [], "approved": []}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr(
        "cli.lib.pull.stream_download",
        lambda *a, **k: pytest.fail("server_only table must not download"),
        raising=False,
    )
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)

    # Pre-flip residue: the parquet landed on a previous pull while the
    # table was still distributed, with a matching sync-state row.
    from cli.config import save_sync_state
    save_sync_state({
        "tables": {"so_tbl": {"hash": "h_so", "rows": 0, "size_bytes": 0}},
        "last_sync": "2026-01-01T00:00:00+00:00",
    })
    parquet_dir = tmp_path / "server" / "parquet"
    parquet_dir.mkdir(parents=True)
    (parquet_dir / "so_tbl.parquet").write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert not (parquet_dir / "so_tbl.parquet").exists(), (
        "stale parquet must be pruned once the manifest marks the table "
        "server_only"
    )
    assert result.tables_removed == 1

    from cli.config import get_sync_state
    assert "so_tbl" not in get_sync_state().get("tables", {}), (
        "the pruned table's sync-state row must be dropped with the parquet"
    )
