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


def test_slack_config_default_transport_http():
    from app.chat.config import ChatConfig, SlackConfig
    cfg = ChatConfig()
    assert isinstance(cfg.slack, SlackConfig)
    assert cfg.slack.transport == "http"


@pytest.mark.parametrize("raw_transport,expected", [
    ("http", "http"),
    ("socket", "socket"),
    ("SOCKET", "socket"),     # case-insensitive
    ("websocket", "http"),    # unknown -> http
    ("", "http"),             # empty -> http
])
def test_load_chat_config_parses_slack_transport(tmp_path, raw_transport, expected, caplog):
    from app.chat.config import load_chat_config
    yaml_path = tmp_path / "instance.yaml"
    yaml_path.write_text(
        "chat:\n"
        "  enabled: true\n"
        "  slack:\n"
        f"    transport: {raw_transport!r}\n"
    )
    caplog.set_level(logging.WARNING, logger="app.chat.config")
    cfg = load_chat_config(yaml_path)
    assert cfg.slack.transport == expected
    if raw_transport.lower() not in ("http", "socket"):
        assert any("unknown slack transport" in r.message for r in caplog.records)


def test_load_chat_config_missing_slack_block_defaults_http(tmp_path):
    from app.chat.config import load_chat_config
    yaml_path = tmp_path / "instance.yaml"
    yaml_path.write_text("chat:\n  enabled: true\n")
    cfg = load_chat_config(yaml_path)
    assert cfg.slack.transport == "http"


@pytest.mark.parametrize("scalar", ["socket", "true", "123"])
def test_load_chat_config_scalar_slack_block_defaults_http(tmp_path, scalar):
    """A non-mapping `slack:` value (operator shorthand) must not crash startup;
    it falls back to the default transport."""
    from app.chat.config import load_chat_config
    yaml_path = tmp_path / "instance.yaml"
    yaml_path.write_text(f"chat:\n  enabled: true\n  slack: {scalar}\n")
    cfg = load_chat_config(yaml_path)
    assert cfg.slack.transport == "http"
