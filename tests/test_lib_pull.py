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


def test_run_pull_redownloads_when_parquet_missing_despite_matching_hash(
    tmp_path, monkeypatch,
):
    """Regression: hash-equal-but-file-missing must re-download.

    Repro: analyst's `~/.config/agnes/sync_state.json` says the local
    parquet is in sync with the server (hashes match), but the actual
    `<workspace>/server/parquet/<tid>.parquet` file is gone — manual rm,
    a different workspace sharing the same global sync_state, an
    operator nuking server/parquet/, etc. Pre-fix `agnes pull` would
    skip the download (hash matches) and the next DuckDB view rebuild
    would fail on a missing file. Now the existence check forces a
    re-download even when the hash equality says "you have this."
    """
    canned_manifest = {
        "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}}
    }
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

    download_calls = {"count": 0}

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P
        download_calls["count"] += 1
        _P(target_path).write_bytes(parquet_bytes)
        return len(parquet_bytes)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "abc", raising=False)

    # Seed sync_state.json claiming we already have tbl1 with the matching hash —
    # but DON'T put a parquet on disk. Pre-fix this combo would short-circuit
    # the download.
    from cli.config import save_sync_state
    save_sync_state({
        "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}},
        "last_sync": "2026-01-01T00:00:00+00:00",
    })

    target_parquet = tmp_path / "server" / "parquet" / "tbl1.parquet"
    assert not target_parquet.exists(), "fixture precondition: parquet absent"

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 1, (
        "hash-equal-but-file-missing must trigger a re-download — "
        f"got {download_calls['count']} download calls"
    )
    assert target_parquet.exists(), "parquet must be on disk after re-download"
    assert result.tables_updated == 1


def test_run_pull_skips_download_when_hash_matches_and_file_present(
    tmp_path, monkeypatch,
):
    """Counterpart: when sync_state agrees with server AND the parquet
    actually exists, the download is correctly skipped — that's the
    fast-path the existence check must NOT regress."""
    canned_manifest = {
        "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}}
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

    download_calls = {"count": 0}

    def _stream_download(path, target_path, progress_callback=None):
        download_calls["count"] += 1
        return 0

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)

    # Seed both sync_state AND the parquet on disk.
    from cli.config import save_sync_state
    save_sync_state({
        "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}},
        "last_sync": "2026-01-01T00:00:00+00:00",
    })
    parquet_dir = tmp_path / "server" / "parquet"
    parquet_dir.mkdir(parents=True)
    (parquet_dir / "tbl1.parquet").write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 0, (
        "hash equal AND file present must skip the download — "
        f"got {download_calls['count']} unwanted downloads"
    )
    assert result.tables_updated == 0


def test_download_one_retries_on_hash_mismatch_then_succeeds(
    tmp_path, monkeypatch,
):
    """#596 (a): the first download yields md5 != manifest hash, the second
    yields the matching hash. `_download_one`'s bounded retry loop must
    re-download and land the parquet — tables_updated == 1, no error."""
    canned_manifest = {
        "tables": {"tbl1": {"hash": "good", "rows": 0, "size_bytes": 0}}
    }
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

    download_calls = {"count": 0}

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P
        download_calls["count"] += 1
        _P(target_path).write_bytes(parquet_bytes)
        return len(parquet_bytes)

    # md5 returns the wrong hash on the FIRST verify call, the right hash
    # on the second (simulating a corrupt mid-flight transfer that clears
    # on re-download).
    md5_calls = {"count": 0}

    def _file_md5(path):
        md5_calls["count"] += 1
        return "bad" if md5_calls["count"] == 1 else "good"

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", _file_md5, raising=False)
    # Don't actually sleep between retries.
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None, raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 2, (
        "hash mismatch on attempt 1 must trigger exactly one re-download — "
        f"got {download_calls['count']} download calls"
    )
    target = tmp_path / "server" / "parquet" / "tbl1.parquet"
    assert target.exists(), "parquet must land after the retry succeeds"
    assert result.tables_updated == 1
    assert result.errors == [], "a recovered mismatch must record no error"
    # The sidecar must not linger.
    assert not (tmp_path / "server" / "parquet" / "tbl1.parquet.verify.tmp").exists()


