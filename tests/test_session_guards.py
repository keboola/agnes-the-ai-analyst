"""Tests for the pre-flight session guards.

A full-suite run writes 20-50 GB into pytest's basetemp and boots a real
postmaster per xdist session. Two failure modes have already cost a filled
disk and a dead run with no result:

* **Concurrent sessions.** ``tmp_path_retention_count = 1`` reclaims the
  previous session at the *start* of the next one, which silently assumes
  runs are serial. Two overlapping sessions each retain their own tree and
  neither sweeps the other, so peak disk doubles.
* **Starting with no headroom.** The suite dies ~90% in with an
  ``INTERNALERROR`` traceback and no test results, which reads as a code
  failure rather than a full disk.

Both are cheap to detect before the first test runs.
"""

from __future__ import annotations

import os

import pytest

from tests.session_guards import (
    concurrent_session_holder,
    disk_preflight,
    write_session_lock,
)


class TestDiskPreflight:
    def test_returns_none_when_headroom_is_sufficient(self, tmp_path):
        msg = disk_preflight(tmp_path, min_free_gb=10, free_bytes_fn=lambda _p: 40 * 1024**3)
        assert msg is None

    def test_returns_message_when_below_floor(self, tmp_path):
        msg = disk_preflight(tmp_path, min_free_gb=15, free_bytes_fn=lambda _p: 3 * 1024**3)

        assert msg is not None
        # Actionable: says how much is free, how much is wanted, and where.
        assert "3" in msg and "15" in msg
        assert str(tmp_path) in msg

    def test_exactly_at_floor_passes(self, tmp_path):
        msg = disk_preflight(tmp_path, min_free_gb=10, free_bytes_fn=lambda _p: 10 * 1024**3)
        assert msg is None

    def test_unreadable_path_does_not_block_the_run(self, tmp_path):
        """A guard that can't measure must not fail the suite."""

        def _boom(_p):
            raise OSError("statvfs failed")

        assert disk_preflight(tmp_path, min_free_gb=15, free_bytes_fn=_boom) is None


class TestConcurrentSessionDetection:
    def test_no_lockfile_means_no_holder(self, tmp_path):
        assert concurrent_session_holder(tmp_path / "absent.lock") is None

    def test_live_pid_in_lockfile_is_reported(self, tmp_path):
        lock = tmp_path / "s.lock"
        write_session_lock(lock, pid=4242)

        assert concurrent_session_holder(lock, pid_alive_fn=lambda _p: True) == 4242

    def test_stale_lockfile_from_dead_session_is_ignored(self, tmp_path):
        lock = tmp_path / "s.lock"
        write_session_lock(lock, pid=4242)

        assert concurrent_session_holder(lock, pid_alive_fn=lambda _p: False) is None

    def test_our_own_pid_is_not_a_concurrent_session(self, tmp_path):
        """Re-entrancy: the session that owns the lock must not block itself."""
        lock = tmp_path / "s.lock"
        write_session_lock(lock, pid=os.getpid())

        assert concurrent_session_holder(lock, pid_alive_fn=lambda _p: True) is None

    def test_garbage_lockfile_never_blocks(self, tmp_path):
        lock = tmp_path / "s.lock"
        lock.write_text("not-a-pid\n")

        assert concurrent_session_holder(lock, pid_alive_fn=lambda _p: True) is None

    def test_write_then_read_round_trips(self, tmp_path):
        lock = tmp_path / "s.lock"
        write_session_lock(lock, pid=777)

        assert concurrent_session_holder(lock, pid_alive_fn=lambda p: p == 777) == 777


@pytest.mark.parametrize("free_gb,floor,blocked", [(50, 15, False), (14, 15, True), (0, 15, True)])
def test_disk_preflight_boundary_table(tmp_path, free_gb, floor, blocked):
    msg = disk_preflight(tmp_path, min_free_gb=floor, free_bytes_fn=lambda _p: free_gb * 1024**3)
    assert (msg is not None) is blocked
