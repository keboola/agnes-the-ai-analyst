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

    assert any("scheduled Slack dispatch failed" in r.message for r in caplog.records), caplog.records


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

    assert any("best-effort Slack failure notice failed" in r.message for r in caplog.records), caplog.records


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


@pytest.mark.parametrize(
    "raw_transport,expected",
    [
        ("http", "http"),
        ("socket", "socket"),
        ("SOCKET", "socket"),  # case-insensitive
        ("websocket", "http"),  # unknown -> http
        ("", "http"),  # empty -> http
    ],
)
def test_load_chat_config_parses_slack_transport(tmp_path, raw_transport, expected, caplog):
    from app.chat.config import load_chat_config

    yaml_path = tmp_path / "instance.yaml"
    yaml_path.write_text(f"chat:\n  enabled: true\n  slack:\n    transport: {raw_transport!r}\n")
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
    pytest.importorskip("slack_sdk")  # optional 'slack-socket' extra; _on_request imports SocketModeResponse
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
                "event": {"type": "app_mention", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@A> hi"},
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

    assert order[0] == "ack:env-1", order  # ack happens first
    assert "dispatch" in order
    assert order.index("ack:env-1") < order.index("dispatch")
    assert dispatched == [{"type": "app_mention", "channel": "C1", "ts": "1.1", "user": "U1", "text": "<@A> hi"}]


def test_socket_dispatcher_ignores_non_event_callback(monkeypatch):
    """A non-event_callback payload (e.g. a hello) is still acked but not
    dispatched (slash/interactivity routing is a later phase)."""
    pytest.importorskip("slack_sdk")  # optional 'slack-socket' extra; _on_request imports SocketModeResponse
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
        workers=1,
        app_token="xapp-abc",
        bot_token="xoxb-def",
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "workers,app_tok,bot_tok,sdk,needle",
    [
        (2, "xapp-a", "xoxb-b", True, "UVICORN_WORKERS"),  # multi-worker
        (1, "", "xoxb-b", True, "SLACK_APP_TOKEN"),  # missing app token
        (1, "xoxb-wrong", "xoxb-b", True, "xapp-"),  # app token wrong prefix
        (1, "xapp-a", "", True, "SLACK_BOT_TOKEN"),  # missing bot token
        (1, "xapp-a", "xapp-wrong", True, "xoxb-"),  # bot token wrong prefix
        (1, "xapp-a", "xoxb-b", False, "slack-socket"),  # sdk not importable
    ],
)
def test_socket_gate_fails_closed(monkeypatch, workers, app_tok, bot_tok, sdk, needle):
    from services.slack_bot import socket_mode_client as smc

    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: sdk)
    ok, reason = smc.socket_mode_preflight(
        workers=workers,
        app_token=app_tok,
        bot_token=bot_tok,
    )
    assert ok is False
    assert needle in reason


def test_start_slack_socket_transport_happy(monkeypatch):
    """The dispatcher only comes up via the slack-socket-mode leader lease
    (app/coordination/leases.py). Under the default memory backend the
    lease is always immediately acquired, so start() still fires before
    _start_slack_socket_transport returns — same externally observable
    contract as before the lease existed."""
    from types import SimpleNamespace
    import app.main as main_mod
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()
    started: list[str] = []

    class FakeDispatcher:
        def __init__(self, *, app, app_token, bot_token):
            self.app = app

        async def start(self):
            started.append("start")

        async def stop(self):
            started.append("stop")

    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "socket")
    monkeypatch.setattr(main_mod, "SocketModeDispatcher", FakeDispatcher)
    monkeypatch.setattr(main_mod, "socket_mode_preflight", lambda **k: (True, ""))
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-a")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-b")
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    app = SimpleNamespace(state=SimpleNamespace())

    import asyncio
    import contextlib

    async def _run():
        await main_mod._start_slack_socket_transport(app)
        assert started == ["start"]
        assert isinstance(app.state.slack_socket_dispatcher, FakeDispatcher)
        # Shutdown path: cancel the lease task like the lifespan teardown
        # does — must stop the dispatcher and not raise.
        task = app.state.slack_socket_lease_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert started == ["start", "stop"]
        assert app.state.slack_socket_dispatcher is None

    asyncio.run(_run())
    reset_coordination_for_tests()


def test_start_slack_socket_transport_http_is_noop(monkeypatch):
    from types import SimpleNamespace
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "http")
    app = SimpleNamespace(state=SimpleNamespace())
    import asyncio

    asyncio.run(main_mod._start_slack_socket_transport(app))
    assert getattr(app.state, "slack_socket_dispatcher", None) is None
    assert getattr(app.state, "slack_socket_lease_task", None) is None


