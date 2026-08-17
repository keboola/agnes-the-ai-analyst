"""Tests for the basetemp disk lifecycle in ``tests/conftest.py``.

A full local run writes tens of GB of DuckDB/parquet/pgserver fixtures into
pytest's basetemp. Two conftest hooks manage that: a pre-flight disk guard
(refuse to start a run the disk cannot absorb — otherwise the suite emits
thousands of ``OSError: [Errno 28]`` teardown errors that bury the real
result and wedge the machine at 100% full) and a post-success sweep (a fully
green run deletes its own basetemp at session end; failed runs keep theirs
for debugging).

The guard is deliberately advisory above a very low floor: CI shards the
suite eight ways (``--splits 8``) and needs far less headroom than a single
local run, so a threshold tuned for local use must never abort a CI shard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (
    DISK_ABORT_GB,
    DISK_WARN_GB,
    basetemp_sweep_verdict,
    disk_guard_verdict,
)

_GB = 1024**3


class TestVerdict:
    def test_plenty_of_space_is_silent(self):
        verdict, message = disk_guard_verdict(free_bytes=500 * _GB)
        assert verdict == "ok"
        assert message == ""

    def test_below_warn_threshold_warns_but_runs(self):
        verdict, message = disk_guard_verdict(free_bytes=(DISK_WARN_GB - 1) * _GB)
        assert verdict == "warn"
        assert "GB free" in message

    def test_below_abort_threshold_aborts(self):
        verdict, message = disk_guard_verdict(free_bytes=(DISK_ABORT_GB - 1) * _GB)
        assert verdict == "abort"
        assert message

    def test_abort_threshold_is_well_below_warn(self):
        """The abort floor must only catch runs that are already doomed —
        otherwise it breaks CI shards, which legitimately run lean."""
        assert DISK_ABORT_GB < DISK_WARN_GB
        assert DISK_ABORT_GB <= 5

    def test_exact_thresholds_are_inclusive_on_the_safe_side(self):
        assert disk_guard_verdict(free_bytes=DISK_WARN_GB * _GB)[0] == "ok"
        assert disk_guard_verdict(free_bytes=DISK_ABORT_GB * _GB)[0] == "warn"

    def test_zero_free_space_aborts(self):
        assert disk_guard_verdict(free_bytes=0)[0] == "abort"

    @pytest.mark.parametrize("verdict_free_gb", [DISK_ABORT_GB - 1, 0])
    def test_abort_message_names_the_cause_and_the_remedy(self, verdict_free_gb):
        _, message = disk_guard_verdict(free_bytes=verdict_free_gb * _GB)
        # The message has to be actionable without this docstring in hand.
        assert "pytest" in message.lower()
        assert "basetemp" in message.lower() or "tmp" in message.lower()

    def test_warn_message_states_how_much_a_full_run_needs(self):
        _, message = disk_guard_verdict(free_bytes=(DISK_WARN_GB - 1) * _GB)
        assert str(DISK_WARN_GB) in message


class TestOptOut:
    def test_env_opt_out_silences_every_verdict(self, monkeypatch):
        monkeypatch.setenv("AGNES_SKIP_DISK_CHECK", "1")
        assert disk_guard_verdict(free_bytes=0)[0] == "ok"

    def test_opt_out_is_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGNES_SKIP_DISK_CHECK", raising=False)
        assert disk_guard_verdict(free_bytes=0)[0] == "abort"

    def test_opt_out_ignores_empty_and_zero_values(self, monkeypatch):
        for value in ("", "0", "false"):
            monkeypatch.setenv("AGNES_SKIP_DISK_CHECK", value)
            assert disk_guard_verdict(free_bytes=0)[0] == "abort", value


class TestBasetempSweep:
    """The post-success sweep must only ever delete pytest's own numbered
    basetemp, only in the controller, and only after a fully green run."""

    def _verdict(self, **overrides) -> bool:
        params = dict(
            exitstatus=0,
            is_xdist_worker=False,
            user_basetemp=False,
            basetemp=Path("/tmp/pytest-of-dev/pytest-42"),
            keep_env="",
        )
        params.update(overrides)
        return basetemp_sweep_verdict(**params)

    def test_green_run_numbered_dir_is_swept(self):
        assert self._verdict() is True

    def test_xdist_worker_never_sweeps(self):
        # Workers finish before the controller; only the controller may
        # delete the shared numbered dir.
        assert self._verdict(is_xdist_worker=True) is False

    @pytest.mark.parametrize("exitstatus", [1, 2, 3, 4, 5])
    def test_non_green_run_keeps_artifacts(self, exitstatus):
        assert self._verdict(exitstatus=exitstatus) is False

    def test_user_specified_basetemp_is_never_deleted(self):
        assert self._verdict(user_basetemp=True) is False

    def test_run_that_never_used_tmp_path_is_a_noop(self):
        assert self._verdict(basetemp=None) is False

    @pytest.mark.parametrize(
        "path",
        [
            Path("/tmp/pytest-of-dev/pytest-current"),
            Path("/tmp/pytest-of-dev/custom-dir"),
            Path("/tmp/somewhere-else/pytest-42"),
        ],
    )
    def test_only_pytests_own_numbered_dirs_qualify(self, path):
        assert self._verdict(basetemp=path) is False

    def test_keep_env_opt_out(self):
        assert self._verdict(keep_env="1") is False

    def test_keep_env_ignores_empty_and_zero_values(self):
        for value in ("", "0", "false"):
            assert self._verdict(keep_env=value) is True, value
