"""Client-side partitioned-table sync for `agnes pull` (partitioned
distribution). A partitioned table is stored locally as a directory of
parts under `server/parquet/{tid}/`; only changed parts are fetched
(incremental), the swap is all-or-nothing, and parts dropped server-side
are pruned locally.
"""

from __future__ import annotations

import hashlib

import pytest

from cli.lib.pull import (
    _DOWNLOAD_RETRIES,
    _diff_parts,
    _retry_backoff,
    _sync_partitioned_table,
)


def _sp(path, b):
    return {"path": path, "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}


@pytest.fixture
def no_sleep(monkeypatch):
    """Don't burn the retry backoffs in wall-clock time."""
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: None)


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
    local = {
        "month=2026-06/data.parquet": hashlib.md5(b"a").hexdigest(),
        "month=2026-07/data.parquet": hashlib.md5(b"old").hexdigest(),
    }
    fetch, prune = _diff_parts(server, local, tdir)
    assert {p["path"] for p in fetch} == {"month=2026-07/data.parquet"}  # only changed month
    assert prune == set()


def test_diff_parts_missing_file_refetched(tmp_path):
    """Hash matches local state but the file is gone → refetch."""
    server = [_sp("month=2026-06/data.parquet", b"a")]
    local = {"month=2026-06/data.parquet": hashlib.md5(b"a").hexdigest()}
    fetch, _prune = _diff_parts(server, local, tmp_path / "issues")  # no dir on disk
    assert {p["path"] for p in fetch} == {"month=2026-06/data.parquet"}


def test_diff_parts_prunes_dropped_month(tmp_path):
    tdir = tmp_path / "issues"
    (tdir / "month=2026-05").mkdir(parents=True)
    (tdir / "month=2026-05" / "data.parquet").write_bytes(b"old")
    server = [_sp("month=2026-06/data.parquet", b"a")]
    local = {"month=2026-05/data.parquet": hashlib.md5(b"old").hexdigest()}
    _fetch, prune = _diff_parts(server, local, tdir)
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
        "issues",
        server,
        {},
        tmp_path,
        _fetcher({"month=2026-06/data.parquet": b6, "month=2026-07/data.parquet": b7}),
        rollup,
        rows=42,
    )
    assert err is None
    assert changed is True
    assert (tmp_path / "issues" / "month=2026-06" / "data.parquet").read_bytes() == b6
    assert (tmp_path / "issues" / "month=2026-07" / "data.parquet").read_bytes() == b7
    assert entry["parts"] == {
        "month=2026-06/data.parquet": server[0]["hash"],
        "month=2026-07/data.parquet": server[1]["hash"],
    }
    assert entry["hash"] == rollup
    assert entry["size_bytes"] == len(b6) + len(b7)
    assert entry["rows"] == 42


def test_sync_partitioned_all_or_nothing_on_hash_mismatch(tmp_path, no_sleep):
    """If a part's downloaded bytes never match its hash, NOTHING is swapped in
    and the prior table dir is left intact (no silent partial view). The retry
    budget is spent in full first."""
    tdir = tmp_path / "issues" / "month=2026-05"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"prior-good")
    server = [_sp("month=2026-06/data.parquet", b"correct")]
    attempts = {"n": 0}

    # fetcher writes WRONG bytes → hash mismatch, every time
    def always_bad(relpath, dest):
        attempts["n"] += 1
        dest.write_bytes(b"CORRUPT")

    entry, changed, err = _sync_partitioned_table("issues", server, {}, tmp_path, always_bad, "R", rows=1)

    assert entry is None and "mismatch" in err
    assert changed is False
    assert attempts["n"] == _DOWNLOAD_RETRIES + 1
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
    local = {
        "month=2026-06/data.parquet": hashlib.md5(b6).hexdigest(),
        "month=2026-05/data.parquet": hashlib.md5(b"drop-me").hexdigest(),
    }
    entry, changed, err = _sync_partitioned_table(
        "issues", server, local, tmp_path, _fetcher({"month=2026-07/data.parquet": b7}), "R", rows=2
    )
    assert err is None
    assert changed is True
    assert (tdir / "month=2026-06" / "data.parquet").read_bytes() == b6  # kept
    assert (tdir / "month=2026-07" / "data.parquet").read_bytes() == b7  # fetched
    assert not (tdir / "month=2026-05" / "data.parquet").exists()  # pruned
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

    entry, changed, err = _sync_partitioned_table("issues", server, local, tmp_path, _fetcher({}), "R", rows=1)
    assert err is None
    assert changed is False
    assert entry is not None  # still returns the current state entry