def test_start_slack_socket_transport_failclosed_on_preflight(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "socket")
    monkeypatch.setattr(main_mod, "socket_mode_preflight", lambda **k: (False, "SLACK_APP_TOKEN missing"))
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    caplog.set_level(logging.ERROR, logger="app.main")
    app = SimpleNamespace(state=SimpleNamespace())
    import asyncio

    asyncio.run(main_mod._start_slack_socket_transport(app))
    assert app.state.slack_socket_dispatcher is None
    assert app.state.slack_socket_lease_task is None
    assert any("Slack Socket Mode disabled" in r.message for r in caplog.records)


def test_start_slack_socket_transport_routes_through_run_with_lease(monkeypatch):
    """Wiring-level check: when socket mode is enabled and preflight passes,
    setup must go through app.coordination.leases.run_with_lease rather than
    calling SocketModeDispatcher directly — this is what gives Slack socket
    mode N-replica safety under a shared (redis) coordination backend."""
    from types import SimpleNamespace
    import app.main as main_mod

    calls: list[dict] = []

    async def fake_run_with_lease(name, holder_id, *, ttl_s, start, stop):
        calls.append({"name": name, "holder_id": holder_id, "ttl_s": ttl_s})
        await start()
        await stop()

    class FakeDispatcher:
        def __init__(self, *, app, app_token, bot_token):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "socket")
    monkeypatch.setattr(main_mod, "SocketModeDispatcher", FakeDispatcher)
    monkeypatch.setattr(main_mod, "socket_mode_preflight", lambda **k: (True, ""))
    monkeypatch.setattr("app.coordination.leases.run_with_lease", fake_run_with_lease)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-a")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-b")
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    app = SimpleNamespace(state=SimpleNamespace())

    import asyncio

    asyncio.run(main_mod._start_slack_socket_transport(app))

    assert len(calls) == 1
    assert calls[0]["name"] == "slack-socket-mode"
    assert calls[0]["ttl_s"] == 15
    assert calls[0]["holder_id"]  # non-empty hostname:pid


def test_socket_dispatcher_routes_slash_commands(monkeypatch):
    """A slash_commands envelope is acked FIRST (with a 'working on it'
    ephemeral payload, mirroring the HTTP endpoint's sync ack body) and the
    raw payload dict — same fields as the HTTP form body — is handed to
    dispatch_command verbatim."""
    pytest.importorskip("slack_sdk")
    from services.slack_bot.socket_mode_client import SocketModeDispatcher

    order: list[str] = []
    acks: list[tuple[str, object]] = []
    dispatched: list[dict] = []

    class FakeClient:
        async def send_socket_mode_response(self, resp):
            order.append(f"ack:{resp.envelope_id}")
            acks.append((resp.envelope_id, getattr(resp, "payload", None)))

    class FakeReq:
        type = "slash_commands"
        envelope_id = "env-3"
        payload = {
            "command": "/agnes-status",
            "text": "",
            "user_id": "U1",
            "channel_id": "C1",
            "response_url": "https://r/1",
        }

    async def fake_dispatch(app, cmd):
        order.append("dispatch")
        dispatched.append(cmd)

    import services.slack_bot.socket_mode_client as smc

    monkeypatch.setattr(smc, "dispatch_command", fake_dispatch)

    disp = SocketModeDispatcher(app=object(), app_token="xapp-x", bot_token="xoxb-y")

    async def _run():
        await disp._on_request(FakeClient(), FakeReq())
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)

    import asyncio

    asyncio.run(_run())

    assert order[0] == "ack:env-3", order
    assert order.index("ack:env-3") < order.index("dispatch")
    assert dispatched == [FakeReq.payload]
    # The ack carries the interim ephemeral so the user sees feedback <3s.
    assert acks[0][1] and "Working on it" in str(acks[0][1])


def test_socket_dispatcher_slash_help_answers_in_ack(monkeypatch):
    """`/agnes` with empty/help text is answered synchronously inside the
    ack payload (mirrors the HTTP endpoint) — no dispatch is scheduled."""
    pytest.importorskip("slack_sdk")
    from services.slack_bot.commands import _help_body
    from services.slack_bot.socket_mode_client import SocketModeDispatcher

    acks: list[object] = []
    dispatched: list[dict] = []

    class FakeClient:
        async def send_socket_mode_response(self, resp):
            acks.append(getattr(resp, "payload", None))

    class FakeReq:
        type = "slash_commands"
        envelope_id = "env-4"
        payload = {
            "command": "/agnes",
            "text": "help",
            "user_id": "U1",
            "channel_id": "C1",
            "response_url": "https://r/2",
        }

    async def fake_dispatch(app, cmd):
        dispatched.append(cmd)

    import asyncio
    import services.slack_bot.socket_mode_client as smc

    monkeypatch.setattr(smc, "dispatch_command", fake_dispatch)

    disp = SocketModeDispatcher(app=object(), app_token="xapp-x", bot_token="xoxb-y")

    async def _run():
        await disp._on_request(FakeClient(), FakeReq())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert dispatched == []
    assert acks and acks[0] and acks[0]["text"] == _help_body()