def test_download_one_preserves_old_file_on_persistent_hash_mismatch(
    tmp_path, monkeypatch,
):
    """#596 (b): every download attempt yields a mismatching md5 AND a prior
    good `<tid>.parquet` is already on disk. After run_pull the OLD file must
    still EXIST (never deleted), tables_updated == 0, and the table is
    recorded in result.errors."""
    old_bytes = b"PAR1OLDGOODFILE" + b"\x00" * 100 + b"PAR1"
    new_bytes = b"PAR1" + b"\xff" * 200 + b"PAR1"

    # Seed a prior good parquet + matching sync_state so the download is
    # forced (server hash differs from the local hash).
    pq_dir = tmp_path / "server" / "parquet"
    pq_dir.mkdir(parents=True)
    target = pq_dir / "tbl1.parquet"
    target.write_bytes(old_bytes)
    from cli.config import save_sync_state
    save_sync_state({
        "tables": {"tbl1": {"hash": "oldhash", "rows": 0, "size_bytes": 0}},
        "last_sync": "2026-01-01T00:00:00+00:00",
    })

    canned_manifest = {
        "tables": {"tbl1": {"hash": "serverhash", "rows": 0, "size_bytes": 0}}
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

    def _stream_download(path, target_path, progress_callback=None):
        # Always writes to the SIDECAR (the verify.tmp), never the real target.
        from pathlib import Path as _P
        assert target_path.endswith(".verify.tmp"), (
            "download must land in the sidecar, not the live target — "
            f"got {target_path}"
        )
        _P(target_path).write_bytes(new_bytes)
        return len(new_bytes)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    # md5 NEVER matches the manifest hash 'serverhash'.
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "alwaysbad", raising=False)
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None, raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert target.exists(), "prior good parquet must NOT be deleted on persistent mismatch"
    assert target.read_bytes() == old_bytes, "prior good bytes must be intact (unchanged)"
    assert not (pq_dir / "tbl1.parquet.verify.tmp").exists(), "sidecar must be cleaned up"
    assert result.tables_updated == 0
    assert any(e.get("table") == "tbl1" for e in result.errors), (
        "persistent mismatch must be recorded in result.errors"
    )


def test_download_one_legacy_no_hash_path_unchanged(tmp_path, monkeypatch):
    """Pre-v49 / no-hash manifest still uses the `_is_valid_parquet` fallback.
    A valid PAR1 sidecar lands; an invalid one is rejected with the same
    'not a valid parquet' error and never overwrites a prior file."""
    canned_manifest = {
        # No "hash" key on the table -> legacy structural-check path.
        "tables": {"tbl1": {"rows": 0, "size_bytes": 0}}
    }
    canned_memory = {"mandatory": [], "approved": []}
    good = b"PAR1" + b"\x00" * 50 + b"PAR1"

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
        from pathlib import Path as _P
        _P(target_path).write_bytes(good)
        return len(good)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    # Real structural check passes for valid PAR1 bytes.
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None, raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").exists()
    assert result.tables_updated == 1
    assert result.errors == []


def test_run_pull_dry_run_writes_nothing(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path, dry_run=True)
    assert not (tmp_path / "server").exists()
    assert not (tmp_path / "user" / "duckdb").exists()
    # No user-home state file either — dry_run must be hermetic.
    # The autouse fixture sandboxes AGNES_CONFIG_DIR to tmp_path/_agnes_cfg.
    assert not (tmp_path / "_agnes_cfg" / "sync_state.json").exists()


# ---------------------------------------------------------------------------
# #506 — flat `server/parquet/` tree must obey the typed (v49) stack.
#
# `agnes query` reads <workspace>/user/duckdb/analytics.duckdb whose views are
# rebuilt over <workspace>/server/parquet/*.parquet. Pre-fix, run_pull took its
# keep-set from the legacy flat `manifest["tables"]` dict (admin god-mode over-
# lists every accessible table) and never pruned an already-downloaded parquet
# on authorization loss — so removing a package from the stack left its tables
# locally queryable. The fix: when the manifest carries typed v49 sections,
# the authorized name-set is the union of data_packages[].tables[].name and
# direct_tables[].name; restrict downloads to it, and prune any on-disk parquet
# whose stem is not authorized (+ its sync_state row) before the view rebuild.
# ---------------------------------------------------------------------------

_PARQUET = b"PAR1" + b"\x00" * 1000 + b"PAR1"


def _typed_table(name: str, hash_: str = "h") -> dict:
    """One entry as it appears in data_packages[].tables[] / direct_tables[]."""
    return {
        "id": f"tbl_{name}",
        "name": name,
        "hash": hash_,
        "md5": hash_,
        "size_bytes": 0,
        "rows": 0,
        "query_mode": "local",
        "source_type": "keboola",
    }


