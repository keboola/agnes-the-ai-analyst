"""Tests for per-issue advisory file locking (connectors/jira/file_lock.py).

Verifies that issue_json_lock correctly:
- Acquires and releases locks via context manager
- Auto-creates the .locks/ directory
- Provides mutual exclusion for the same issue key (threading)
- Allows concurrent locks on different issue keys
"""

import threading
import time
from pathlib import Path

import pytest

from connectors.jira.file_lock import issue_json_lock


class TestBasicLockUnlock:
    """Context manager acquires and releases the lock cleanly."""

    def test_lock_creates_lock_file(self, tmp_path: Path) -> None:
        """Lock file is created when the context manager is entered."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        with issue_json_lock(issues_dir, "SUPPORT-100"):
            lock_file = issues_dir / ".locks" / "SUPPORT-100.lock"
            assert lock_file.exists(), "Lock file should exist while lock is held"

    def test_lock_file_persists_after_release(self, tmp_path: Path) -> None:
        """Lock file remains on disk after context manager exits (only the advisory lock is released)."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        with issue_json_lock(issues_dir, "SUPPORT-200"):
            pass

        lock_file = issues_dir / ".locks" / "SUPPORT-200.lock"
        assert lock_file.exists(), "Lock file should persist after release"

    def test_lock_can_be_reacquired(self, tmp_path: Path) -> None:
        """The same issue key can be locked again after the previous lock is released."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        with issue_json_lock(issues_dir, "SUPPORT-300"):
            pass

        # Should not block - the lock was released
        with issue_json_lock(issues_dir, "SUPPORT-300"):
            pass


class TestLocksDirectoryCreation:
    """The .locks/ subdirectory is created automatically."""

    def test_locks_dir_created_when_missing(self, tmp_path: Path) -> None:
        """If .locks/ does not exist, it is created on first lock acquisition."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        locks_dir = issues_dir / ".locks"
        assert not locks_dir.exists(), "Precondition: .locks/ should not exist yet"

        with issue_json_lock(issues_dir, "TEST-1"):
            assert locks_dir.is_dir(), ".locks/ directory should be created"

    def test_locks_dir_already_exists(self, tmp_path: Path) -> None:
        """If .locks/ already exists, no error is raised."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / ".locks").mkdir()

        with issue_json_lock(issues_dir, "TEST-2"):
            pass  # Should not raise

    def test_nested_parent_creation(self, tmp_path: Path) -> None:
        """If issues_dir itself does not exist, parents are created (parents=True)."""
        issues_dir = tmp_path / "deep" / "nested" / "issues"
        # issues_dir does not exist yet

        with issue_json_lock(issues_dir, "TEST-3"):
            assert (issues_dir / ".locks").is_dir()


class TestConcurrentLockExclusion:
    """Two threads cannot hold the same issue lock simultaneously."""

    def test_mutual_exclusion_same_issue(self, tmp_path: Path) -> None:
        """Prove that two threads holding the same lock do not overlap.

        Each thread appends "enter:<name>" and "exit:<name>" to a shared list
        with a sleep in between.  If locking works, the entries must appear
        in non-interleaved pairs: [enter:A, exit:A, enter:B, exit:B] or
        [enter:B, exit:B, enter:A, exit:A].
        """
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        order: list[str] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            barrier.wait()  # Ensure both threads start at the same time
            with issue_json_lock(issues_dir, "SUPPORT-999"):
                order.append(f"enter:{name}")
                time.sleep(0.1)
                order.append(f"exit:{name}")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(order) == 4, f"Expected 4 events, got {order}"

        # Verify non-interleaved ordering:
        # Either [enter:A, exit:A, enter:B, exit:B]
        # or     [enter:B, exit:B, enter:A, exit:A]
        first_entrant = order[0].split(":")[1]
        second_entrant = order[2].split(":")[1]

        assert order[0] == f"enter:{first_entrant}"
        assert order[1] == f"exit:{first_entrant}"
        assert order[2] == f"enter:{second_entrant}"
        assert order[3] == f"exit:{second_entrant}"
        assert first_entrant != second_entrant, "Both threads should be different"

    def test_counter_integrity_under_contention(self, tmp_path: Path) -> None:
        """Multiple threads incrementing a shared counter must not lose updates.

        Without locking, concurrent read-modify-write would cause lost updates.
        With locking, the final counter value must equal the number of increments.
        """
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        counter_file = tmp_path / "counter.txt"
        counter_file.write_text("0")

        num_threads = 4
        increments_per_thread = 20
        barrier = threading.Barrier(num_threads)

        def increment_worker() -> None:
            barrier.wait()
            for _ in range(increments_per_thread):
                with issue_json_lock(issues_dir, "COUNTER-ISSUE"):
                    value = int(counter_file.read_text())
                    value += 1
                    counter_file.write_text(str(value))

        threads = [
            threading.Thread(target=increment_worker)
            for _ in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        expected = num_threads * increments_per_thread
        actual = int(counter_file.read_text())
        assert actual == expected, (
            f"Counter should be {expected} but got {actual} "
            f"(indicates lost updates due to missing mutual exclusion)"
        )


class TestDifferentIssuesNotBlocked:
    """Locks on different issue keys do not block each other."""

    def test_different_keys_lock_concurrently(self, tmp_path: Path) -> None:
        """Two threads locking different issue keys can hold locks at the same time.

        Both threads record the time they enter and exit the critical section.
        If different keys are truly independent, their time intervals must overlap.
        """
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        timings: dict[str, dict[str, float]] = {}
        barrier = threading.Barrier(2)

        def worker(issue_key: str) -> None:
            barrier.wait()
            with issue_json_lock(issues_dir, issue_key):
                timings[issue_key] = {"enter": time.monotonic()}
                time.sleep(0.15)
                timings[issue_key]["exit"] = time.monotonic()

        t1 = threading.Thread(target=worker, args=("ALPHA-1",))
        t2 = threading.Thread(target=worker, args=("BETA-2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert "ALPHA-1" in timings and "BETA-2" in timings, (
            "Both threads should have recorded timings"
        )

        alpha = timings["ALPHA-1"]
        beta = timings["BETA-2"]

        # Overlap check: alpha entered before beta exited AND beta entered before alpha exited
        overlap = alpha["enter"] < beta["exit"] and beta["enter"] < alpha["exit"]
        assert overlap, (
            f"Different issue locks should allow concurrent access. "
            f"ALPHA-1: {alpha}, BETA-2: {beta}"
        )

    def test_separate_lock_files_created(self, tmp_path: Path) -> None:
        """Each issue key gets its own lock file."""
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        with issue_json_lock(issues_dir, "FOO-1"):
            with issue_json_lock(issues_dir, "BAR-2"):
                locks_dir = issues_dir / ".locks"
                lock_files = sorted(f.name for f in locks_dir.iterdir())
                assert "BAR-2.lock" in lock_files
                assert "FOO-1.lock" in lock_files
