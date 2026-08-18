"""
Tests for connectors/jira/scripts/consistency_check.py.

Issue #1363: one corrupt Parquet partition failed the checker's combined
``read_parquet([...])`` read of every partition at once. The failure
degraded to "there are no Parquet files at all", turning EVERY non-deleted
issue into a phantom ``missing_in_parquet`` (order 10**4) — and that fix
path had no threshold, so it shelled out once per key, every 30 minutes,
forever, while still reporting ``status: success`` / ``alert_level: INFO``.

Covers:
- scan_parquet_keys(): per-file isolation (one corrupt part costs only its
  own rows) and the "no parquets" vs "could not read parquets" distinction.
- run_check(): the missing_in_parquet fix path is threshold-gated exactly
  like missing_in_json already is, the corrupt-file repro stays bounded,
  and a run that could not read Parquet (or left a large gap unfixed)
  cannot report status=success / alert_level=INFO.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from connectors.jira.scripts.consistency_check import Config, JiraConsistencyChecker


def _config(raw_dir: Path, parquet_dir: Path) -> Config:
    return Config(
        jira_domain="example.atlassian.net",
        jira_email="e@example.com",
        jira_api_token="t",
        raw_dir=raw_dir,
        parquet_dir=parquet_dir,
        repo_dir=Path("/srv/repo"),
        venv_python=Path("/srv/python"),
    )


def _write_parquet(path: Path, issue_keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"issue_key": issue_keys}).to_parquet(path)


def _write_corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file at all")


def _write_json_issue(issues_dir: Path, issue_key: str) -> None:
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / f"{issue_key}.json").write_text(json.dumps({"key": issue_key, "fields": {}}))


def _run_check(
    tmp_path: Path,
    all_keys: list[str],
    parquet_keys_by_month: dict[str, list[str]],
    *,
    corrupt_file: bool = False,
    auto_fix: bool = True,
):
    """Drive run_check() over a synthetic Jira/JSON/Parquet trio.

    ``all_keys`` is the full ground truth (Jira API + JSON) — every key gets
    a JSON file. ``parquet_keys_by_month`` seeds healthy Parquet partitions.
    ``corrupt_file`` additionally writes one unreadable partition alongside
    them, so any key in ``all_keys`` not covered by ``parquet_keys_by_month``
    is "missing" for exactly the reason the caller intends: a plain
    not-yet-transformed lag, or one hidden behind a corrupt file.
    """
    raw_dir = tmp_path / "raw"
    parquet_dir = tmp_path / "parquet"
    issues_dir = parquet_dir / "issues"
    json_dir = raw_dir / "issues"

    for month, keys in parquet_keys_by_month.items():
        _write_parquet(issues_dir / f"{month}.parquet", keys)
    if corrupt_file:
        _write_corrupt(issues_dir / "corrupt.parquet")

    for key in all_keys:
        _write_json_issue(json_dir, key)

    checker = JiraConsistencyChecker(_config(raw_dir, parquet_dir))
    with (
        patch.object(checker, "fetch_jira_keys", return_value=set(all_keys)),
        patch.object(checker, "_enqueue_jira_refresh") as mock_enqueue,
        patch("connectors.jira.scripts.consistency_check.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        report = checker.run_check(auto_fix=auto_fix, dry_run=False)
    return report, mock_run, mock_enqueue


class TestScanParquetKeysPerFileIsolation:
    """One corrupt part must cost only its own rows. The model to copy is
    ``find_open_issues`` in ``scripts/poll_sla.py:132-139`` — a per-file
    read wrapped in its own try/except, not one combined multi-file query."""

    def test_healthy_keys_survive_a_sibling_corrupt_file(self, tmp_path: Path) -> None:
        issues_dir = tmp_path / "parquet" / "issues"
        _write_parquet(issues_dir / "2026-01.parquet", ["PROJ-1", "PROJ-2"])
        _write_parquet(issues_dir / "2026-02.parquet", ["PROJ-3"])
        _write_corrupt(issues_dir / "2026-03.parquet")

        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        keys, failed = checker.scan_parquet_keys()

        assert keys == {"PROJ-1", "PROJ-2", "PROJ-3"}, "one corrupt month must not fail the healthy months too"
        assert failed == [str(issues_dir / "2026-03.parquet")]

    def test_corrupt_file_does_not_empty_the_whole_result(self, tmp_path: Path) -> None:
        """Old behavior: one bad file failed the combined read entirely and
        the caller got back an empty set — indistinguishable from "no data"."""
        issues_dir = tmp_path / "parquet" / "issues"
        _write_parquet(issues_dir / "2026-01.parquet", ["PROJ-1"])
        _write_corrupt(issues_dir / "2026-02.parquet")

        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        keys, _failed = checker.scan_parquet_keys()

        assert keys, "a healthy file's keys must not be wiped out by a corrupt sibling"


class TestScanParquetKeysReadFailureVisibility:
    """scan_parquet_keys must distinguish "no parquets" from "could not read
    parquets" — an empty set alone used to mean both, indistinguishably."""

    def test_no_parquet_directory_is_a_genuinely_empty_corpus(self, tmp_path: Path) -> None:
        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        keys, failed = checker.scan_parquet_keys()
        assert keys == set()
        assert failed == [], "no parquet directory at all is not a read failure"

    def test_no_parquet_files_in_an_existing_directory_is_also_empty(self, tmp_path: Path) -> None:
        (tmp_path / "parquet" / "issues").mkdir(parents=True)
        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        keys, failed = checker.scan_parquet_keys()
        assert keys == set()
        assert failed == []

    def test_unreadable_files_are_reported_not_swallowed(self, tmp_path: Path) -> None:
        issues_dir = tmp_path / "parquet" / "issues"
        _write_corrupt(issues_dir / "2026-01.parquet")
        _write_corrupt(issues_dir / "2026-02.parquet")

        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        keys, failed = checker.scan_parquet_keys()

        assert keys == set()
        assert len(failed) == 2, "both unreadable files must show up — not an empty-but-clean return"