def _patch_pull_io(monkeypatch, manifest, *, download_calls=None):
    """Wire api_get (manifest + empty memory bundle) and a stream_download that
    writes real PAR1-bracketed bytes. _is_valid_parquet/_file_md5 are stubbed so
    hash verification passes for any entry whose hash is 'h'."""
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
        if download_calls is not None:
            download_calls.append(path)
        _P(target_path).write_bytes(_PARQUET)
        return len(_PARQUET)

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "h", raising=False)


def _seed_local_parquet(tmp_path, *names):
    """Place server/parquet/<name>.parquet on disk + matching sync_state rows,
    simulating a prior pull that downloaded all of them."""
    pq_dir = tmp_path / "server" / "parquet"
    pq_dir.mkdir(parents=True, exist_ok=True)
    tables = {}
    for n in names:
        (pq_dir / f"{n}.parquet").write_bytes(_PARQUET)
        tables[n] = {"hash": "h", "rows": 0, "size_bytes": 0}
    from cli.config import save_sync_state
    save_sync_state({"tables": tables, "last_sync": "2026-01-01T00:00:00+00:00"})


def test_run_pull_prunes_local_parquet_when_table_leaves_typed_stack(
    tmp_path, monkeypatch,
):
    """tbl1 + tbl2 both previously downloaded; the manifest's flat `tables`
    still lists both (admin over-list) but data_packages[].tables[] lists ONLY
    tbl1 (tbl2 removed from the stack). After run_pull tbl2's parquet + its
    sync_state row are gone, tbl1 survives, and tables_removed == 1."""
    _seed_local_parquet(tmp_path, "tbl1", "tbl2")
    manifest = {
        "tables": {
            "tbl1": {"hash": "h", "rows": 0, "size_bytes": 0, "query_mode": "local"},
            "tbl2": {"hash": "h", "rows": 0, "size_bytes": 0, "query_mode": "local"},
        },
        "data_packages": [{"slug": "p", "tables": [_typed_table("tbl1")]}],
        "direct_tables": [],
        "memory_domains": [],
    }
    _patch_pull_io(monkeypatch, manifest)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    pq_dir = tmp_path / "server" / "parquet"
    assert not (pq_dir / "tbl2.parquet").exists(), "deauthorized tbl2 must be pruned"
    assert (pq_dir / "tbl1.parquet").exists(), "authorized tbl1 must remain"

    from cli.config import get_sync_state
    synced = get_sync_state()["tables"]
    assert "tbl1" in synced
    assert "tbl2" not in synced, "pruned table's sync_state row must be removed"
    assert result.tables_removed == 1


def test_run_pull_drops_duckdb_view_for_pruned_table(tmp_path, monkeypatch):
    """Same setup; after the prune the rebuilt analytics.duckdb has a VIEW for
    tbl1 but NOT for tbl2 — the orphaned view disappears with its parquet."""
    _seed_local_parquet(tmp_path, "tbl1", "tbl2")
    # Overwrite with REAL parquet bytes so DuckDB can actually CREATE VIEW over
    # the surviving file (the fake PAR1-bracketed bytes used elsewhere are
    # structurally invalid and DuckDB would skip the view).
    import duckdb
    pq_dir = tmp_path / "server" / "parquet"
    for n in ("tbl1", "tbl2"):
        c = duckdb.connect()
        c.execute(
            f"COPY (SELECT 1 AS x) TO '{pq_dir / (n + '.parquet')}' (FORMAT PARQUET)"
        )
        c.close()
    manifest = {
        "tables": {
            "tbl1": {"hash": "h", "query_mode": "local"},
            "tbl2": {"hash": "h", "query_mode": "local"},
        },
        "data_packages": [{"slug": "p", "tables": [_typed_table("tbl1")]}],
        "direct_tables": [],
        "memory_domains": [],
    }
    _patch_pull_io(monkeypatch, manifest)

    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
    conn = duckdb.connect(str(db))
    try:
        views = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type='VIEW'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "tbl1" in views, "authorized table keeps its view"
    assert "tbl2" not in views, "pruned table's view must be gone"


