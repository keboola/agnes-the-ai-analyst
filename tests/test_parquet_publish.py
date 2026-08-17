"""`src.parquet_publish` — the shared atomic-publish protocol every extract-layout
parquet writer routes through: per-process temp path -> chmod 0644 -> os.replace.

Extracted out of `connectors/jira/transform.py::write_parquet_atomic` (#1354) so
the ten other publish sites #1359 found (Keboola, BigQuery, MCP, `src/ingest`)
share one implementation instead of re-growing the two defects that helper was
written to prevent: a shared (non-per-process) temp name racing two writers
(#1274), and a published file silently dropping to mode 0600 (#203). See
`src/parquet_publish.py`'s module docstring for the full incident history and
why this lives in `src/` rather than under any one `connectors/` package.

Modeled on `tests/test_jira_atomic_parquet_writes.py`: a failure stub that
never touches the filesystem before raising would pass against completely
unfixed code too (the destination was never non-atomically written to begin
with), which is not a test of anything. `_boom` below emits the footerless
bytes a `SIGKILL`'d writer leaves AT WHATEVER PATH THE CALLER CHOSE, then
raises — so a broken implementation (e.g. one that forgot to stage through a
temp at all) fails these tests, and a correct one does not.
"""

import os
from pathlib import Path

import duckdb
import pytest

from src import parquet_publish as pub

# What a killed write leaves behind: the magic bytes, no footer.
FOOTERLESS = b"PAR1" + b"\x00" * 64


def _boom(path: Path) -> None:
    """Model a write that DIES MIDWAY, not one that never starts."""
    Path(path).write_bytes(FOOTERLESS)
    raise OSError("disk full mid-write")


# --------------------------------------------------------------------------
# atomic_publish_temp_path
# --------------------------------------------------------------------------


def test_temp_path_is_per_process(tmp_path):
    dest = tmp_path / "t.parquet"
    tmp = pub.atomic_publish_temp_path(dest)
    assert str(os.getpid()) in tmp.name


def test_temp_path_never_matches_the_parquet_glob(tmp_path):
    dest = tmp_path / "t.parquet"
    tmp = pub.atomic_publish_temp_path(dest)
    assert tmp.match("*.tmp")
    assert not tmp.match("*.parquet")


def test_temp_path_accepts_str_or_path(tmp_path):
    dest_str = str(tmp_path / "t.parquet")
    assert pub.atomic_publish_temp_path(dest_str) == pub.atomic_publish_temp_path(Path(dest_str))


def test_two_different_processes_get_different_temp_paths(tmp_path, monkeypatch):
    dest = tmp_path / "t.parquet"
    monkeypatch.setattr(os, "getpid", lambda: 11111)
    tmp_a = pub.atomic_publish_temp_path(dest)
    monkeypatch.setattr(os, "getpid", lambda: 22222)
    tmp_b = pub.atomic_publish_temp_path(dest)
    assert tmp_a != tmp_b


# --------------------------------------------------------------------------
# atomic_publish (context manager) — happy path
# --------------------------------------------------------------------------


def test_publishes_the_full_content_on_clean_exit(tmp_path):
    dest = tmp_path / "sub" / "dir" / "t.parquet"  # parent doesn't exist yet
    with pub.atomic_publish(dest) as tmp:
        tmp.write_bytes(b"hello")
    assert dest.read_bytes() == b"hello"


def test_yields_the_path_atomic_publish_temp_path_would_compute(tmp_path):
    dest = tmp_path / "t.parquet"
    with pub.atomic_publish(dest) as tmp:
        assert tmp == pub.atomic_publish_temp_path(dest)
        tmp.write_bytes(b"x")


def test_dest_accepts_str_or_path(tmp_path):
    dest = str(tmp_path / "t.parquet")
    with pub.atomic_publish(dest) as tmp:
        tmp.write_bytes(b"x")
    assert Path(dest).read_bytes() == b"x"


# --------------------------------------------------------------------------
# atomic_publish — a write that dies midway must not damage what is published
# --------------------------------------------------------------------------


def test_a_killed_write_leaves_the_previously_published_file_intact(tmp_path):
    dest = tmp_path / "t.parquet"
    dest.write_bytes(b"previously published")
    with pytest.raises(OSError):
        with pub.atomic_publish(dest) as tmp:
            _boom(tmp)
    assert dest.read_bytes() == b"previously published"


def test_a_killed_first_publish_leaves_no_file_at_the_published_path(tmp_path):
    dest = tmp_path / "t.parquet"  # never published before
    with pytest.raises(OSError):
        with pub.atomic_publish(dest) as tmp:
            _boom(tmp)
    assert not dest.exists()


def test_a_killed_write_leaves_no_temp_behind(tmp_path):
    dest = tmp_path / "t.parquet"
    with pytest.raises(OSError):
        with pub.atomic_publish(dest) as tmp:
            _boom(tmp)
    assert list(tmp_path.glob("*.tmp")) == [], "the killed write's temp must be cleaned up"


