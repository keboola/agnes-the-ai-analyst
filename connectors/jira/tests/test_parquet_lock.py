"""Tests for per-month Parquet advisory file locking (connectors/jira/file_lock.py).

Verifies that parquet_month_lock correctly:
- Acquires and releases locks via context manager
- Auto-creates the .locks/ directory
- Provides mutual exclusion for the same month key (threading)
- Allows concurrent locks on different month keys
- Integration: N threads calling transform_single_issue with different
  issues in the same month produce a Parquet file with all issues
"""

import json
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from connectors.jira.file_lock import parquet_month_lock


# ---------------------------------------------------------------------------
# Helpers for integration test
# ---------------------------------------------------------------------------

def _make_minimal_issue_json(issue_key: str, created_at: str) -> dict:
    """Build a minimal raw Jira JSON that passes through transform_issue()."""
    return {
        "key": issue_key,
        "id": issue_key.replace("-", ""),
        "fields": {
            "summary": f"Test issue {issue_key}",
            "description": None,
            "issuetype": {"name": "Bug"},
            "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
            "priority": {"name": "Medium"},
            "resolution": None,
            "project": {"key": "TEST", "name": "Test Project"},
            "creator": None,
            "reporter": None,
            "assignee": None,
            "created": created_at,
            "updated": created_at,
            "resolutiondate": None,
            "duedate": None,
            "labels": [],
            "attachment": [],
            "comment": {"total": 0, "comments": []},
            "issuelinks": [],
            "customfield_10010": None,
            "customfield_10004": None,
            "customfield_10323": None,
            "customfield_10511": None,
            "customfield_10156": None,
            "customfield_10002": None,
            "customfield_10365": None,
            "customfield_10330": None,
            "customfield_10325": None,
            "customfield_10350": None,
            "customfield_10676": None,
            "customfield_10475": None,
            "customfield_10157": None,
            "customfield_10328": None,
            "customfield_10161": None,
            "customfield_11831": None,
        },
        "changelog": {"histories": []},
        "_remote_links": [],
        "_synced_at": "2025-01-15T12:00:00Z",
    }


# ===========================================================================
# TestBasicParquetLock
# ===========================================================================