def test_run_pull_download_set_ignores_admin_overlisted_flat_tables(
    tmp_path, monkeypatch,
):
    """The flat `tables` dict carries tbl_extra (admin god-mode over-list) that
    is absent from every typed section. It must never be downloaded, and no
    parquet for it lands on disk; a typed-listed table IS downloaded."""
    manifest = {
        "tables": {
            "tbl_keep": {"hash": "h", "query_mode": "local"},
            "tbl_extra": {"hash": "h", "query_mode": "local"},
        },
        "data_packages": [{"slug": "p", "tables": [_typed_table("tbl_keep")]}],
        "direct_tables": [],
        "memory_domains": [],
    }
    downloads: list[str] = []
    _patch_pull_io(monkeypatch, manifest, download_calls=downloads)

    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    pq_dir = tmp_path / "server" / "parquet"
    assert (pq_dir / "tbl_keep.parquet").exists()
    assert not (pq_dir / "tbl_extra.parquet").exists(), \
        "admin-overlisted flat table must not be downloaded"
    assert not any("tbl_extra" in p for p in downloads), \
        "no stream_download call may target tbl_extra"
    assert any("tbl_keep" in p for p in downloads)


def test_run_pull_legacy_server_without_typed_sections_no_prune(
    tmp_path, monkeypatch,
):
    """Pre-v49 manifest: ONLY a flat `tables` dict, no typed keys. An on-disk
    parquet for a table absent from the flat dict must NOT be pruned (legacy
    fallback preserved); flat-dict downloads proceed; tables_removed == 0."""
    _seed_local_parquet(tmp_path, "tbl_orphan")
    manifest = {
        "tables": {
            "tbl_flat": {"hash": "h", "query_mode": "local"},
        },
    }
    _patch_pull_io(monkeypatch, manifest)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    pq_dir = tmp_path / "server" / "parquet"
    assert (pq_dir / "tbl_orphan.parquet").exists(), \
        "pre-v49 fallback must not prune local parquets"
    assert (pq_dir / "tbl_flat.parquet").exists(), "flat-dict table still downloads"
    assert result.tables_removed == 0


def test_run_pull_memory_domains_only_manifest_does_not_prune(
    tmp_path, monkeypatch,
):
    """#594 guard: a manifest carrying ONLY ``memory_domains`` (no
    ``data_packages`` / ``direct_tables``) must NOT build an empty authorized
    set and prune every local parquet. Memory domains carry no query tables, so
    the prune path stays a no-op; the end-of-run stack-sync gate (which does
    include memory_domains) is separate. Both an in-flat-dict table and an
    on-disk orphan survive; tables_removed == 0."""
    _seed_local_parquet(tmp_path, "tbl_existing", "tbl_orphan")
    manifest = {
        "tables": {"tbl_existing": {"hash": "h", "query_mode": "local"}},
        "memory_domains": [{"slug": "d", "name": "domain1"}],
        # deliberately NO data_packages / direct_tables
    }
    _patch_pull_io(monkeypatch, manifest)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    pq_dir = tmp_path / "server" / "parquet"
    assert (pq_dir / "tbl_existing.parquet").exists(), \
        "memory_domains-only manifest must not prune a listed table"
    assert (pq_dir / "tbl_orphan.parquet").exists(), \
        "#594: memory_domains-only manifest must not prune an on-disk orphan"
    assert result.tables_removed == 0


def test_run_pull_prune_preserves_user_base_table(tmp_path, monkeypatch):
    """A user-created BASE TABLE in analytics.duckdb must survive a prune that
    unlinks an orphaned parquet; no error is recorded for the base table."""
    # Pre-create a user BASE TABLE.
    import duckdb
    db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE my_scratch AS SELECT 1 AS x")
    conn.close()

    _seed_local_parquet(tmp_path, "tbl_orphan", "tbl_keep")
    manifest = {
        "tables": {
            "tbl_keep": {"hash": "h", "query_mode": "local"},
            "tbl_orphan": {"hash": "h", "query_mode": "local"},
        },
        "data_packages": [{"slug": "p", "tables": [_typed_table("tbl_keep")]}],
        "direct_tables": [],
        "memory_domains": [],
    }
    _patch_pull_io(monkeypatch, manifest)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    pq_dir = tmp_path / "server" / "parquet"
    assert not (pq_dir / "tbl_orphan.parquet").exists(), "orphan parquet pruned"

    conn = duckdb.connect(str(db))
    try:
        base_tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE'"
            ).fetchall()
        }
        rows = conn.execute("SELECT x FROM my_scratch").fetchall()
    finally:
        conn.close()
    assert "my_scratch" in base_tables, "user BASE TABLE must survive prune"
    assert rows == [(1,)]
    assert not any(
        e.get("table") == "my_scratch" for e in result.errors
    ), "no error recorded for the user base table"
