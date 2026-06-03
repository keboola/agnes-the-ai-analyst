"""Phase 0 — Slack transport abstraction unit tests."""
from __future__ import annotations

import asyncio
import logging

import pytest


def test_run_logged_swallows_and_logs_exception(caplog):
    """_run_logged must not propagate; it logs the failure so a scheduled
    task crash never tears down the event loop or surfaces as an unhandled
    task exception."""
    from services.slack_bot import events as ev

    caplog.set_level(logging.ERROR, logger="services.slack_bot.events")

    async def boom():
        raise RuntimeError("dispatch blew up")

    asyncio.run(ev._run_logged(boom()))

    assert any(
        "scheduled Slack dispatch failed" in r.message for r in caplog.records
    ), caplog.records


def test_run_logged_returns_normally_on_success():
    from services.slack_bot import events as ev

    seen: list[int] = []

    async def ok():
        seen.append(1)

    asyncio.run(ev._run_logged(ok()))
    assert seen == [1]


def test_run_logged_invokes_on_failure_notifier_on_exception():
    """Ack-then-fail recovery contract: since we ack Slack BEFORE processing,
    a dispatch failure triggers no Slack retry — _run_logged is the only
    recovery path. It must call the best-effort on_failure notifier with the
    raised exception, while still swallowing the error."""
    from services.slack_bot import events as ev

    notified: list[BaseException] = []

    async def boom():
        raise RuntimeError("dispatch blew up")

    async def notify(exc: BaseException) -> None:
        notified.append(exc)

    asyncio.run(ev._run_logged(boom(), on_failure=notify))

    assert len(notified) == 1
    assert isinstance(notified[0], RuntimeError)
    assert str(notified[0]) == "dispatch blew up"


def test_run_logged_does_not_call_on_failure_on_success():
    from services.slack_bot import events as ev

    notified: list[BaseException] = []

    async def ok():
        return None

    async def notify(exc: BaseException) -> None:
        notified.append(exc)

    asyncio.run(ev._run_logged(ok(), on_failure=notify))
    assert notified == []


def test_run_logged_swallows_a_failing_on_failure_notifier(caplog):
    """The recovery notifier is best-effort: if it ALSO raises, _run_logged
    must still not propagate and must log."""
    from services.slack_bot import events as ev

    caplog.set_level(logging.ERROR, logger="services.slack_bot.events")

    async def boom():
        raise RuntimeError("primary failure")

    async def notify(exc: BaseException) -> None:
        raise RuntimeError("notifier also failed")

    asyncio.run(ev._run_logged(boom(), on_failure=notify))  # must not raise

    assert any(
        "best-effort Slack failure notice failed" in r.message
        for r in caplog.records
    ), caplog.records


def test_schedule_keeps_strong_reference_until_done():
    """_schedule must retain a strong ref in the module-level set while the
    task runs, and discard it on completion (no GC-cancellation, no leak)."""
    from services.slack_bot import events as ev

    started = asyncio.Event()
    release = asyncio.Event()

    async def body():
        started.set()
        await release.wait()

    async def _run():
        task = ev._schedule(body())
        await started.wait()
        assert task in ev._BACKGROUND_TASKS  # strong ref held while running
        release.set()
        await task
        await asyncio.sleep(0)  # let the done-callback fire
        assert task not in ev._BACKGROUND_TASKS  # discarded on completion

    asyncio.run(_run())