def test_cleanup_only_runs_on_the_failure_path_not_on_success(tmp_path, monkeypatch):
    """Docstring's `finally` critique, made concrete: `Path.unlink` must not
    even be CALLED on a successful publish (`os.replace` already moved the
    temp away) — only on the exception path."""
    dest = tmp_path / "t.parquet"
    calls: list[Path] = []
    real_unlink = Path.unlink

    def traced(self, *a, **kw):
        calls.append(self)
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", traced)
    with pub.atomic_publish(dest) as tmp:
        tmp.write_bytes(b"x")
    assert calls == [], f"unlink must not be called on the success path, got {calls}"


# --------------------------------------------------------------------------
# atomic_publish — permissions must not depend on the writer's umask
# --------------------------------------------------------------------------


def test_published_file_is_0644_even_under_a_restrictive_umask(tmp_path):
    dest = tmp_path / "t.parquet"
    previous = os.umask(0o077)
    try:
        with pub.atomic_publish(dest) as tmp:
            tmp.write_bytes(b"x")
    finally:
        os.umask(previous)
    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)


# --------------------------------------------------------------------------
# atomic_publish — two concurrent writers (incident #1274, modeled without
# real OS processes: two "writers" distinguished only by os.getpid()).
# --------------------------------------------------------------------------


def test_two_concurrent_writers_do_not_clobber_each_others_temp(tmp_path, monkeypatch):
    dest = tmp_path / "t.parquet"

    monkeypatch.setattr(os, "getpid", lambda: 11111)
    tmp_a = pub.atomic_publish_temp_path(dest)
    tmp_a.parent.mkdir(parents=True, exist_ok=True)
    tmp_a.write_bytes(b"A-IN-FLIGHT")  # writer A is mid-write, not yet committed

    # Writer B (different pid) starts and finishes cleanly while A is still open.
    monkeypatch.setattr(os, "getpid", lambda: 22222)
    with pub.atomic_publish(dest) as tmp_b:
        assert tmp_b != tmp_a
        tmp_b.write_bytes(b"B-CONTENT")
    assert dest.read_bytes() == b"B-CONTENT"
    assert tmp_a.read_bytes() == b"A-IN-FLIGHT", "writer B's publish touched writer A's temp"

    # Writer A now fails. Its own cleanup must remove only ITS OWN temp — the
    # #1274 bug was the loser's cleanup deleting the winner's (already-published)
    # file/temp.
    monkeypatch.setattr(os, "getpid", lambda: 11111)
    with pytest.raises(RuntimeError):
        with pub.atomic_publish(dest) as tmp_a2:
            assert tmp_a2 == tmp_a
            raise RuntimeError("writer A died")
    assert not tmp_a.exists(), "writer A's own cleanup should remove its own temp"
    assert dest.read_bytes() == b"B-CONTENT", "writer A's failure must not touch writer B's publish"


# --------------------------------------------------------------------------
# atomic_publish — no opinion on the writer (PyArrow, pandas, DuckDB COPY all
# work identically; the shared piece is the publish protocol, not the writer)
# --------------------------------------------------------------------------


def test_duckdb_copy_style_writer_works_identically(tmp_path):
    dest = tmp_path / "t.parquet"
    con = duckdb.connect(":memory:")
    try:
        with pub.atomic_publish(dest) as tmp:
            safe = str(tmp).replace("'", "''")
            con.execute(f"COPY (SELECT 1 AS n) TO '{safe}' (FORMAT PARQUET)")
    finally:
        con.close()
    assert dest.exists()
    check = duckdb.connect(":memory:")
    try:
        assert check.execute(f"SELECT n FROM read_parquet('{dest}')").fetchall() == [(1,)]
    finally:
        check.close()


# --------------------------------------------------------------------------
# atomic_publish_finalize — the two-step API for callers whose write spans
# control flow too complex to nest cleanly inside a single `with` block.
# --------------------------------------------------------------------------


def test_atomic_publish_finalize_commits_a_manually_written_temp(tmp_path):
    dest = tmp_path / "t.parquet"
    tmp = pub.atomic_publish_temp_path(dest)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"manual")

    result = pub.atomic_publish_finalize(tmp, dest)

    assert result == dest
    assert dest.read_bytes() == b"manual"
    assert not tmp.exists()


def test_atomic_publish_finalize_mode_is_0644_under_restrictive_umask(tmp_path):
    dest = tmp_path / "t.parquet"
    tmp = pub.atomic_publish_temp_path(dest)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    previous = os.umask(0o077)
    try:
        tmp.write_bytes(b"manual")
    finally:
        os.umask(previous)

    pub.atomic_publish_finalize(tmp, dest)

    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)
