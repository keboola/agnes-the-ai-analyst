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
