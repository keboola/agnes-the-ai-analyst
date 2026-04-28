"""Tests for the process-local v2 scan quota tracker (spec §3.8)."""

from datetime import datetime, timedelta, timezone
import pytest

from app.api.v2_quota import (
    QuotaTracker,
    QuotaExceededError,
    KIND_CONCURRENT,
    KIND_DAILY_BYTES,
)


def make_tracker(max_concurrent=5, max_daily_bytes=100):
    return QuotaTracker(
        max_concurrent_per_user=max_concurrent,
        max_daily_bytes_per_user=max_daily_bytes,
    )


class TestConcurrent:
    def test_acquire_within_cap_succeeds(self):
        q = make_tracker(max_concurrent=3)
        with q.acquire(user="alice"):
            with q.acquire(user="alice"):
                with q.acquire(user="alice"):
                    pass

    def test_acquire_above_cap_raises(self):
        q = make_tracker(max_concurrent=2)
        with q.acquire(user="alice"):
            with q.acquire(user="alice"):
                with pytest.raises(QuotaExceededError) as e:
                    with q.acquire(user="alice"):
                        pass
                assert e.value.kind == KIND_CONCURRENT
                assert e.value.current == 2
                assert e.value.limit == 2

    def test_release_on_context_exit(self):
        q = make_tracker(max_concurrent=1)
        with q.acquire(user="alice"):
            pass
        # Counter dropped on exit; new acquire works
        with q.acquire(user="alice"):
            pass

    def test_release_on_exception(self):
        q = make_tracker(max_concurrent=1)
        try:
            with q.acquire(user="alice"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with q.acquire(user="alice"):
            pass

    def test_per_user_isolation(self):
        q = make_tracker(max_concurrent=1)
        with q.acquire(user="alice"):
            with q.acquire(user="bob"):
                pass


class TestDailyBytes:
    def test_record_within_cap(self):
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=300)
        q.record_bytes(user="alice", n=400)
        assert q.bytes_used_today(user="alice") == 700

    def test_record_above_cap_no_longer_raises(self):
        """Post-scan recording NEVER raises — the user already paid for the
        BQ scan, refusing to return the bytes they fetched would be perverse.
        Pre-flight enforcement lives in check_daily_budget (called before
        the scan runs)."""
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=600)
        # Push over cap — record completes without raising.
        q.record_bytes(user="alice", n=500)
        assert q.bytes_used_today(user="alice") == 1100

    def test_check_daily_budget_blocks_when_over_cap(self):
        """Once recorded bytes push past the cap, check_daily_budget refuses
        the next request pre-flight — server doesn't run the BQ scan."""
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=600)
        q.check_daily_budget(user="alice")  # 600 < 1000 → ok
        q.record_bytes(user="alice", n=500)  # now at 1100
        with pytest.raises(QuotaExceededError) as e:
            q.check_daily_budget(user="alice")
        assert e.value.kind == KIND_DAILY_BYTES

    def test_check_daily_budget_at_exact_cap_rejects(self):
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=1000)
        with pytest.raises(QuotaExceededError):
            q.check_daily_budget(user="alice")

    def test_per_user_isolation(self):
        q = make_tracker(max_daily_bytes=100)
        q.record_bytes(user="alice", n=80)
        q.record_bytes(user="bob", n=80)  # bob's bucket independent
        # alice's check fails when over cap; bob's check still passes.
        q.record_bytes(user="alice", n=30)  # alice now at 110
        with pytest.raises(QuotaExceededError):
            q.check_daily_budget(user="alice")
        q.check_daily_budget(user="bob")  # bob still under

    def test_reset_on_utc_midnight(self, monkeypatch):
        q = make_tracker(max_daily_bytes=100)
        d1 = datetime(2026, 4, 27, 23, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("app.api.v2_quota._utcnow", lambda: d1)
        q.record_bytes(user="alice", n=80)
        assert q.bytes_used_today(user="alice") == 80

        d2 = d1 + timedelta(hours=2)  # crosses UTC midnight
        monkeypatch.setattr("app.api.v2_quota._utcnow", lambda: d2)
        assert q.bytes_used_today(user="alice") == 0
        q.record_bytes(user="alice", n=80)  # ok, fresh bucket