def test_sync_partitioned_download_error_is_returned_not_raised(tmp_path, no_sleep):
    """A network/transport error while fetching a part must be RETURNED as an
    error (so run_pull records it and moves on), never RAISED — a raise would
    abort the whole pull, discarding tables that already downloaded fine
    (Devin re-review #A). The error surfaces only after the retry budget is
    spent."""
    tdir = tmp_path / "issues" / "month=2026-05"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"prior-good")
    server = [_sp("month=2026-06/data.parquet", b"x")]
    attempts = {"n": 0}

    def boom(relpath, dest):
        attempts["n"] += 1
        raise ConnectionError("network blip")

    entry, changed, err = _sync_partitioned_table("issues", server, {}, tmp_path, boom, "R", rows=1)
    assert entry is None
    assert changed is False
    assert err is not None and "network blip" in err
    assert attempts["n"] == _DOWNLOAD_RETRIES + 1
    # prior data untouched (all-or-nothing)
    assert (tmp_path / "issues" / "month=2026-05" / "data.parquet").read_bytes() == b"prior-good"


# --- per-part retry (#596/#626 parity) -------------------------------------
#
# The single-file path retries a bad download `_DOWNLOAD_RETRIES` times before
# giving up. The partitioned path fetched each part exactly once, so the first
# bad part killed the whole table's sync. These guard the parity.


@pytest.mark.parametrize("first_failure", ["hash_mismatch", "transport_error"])
def test_sync_partitioned_retries_part_then_succeeds(tmp_path, no_sleep, first_failure):
    """Either symptom of one flaky transfer — wrong bytes, or a dead connection
    — is retried, and the good bytes on the second attempt sync normally."""
    good = b"june-data"
    server = [_sp("month=2026-06/data.parquet", good)]
    attempts = {"n": 0}

    def flaky(relpath, dest):
        attempts["n"] += 1
        if attempts["n"] == 1:
            if first_failure == "transport_error":
                raise ConnectionError("network blip")
            dest.write_bytes(b"CORRUPT")
            return
        dest.write_bytes(good)

    _entry, changed, err = _sync_partitioned_table("issues", server, {}, tmp_path, flaky, "R", rows=1)

    assert err is None
    assert changed is True
    assert attempts["n"] == 2, f"expected one retry, got {attempts['n']} attempts"
    assert (tmp_path / "issues" / "month=2026-06" / "data.parquet").read_bytes() == good


def test_sync_partitioned_retry_does_not_discard_sibling_parts(tmp_path, no_sleep):
    """The bug this fixes, stated directly: a retryable failure on ONE part
    must not throw away the parts already staged in the same run. Both months
    land after the retry."""
    b6, b7 = b"june-data", b"july-data"
    server = [_sp("month=2026-06/data.parquet", b6), _sp("month=2026-07/data.parquet", b7)]
    parts = {"month=2026-06/data.parquet": b6, "month=2026-07/data.parquet": b7}
    seen: list[str] = []

    def flaky(relpath, dest):
        seen.append(relpath)
        # Fail the SECOND part's first attempt, after the first part staged.
        if relpath == "month=2026-07/data.parquet" and seen.count(relpath) == 1:
            dest.write_bytes(b"CORRUPT")
            return
        dest.write_bytes(parts[relpath])

    _entry, changed, err = _sync_partitioned_table("issues", server, {}, tmp_path, flaky, "R", rows=2)

    assert err is None
    assert changed is True
    assert (tmp_path / "issues" / "month=2026-06" / "data.parquet").read_bytes() == b6
    assert (tmp_path / "issues" / "month=2026-07" / "data.parquet").read_bytes() == b7


