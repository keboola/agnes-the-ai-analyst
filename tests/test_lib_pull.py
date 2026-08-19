"""Tests for cli/lib/pull.py:run_pull."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import PullResult, run_pull


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
    assert not (tmp_path / "server" / "parquet").exists(), "lazy mkdir: empty manifest must not create server/parquet/"


def test_run_pull_empty_memory_no_rules_dir(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert not (tmp_path / ".claude" / "rules").exists(), "lazy mkdir: empty bundle must not create .claude/rules/"


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
    tmp_path,
    monkeypatch,
):
    """Regression: hash-equal-but-file-missing must re-download.

    Repro: the workspace's `.claude/agnes/sync_state.json` says the local
    parquet is in sync with the server (hashes match), but the actual
    `<workspace>/server/parquet/<tid>.parquet` file is gone — manual rm, an
    operator nuking server/parquet/, the one-time legacy-state migration
    (#1311) seeding a hash from a stale machine-global record, etc. Pre-fix
    `agnes pull` would skip the download (hash matches) and the next DuckDB
    view rebuild would fail on a missing file. Now the existence check
    forces a re-download even when the hash equality says "you have this."
    """
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

    # Seed the workspace-scoped sync_state claiming we already have tbl1
    # with the matching hash — but DON'T put a parquet on disk. Pre-fix
    # this combo would short-circuit the download.
    from cli.config import save_sync_state

    save_sync_state(
        {
            "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}},
            "last_sync": "2026-01-01T00:00:00+00:00",
        },
        workspace=tmp_path,
    )

    target_parquet = tmp_path / "server" / "parquet" / "tbl1.parquet"
    assert not target_parquet.exists(), "fixture precondition: parquet absent"

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 1, (
        f"hash-equal-but-file-missing must trigger a re-download — got {download_calls['count']} download calls"
    )
    assert target_parquet.exists(), "parquet must be on disk after re-download"
    assert result.tables_updated == 1


def test_run_pull_skips_download_when_hash_matches_and_file_present(
    tmp_path,
    monkeypatch,
):
    """Counterpart: when sync_state agrees with server AND the parquet
    actually exists, the download is correctly skipped — that's the
    fast-path the existence check must NOT regress."""
    canned_manifest = {"tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}}}
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

    # Seed both the workspace-scoped sync_state AND the parquet on disk.
    from cli.config import save_sync_state

    save_sync_state(
        {
            "tables": {"tbl1": {"hash": "abc", "rows": 0, "size_bytes": 0}},
            "last_sync": "2026-01-01T00:00:00+00:00",
        },
        workspace=tmp_path,
    )
    parquet_dir = tmp_path / "server" / "parquet"
    parquet_dir.mkdir(parents=True)
    (parquet_dir / "tbl1.parquet").write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 0, (
        f"hash equal AND file present must skip the download — got {download_calls['count']} unwanted downloads"
    )
    assert result.tables_updated == 0


def test_download_one_retries_on_hash_mismatch_then_succeeds(
    tmp_path,
    monkeypatch,
):
    """#596 (a): the first download yields md5 != manifest hash, the second
    yields the matching hash. `_download_one`'s bounded retry loop must
    re-download and land the parquet — tables_updated == 1, no error."""
    canned_manifest = {"tables": {"tbl1": {"hash": "good", "rows": 0, "size_bytes": 0}}}
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


def test_download_one_honors_the_shared_retry_budget(tmp_path, monkeypatch):
    """The single-file path must spend the budget named by `_DOWNLOAD_RETRIES`,
    not a hardcoded count. This is the single-file half of the retry parity the
    partitioned path relies on — `tests/test_pull_partitioned.py` pins the
    other half against the same constant, so dropping retry from EITHER path
    turns something red."""
    canned_manifest = {"tables": {"tbl1": {"hash": "good", "rows": 0, "size_bytes": 0}}}
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
        from pathlib import Path as _P

        download_calls["count"] += 1
        _P(target_path).write_bytes(b"PAR1" + b"\x00" * 100 + b"PAR1")
        return 0

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    # Never matches the manifest hash → the retry budget is spent in full.
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "bad", raising=False)
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None)
    monkeypatch.setattr("cli.lib.pull._DOWNLOAD_RETRIES", 4)

    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert download_calls["count"] == 5, (
        f"expected 4 retries + 1 initial from the shared constant, got {download_calls['count']}"
    )


def test_textual_progress_reset_zeroes_failed_attempt_bytes():
    """#626 review: a hash-mismatch retry re-reports the whole file through
    the same callback. `reset()` must zero the per-file state between
    attempts or the display inflates past the file's total
    (e.g. "200.0 MB / 100.0 MB")."""
    import io

    from cli.lib.pull import _TextualProgress

    tp = _TextualProgress(
        stream=io.StringIO(),
        total_files=1,
        file_sizes={"tbl1": 100},
    )
    tp.advance("tbl1", 100)  # attempt 0: full download, fails verification
    tp.reset("tbl1")  # retry starts clean
    tp.advance("tbl1", 60)
    assert tp._bytes["tbl1"] == 60, "retry bytes must not stack on top of the failed attempt's"


def test_download_retry_resets_progress_between_attempts(tmp_path, monkeypatch):
    """#626 review: the retry loop must invoke the progress reset before
    re-downloading, so each attempt reports at most the file's size."""
    canned_manifest = {"tables": {"tbl1": {"hash": "good", "rows": 0, "size_bytes": 1008}}}
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
        from pathlib import Path as _P

        if progress_callback is not None:
            progress_callback(len(parquet_bytes))
        _P(target_path).write_bytes(parquet_bytes)
        return len(parquet_bytes)

    md5_calls = {"count": 0}

    def _file_md5(path):
        md5_calls["count"] += 1
        return "bad" if md5_calls["count"] == 1 else "good"

    resets = {"count": 0}

    class _SpyTextual:
        def __init__(self, *, stream, total_files, file_sizes):
            pass

        def advance(self, tid, n):
            pass

        def reset(self, tid):
            resets["count"] += 1

        def finish(self):
            pass

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr(
        "cli.lib.pull.stream_download",
        _stream_download,
        raising=False,
    )
    monkeypatch.setattr(
        "cli.lib.pull._is_valid_parquet",
        lambda p: True,
        raising=False,
    )
    monkeypatch.setattr("cli.lib.pull._file_md5", _file_md5, raising=False)
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None, raising=False)
    # Force the non-TTY textual path with the spy standing in for
    # _TextualProgress (pytest's captured stderr is already non-TTY, but
    # being explicit keeps the test honest under -s).
    import sys as _sys

    monkeypatch.setattr(_sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr("cli.lib.pull._TextualProgress", _SpyTextual)

    result = run_pull(
        server_url="http://x",
        token="t",
        workspace=tmp_path,
        show_progress=True,
    )

    assert result.tables_updated == 1
    assert resets["count"] == 1, (
        f"exactly one retry happened, so the progress must be reset exactly once — got {resets['count']}"
    )


def test_download_one_preserves_old_file_on_persistent_hash_mismatch(
    tmp_path,
    monkeypatch,
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

    save_sync_state(
        {
            "tables": {"tbl1": {"hash": "oldhash", "rows": 0, "size_bytes": 0}},
            "last_sync": "2026-01-01T00:00:00+00:00",
        },
        workspace=tmp_path,
    )

    canned_manifest = {"tables": {"tbl1": {"hash": "serverhash", "rows": 0, "size_bytes": 0}}}
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
            f"download must land in the sidecar, not the live target — got {target_path}"
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
    assert any(e.get("table") == "tbl1" for e in result.errors), "persistent mismatch must be recorded in result.errors"


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

    save_sync_state({"tables": tables, "last_sync": "2026-01-01T00:00:00+00:00"}, workspace=tmp_path)


def test_run_pull_prunes_local_parquet_when_table_leaves_typed_stack(
    tmp_path,
    monkeypatch,
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

    synced = get_sync_state(workspace=tmp_path)["tables"]
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
        c.execute(f"COPY (SELECT 1 AS x) TO '{pq_dir / (n + '.parquet')}' (FORMAT PARQUET)")
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
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "tbl1" in views, "authorized table keeps its view"
    assert "tbl2" not in views, "pruned table's view must be gone"


def test_run_pull_download_set_ignores_admin_overlisted_flat_tables(
    tmp_path,
    monkeypatch,
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
    assert not (pq_dir / "tbl_extra.parquet").exists(), "admin-overlisted flat table must not be downloaded"
    assert not any("tbl_extra" in p for p in downloads), "no stream_download call may target tbl_extra"
    assert any("tbl_keep" in p for p in downloads)


def test_run_pull_legacy_server_without_typed_sections_no_prune(
    tmp_path,
    monkeypatch,
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
    assert (pq_dir / "tbl_orphan.parquet").exists(), "pre-v49 fallback must not prune local parquets"
    assert (pq_dir / "tbl_flat.parquet").exists(), "flat-dict table still downloads"
    assert result.tables_removed == 0


def test_run_pull_memory_domains_only_manifest_does_not_prune(
    tmp_path,
    monkeypatch,
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
    assert (pq_dir / "tbl_existing.parquet").exists(), "memory_domains-only manifest must not prune a listed table"
    assert (pq_dir / "tbl_orphan.parquet").exists(), (
        "#594: memory_domains-only manifest must not prune an on-disk orphan"
    )
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
                "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
            ).fetchall()
        }
        rows = conn.execute("SELECT x FROM my_scratch").fetchall()
    finally:
        conn.close()
    assert "my_scratch" in base_tables, "user BASE TABLE must survive prune"
    assert rows == [(1,)]
    assert not any(e.get("table") == "my_scratch" for e in result.errors), "no error recorded for the user base table"


# ---------------------------------------------------------------------------
# #1311 — sync_state is workspace-scoped: two workspaces on the same machine
# must not share one download-hash record.
# ---------------------------------------------------------------------------


def test_run_pull_workspace_state_does_not_leak_across_workspaces(tmp_path, monkeypatch):
    """A second workspace on the same machine must not inherit the first
    workspace's fresh hash record and wrongly skip its own stale parquet's
    re-download.

    Repro: both workspaces already have an independent, up-to-date-as-of-
    "v1" scoped state AND a matching on-disk parquet (two prior,
    already-migrated pulls). The server bumps the table to "v2". Workspace A
    pulls first and updates its OWN state. Under the pre-#1311 machine-global
    file, workspace B's `run_pull` would then see A's already-fresh "v2"
    record, match it against the server's "v2", and skip its own download —
    despite B's on-disk parquet still holding "v1" bytes. Scoped state means
    B's own record is untouched by A's write, so B correctly re-downloads.
    """
    from cli.config import get_sync_state, save_sync_state

    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"

    for ws in (ws_a, ws_b):
        pq = ws / "server" / "parquet"
        pq.mkdir(parents=True)
        (pq / "tbl1.parquet").write_bytes(b"OLD-V1" + b"\x00" * 50)
        save_sync_state(
            {
                "tables": {"tbl1": {"hash": "v1", "rows": 0, "size_bytes": 0}},
                "last_sync": "2026-01-01T00:00:00+00:00",
            },
            workspace=ws,
        )

    manifest = {"tables": {"tbl1": {"hash": "v2", "rows": 0, "size_bytes": 0}}}
    canned_memory = {"mandatory": [], "approved": []}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        resp.raise_for_status = lambda: None
        return resp

    downloaded: list[str] = []

    def _stream_download(path, target_path, progress_callback=None):
        from pathlib import Path as _P

        downloaded.append(str(target_path))
        _P(target_path).write_bytes(b"NEW-V2" + b"\x00" * 50)
        return 56

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "v2", raising=False)

    result_a = run_pull(server_url="http://x", token="t", workspace=ws_a)
    assert result_a.tables_updated == 1
    assert len(downloaded) == 1

    result_b = run_pull(server_url="http://x", token="t", workspace=ws_b)
    assert result_b.tables_updated == 1, (
        "workspace B must re-download tbl1 independently of workspace A's already-fresh state — "
        f"got tables_updated={result_b.tables_updated}, downloads so far={downloaded}"
    )
    assert len(downloaded) == 2

    assert get_sync_state(workspace=ws_a)["tables"]["tbl1"]["hash"] == "v2"
    assert get_sync_state(workspace=ws_b)["tables"]["tbl1"]["hash"] == "v2"


# ---------------------------------------------------------------------------
# #1325 — a table the v49 stack sync fetches for the FIRST time must already
# be queryable by the time `run_pull` returns, not one pull cycle later.
# ---------------------------------------------------------------------------


def test_run_pull_direct_table_is_queryable_in_the_same_pull(tmp_path, monkeypatch):
    """Regression for step ordering: `_rebuild_duckdb_views` used to run
    BEFORE the v49 stack sync populated `.claude/data/`, so a table synced
    for the first time by a given `agnes pull` stayed unqueryable until the
    NEXT pull. The stack sync must run first."""
    manifest = {
        "tables": {},
        "direct_tables": [_typed_table("orders")],
        "data_packages": [],
        "memory_domains": [],
    }

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
        # Real parquet content (unlike `_PARQUET`'s PAR1-bracketed zero
        # bytes elsewhere in this file) — CREATE VIEW needs a real,
        # bindable footer to prove the view actually resolves.
        import duckdb as _duckdb
        from pathlib import Path as _P

        _P(target_path).parent.mkdir(parents=True, exist_ok=True)
        c = _duckdb.connect()
        try:
            c.execute(f"COPY (SELECT 42 AS answer) TO '{target_path}' (FORMAT PARQUET)")
        finally:
            c.close()

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)
    monkeypatch.setattr("cli.lib.pull._file_md5", lambda p: "h", raising=False)

    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    ref = tmp_path / ".claude" / "data" / "_direct" / "orders.parquet"
    assert ref.exists(), "stack sync must have fetched the direct table"

    import duckdb

    conn = duckdb.connect(str(tmp_path / "user" / "duckdb" / "analytics.duckdb"))
    try:
        assert conn.execute("SELECT answer FROM orders").fetchone()[0] == 42, (
            "a table synced by THIS pull's stack sync must already be queryable when run_pull returns"
        )
    finally:
        conn.close()


class TestMaterializedSkippedCount:
    """`materialized_skipped` is printed by `agnes init` as "N materialized
    row(s) skipped by default -- re-run with --materialize". So it must count
    exactly the rows such a re-run WOULD fetch: a row outside the analyst's
    stack, or one the server never distributes, is not one of them, and
    counting it sends the analyst after data they cannot have.
    """

    @staticmethod
    def _manifest(tables, *, typed=None):
        m = {"tables": tables}
        if typed is not None:
            m["direct_tables"] = [{"name": n} for n in typed]
        return m

    def _run(self, monkeypatch, tmp_path, manifest):
        canned = {"/api/sync/manifest": manifest, "/api/memory/bundle": {"mandatory": [], "approved": []}}

        def _api_get(path, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = canned.get(path, {})
            resp.iter_bytes = lambda chunk_size=65536: iter([b""])
            resp.raise_for_status = lambda: None
            return resp

        monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
        return run_pull("http://server", "tok", tmp_path, skip_materialize=True)

    def test_counts_a_materialized_row_the_analyst_could_fetch(self, monkeypatch, tmp_path):
        result = self._run(
            monkeypatch,
            tmp_path,
            self._manifest({"mat": {"query_mode": "materialized", "hash": "h"}}),
        )
        assert result.materialized_skipped == 1

    def test_does_not_count_a_row_outside_the_analyst_stack(self, monkeypatch, tmp_path):
        """Typed sections present makes the stack the unit of access. A
        materialized row the stack omits is never downloaded, with or without
        --materialize, so it is not "skipped by default"."""
        result = self._run(
            monkeypatch,
            tmp_path,
            self._manifest(
                {
                    "in_stack": {"query_mode": "materialized", "hash": "h"},
                    "out_of_stack": {"query_mode": "materialized", "hash": "h"},
                },
                typed=["in_stack"],
            ),
        )
        assert result.materialized_skipped == 1, "only the in-stack row is fetchable"

    def test_does_not_count_a_server_only_row(self, monkeypatch, tmp_path):
        """`server_only` parquets are never shipped to laptops (#607), so
        `--materialize` would not fetch this one either."""
        result = self._run(
            monkeypatch,
            tmp_path,
            self._manifest({"mat": {"query_mode": "materialized", "hash": "h", "server_only": True}}),
        )
        assert result.materialized_skipped == 0

    def test_an_ordinary_table_is_never_counted_as_skipped(self, monkeypatch, tmp_path):
        """The counter used to be `parquets_total`, which counts tables that
        WERE considered — so a plain local table read as a skipped one."""
        result = self._run(
            monkeypatch,
            tmp_path,
            self._manifest({"plain": {"query_mode": "local", "hash": "h"}}),
        )
        assert result.materialized_skipped == 0
