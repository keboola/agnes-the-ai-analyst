"""Issue #81 Group B — Keboola extractor exit codes.

Three contracts:
- 0 = full success (every table OK)
- 1 = full failure (every table failed)
- 2 = partial (at least one OK + at least one failed)

Plus the sync.py interpretation: exit 2 must NOT be treated as a crash;
it logs a PARTIAL FAILURE notice and continues to the orchestrator
rebuild step (the orchestrator's per-table _meta machinery already
captures which tables succeeded).
"""

import subprocess as subprocess_real

import pytest

from connectors.keboola.extractor import compute_exit_code


class TestComputeExitCode:
    @pytest.mark.parametrize(
        "stats,total,expected",
        [
            # Full success
            ({"tables_extracted": 10, "tables_failed": 0}, 10, 0),
            # Single-table full success
            ({"tables_extracted": 1, "tables_failed": 0}, 1, 0),
            # No tables registered → 0 (vacuous success)
            ({"tables_extracted": 0, "tables_failed": 0}, 0, 0),
            # Full failure
            ({"tables_extracted": 0, "tables_failed": 10}, 10, 1),
            # Single-table full failure
            ({"tables_extracted": 0, "tables_failed": 1}, 1, 1),
            # Partial — single failure in 10
            ({"tables_extracted": 9, "tables_failed": 1}, 10, 2),
            # Partial — half-and-half
            ({"tables_extracted": 5, "tables_failed": 5}, 10, 2),
            # Partial — only one succeeded
            ({"tables_extracted": 1, "tables_failed": 9}, 10, 2),
        ],
    )
    def test_exit_code_matrix(self, stats, total, expected):
        assert compute_exit_code(stats, total) == expected

    def test_missing_tables_failed_key_treated_as_zero(self):
        """Defensive — older stats dicts without `tables_failed` should
        be treated as full success."""
        assert compute_exit_code({"tables_extracted": 5}, 5) == 0

    def test_failed_exceeds_total_still_full_failure(self):
        """If somehow `tables_failed > total` (counting bug, retries),
        exit 1 — not 2 — so partial-failure alerting only fires on a
        legitimate mixed outcome."""
        assert compute_exit_code({"tables_failed": 11}, 10) == 1


class TestSyncApiPartialFailureHandling:
    """Runtime test: exit code from the extractor subprocess maps to the
    correct [SYNC] log branch. Drives `_run_sync` with a mocked
    `subprocess.run` and asserts the print() calls into stderr. This
    catches inverted-comparison regressions (e.g. `if returncode == 1`
    used for the partial branch) that a source-substring grep would
    miss.
    """

    def _drive_run_sync(self, monkeypatch, capsys, returncode):
        """Invoke `_run_sync` with the extractor subprocess returning
        ``returncode``, return the captured stderr as a single string.

        sync.py does `import subprocess` locally inside `_run_sync`, so
        we patch `subprocess.run` on the real `subprocess` module —
        Python caches it, the local import resolves to the patched one.
        """
        from unittest.mock import MagicMock
        from app.api import sync as sync_mod

        def fake_run(*args, **kwargs):
            return MagicMock(
                returncode=returncode, stdout="{}", stderr="",
            )
        monkeypatch.setattr(subprocess_real, "run", fake_run)

        # Stub the orchestrator + profiler (out of scope for this test).
        monkeypatch.setattr(
            sync_mod, "SyncOrchestrator",
            lambda: MagicMock(rebuild=MagicMock(return_value={})),
            raising=False,
        )

        # Pretend a Keboola token is configured so the inline subprocess
        # cmd is built (don't enter the missing-credentials early-exit).
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "test-token")
        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://test.example")

        # _run_sync needs at least one table_config to reach the
        # subprocess; stub the registry lookup to return one.
        from src.repositories.table_registry import TableRegistryRepository
        monkeypatch.setattr(
            TableRegistryRepository, "list_local",
            lambda self, *a, **kw: [
                {"id": "x", "name": "x", "source_type": "keboola",
                 "bucket": "in.c-x", "source_table": "y",
                 "query_mode": "local"}
            ],
        )

        sync_mod._run_sync()
        return capsys.readouterr().err

    def test_exit_0_is_logged_as_ok(self, monkeypatch, capsys):
        stderr = self._drive_run_sync(monkeypatch, capsys, returncode=0)
        assert "[SYNC] Extractor OK" in stderr
        assert "PARTIAL FAILURE" not in stderr
        assert "Extractor FAILED" not in stderr

    def test_exit_1_is_logged_as_failed(self, monkeypatch, capsys):
        stderr = self._drive_run_sync(monkeypatch, capsys, returncode=1)
        assert "[SYNC] Extractor FAILED (exit 1)" in stderr
        assert "PARTIAL FAILURE" not in stderr
        assert "Extractor OK" not in stderr

    def test_exit_2_is_logged_as_partial(self, monkeypatch, capsys):
        stderr = self._drive_run_sync(monkeypatch, capsys, returncode=2)
        assert "[SYNC] Extractor PARTIAL FAILURE (exit 2)" in stderr
        # The partial branch must NOT also log OK or FAILED.
        assert "Extractor OK" not in stderr
        assert "Extractor FAILED (exit" not in stderr

    def test_exit_124_falls_through_to_failed(self, monkeypatch, capsys):
        """Timeouts (124), signal kills (-N), and other non-zero codes
        all hit the catchall else branch and log FAILED."""
        stderr = self._drive_run_sync(monkeypatch, capsys, returncode=124)
        assert "[SYNC] Extractor FAILED (exit 124)" in stderr