class TestParquetScanThatCouldNotRunAtAll:
    """ "DuckDB is not installed" is the same conflation as a corrupt file, one
    level up: the scan never looked, so returning a bare empty set makes EVERY
    issue look missing from Parquet.

    That was harmless while nothing keyed on the Parquet gap. It stopped being
    harmless once the gap became threshold-gated and alert-scored: the run
    would report a corpus-sized `missing_in_parquet`, log "exceeds threshold,
    manual review required" about issues it never checked, and page at ERROR
    every 30 minutes — naming N issues that may be perfectly fine instead of
    the one thing actually wrong (Devin review).

    The run must still FAIL — a checker that cannot check is broken — but for
    the true reason, and without inventing a number."""

    def test_the_scan_reports_unavailable_rather_than_an_empty_corpus(self, tmp_path: Path) -> None:
        checker = JiraConsistencyChecker(_config(tmp_path / "raw", tmp_path / "parquet"))
        with patch("connectors.jira.scripts.consistency_check.HAS_DUCKDB", False):
            keys, failed = checker.scan_parquet_keys()
        assert keys == set()
        assert failed == [JiraConsistencyChecker.PARQUET_SCAN_UNAVAILABLE], (
            "no DuckDB means the scan could not run — that is not an empty corpus"
        )

    def test_no_phantom_corpus_sized_gap_is_reported(self, tmp_path: Path) -> None:
        keys = [f"PROJ-{i}" for i in range(50)]
        with patch("connectors.jira.scripts.consistency_check.HAS_DUCKDB", False):
            report, mock_run, _ = _run_check(tmp_path, keys, {})
        assert report["discrepancies"]["missing_in_parquet"] == [], (
            "the checker must not report a Parquet gap it never measured"
        )
        assert mock_run.call_count == 0, "and must not transform issues it never checked"

    def test_the_run_still_fails_loudly_for_the_real_reason(self, tmp_path: Path) -> None:
        keys = [f"PROJ-{i}" for i in range(50)]
        with patch("connectors.jira.scripts.consistency_check.HAS_DUCKDB", False):
            report, _, _ = _run_check(tmp_path, keys, {})
        assert report["alert_level"] == "ERROR"
        assert report["status"] != "success"
        assert JiraConsistencyChecker.PARQUET_SCAN_UNAVAILABLE in report["discrepancies"]["parquet_read_failed"], (
            "the report must name the actual failure, not a list of issue keys"
        )

    def test_a_never_transformed_corpus_is_still_a_real_gap(self, tmp_path: Path) -> None:
        """The neighbouring branch, deliberately NOT changed: an existing-but-
        empty Parquet dir means the scan ran and found nothing, so on a fresh
        instance carrying raw JSON the whole-corpus gap is real and the
        over-threshold "manual review required" path is the right answer —
        the same one `missing_in_json` has always taken at that size."""
        keys = [f"PROJ-{i}" for i in range(50)]
        report, mock_run, _ = _run_check(tmp_path, keys, {})
        assert len(report["discrepancies"]["missing_in_parquet"]) == 50
        assert report["discrepancies"]["parquet_read_failed"] == []
        assert mock_run.call_count == 0, "over threshold — still not fixed one subprocess per key"


