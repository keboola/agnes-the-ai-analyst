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


def test_get_slack_transport_env_overrides_yaml(monkeypatch):
    import app.instance_config as ic
    monkeypatch.setattr(ic, "get_value", lambda *k, default=None: "socket")
    monkeypatch.setenv("SLACK_TRANSPORT", "http")
    assert ic.get_slack_transport() == "http"  # env wins


def test_get_slack_transport_yaml_when_env_unset(monkeypatch):
    import app.instance_config as ic
    monkeypatch.delenv("SLACK_TRANSPORT", raising=False)
    monkeypatch.setattr(ic, "get_value", lambda *k, default=None: "socket")
    assert ic.get_slack_transport() == "socket"


def test_get_slack_transport_default_http(monkeypatch):
    import app.instance_config as ic
    monkeypatch.delenv("SLACK_TRANSPORT", raising=False)
    monkeypatch.setattr(ic, "get_value", lambda *k, default=None: default)
    assert ic.get_slack_transport() == "http"


def test_get_slack_transport_unknown_value_falls_back_http(monkeypatch):
    import app.instance_config as ic
    monkeypatch.setenv("SLACK_TRANSPORT", "carrier-pigeon")
    assert ic.get_slack_transport() == "http"


def test_socket_dispatcher_acks_envelope_before_scheduling(monkeypatch):
    """_on_request must call send_socket_mode_response(envelope_id) BEFORE
    it schedules the dispatch, and dispatch_event must receive the exact
    payload["event"] dict (byte-identical to the HTTP extraction)."""
    from services.slack_bot.socket_mode_client import SocketModeDispatcher

    order: list[str] = []
    dispatched: list[dict] = []

    class FakeClient:
        async def send_socket_mode_response(self, resp):
            order.append(f"ack:{resp.envelope_id}")

    class FakeReq:
        def __init__(self):
            self.type = "events_api"
            self.envelope_id = "env-1"
            self.payload = {
                "type": "event_callback",
                "event": {"type": "app_mention", "channel": "C1",
                          "ts": "1.1", "user": "U1", "text": "<@A> hi"},
            }

    async def fake_dispatch(app, event):
        order.append("dispatch")
        dispatched.append(event)

    import services.slack_bot.socket_mode_client as smc
    monkeypatch.setattr(smc, "dispatch_event", fake_dispatch)

    app = object()
    disp = SocketModeDispatcher(app=app, app_token="xapp-x", bot_token="xoxb-y")

    async def _run():
        req = FakeReq()
        await disp._on_request(FakeClient(), req)
        import asyncio
        # two yields: one to enqueue the _schedule task, one to run the _run_logged body
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    import asyncio
    asyncio.run(_run())

    assert order[0] == "ack:env-1", order      # ack happens first
    assert "dispatch" in order
    assert order.index("ack:env-1") < order.index("dispatch")
    assert dispatched == [
        {"type": "app_mention", "channel": "C1", "ts": "1.1",
         "user": "U1", "text": "<@A> hi"}
    ]


def test_socket_dispatcher_ignores_non_event_callback(monkeypatch):
    """A non-event_callback payload (e.g. a hello) is still acked but not
    dispatched (slash/interactivity routing is a later phase)."""
    from services.slack_bot.socket_mode_client import SocketModeDispatcher

    acked: list[str] = []
    dispatched: list[dict] = []

    class FakeClient:
        async def send_socket_mode_response(self, resp):
            acked.append(resp.envelope_id)

    class FakeReq:
        type = "events_api"
        envelope_id = "env-2"
        payload = {"type": "hello"}

    async def fake_dispatch(app, event):
        dispatched.append(event)

    import asyncio
    import services.slack_bot.socket_mode_client as smc
    monkeypatch.setattr(smc, "dispatch_event", fake_dispatch)

    disp = SocketModeDispatcher(app=object(), app_token="xapp-x", bot_token="xoxb-y")
    asyncio.run(disp._on_request(FakeClient(), FakeReq()))
    assert acked == ["env-2"]
    assert dispatched == []


def test_socket_gate_ok_when_all_conditions_met(monkeypatch):
    from services.slack_bot import socket_mode_client as smc
    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: True)
    ok, reason = smc.socket_mode_preflight(
        workers=1, app_token="xapp-abc", bot_token="xoxb-def",
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize("workers,app_tok,bot_tok,sdk,needle", [
    (2, "xapp-a", "xoxb-b", True, "UVICORN_WORKERS"),     # multi-worker
    (1, "", "xoxb-b", True, "SLACK_APP_TOKEN"),           # missing app token
    (1, "xoxb-wrong", "xoxb-b", True, "xapp-"),           # app token wrong prefix
    (1, "xapp-a", "", True, "SLACK_BOT_TOKEN"),           # missing bot token
    (1, "xapp-a", "xapp-wrong", True, "xoxb-"),           # bot token wrong prefix
    (1, "xapp-a", "xoxb-b", False, "slack-socket"),       # sdk not importable
])
def test_socket_gate_fails_closed(monkeypatch, workers, app_tok, bot_tok, sdk, needle):
    from services.slack_bot import socket_mode_client as smc
    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: sdk)
    ok, reason = smc.socket_mode_preflight(
        workers=workers, app_token=app_tok, bot_token=bot_tok,
    )
    assert ok is False
    assert needle in reason
