"""Unit tests for Slack slash commands (Phase 2)."""
from __future__ import annotations

import asyncio
import json

import pytest


def test_send_ephemeral_posts_to_response_url(monkeypatch):
    from services.slack_bot import sender as snd

    posted = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["json"] = json
            return _FakeResp()

    monkeypatch.setattr(snd.httpx, "AsyncClient", _FakeClient)
    asyncio.run(snd.send_ephemeral("https://hooks.slack/r/1", "hi", blocks=None))
    assert posted["url"] == "https://hooks.slack/r/1"
    assert posted["json"]["response_type"] == "ephemeral"
    assert posted["json"]["text"] == "hi"
    assert "blocks" not in posted["json"]


def test_open_im_returns_channel_id(monkeypatch):
    from services.slack_bot import sender as snd

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True, "channel": {"id": "D777"}}

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            assert url.endswith("/conversations.open")
            assert json == {"users": "U123"}
            return _FakeResp()

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(snd.httpx, "AsyncClient", _FakeClient)
    got = asyncio.run(snd.open_im("U123"))
    assert got == "D777"


def test_open_im_returns_none_without_token(monkeypatch):
    from services.slack_bot import sender as snd
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert asyncio.run(snd.open_im("U123")) is None


def test_ephemeral_command_sink_forwards_first_assistant_message(monkeypatch):
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)

    async def _run():
        s = sink_mod.EphemeralCommandSink(response_url="https://r/1")
        await s.send_json({"type": "token", "text": "noisy"})   # dropped
        await s.send_json({"type": "ready"})                    # dropped
        await s.send_json({"type": "assistant_message", "content": "answer"})
        await s.send_json({"type": "assistant_message", "content": "second"})  # ignored
        await s.close()

    asyncio.run(_run())
    assert sent == [("https://r/1", "answer")]


def test_help_body_is_nonempty_and_lists_commands():
    from services.slack_bot.commands import _help_body
    body = _help_body()
    assert "/agnes" in body
    assert "/agnes-new" in body
    assert "/agnes-status" in body


def test_dispatch_command_routes_unknown_to_noop():
    """Unknown command must not raise — log + return."""
    from services.slack_bot import commands as cmds

    cmd = {"command": "/nope", "text": "", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/x"}

    # Should complete without raising.
    asyncio.run(cmds.dispatch_command(app=object(), cmd=cmd))


def test_run_logged_swallows_and_posts_ephemeral(monkeypatch):
    """_run_logged must not propagate; it posts a best-effort ephemeral."""
    from services.slack_bot import commands as cmds

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(cmds, "send_ephemeral", fake_send)

    async def _boom():
        raise RuntimeError("kaboom")

    # Completes without raising; posts to the response_url it was given.
    asyncio.run(cmds._run_logged(_boom(), response_url="https://r/err"))
    assert sent and sent[0][0] == "https://r/err"
    assert "went wrong" in sent[0][1].lower()


def test_run_logged_no_response_url_still_swallows(monkeypatch):
    from services.slack_bot import commands as cmds

    async def _boom():
        raise RuntimeError("kaboom")

    # No response_url → nothing posted, but still no raise.
    asyncio.run(cmds._run_logged(_boom(), response_url=None))


def test_ephemeral_command_sink_forwards_error(monkeypatch):
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)

    async def _run():
        s = sink_mod.EphemeralCommandSink(response_url="https://r/2")
        await s.send_json({"type": "error", "kind": "rate_limit", "message": "slow down"})
        await s.close()

    asyncio.run(_run())
    assert len(sent) == 1
    assert "rate_limit" in sent[0][1] and "slow down" in sent[0][1]
