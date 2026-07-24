"""Tests for the pre-flight disk guard in ``tests/conftest.py``.

A full local run writes tens of GB of DuckDB/parquet/pgserver fixtures into
pytest's basetemp. When the disk cannot absorb that, the suite does not fail
cleanly — it emits thousands of ``OSError: [Errno 28]`` teardown errors that
bury the real result, and it leaves the machine wedged at 100% full.

The guard turns that into one sentence printed before the first test runs.
It is deliberately advisory above a very low floor: CI shards the suite eight
ways (``--splits 8``) and needs far less headroom than a single local run, so
a threshold tuned for local use must never abort a CI shard.
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    DISK_ABORT_GB,
    DISK_WARN_GB,
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