def test_sync_partitioned_honors_the_shared_retry_budget(tmp_path, no_sleep, monkeypatch):
    """The partitioned path must spend the SAME budget the single-file path
    does, not a hardcoded count that merely happens to equal it today. Move the
    shared constant and the attempt count has to follow.
    (`tests/test_lib_pull.py` pins the single-file side of this parity.)"""
    monkeypatch.setattr("cli.lib.pull._DOWNLOAD_RETRIES", 4)
    server = [_sp("month=2026-06/data.parquet", b"correct")]
    attempts = {"n": 0}

    def always_bad(relpath, dest):
        attempts["n"] += 1
        dest.write_bytes(b"CORRUPT")

    _sync_partitioned_table("issues", server, {}, tmp_path, always_bad, "R", rows=1)

    assert attempts["n"] == 5, f"expected 4 retries + 1 initial, got {attempts['n']}"


def test_sync_partitioned_retry_backs_off_between_attempts(tmp_path, monkeypatch):
    """Retries are spaced by the shared backoff schedule — a tight loop would
    just hammer a server that is already struggling."""
    slept: list[float] = []
    monkeypatch.setattr("cli.lib.pull.time.sleep", lambda s: slept.append(s), raising=False)
    server = [_sp("month=2026-06/data.parquet", b"correct")]

    _sync_partitioned_table(
        "issues", server, {}, tmp_path, _fetcher({"month=2026-06/data.parquet": b"CORRUPT"}), "R", rows=1
    )

    # Derived from the helper, not sliced from the tuple: raising
    # _DOWNLOAD_RETRIES past the end of the schedule is a supported change (the
    # clamp exists for exactly that), and a sliced expectation would fail red
    # against correct code the moment someone made it.
    assert slept == [_retry_backoff(i) for i in range(_DOWNLOAD_RETRIES)]


def test_sync_partitioned_rejected_part_bytes_never_staged(tmp_path, no_sleep):
    """A rejected attempt's bytes are removed before the retry, so a fetch that
    dies before writing can't leave the previous attempt's corrupt file to be
    verified (or promoted) in its place."""
    good = b"june-data"
    server = [_sp("month=2026-06/data.parquet", good)]
    attempts = {"n": 0}

    def flaky(relpath, dest):
        attempts["n"] += 1
        if attempts["n"] == 1:
            dest.write_bytes(b"CORRUPT")
            return
        # Second attempt dies WITHOUT writing — the corrupt file from attempt 1
        # must already be gone rather than sitting there looking fetched.
        assert not dest.exists(), "rejected attempt's bytes were left in staging"
        raise ConnectionError("died before writing")

    entry, changed, _err = _sync_partitioned_table("issues", server, {}, tmp_path, flaky, "R", rows=1)

    assert entry is None and changed is False
    assert not (tmp_path / "issues" / "month=2026-06" / "data.parquet").exists()


def test_drop_stale_layout_removes_flat_when_now_partitioned(tmp_path):
    """A table that switched single-file -> partitioned: the stale
    {tid}.parquet must be removed so the view build can't resurrect it
    (Devin re-review: layout-switch stale data)."""
    from cli.lib.pull import _drop_stale_layout

    (tmp_path / "issues.parquet").write_bytes(b"stale-single")
    (tmp_path / "issues" / "month=2026-06").mkdir(parents=True)
    (tmp_path / "issues" / "month=2026-06" / "data.parquet").write_bytes(b"new")

    _drop_stale_layout(tmp_path, "issues", partitioned=True)

    assert not (tmp_path / "issues.parquet").exists()  # stale flat gone
    assert (tmp_path / "issues" / "month=2026-06" / "data.parquet").exists()  # dir kept


def test_drop_stale_layout_removes_dir_when_now_single_file(tmp_path):
    """A table that switched partitioned -> single-file: the stale {tid}/
    directory must be removed."""
    from cli.lib.pull import _drop_stale_layout

    (tmp_path / "issues" / "month=2026-06").mkdir(parents=True)
    (tmp_path / "issues" / "month=2026-06" / "data.parquet").write_bytes(b"stale-parts")
    (tmp_path / "issues.parquet").write_bytes(b"new-single")

    _drop_stale_layout(tmp_path, "issues", partitioned=False)

    assert not (tmp_path / "issues").exists()  # stale dir gone
    assert (tmp_path / "issues.parquet").read_bytes() == b"new-single"  # file kept