class TestBasicParquetLock:
    """Context manager acquires and releases the parquet month lock cleanly."""

    def test_lock_creates_lock_file(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        with parquet_month_lock(output_dir, "2025-01"):
            lock_file = output_dir / ".locks" / "parquet-2025-01.lock"
            assert lock_file.exists(), "Lock file should exist while lock is held"

    def test_lock_file_persists_after_release(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        with parquet_month_lock(output_dir, "2025-06"):
            pass

        lock_file = output_dir / ".locks" / "parquet-2025-06.lock"
        assert lock_file.exists(), "Lock file should persist after release"

    def test_lock_can_be_reacquired(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        with parquet_month_lock(output_dir, "2025-03"):
            pass

        # Should not block
        with parquet_month_lock(output_dir, "2025-03"):
            pass

    def test_locks_dir_created_when_missing(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        locks_dir = output_dir / ".locks"
        assert not locks_dir.exists()

        with parquet_month_lock(output_dir, "2025-01"):
            assert locks_dir.is_dir()

    def test_nested_parent_creation(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "deep" / "nested" / "parquet"

        with parquet_month_lock(output_dir, "2025-12"):
            assert (output_dir / ".locks").is_dir()


# ===========================================================================
# TestConcurrentParquetLock
# ===========================================================================


class TestConcurrentParquetLock:
    """Two threads cannot hold the same month lock simultaneously."""

    def test_mutual_exclusion_same_month(self, tmp_path: Path) -> None:
        """Prove two threads locking the same month do not overlap."""
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        order: list[str] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            barrier.wait()
            with parquet_month_lock(output_dir, "2025-01"):
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

        first = order[0].split(":")[1]
        second = order[2].split(":")[1]
        assert order[0] == f"enter:{first}"
        assert order[1] == f"exit:{first}"
        assert order[2] == f"enter:{second}"
        assert order[3] == f"exit:{second}"
        assert first != second

    def test_counter_integrity_under_contention(self, tmp_path: Path) -> None:
        """Multiple threads incrementing a shared counter must not lose updates."""
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        counter_file = tmp_path / "counter.txt"
        counter_file.write_text("0")

        num_threads = 4
        increments_per_thread = 20
        barrier = threading.Barrier(num_threads)

        def increment_worker() -> None:
            barrier.wait()
            for _ in range(increments_per_thread):
                with parquet_month_lock(output_dir, "2025-01"):
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


# ===========================================================================
# TestDifferentMonthsNotBlocked
# ===========================================================================


class TestDifferentMonthsNotBlocked:
    """Locks on different month keys do not block each other."""

    def test_different_months_lock_concurrently(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        timings: dict[str, dict[str, float]] = {}
        barrier = threading.Barrier(2)

        def worker(month_key: str) -> None:
            barrier.wait()
            with parquet_month_lock(output_dir, month_key):
                timings[month_key] = {"enter": time.monotonic()}
                time.sleep(0.15)
                timings[month_key]["exit"] = time.monotonic()

        t1 = threading.Thread(target=worker, args=("2025-01",))
        t2 = threading.Thread(target=worker, args=("2025-02",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert "2025-01" in timings and "2025-02" in timings

        jan = timings["2025-01"]
        feb = timings["2025-02"]

        overlap = jan["enter"] < feb["exit"] and feb["enter"] < jan["exit"]
        assert overlap, (
            f"Different month locks should allow concurrent access. "
            f"Jan: {jan}, Feb: {feb}"
        )

    def test_separate_lock_files_created(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        with parquet_month_lock(output_dir, "2025-01"):
            with parquet_month_lock(output_dir, "2025-02"):
                locks_dir = output_dir / ".locks"
                lock_files = sorted(f.name for f in locks_dir.iterdir())
                assert "parquet-2025-01.lock" in lock_files
                assert "parquet-2025-02.lock" in lock_files


# ===========================================================================
# TestParquetLockIntegration
# ===========================================================================


class TestParquetLockIntegration:
    """Integration test: concurrent transform_single_issue calls preserve all data.

    This is the key test that reproduces the race condition from issue #205.
    N threads call transform_single_issue() with different issues that all
    belong to the same month. The resulting Parquet file must contain ALL issues.
    """

    def test_concurrent_transforms_no_data_loss(self, tmp_path: Path) -> None:
        """Simulate concurrent webhook transforms for same month."""
        from connectors.jira.incremental_transform import transform_single_issue

        raw_dir = tmp_path / "raw"
        issues_dir = raw_dir / "issues"
        issues_dir.mkdir(parents=True)
        attachments_dir = raw_dir / "attachments"
        attachments_dir.mkdir(parents=True)

        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        # All issues created in January 2025 → same month partition
        num_issues = 8
        issue_keys = [f"TEST-{i}" for i in range(1, num_issues + 1)]

        for key in issue_keys:
            raw_json = _make_minimal_issue_json(key, "2025-01-15T10:00:00.000+0000")
            json_path = issues_dir / f"{key}.json"
            json_path.write_text(json.dumps(raw_json))

        barrier = threading.Barrier(num_issues)
        errors: list[str] = []

        def transform_worker(issue_key: str) -> None:
            try:
                barrier.wait(timeout=10)
                result = transform_single_issue(
                    issue_key=issue_key,
                    raw_dir=raw_dir,
                    output_dir=output_dir,
                    attachments_dir=attachments_dir,
                )
                if not result:
                    errors.append(f"transform_single_issue returned False for {issue_key}")
            except Exception as e:
                errors.append(f"Exception for {issue_key}: {e}")

        threads = [
            threading.Thread(target=transform_worker, args=(key,))
            for key in issue_keys
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"Transform errors: {errors}"

        # Verify: all issues must be present in the Parquet file
        issues_parquet = output_dir / "issues" / "2025-01.parquet"
        assert issues_parquet.exists(), "Issues Parquet file should exist"

        df = pd.read_parquet(issues_parquet)
        found_keys = set(df["issue_key"].tolist())

        assert found_keys == set(issue_keys), (
            f"Expected all {num_issues} issues in Parquet but found {len(found_keys)}. "
            f"Missing: {set(issue_keys) - found_keys}"
        )

    def test_concurrent_transforms_different_months_independent(self, tmp_path: Path) -> None:
        """Issues in different months should not interfere with each other."""
        from connectors.jira.incremental_transform import transform_single_issue

        raw_dir = tmp_path / "raw"
        issues_dir = raw_dir / "issues"
        issues_dir.mkdir(parents=True)
        attachments_dir = raw_dir / "attachments"
        attachments_dir.mkdir(parents=True)

        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        # 2 issues in Jan, 2 in Feb
        jan_keys = ["JAN-1", "JAN-2"]
        feb_keys = ["FEB-1", "FEB-2"]

        for key in jan_keys:
            raw_json = _make_minimal_issue_json(key, "2025-01-10T10:00:00.000+0000")
            (issues_dir / f"{key}.json").write_text(json.dumps(raw_json))

        for key in feb_keys:
            raw_json = _make_minimal_issue_json(key, "2025-02-10T10:00:00.000+0000")
            (issues_dir / f"{key}.json").write_text(json.dumps(raw_json))

        all_keys = jan_keys + feb_keys
        barrier = threading.Barrier(len(all_keys))
        errors: list[str] = []

        def transform_worker(issue_key: str) -> None:
            try:
                barrier.wait(timeout=10)
                result = transform_single_issue(
                    issue_key=issue_key,
                    raw_dir=raw_dir,
                    output_dir=output_dir,
                    attachments_dir=attachments_dir,
                )
                if not result:
                    errors.append(f"transform_single_issue returned False for {issue_key}")
            except Exception as e:
                errors.append(f"Exception for {issue_key}: {e}")

        threads = [
            threading.Thread(target=transform_worker, args=(key,))
            for key in all_keys
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"Transform errors: {errors}"

        # Verify Jan
        jan_df = pd.read_parquet(output_dir / "issues" / "2025-01.parquet")
        assert set(jan_df["issue_key"].tolist()) == set(jan_keys)

        # Verify Feb
        feb_df = pd.read_parquet(output_dir / "issues" / "2025-02.parquet")
        assert set(feb_df["issue_key"].tolist()) == set(feb_keys)