class TestParquetLagThreshold:
    """The missing_in_parquet fix path must be threshold-gated exactly like
    missing_in_json already is — a lag in the thousands is a broken read,
    not something to fix by shelling out once per key."""

    def test_a_gap_within_threshold_is_still_auto_fixed(self, tmp_path: Path) -> None:
        lagging = [f"LAG-{i}" for i in range(3)]
        report, mock_run, _mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        transformed_keys = {call.args[0][3] for call in mock_run.call_args_list}
        assert transformed_keys == set(lagging)
        assert report["status"] == "success"
        assert report["alert_level"] == "INFO"

    def test_a_gap_over_threshold_is_not_auto_fixed(self, tmp_path: Path) -> None:
        lagging = [f"LAG-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 5)]
        report, mock_run, _mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        mock_run.assert_not_called()
        assert set(report["discrepancies"]["missing_in_parquet"]) == set(lagging)

    def test_a_gap_over_threshold_is_not_reported_as_info(self, tmp_path: Path) -> None:
        lagging = [f"LAG-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 5)]
        report, _mock_run, _mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        assert report["alert_level"] == "ERROR"


class TestCorruptFileRegression:
    """issue #1363, end to end: a single corrupt Parquet partition must not
    turn into a corpus-sized, auto-fixed missing_in_parquet — and the run
    must not come back looking clean."""

    def test_missing_in_parquet_is_bounded_to_the_corrupt_files_own_issues(self, tmp_path: Path) -> None:
        corrupt_keys = [f"CORRUPT-{i}" for i in range(5)]
        healthy = {"2026-01": ["PROJ-1", "PROJ-2"], "2026-02": ["PROJ-3"]}
        all_keys = corrupt_keys + [k for keys in healthy.values() for k in keys]

        report, _mock_run, _mock_enqueue = _run_check(tmp_path, all_keys, healthy, corrupt_file=True)

        missing = report["discrepancies"]["missing_in_parquet"]
        assert set(missing) == set(corrupt_keys), "must not be the whole corpus"

    def test_a_large_corrupt_backlog_is_not_auto_fixed(self, tmp_path: Path) -> None:
        """Even bounded to just the corrupt file's own issues, a real month
        is routinely well over AUTO_FIX_THRESHOLD — must still not shell out."""
        corrupt_keys = [f"CORRUPT-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 30)]
        healthy = {"2026-01": ["PROJ-1"]}
        all_keys = corrupt_keys + ["PROJ-1"]

        report, mock_run, _mock_enqueue = _run_check(tmp_path, all_keys, healthy, corrupt_file=True)

        mock_run.assert_not_called()
        assert set(report["discrepancies"]["missing_in_parquet"]) == set(corrupt_keys)

    def test_the_run_does_not_report_clean(self, tmp_path: Path) -> None:
        corrupt_keys = [f"CORRUPT-{i}" for i in range(5)]
        healthy = {"2026-01": ["PROJ-1"]}
        all_keys = corrupt_keys + ["PROJ-1"]

        report, _mock_run, _mock_enqueue = _run_check(tmp_path, all_keys, healthy, corrupt_file=True)

        assert report["status"] != "success"
        assert report["alert_level"] != "INFO"


class TestReadFailureReflectedInReport:
    """scan_parquet_keys' read failures must reach the top-level report, and
    a run that could not read Parquet cannot report status=success /
    alert_level=INFO — that is exactly what let the corrupt-file bug hide."""

    def test_unreadable_parquet_is_visible_in_the_report(self, tmp_path: Path) -> None:
        report, _mock_run, _mock_enqueue = _run_check(tmp_path, ["PROJ-1"], {}, corrupt_file=True)

        assert report["discrepancies"]["parquet_read_failed"], (
            "the read failure must be visible in the report, not silently absorbed"
        )
        assert report["status"] != "success"
        assert report["alert_level"] != "INFO"

    def test_a_clean_run_still_reports_success_and_info(self, tmp_path: Path) -> None:
        """Regression guard on the guard: a genuinely healthy run must not
        get swept up by the new checks."""
        report, _mock_run, _mock_enqueue = _run_check(tmp_path, ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        assert report["status"] == "success"
        assert report["alert_level"] == "INFO"
        assert report["discrepancies"]["parquet_read_failed"] == []


class TestRebuildIsEnqueuedOnlyForWorkActuallyDone:
    """The coalesced `jira-refresh` must follow what a fix WROTE, not the mere
    existence of a gap.

    The threshold added for #1363 means a large Parquet gap transforms nothing,
    but the enqueue guard still keyed on `missing_in_parquet` being non-empty —
    so an unreadable or badly lagging partition queued a no-op rebuild (plus a
    `jira-refresh-followup` when one was already running) every 30 minutes,
    forever. That is the same silent churn #1363 set out to stop, reintroduced
    one branch over (Devin review on #1384).
    """

    def test_over_threshold_gap_enqueues_nothing(self, tmp_path: Path) -> None:
        lagging = [f"LAG-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 5)]
        _report, mock_run, mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        mock_run.assert_not_called()
        mock_enqueue.assert_not_called(), "nothing was transformed — a rebuild would be pure churn"

    def test_corrupt_partition_enqueues_nothing_run_after_run(self, tmp_path: Path) -> None:
        """The #1363 shape end to end: the corrupt file fails identically every
        run, so an enqueue here is the 30-minute loop in a new disguise."""
        corrupt_keys = [f"CORRUPT-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 30)]
        _report, _mock_run, mock_enqueue = _run_check(
            tmp_path, corrupt_keys + ["PROJ-1"], {"2026-01": ["PROJ-1"]}, corrupt_file=True
        )

        mock_enqueue.assert_not_called()

    def test_a_repaired_gap_still_enqueues(self, tmp_path: Path) -> None:
        """The guard must not over-correct: a fix that really wrote parquet
        still needs the coalesced rebuild that refreshes `_meta` and the view."""
        lagging = [f"LAG-{i}" for i in range(3)]
        _report, mock_run, mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        assert mock_run.called
        mock_enqueue.assert_called_once()


class TestAlertLevelScoresTheUnrepairedResidual:
    """A routine lag the checker fully repairs is a healthy self-healing run
    and must stay INFO — scoring the gap as first observed turned every such
    run into WARNING noise for anything alerting on `alert_level != INFO`
    (Devin review on #1384)."""

    def test_a_fully_repaired_lag_stays_info(self, tmp_path: Path) -> None:
        # Above WARNING_THRESHOLD but at/below AUTO_FIX_THRESHOLD: pre-fix
        # scoring would call this WARNING even though nothing is left undone.
        lagging = [f"LAG-{i}" for i in range(JiraConsistencyChecker.WARNING_THRESHOLD + 1)]
        assert len(lagging) <= JiraConsistencyChecker.AUTO_FIX_THRESHOLD, "fixture must stay auto-fixable"

        report, mock_run, _mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        assert mock_run.called, "the fixture must actually exercise the repair path"
        assert report["alert_level"] == "INFO"

    def test_an_unrepaired_lag_is_still_scored(self, tmp_path: Path) -> None:
        """The residual scoring must not silence a genuine gap: over threshold
        nothing is transformed, so the whole gap is still unrepaired."""
        lagging = [f"LAG-{i}" for i in range(JiraConsistencyChecker.AUTO_FIX_THRESHOLD + 5)]
        report, _mock_run, _mock_enqueue = _run_check(tmp_path, lagging + ["PROJ-1"], {"2026-01": ["PROJ-1"]})

        assert report["alert_level"] == "ERROR"
