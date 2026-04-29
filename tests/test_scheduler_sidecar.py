"""Unit tests for the env-driven JOBS builder in services.scheduler."""

import pytest


def test_build_jobs_uses_documented_defaults(monkeypatch):
    """No env overrides → default cadences."""
    for v in (
        "SCHEDULER_DATA_REFRESH_INTERVAL",
        "SCHEDULER_HEALTH_CHECK_INTERVAL",
        "SCHEDULER_TICK_SECONDS",
        "SCHEDULER_SCRIPT_RUN_INTERVAL",
    ):
        monkeypatch.delenv(v, raising=False)
    from services.scheduler.__main__ import build_jobs, resolved_tick_seconds
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["data-refresh"]    == "every 15m"
    assert jobs["health-check"]    == "every 5m"
    assert jobs["script-runner"]   == "every 1m"
    assert jobs["marketplaces"]    == "daily 03:00"
    assert resolved_tick_seconds() == 30


def test_build_jobs_honors_env_overrides(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_REFRESH_INTERVAL", "1800")  # 30m
    monkeypatch.setenv("SCHEDULER_HEALTH_CHECK_INTERVAL", "60")    # 1m
    monkeypatch.setenv("SCHEDULER_SCRIPT_RUN_INTERVAL", "120")     # 2m
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "10")
    from services.scheduler.__main__ import build_jobs, resolved_tick_seconds
    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["data-refresh"]  == "every 30m"
    assert jobs["health-check"]  == "every 1m"
    assert jobs["script-runner"] == "every 2m"
    assert resolved_tick_seconds() == 10


@pytest.mark.parametrize("var", [
    "SCHEDULER_DATA_REFRESH_INTERVAL",
    "SCHEDULER_HEALTH_CHECK_INTERVAL",
    "SCHEDULER_TICK_SECONDS",
    "SCHEDULER_SCRIPT_RUN_INTERVAL",
])
@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_build_jobs_rejects_invalid_env(monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    from services.scheduler.__main__ import build_jobs
    with pytest.raises(ValueError):
        build_jobs()


def test_build_jobs_rejects_tick_larger_than_smallest_interval(monkeypatch):
    """Tick must be <= the smallest job interval, otherwise jobs would
    consistently miss their cadence by up to one tick."""
    monkeypatch.setenv("SCHEDULER_HEALTH_CHECK_INTERVAL", "60")
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "120")
    from services.scheduler.__main__ import build_jobs
    with pytest.raises(ValueError, match="tick"):
        build_jobs()


def test_build_jobs_includes_run_due_endpoint():
    """The script-runner job must POST to /api/scripts/run-due."""
    from services.scheduler.__main__ import build_jobs
    target = next(j for j in build_jobs() if j[0] == "script-runner")
    name, schedule, endpoint, method, _timeout = target
    assert endpoint == "/api/scripts/run-due"
    assert method == "POST"


@pytest.mark.parametrize("seconds,expected", [
    # Exact multiples of 60 → unchanged.
    (60,   "every 1m"),
    (120,  "every 2m"),
    (900,  "every 15m"),
    # Exact multiples of 3600 → hour form.
    (3600, "every 1h"),
    (7200, "every 2h"),
    # Non-multiples of 60 must round UP (ceiling), so the job never fires
    # MORE often than the operator configured. Devin BUG_0001 on 1af2081.
    (90,   "every 2m"),  # 90s asked → 120s scheduled, NOT 60s
    (150,  "every 3m"),
    (61,   "every 2m"),
    (3601, "every 61m"),
    # Sub-minute clamps to 1m (schedule grammar minute-grained).
    (30,   "every 1m"),
    (1,    "every 1m"),
])
def test_seconds_to_schedule_rounds_up_not_down(seconds, expected):
    from services.scheduler.__main__ import _seconds_to_schedule
    assert _seconds_to_schedule(seconds) == expected, (
        f"_seconds_to_schedule({seconds}) must round UP — flooring would "
        f"make jobs fire more often than the operator configured."
    )
