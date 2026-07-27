"""Client-side partitioned-table sync for `agnes pull` (partitioned
distribution). A partitioned table is stored locally as a directory of
parts under `server/parquet/{tid}/`; only changed parts are fetched
(incremental), the swap is all-or-nothing, and parts dropped server-side
are pruned locally.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cli.lib.pull import _diff_parts, _sync_partitioned_table


def _sp(path, b):
    return {"path": path, "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}


# --- _diff_parts -----------------------------------------------------------

def test_diff_parts_fresh_fetches_all(tmp_path):
    server = [_sp("month=2026-06/data.parquet", b"a"), _sp("month=2026-07/data.parquet", b"bb")]
    fetch, prune = _diff_parts(server, {}, tmp_path / "issues")
    assert {p["path"] for p in fetch} == {"month=2026-06/data.parquet", "month=2026-07/data.parquet"}
    assert prune == set()


def test_diff_parts_incremental_fetches_only_changed(tmp_path):
    tdir = tmp_path / "issues"
    (tdir / "month=2026-06").mkdir(parents=True)
    (tdir / "month=2026-06" / "data.parquet").write_bytes(b"a")
    (tdir / "month=2026-07").mkdir(parents=True)
    (tdir / "month=2026-07" / "data.parquet").write_bytes(b"old")
    server = [_sp("month=2026-06/data.parquet", b"a"), _sp("month=2026-07/data.parquet", b"NEW")]
    local = {"month=2026-06/data.parquet": hashlib.md5(b"a").hexdigest(),
             "month=2026-07/data.parquet": hashlib.md5(b"old").hexdigest()}
    fetch, prune = _diff_parts(server, local, tdir)
    assert {p["path"] for p in fetch} == {"month=2026-07/data.parquet"}  # only changed month
    assert prune == set()


def test_diff_parts_missing_file_refetched(tmp_path):
    """Hash matches local state but the file is gone → refetch."""
    server = [_sp("month=2026-06/data.parquet", b"a")]
    local = {"month=2026-06/data.parquet": hashlib.md5(b"a").hexdigest()}
    fetch, prune = _diff_parts(server, local, tmp_path / "issues")  # no dir on disk
    assert {p["path"] for p in fetch} == {"month=2026-06/data.parquet"}


def test_diff_parts_prunes_dropped_month(tmp_path):
    tdir = tmp_path / "issues"
    (tdir / "month=2026-05").mkdir(parents=True)
    (tdir / "month=2026-05" / "data.parquet").write_bytes(b"old")
    server = [_sp("month=2026-06/data.parquet", b"a")]
    local = {"month=2026-05/data.parquet": hashlib.md5(b"old").hexdigest()}
    fetch, prune = _diff_parts(server, local, tdir)
    assert prune == {"month=2026-05/data.parquet"}


# --- _sync_partitioned_table ----------------------------------------------

def _fetcher(parts_bytes):
    """Return a fetch_part(relpath, dest) that writes the known bytes."""
    def fetch_part(relpath, dest):
        dest.write_bytes(parts_bytes[relpath])
    return fetch_part


def test_sync_partitioned_fresh_download(tmp_path):
    b6, b7 = b"june-data", b"july-data-longer"
    server = [_sp("month=2026-06/data.parquet", b6), _sp("month=2026-07/data.parquet", b7)]
    rollup = "ROLLUP123"
    entry, changed, err = _sync_partitioned_table(
        "issues", server, {}, tmp_path, _fetcher({
            "month=2026-06/data.parquet": b6, "month=2026-07/data.parquet": b7}), rollup, rows=42)
    assert err is None
    assert changed is True
    assert (tmp_path / "issues" / "month=2026-06" / "data.parquet").read_bytes() == b6
    assert (tmp_path / "issues" / "month=2026-07" / "data.parquet").read_bytes() == b7
    assert entry["parts"] == {"month=2026-06/data.parquet": server[0]["hash"],
                              "month=2026-07/data.parquet": server[1]["hash"]}
    assert entry["hash"] == rollup
    assert entry["size_bytes"] == len(b6) + len(b7)
    assert entry["rows"] == 42


def test_sync_partitioned_all_or_nothing_on_hash_mismatch(tmp_path):
    """If a part's downloaded bytes don't match its hash, NOTHING is swapped
    in and the prior table dir is left intact (no silent partial view)."""
    tdir = tmp_path / "issues" / "month=2026-05"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"prior-good")
    server = [_sp("month=2026-06/data.parquet", b"correct")]
    # fetcher writes WRONG bytes → hash mismatch
    entry, changed, err = _sync_partitioned_table(
        "issues", server, {}, tmp_path,
        _fetcher({"month=2026-06/data.parquet": b"CORRUPT"}), "R", rows=1)
    assert entry is None and "mismatch" in err
    assert changed is False
    # prior data untouched; the bad part never promoted
    assert (tmp_path / "issues" / "month=2026-05" / "data.parquet").read_bytes() == b"prior-good"
    assert not (tmp_path / "issues" / "month=2026-06" / "data.parquet").exists()


def test_sync_partitioned_incremental_and_prune(tmp_path):
    """Unchanged parts stay, changed part refetched, dropped part pruned."""
    tdir = tmp_path / "issues"
    (tdir / "month=2026-06").mkdir(parents=True)
    (tdir / "month=2026-06" / "data.parquet").write_bytes(b"keep")
    (tdir / "month=2026-05").mkdir(parents=True)
    (tdir / "month=2026-05" / "data.parquet").write_bytes(b"drop-me")
    b6 = b"keep"
    b7 = b"new-july"
    server = [_sp("month=2026-06/data.parquet", b6), _sp("month=2026-07/data.parquet", b7)]
    local = {"month=2026-06/data.parquet": hashlib.md5(b6).hexdigest(),
             "month=2026-05/data.parquet": hashlib.md5(b"drop-me").hexdigest()}
    entry, changed, err = _sync_partitioned_table(
        "issues", server, local, tmp_path,
        _fetcher({"month=2026-07/data.parquet": b7}), "R", rows=2)
    assert err is None
    assert changed is True
    assert (tdir / "month=2026-06" / "data.parquet").read_bytes() == b6  # kept
    assert (tdir / "month=2026-07" / "data.parquet").read_bytes() == b7  # fetched
    assert not (tdir / "month=2026-05" / "data.parquet").exists()        # pruned
    assert set(entry["parts"]) == {"month=2026-06/data.parquet", "month=2026-07/data.parquet"}


def test_sync_partitioned_noop_reports_not_changed(tmp_path):
    """When every part is already current (nothing fetched, nothing pruned),
    the sync succeeds but reports changed=False so the pull summary does not
    over-count it as an update (Devin #2)."""
    tdir = tmp_path / "issues" / "month=2026-06"
    tdir.mkdir(parents=True)
    b = b"already-here"
    (tdir / "data.parquet").write_bytes(b)
    server = [_sp("month=2026-06/data.parquet", b)]
    local = {"month=2026-06/data.parquet": hashlib.md5(b).hexdigest()}

    entry, changed, err = _sync_partitioned_table(
        "issues", server, local, tmp_path, _fetcher({}), "R", rows=1)
    assert err is None
    assert changed is False
    assert entry is not None  # still returns the current state entry
