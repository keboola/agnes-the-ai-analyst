# Phase 3 — Interactivity / Block Kit Buttons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add four Block Kit interactive buttons to Slack replies — Stop, Continue-on-web (deep link), Share-to-channel (allowlisted-only, re-checked at click time), New-session — wired through a new signature-verified `POST /api/slack/interactivity` endpoint that acks in 3s and processes async, and actually emitted onto outbound bot DM replies.

**Architecture:** Slack delivers interactions as `x-www-form-urlencoded` POSTs carrying a single `payload` JSON field to a dedicated Request URL. A new endpoint in `app/api/slack.py` verifies the HMAC on the raw body, then schedules `dispatch_interaction` via an ack-then-async helper pair (`_schedule` / `_run_logged`) and returns an empty 200 (leaving the source message unchanged). A leaf `blocks.py` builds pure Block Kit JSON (every interactive element carries a structured JSON `value` so handlers never re-parse free text); `interactivity.py` parses + routes; `sender.py` and `sink.py` gain the outbound primitives, and `SlackSinkBridge` both emits the buttons onto DM replies and runs the Stop-button lifecycle.

**Tech Stack:** Python 3.11, FastAPI, httpx (async Slack Web API calls), DuckDB/Postgres (audit), pytest. Vendor-agnostic OSS.

> **Self-contained foundation.** The original spec assigns `_schedule`/`_run_logged` to Phase 0, `is_channel_allowlisted`/`slack_bot_user_id` to Phase 1, and `send_ephemeral`/`_soft_archive_dm`/`EphemeralCommandSink` to Phase 2. On current `main` **none of those exist yet** (`services/slack_bot/events.py` has no `_schedule`; `_handle_mention` is a logging stub; `binding.py` has no `is_channel_allowlisted`; `sender.py` has only `send_thread_reply`; there is no `commands.py`). To keep this plan executable against current `main` without depending on unbuilt phases, **Task 0 builds the small shared primitives this phase actually needs** (`_schedule`, `_run_logged`, `send_ephemeral`, `is_channel_allowlisted`, `_soft_archive_dm`). They are written so the later phases can adopt them as-is rather than redefining — each is the canonical home named in the spec. Symbols this phase reuses that already exist on `main`: `ChatManager.cancel(chat_id)` (`app/chat/manager.py`), `write_audit(conn, *, user_email, action, details)` (`app/chat/audit.py`), `lookup_user_email(repo, slack_user_id)` (`services/slack_bot/binding.py`), `verify_slack_signature(secret, ts, sig, body)` (`services/slack_bot/sigverify.py`), `app.state.chat_repo` / `app.state.chat_manager`, `ChatSession.id`, and the runner's `{"type":"done"}` turn-end frame (`app/chat/runner.py`).

---

## File Structure

**Created**
- `services/slack_bot/blocks.py` — leaf, pure Block Kit builders (no I/O, imports nothing from the other slack_bot modules). Exports `stop_button_blocks`, `continue_on_web_block`, `share_to_channel_blocks`, `new_session_block`, and an `encode_value`/`decode_value` codec pair.
- `services/slack_bot/interactivity.py` — `parse_interaction(payload) -> Interaction` + `dispatch_interaction(app, interaction)` router + the three callback handlers (`_on_stop`, `_on_share`, `_on_new_session`) + the share-answer TTL token map. Verification lives in `app/api/slack.py`, not here.
- `services/slack_bot/commands.py` — created in Task 0 to host the shared `_soft_archive_dm(app, owner_email, channel_id)` helper (the spec's Phase-2 file; this phase only adds the one helper New-session and `/agnes-new` share).
- `tests/test_slack_interactivity.py` — unit + integration tests for blocks, parse/dispatch routing, endpoint sig-verify, per-button rules, sink button emission + Stop lifecycle, 3s-ack regression.

**Modified**
- `services/slack_bot/events.py` — add `_schedule(coro)` + `_run_logged(coro)`; flip `_handle_dm` to ack-then-async via `_schedule`; pass `chat_id=session.id`, `owner=user_email`, and `web_base` when constructing `SlackSinkBridge`.
- `app/api/slack.py` — flip `/api/slack/events` to `_schedule(_run_logged(dispatch_event(...)))`; add `POST /api/slack/interactivity` (verify HMAC on raw body, parse form, schedule dispatch, empty 200 ack).
- `services/slack_bot/sender.py` — add `post_thread_reply_with_blocks(channel, thread_ts, text, blocks) -> str|None` (returns posted `ts`), `update_message(channel, ts, text, blocks) -> None`, `post_channel_message(channel, text) -> None`, `respond_via_response_url(response_url, body) -> None`, and `send_ephemeral(response_url, text, blocks=None) -> None`.
- `services/slack_bot/binding.py` — add `is_channel_allowlisted(conn, channel_id) -> bool` (direct `Everyone`-scoped grant lookup, no admin short-circuit).
- `services/slack_bot/sink.py` — `SlackSinkBridge.__init__` gains `chat_id`, `owner`, `web_base`; emits Stop + Continue-on-web + New-session blocks on the first assistant turn-post; strips the Stop button on `cancelled`/`error`/`done`.
- `services/slack_bot/manifest.yaml` — flip `interactivity.is_enabled: true` + add `request_url`.
- `docs/cloud-chat.md` — add two documented manifest stanzas (HTTP + Socket) including the interactivity Request URL.
- `CHANGELOG.md` — `[Unreleased]` bullet.

---

## Task 0 — Shared foundation primitives (ack-then-async, ephemerals, allowlist, soft-archive)

**Files:**
- `services/slack_bot/events.py` (Modify)
- `app/api/slack.py` (Modify)
- `services/slack_bot/sender.py` (Modify)
- `services/slack_bot/binding.py` (Modify)
- `services/slack_bot/commands.py` (Create)
- `tests/test_slack_interactivity.py` (Test, Create)

These five primitives are imported by Tasks 4/6/7/8/9. The spec assigns them to earlier phases, but they do not exist on current `main`, so this phase builds the minimal versions it needs. Each lives in its spec-designated home so a later phase adopts rather than re-defines it.

- [ ] Write a failing test for `_schedule` + `_run_logged`. Create `tests/test_slack_interactivity.py`:
```python
"""Tests for Slack Block Kit interactivity (Phase 3)."""
import asyncio


def test_schedule_keeps_strong_ref_until_done():
    from services.slack_bot import events as ev

    ran = []
    async def work():
        ran.append(True)

    async def _drive():
        ev._schedule(work())
        # Give the scheduled task a turn to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert ran == [True]


def test_run_logged_swallows_exceptions():
    from services.slack_bot import events as ev

    async def boom():
        raise ValueError("kaboom")

    # Must NOT raise — _run_logged is the only recovery path post-ack.
    asyncio.run(ev._run_logged(boom()))
```
- [ ] Run it, expect FAIL (`AttributeError: _schedule`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "schedule or run_logged" -v`.
- [ ] Add the helpers to `services/slack_bot/events.py` (after the `logger = ...` line):
```python
# Strong refs to in-flight detached dispatch tasks so the GC can't cancel
# one mid-flight. Discarded on completion. (Spec §1 "detached-task leakage".)
_INFLIGHT: set[asyncio.Task] = set()


async def _run_logged(coro) -> None:
    """Wrap a scheduled dispatch coroutine: log any exception instead of
    letting it surface as an unhandled task error. Post-ack failures have
    no Slack retry, so this is the only recovery path (spec §1)."""
    try:
        await coro
    except Exception:
        logger.exception("slack dispatch failed")


def _schedule(coro) -> None:
    """Fire-and-forget a coroutine while holding a strong reference."""
    task = asyncio.create_task(coro)
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "schedule or run_logged" -v`.
- [ ] Flip the HTTP events endpoint to ack-then-async. Edit `app/api/slack.py` — change the import line:
```python
from services.slack_bot.events import dispatch_event
```
to:
```python
from services.slack_bot.events import _run_logged, _schedule, dispatch_event
```
- [ ] In `app/api/slack.py` replace:
```python
    if payload.get("type") == "event_callback":
        await dispatch_event(request.app, payload["event"])
        return {"ok": True}
```
with:
```python
    if payload.get("type") == "event_callback":
        _schedule(_run_logged(dispatch_event(request.app, payload["event"])))
        return {"ok": True}
```
- [ ] Flip `_handle_dm` off its `create_task` + `sleep(0.1)` shim onto `_schedule`. In `services/slack_bot/events.py` replace:
```python
    if not _is_attached(mgr, session.id):
        sink = SlackSinkBridge(channel=channel, thread_ts=thread_ts)
        asyncio.create_task(mgr.attach(session.id, sink))
        # Give attach() a beat to set up the pump and emit `ready` before
        # we feed the user message into the runner stdin.
        await asyncio.sleep(0.1)
    await mgr.send_user_message(session.id, text)
```
with:
```python
    if not _is_attached(mgr, session.id):
        web_base = getattr(app.state, "public_url", "")
        sink = SlackSinkBridge(
            channel=channel, thread_ts=thread_ts,
            chat_id=session.id, owner=user_email, web_base=web_base,
        )
        _schedule(mgr.attach(session.id, sink))
        # Give attach() a beat to set up the pump and emit `ready` before
        # we feed the user message into the runner stdin.
        await asyncio.sleep(0.1)
    await mgr.send_user_message(session.id, text)
```
- [ ] Run the existing slack DM suite, expect PASS (back-compat: `SlackSinkBridge` still constructed, sink-instance assertion still holds): `.venv/bin/pytest tests/test_slack_bot.py -k "dm" -v`.
- [ ] Write a failing test for `send_ephemeral`. Append to `tests/test_slack_interactivity.py`:
```python
class _FakeResp:
    def __init__(self, data): self._data = data
    def json(self): return self._data


class _FakeClient:
    """Captures (url, json) of each post; returns canned ts for postMessage."""
    def __init__(self, *a, **k): self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return _FakeResp({"ok": True, "ts": "9.9"})


def _patch_client(monkeypatch):
    captured = {}
    def factory(*a, **k):
        captured["client"] = _FakeClient()
        return captured["client"]
    import services.slack_bot.sender as snd
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(snd.httpx, "AsyncClient", factory)
    return captured


def test_send_ephemeral_posts_to_response_url(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.send_ephemeral("https://hooks.example/r", "nope"))
    url, body = cap["client"].calls[0]
    assert url == "https://hooks.example/r"
    assert body["response_type"] == "ephemeral"
    assert body["text"] == "nope"
```
- [ ] Run it, expect FAIL (`AttributeError: send_ephemeral`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "send_ephemeral" -v`.
- [ ] Add `send_ephemeral` to `services/slack_bot/sender.py` (after `send_thread_reply`):
```python
async def send_ephemeral(response_url: str, text: str, blocks: list[dict] | None = None) -> None:
    """POST an ephemeral message to a Slack response_url. Used by slash /
    interactivity handlers for per-clicker, non-public replies."""
    body: dict = {"response_type": "ephemeral", "text": text}
    if blocks is not None:
        body["blocks"] = blocks
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(response_url, json=body)
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "send_ephemeral" -v`.
- [ ] Write a failing test for `is_channel_allowlisted`. Append to `tests/test_slack_interactivity.py`:
```python
def test_is_channel_allowlisted_default_deny_and_grant(tmp_path):
    import duckdb
    from services.slack_bot.binding import is_channel_allowlisted
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE resource_grants ("
        " group_name VARCHAR, resource_type VARCHAR, resource_id VARCHAR)"
    )
    # default-deny
    assert is_channel_allowlisted(conn, "C1") is False
    # Everyone grant flips it on
    conn.execute(
        "INSERT INTO resource_grants VALUES ('Everyone', 'slack_channel', 'C1')"
    )
    assert is_channel_allowlisted(conn, "C1") is True
    # other channels stay denied
    assert is_channel_allowlisted(conn, "C2") is False
```
- [ ] Run it, expect FAIL (`ImportError: cannot import name 'is_channel_allowlisted'`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "allowlisted" -v`.
- [ ] Add `is_channel_allowlisted` to `services/slack_bot/binding.py` (after `lookup_user_email`):
```python
def is_channel_allowlisted(conn, channel_id: str) -> bool:
    """True iff the Everyone group holds (SLACK_CHANNEL, channel_id).

    Direct grant lookup scoped to the Everyone group — deliberately does NOT
    use can_access (no admin short-circuit), so an admin's mere presence in a
    channel can't make Agnes post there (spec §2/§4 security note).
    """
    row = conn.execute(
        "SELECT 1 FROM resource_grants"
        " WHERE group_name = 'Everyone' AND resource_type = 'slack_channel'"
        " AND resource_id = ? LIMIT 1",
        [channel_id],
    ).fetchone()
    return row is not None
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "allowlisted" -v`.
- [ ] Write a failing test for `_soft_archive_dm`. Append to `tests/test_slack_interactivity.py`:
```python
def test_soft_archive_dm_kills_and_archives_existing(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import commands as cmd

    killed, archived = [], []
    async def kill(sid): killed.append(sid)
    mgr = SimpleNamespace(kill=kill)
    repo = SimpleNamespace(
        get_slack_dm_session=lambda owner, channel: SimpleNamespace(id="s1"),
        archive_session=lambda sid: archived.append(sid),
    )
    app = SimpleNamespace(state=SimpleNamespace(chat_manager=mgr, chat_repo=repo))
    asyncio.run(cmd._soft_archive_dm(app, "a@example.com", "D1"))
    assert killed == ["s1"]
    assert archived == ["s1"]


def test_soft_archive_dm_noop_when_no_session(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import commands as cmd

    killed, archived = [], []
    async def kill(sid): killed.append(sid)
    mgr = SimpleNamespace(kill=kill)
    repo = SimpleNamespace(
        get_slack_dm_session=lambda owner, channel: None,
        archive_session=lambda sid: archived.append(sid),
    )
    app = SimpleNamespace(state=SimpleNamespace(chat_manager=mgr, chat_repo=repo))
    asyncio.run(cmd._soft_archive_dm(app, "a@example.com", "D1"))
    assert killed == [] and archived == []
```
- [ ] Run it, expect FAIL (`ModuleNotFoundError: services.slack_bot.commands`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "soft_archive" -v`.
- [ ] Create `services/slack_bot/commands.py`:
```python
"""Slack slash-command dispatch + shared helpers.

This phase (interactivity) only needs the shared soft-archive helper that
New-session and /agnes-new both route through; the full slash dispatcher is
built in the slash-commands phase and lands alongside this file.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _soft_archive_dm(app, owner_email: str, channel_id: str) -> None:
    """Soft-archive the owner's live DM session for an IM channel.

    Shared path for New-session (button) and /agnes-new (slash): resolve the
    owner's DM session, kill the live runner, mark the row archived. No-op
    when there is no live/active session for that owner+channel.
    """
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    session = repo.get_slack_dm_session(owner_email, channel_id)
    if session is None:
        return
    await mgr.kill(session.id)
    repo.archive_session(session.id)
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "soft_archive" -v`.
- [ ] Commit: `git add services/slack_bot/events.py app/api/slack.py services/slack_bot/sender.py services/slack_bot/binding.py services/slack_bot/commands.py tests/test_slack_interactivity.py && git commit -m "slack: ack-then-async helpers, ephemeral sender, channel allowlist, soft-archive helper"`

---

## Task 1 — `blocks.py`: pure Block Kit builders + value codec

**Files:**
- `services/slack_bot/blocks.py` (Create)
- `tests/test_slack_interactivity.py` (Test)

The four buttons all live behind one `action_id` namespace and carry a structured JSON `value`. Slack caps a button `value` at 2000 chars; the codec keeps `value` small and JSON-typed so handlers never parse free text.

- [ ] Write a failing test for the value codec. Append to `tests/test_slack_interactivity.py`:
```python
def test_value_codec_roundtrip():
    from services.slack_bot import blocks
    v = blocks.encode_value({"chat_id": "sess-1", "owner": "a@example.com"})
    assert isinstance(v, str)
    assert blocks.decode_value(v) == {"chat_id": "sess-1", "owner": "a@example.com"}


def test_decode_value_rejects_garbage():
    from services.slack_bot import blocks
    assert blocks.decode_value("not-json") == {}
    assert blocks.decode_value("") == {}
```
- [ ] Run it, expect FAIL (module missing): `.venv/bin/pytest tests/test_slack_interactivity.py -k "value_codec or decode_value" -v` → `ModuleNotFoundError: No module named 'services.slack_bot.blocks'`.
- [ ] Create `services/slack_bot/blocks.py` with the codec:
```python
"""Pure Block Kit builders for Slack interactivity (Phase 3).

Leaf module: imports nothing from the other slack_bot modules. Every
interactive element carries a structured JSON ``value`` so handlers in
interactivity.py never re-parse free text. Slack caps a button ``value``
at 2000 chars — keep payloads tiny (ids + emails, never message bodies).
"""
from __future__ import annotations

import json
from typing import Any

# Single action_id namespace; the dispatcher routes on these.
ACTION_STOP = "agnes_stop"
ACTION_CONTINUE_WEB = "agnes_continue_web"
ACTION_SHARE_CHANNEL = "agnes_share_channel"
ACTION_NEW_SESSION = "agnes_new_session"


def encode_value(data: dict[str, Any]) -> str:
    """Serialize a structured button value to a compact JSON string."""
    return json.dumps(data, separators=(",", ":"))


def decode_value(raw: str) -> dict[str, Any]:
    """Parse a button value; return {} on any malformed input (fail-soft)."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "value_codec or decode_value" -v`.
- [ ] Commit: `git add services/slack_bot/blocks.py tests/test_slack_interactivity.py && git commit -m "slack: add Block Kit value codec"`

- [ ] Write a failing test for the four builders. Append to `tests/test_slack_interactivity.py`:
```python
def test_stop_button_blocks_shape():
    from services.slack_bot import blocks
    bs = blocks.stop_button_blocks(text="working...", chat_id="sess-1", owner="a@example.com")
    section = next(b for b in bs if b["type"] == "section")
    assert section["text"]["text"] == "working..."
    actions = next(b for b in bs if b["type"] == "actions")
    btn = actions["elements"][0]
    assert btn["action_id"] == blocks.ACTION_STOP
    assert blocks.decode_value(btn["value"]) == {"chat_id": "sess-1", "owner": "a@example.com"}


def test_continue_on_web_block_is_link_only():
    from services.slack_bot import blocks
    block = blocks.continue_on_web_block(web_base="https://host.example", chat_id="sess-1")
    btn = block["elements"][0]
    assert btn["url"] == "https://host.example/chat?session=sess-1"
    # Pure link button: no action_id callback (Slack never POSTs link clicks).
    assert "action_id" not in btn


def test_continue_on_web_block_none_when_no_web_base():
    from services.slack_bot import blocks
    # No public_url configured → no deep-link button rather than a broken URL.
    assert blocks.continue_on_web_block(web_base="", chat_id="sess-1") is None


def test_share_to_channel_blocks_carry_token():
    from services.slack_bot import blocks
    bs = blocks.share_to_channel_blocks(channel_id="C123", token="tok-abc")
    actions = next(b for b in bs if b["type"] == "actions")
    btn = actions["elements"][0]
    assert btn["action_id"] == blocks.ACTION_SHARE_CHANNEL
    assert blocks.decode_value(btn["value"]) == {"channel_id": "C123", "token": "tok-abc"}


def test_new_session_block_carries_owner_and_channel():
    from services.slack_bot import blocks
    block = blocks.new_session_block(channel_id="D1", owner="a@example.com")
    btn = block["elements"][0]
    assert btn["action_id"] == blocks.ACTION_NEW_SESSION
    assert blocks.decode_value(btn["value"]) == {"channel_id": "D1", "owner": "a@example.com"}
```
- [ ] Run it, expect FAIL (`AttributeError: stop_button_blocks`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "blocks_shape or continue_on_web or share_to_channel or new_session_block" -v`.
- [ ] Add the builders to `services/slack_bot/blocks.py`:
```python
def stop_button_blocks(*, text: str, chat_id: str, owner: str) -> list[dict[str, Any]]:
    """A reply section + a Stop button that cancels the live turn.

    ``value`` carries chat_id + owner so the handler authorizes the clicker
    against the session owner without a DB round-trip for ownership shape.
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_STOP,
                    "text": {"type": "plain_text", "text": "Stop"},
                    "style": "danger",
                    "value": encode_value({"chat_id": chat_id, "owner": owner}),
                }
            ],
        },
    ]


def continue_on_web_block(*, web_base: str, chat_id: str) -> dict[str, Any] | None:
    """A pure link button to the web deep link. No callback — Slack never
    POSTs clicks on buttons that carry a ``url``. Returns None when no
    web_base is configured (so callers simply omit the button)."""
    if not web_base:
        return None
    base = web_base.rstrip("/")
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Continue on web"},
                "url": f"{base}/chat?session={chat_id}",
            }
        ],
    }


def share_to_channel_blocks(*, channel_id: str, token: str) -> list[dict[str, Any]]:
    """Share button for an ephemeral /agnes answer. The answer body is held
    server-side under ``token`` (a long answer can exceed the 2000-char value
    cap), so only the token + channel_id ride in ``value``."""
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_SHARE_CHANNEL,
                    "text": {"type": "plain_text", "text": "Share to channel"},
                    "value": encode_value({"channel_id": channel_id, "token": token}),
                }
            ],
        }
    ]


def new_session_block(*, channel_id: str, owner: str) -> dict[str, Any]:
    """New-session button for a DM thread. Soft-archives the current DM
    session (shared path with /agnes-new)."""
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_NEW_SESSION,
                "text": {"type": "plain_text", "text": "New session"},
                "value": encode_value({"channel_id": channel_id, "owner": owner}),
            }
        ],
    }
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "blocks_shape or continue_on_web or share_to_channel or new_session_block" -v`.
- [ ] Commit: `git add services/slack_bot/blocks.py tests/test_slack_interactivity.py && git commit -m "slack: add Block Kit button builders"`

---

## Task 2 — `sender.py`: outbound primitives for interactivity

**Files:**
- `services/slack_bot/sender.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Outbound always rides the Slack Web API (mirrors the existing `send_thread_reply`). Add: post-with-blocks (returns `ts`), `update_message`, a non-threaded channel post, and a `response_url` POST. All read `SLACK_BOT_TOKEN` from env at use site (per spec; tokens never in config). (`send_ephemeral` was added in Task 0.)

- [ ] Write a failing test that the new sender functions post the right Slack API payloads. Append to `tests/test_slack_interactivity.py`:
```python
def test_post_thread_reply_with_blocks_returns_ts(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    ts = asyncio.run(snd.post_thread_reply_with_blocks("C1", "1.1", "hi", [{"type": "x"}]))
    assert ts == "9.9"
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.postMessage")
    assert body["channel"] == "C1" and body["thread_ts"] == "1.1"
    assert body["blocks"] == [{"type": "x"}] and body["text"] == "hi"


def test_update_message_calls_chat_update(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.update_message("C1", "9.9", "final", []))
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.update")
    assert body == {"channel": "C1", "ts": "9.9", "text": "final", "blocks": []}


def test_post_channel_message_omits_thread_ts(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.post_channel_message("C1", "public answer"))
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.postMessage")
    assert body == {"channel": "C1", "text": "public answer"}


def test_respond_via_response_url_posts_body(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.respond_via_response_url("https://hooks.example/r", {"delete_original": True}))
    url, body = cap["client"].calls[0]
    assert url == "https://hooks.example/r"
    assert body == {"delete_original": True}
```
- [ ] Run it, expect FAIL (`AttributeError: post_thread_reply_with_blocks`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "post_thread or update_message or channel_message or response_url" -v`.
- [ ] Add the functions to `services/slack_bot/sender.py` (after `send_ephemeral`):
```python
async def post_thread_reply_with_blocks(
    channel: str, thread_ts: str, text: str, blocks: list[dict],
) -> str | None:
    """Post a threaded reply with Block Kit blocks; return the message ts
    (so the caller can later chat.update it to strip the buttons), or None
    on failure."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot reply")
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "thread_ts": thread_ts, "text": text, "blocks": blocks},
        )
    data = resp.json()
    if not data.get("ok"):
        logger.error("chat.postMessage failed: %s", data.get("error"))
        return None
    return data.get("ts")


async def update_message(channel: str, ts: str, text: str, blocks: list[dict]) -> None:
    """Edit an existing message (used to strip the Stop button at turn end)."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot update")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "ts": ts, "text": text, "blocks": blocks},
        )


async def post_channel_message(channel: str, text: str) -> None:
    """Public, non-threaded channel post (Share-to-channel promotion)."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot post")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
        )


async def respond_via_response_url(response_url: str, body: dict) -> None:
    """POST a raw body to a Slack response_url (clear-ephemeral, ephemeral
    fallback). 30-min / 5-post limited — single-shot use only."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(response_url, json=body)
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "post_thread or update_message or channel_message or response_url" -v`.
- [ ] Commit: `git add services/slack_bot/sender.py tests/test_slack_interactivity.py && git commit -m "slack: add block/update/channel/response_url sender primitives"`

---

## Task 3 — `interactivity.py`: `parse_interaction` + routing skeleton

**Files:**
- `services/slack_bot/interactivity.py` (Create)
- `tests/test_slack_interactivity.py` (Test)

`parse_interaction` normalizes Slack's `block_actions` payload into a small dataclass so handlers never reach into raw JSON. `dispatch_interaction` routes on the clicked button's `action_id`. The three callback handlers are stubbed here and filled in Tasks 4/6/7. (Continue-on-web is a pure link button — Slack never POSTs it — so it has no handler.)

- [ ] Write a failing test for `parse_interaction` + routing. Append to `tests/test_slack_interactivity.py`:
```python
def _block_actions_payload(action_id, value, *, user="U1", channel="C1", response_url="https://r"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": channel},
        "response_url": response_url,
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_parse_interaction_extracts_first_action():
    from services.slack_bot import interactivity as inter, blocks
    payload = _block_actions_payload(blocks.ACTION_STOP, blocks.encode_value({"chat_id": "s1"}))
    it = inter.parse_interaction(payload)
    assert it.action_id == blocks.ACTION_STOP
    assert it.slack_user_id == "U1"
    assert it.channel_id == "C1"
    assert it.response_url == "https://r"
    assert it.value == {"chat_id": "s1"}


def test_parse_interaction_no_actions_yields_empty_action_id():
    from services.slack_bot import interactivity as inter
    it = inter.parse_interaction({"type": "block_actions", "user": {"id": "U1"}, "actions": []})
    assert it.action_id == ""
    assert it.value == {}


def test_dispatch_routes_on_action_id(monkeypatch):
    from services.slack_bot import interactivity as inter, blocks
    seen = []
    async def fake_stop(app, it): seen.append(("stop", it.action_id))
    monkeypatch.setattr(inter, "_on_stop", fake_stop)
    it = inter.parse_interaction(_block_actions_payload(blocks.ACTION_STOP, blocks.encode_value({})))
    asyncio.run(inter.dispatch_interaction(object(), it))
    assert seen == [("stop", blocks.ACTION_STOP)]


def test_dispatch_unknown_action_is_noop():
    from services.slack_bot import interactivity as inter
    it = inter.parse_interaction(_block_actions_payload("agnes_unknown", "{}"))
    asyncio.run(inter.dispatch_interaction(object(), it))  # no raise
```
- [ ] Run it, expect FAIL (`ModuleNotFoundError: services.slack_bot.interactivity`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "parse_interaction or dispatch_routes or unknown_action" -v`.
- [ ] Create `services/slack_bot/interactivity.py`:
```python
"""Slack interactivity (Block Kit button clicks) parsing + routing.

Signature verification lives in app/api/slack.py; by the time a payload
reaches parse_interaction it is trusted. Handlers deliver async via the
Slack Web API / response_url and never raise (each dispatch runs under
events._run_logged, so an exception becomes a logged best-effort failure,
never a Slack retry).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from services.slack_bot import blocks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Interaction:
    action_id: str
    slack_user_id: str
    channel_id: str
    response_url: str
    value: dict[str, Any] = field(default_factory=dict)


def parse_interaction(payload: dict[str, Any]) -> Interaction:
    """Normalize a Slack block_actions payload into an Interaction.

    Only the first clicked action is considered (Agnes never bundles two
    interactive elements into one block that fire together)."""
    actions = payload.get("actions") or []
    first = actions[0] if actions else {}
    return Interaction(
        action_id=first.get("action_id", ""),
        slack_user_id=(payload.get("user") or {}).get("id", ""),
        channel_id=(payload.get("channel") or {}).get("id", ""),
        response_url=payload.get("response_url", ""),
        value=blocks.decode_value(first.get("value", "")),
    )


async def dispatch_interaction(app, interaction: Interaction) -> None:
    if interaction.action_id == blocks.ACTION_STOP:
        await _on_stop(app, interaction)
    elif interaction.action_id == blocks.ACTION_SHARE_CHANNEL:
        await _on_share(app, interaction)
    elif interaction.action_id == blocks.ACTION_NEW_SESSION:
        await _on_new_session(app, interaction)
    else:
        # Link buttons (Continue-on-web) never POST; anything else is unknown.
        logger.info("ignoring unrouted interaction action_id=%s", interaction.action_id)


async def _on_stop(app, it: Interaction) -> None:  # filled in Task 4
    raise NotImplementedError


async def _on_share(app, it: Interaction) -> None:  # filled in Task 6
    raise NotImplementedError


async def _on_new_session(app, it: Interaction) -> None:  # filled in Task 7
    raise NotImplementedError
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "parse_interaction or dispatch_routes or unknown_action" -v`.
- [ ] Commit: `git add services/slack_bot/interactivity.py tests/test_slack_interactivity.py && git commit -m "slack: add interactivity parse + dispatch skeleton"`

---

## Task 4 — `_on_stop`: owner-gated cancel

**Files:**
- `services/slack_bot/interactivity.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4: resolve clicker→owner email; a non-owner in a shared thread → ephemeral "belongs to @X" and no cancel; otherwise `mgr.cancel(chat_id)` (idempotent). The sink strips the button itself on the `cancelled` frame (Task 8), so `_on_stop` only triggers cancel — it does not edit the message.

Identity resolution reuses `lookup_user_email(repo, slack_user_id)` (`services/slack_bot/binding.py`, exists on `main`). Ephemerals reuse `send_ephemeral(response_url, text, blocks=None)` (added Task 0). `mgr.cancel(chat_id)` exists (`app/chat/manager.py`).

> **Test-hygiene note:** `send_ephemeral` is `async`, so all `_on_*` tests patch it with an `async def` mock (never a sync `lambda`) — a sync stub would raise `TypeError` the moment a denial path awaits it.

- [ ] Write a failing test for `_on_stop`. Append to `tests/test_slack_interactivity.py`:
```python
def _stop_app(monkeypatch, *, bound_email):
    from types import SimpleNamespace
    import services.slack_bot.interactivity as inter_mod
    monkeypatch.setattr(inter_mod, "lookup_user_email", lambda repo, uid: bound_email)
    cancelled = []
    async def cancel(chat_id): cancelled.append(chat_id)
    mgr = SimpleNamespace(cancel=cancel, _cancelled=cancelled)
    repo = SimpleNamespace(_conn=object())
    app = SimpleNamespace(state=SimpleNamespace(chat_repo=repo, chat_manager=mgr))
    return app, mgr


def test_on_stop_owner_cancels(monkeypatch):
    from services.slack_bot import interactivity as inter
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter.sender, "send_ephemeral", fake_eph)
    app, mgr = _stop_app(monkeypatch, bound_email="a@example.com")
    it = inter.Interaction(action_id=inter.blocks.ACTION_STOP, slack_user_id="U1",
                           channel_id="D1", response_url="https://r",
                           value={"chat_id": "s1", "owner": "a@example.com"})
    asyncio.run(inter._on_stop(app, it))
    assert mgr._cancelled == ["s1"]
    assert eph == []


def test_on_stop_non_owner_denied(monkeypatch):
    from services.slack_bot import interactivity as inter
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter.sender, "send_ephemeral", fake_eph)
    app, mgr = _stop_app(monkeypatch, bound_email="intruder@example.com")
    it = inter.Interaction(action_id=inter.blocks.ACTION_STOP, slack_user_id="U2",
                           channel_id="C1", response_url="https://r",
                           value={"chat_id": "s1", "owner": "a@example.com"})
    asyncio.run(inter._on_stop(app, it))
    assert mgr._cancelled == []
    assert any("belongs to" in t for t in eph)


def test_on_stop_unbound_clicker_denied(monkeypatch):
    from services.slack_bot import interactivity as inter
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter.sender, "send_ephemeral", fake_eph)
    app, mgr = _stop_app(monkeypatch, bound_email=None)
    it = inter.Interaction(action_id=inter.blocks.ACTION_STOP, slack_user_id="U9",
                           channel_id="C1", response_url="https://r",
                           value={"chat_id": "s1", "owner": "a@example.com"})
    asyncio.run(inter._on_stop(app, it))
    assert mgr._cancelled == []
    assert eph  # some denial ephemeral
```
- [ ] Run it, expect FAIL (`NotImplementedError`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_stop" -v`.
- [ ] In `services/slack_bot/interactivity.py` add the imports near the top (after `from services.slack_bot import blocks`):
```python
from services.slack_bot import sender
from services.slack_bot.binding import lookup_user_email
```
- [ ] Replace the `_on_stop` stub body:
```python
async def _on_stop(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    chat_id = it.value.get("chat_id", "")
    owner = it.value.get("owner", "")
    if not clicker_email:
        await sender.send_ephemeral(
            it.response_url, "Bind your Slack identity first (DM Agnes to start)."
        )
        return
    if clicker_email != owner:
        await sender.send_ephemeral(
            it.response_url,
            f"This session belongs to <@{it.slack_user_id}>'s owner; only they can stop it.",
        )
        return
    await mgr.cancel(chat_id)  # idempotent; sink strips the button on `cancelled`
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_stop" -v`.
- [ ] Commit: `git add services/slack_bot/interactivity.py tests/test_slack_interactivity.py && git commit -m "slack: implement Stop button (owner-gated cancel)"`

---

## Task 5 — Share-answer token map (in-memory TTL)

**Files:**
- `services/slack_bot/interactivity.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4: a `/agnes` answer can exceed the 2000-char `value` cap, so Share buttons carry a token; the full body lives in a small in-memory TTL map. The token map lives on the `interactivity` module so the future `EphemeralCommandSink` producer (slash-commands phase) and the `_on_share` consumer share one source of truth.

> **Scope note (producer vs consumer).** This phase builds the Share **consumer** (`_on_share`, Task 6) and the token store/get (this task). The **producer** — attaching `share_to_channel_blocks` to an ephemeral `/agnes` answer and calling `store_share_answer` — belongs to `EphemeralCommandSink` in the slash-commands phase and is intentionally **not** wired here (the `/agnes` command and its ephemeral sink do not exist on `main`). The end-to-end Share click path therefore can only be exercised once that phase lands; this phase's tests drive `_on_share` directly with a manually-stored token.

- [ ] Write a failing test for the token map. Append to `tests/test_slack_interactivity.py`:
```python
def test_share_token_store_and_get(monkeypatch):
    from services.slack_bot import interactivity as inter
    inter._SHARE_ANSWERS.clear()
    tok = inter.store_share_answer("a long answer body")
    assert isinstance(tok, str) and tok
    assert inter.get_share_answer(tok) == "a long answer body"


def test_share_token_expires(monkeypatch):
    from services.slack_bot import interactivity as inter
    inter._SHARE_ANSWERS.clear()
    monkeypatch.setattr(inter, "_SHARE_TTL_SECONDS", -1)
    tok = inter.store_share_answer("body")
    assert inter.get_share_answer(tok) is None


def test_share_token_missing_returns_none():
    from services.slack_bot import interactivity as inter
    assert inter.get_share_answer("nope") is None
```
- [ ] Run it, expect FAIL (`AttributeError: _SHARE_ANSWERS`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "share_token" -v`.
- [ ] Add the token map to `services/slack_bot/interactivity.py` (after the imports, before the dataclass):
```python
import secrets
import time as _time

# Share-to-channel answer store. A /agnes answer can exceed the 2000-char
# Slack button `value` cap, so only a token rides in the button; the body
# lives here keyed by token with a short TTL. In-memory + single-worker
# (chat is disabled under multiple uvicorn workers — see app/main.py).
_SHARE_TTL_SECONDS = 30 * 60
_SHARE_ANSWERS: dict[str, tuple[float, str]] = {}


def store_share_answer(text: str) -> str:
    """Stash a shareable answer body, returning its lookup token."""
    token = secrets.token_urlsafe(12)
    _SHARE_ANSWERS[token] = (_time.monotonic(), text)
    return token


def get_share_answer(token: str) -> str | None:
    """Return the stored body, or None if missing/expired (and evict it)."""
    entry = _SHARE_ANSWERS.get(token)
    if entry is None:
        return None
    stored_at, text = entry
    if (_time.monotonic() - stored_at) > _SHARE_TTL_SECONDS:
        _SHARE_ANSWERS.pop(token, None)
        return None
    return text
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "share_token" -v`.
- [ ] Commit: `git add services/slack_bot/interactivity.py tests/test_slack_interactivity.py && git commit -m "slack: add share-answer TTL token map"`

---

## Task 6 — `_on_share`: click-time allowlist re-check + public post + audit

**Files:**
- `services/slack_bot/interactivity.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4 (security-critical):
1. resolve clicker→email; unbound → deny (ephemeral, no post).
2. **re-resolve the allowlist at click time** using `is_channel_allowlisted(conn, channel_id)` against the signature-verified payload's `channel_id` (from Task 0; direct `Everyone`-scoped grant lookup, **not** `can_access`, so no admin short-circuit). Never trust a payload channel for posting. Not allowlisted → ephemeral "not here", no post.
3. public `post_channel_message`.
4. clear the ephemeral via `respond_via_response_url` (`{"delete_original": True}`); on a `response_url` failure, the public post already happened — log only.
5. `write_audit(conn, user_email=clicker, action="slack_share", details={...})` (signature confirmed in `app/chat/audit.py`).

- [ ] Write failing tests for `_on_share`. Append to `tests/test_slack_interactivity.py`:
```python
def _share_app(monkeypatch, *, bound_email, allowlisted, body="shared body"):
    from types import SimpleNamespace
    import services.slack_bot.interactivity as inter_mod
    monkeypatch.setattr(inter_mod, "lookup_user_email", lambda repo, uid: bound_email)
    monkeypatch.setattr(inter_mod, "is_channel_allowlisted", lambda conn, ch: allowlisted)
    audits = []
    monkeypatch.setattr(inter_mod, "write_audit", lambda conn, **kw: audits.append(kw))
    posts, clears = [], []
    async def fake_post(ch, text): posts.append((ch, text))
    async def fake_resp(url, b): clears.append((url, b))
    monkeypatch.setattr(inter_mod.sender, "post_channel_message", fake_post)
    monkeypatch.setattr(inter_mod.sender, "respond_via_response_url", fake_resp)
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter_mod.sender, "send_ephemeral", fake_eph)
    inter_mod._SHARE_ANSWERS.clear()
    tok = inter_mod.store_share_answer(body)
    repo = SimpleNamespace(_conn=object())
    app = SimpleNamespace(state=SimpleNamespace(chat_repo=repo))
    return app, tok, SimpleNamespace(posts=posts, clears=clears, audits=audits, eph=eph)


def test_on_share_allowlisted_posts_clears_and_audits(monkeypatch):
    from services.slack_bot import interactivity as inter
    app, tok, rec = _share_app(monkeypatch, bound_email="a@example.com", allowlisted=True)
    it = inter.Interaction(action_id=inter.blocks.ACTION_SHARE_CHANNEL, slack_user_id="U1",
                           channel_id="C1", response_url="https://r",
                           value={"channel_id": "C1", "token": tok})
    asyncio.run(inter._on_share(app, it))
    assert rec.posts == [("C1", "shared body")]
    assert rec.clears and rec.clears[0][1].get("delete_original") is True
    assert len(rec.audits) == 1 and rec.audits[0]["action"] == "slack_share"
    assert rec.audits[0]["user_email"] == "a@example.com"


def test_on_share_not_allowlisted_never_posts(monkeypatch):
    from services.slack_bot import interactivity as inter
    app, tok, rec = _share_app(monkeypatch, bound_email="a@example.com", allowlisted=False)
    it = inter.Interaction(action_id=inter.blocks.ACTION_SHARE_CHANNEL, slack_user_id="U1",
                           channel_id="C1", response_url="https://r",
                           value={"channel_id": "C1", "token": tok})
    asyncio.run(inter._on_share(app, it))
    assert rec.posts == []
    assert rec.audits == []
    assert rec.eph  # denial ephemeral


def test_on_share_unbound_never_posts(monkeypatch):
    from services.slack_bot import interactivity as inter
    app, tok, rec = _share_app(monkeypatch, bound_email=None, allowlisted=True)
    it = inter.Interaction(action_id=inter.blocks.ACTION_SHARE_CHANNEL, slack_user_id="U9",
                           channel_id="C1", response_url="https://r",
                           value={"channel_id": "C1", "token": tok})
    asyncio.run(inter._on_share(app, it))
    assert rec.posts == [] and rec.audits == [] and rec.eph


def test_on_share_expired_token_no_post(monkeypatch):
    from services.slack_bot import interactivity as inter
    app, _tok, rec = _share_app(monkeypatch, bound_email="a@example.com", allowlisted=True)
    it = inter.Interaction(action_id=inter.blocks.ACTION_SHARE_CHANNEL, slack_user_id="U1",
                           channel_id="C1", response_url="https://r",
                           value={"channel_id": "C1", "token": "missing"})
    asyncio.run(inter._on_share(app, it))
    assert rec.posts == [] and rec.audits == []
    assert rec.eph  # "expired" ephemeral
```
- [ ] Run it, expect FAIL (`NotImplementedError`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_share" -v`.
- [ ] Add the remaining imports to `services/slack_bot/interactivity.py` (next to the others):
```python
from app.chat.audit import write_audit
from services.slack_bot.binding import is_channel_allowlisted
```
- [ ] Replace the `_on_share` stub body:
```python
async def _on_share(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    conn = repo._conn
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    if not clicker_email:
        await sender.send_ephemeral(
            it.response_url, "Bind your Slack identity first (DM Agnes to start)."
        )
        return
    channel_id = it.value.get("channel_id", "")
    # SECURITY: re-check the allowlist at click time against the
    # signature-verified channel — never trust a stale grant or the payload's
    # display channel. is_channel_allowlisted does a direct Everyone-scoped
    # grant lookup (no admin short-circuit).
    if not is_channel_allowlisted(conn, channel_id):
        await sender.send_ephemeral(it.response_url, "Agnes can't post in this channel.")
        return
    body = get_share_answer(it.value.get("token", ""))
    if body is None:
        await sender.send_ephemeral(
            it.response_url, "That answer expired — re-run /agnes to share again."
        )
        return
    await sender.post_channel_message(channel_id, body)
    # Clear the ephemeral; the public post already landed, so a response_url
    # expiry here is non-fatal.
    try:
        await sender.respond_via_response_url(it.response_url, {"delete_original": True})
    except Exception:
        logger.warning("response_url clear failed after share (post already public)")
    write_audit(
        conn, user_email=clicker_email, action="slack_share",
        details={"channel_id": channel_id},
    )
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_share" -v`.
- [ ] Commit: `git add services/slack_bot/interactivity.py tests/test_slack_interactivity.py && git commit -m "slack: implement Share-to-channel with click-time allowlist re-check + audit"`

---

## Task 7 — `_on_new_session`: owner-gated soft-archive

**Files:**
- `services/slack_bot/interactivity.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4: owner-gated; routes through the **shared** `_soft_archive_dm(app, owner_email, channel_id)` helper (built in Task 0 in `services/slack_bot/commands.py`) so New-session and `/agnes-new` are one code path. Resolve clicker→email, require it equals the `owner` in the button value, then call the helper and confirm via ephemeral.

- [ ] Write failing tests for `_on_new_session`. Append to `tests/test_slack_interactivity.py`:
```python
def test_on_new_session_owner_archives(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import interactivity as inter
    monkeypatch.setattr(inter, "lookup_user_email", lambda repo, uid: "a@example.com")
    archived = []
    async def fake_archive(app, owner, ch): archived.append((owner, ch))
    monkeypatch.setattr(inter, "_soft_archive_dm", fake_archive)
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter.sender, "send_ephemeral", fake_eph)
    app = SimpleNamespace(state=SimpleNamespace(chat_repo=SimpleNamespace(_conn=object())))
    it = inter.Interaction(action_id=inter.blocks.ACTION_NEW_SESSION, slack_user_id="U1",
                           channel_id="D1", response_url="https://r",
                           value={"channel_id": "D1", "owner": "a@example.com"})
    asyncio.run(inter._on_new_session(app, it))
    assert archived == [("a@example.com", "D1")]
    assert eph  # confirmation ephemeral


def test_on_new_session_non_owner_denied(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import interactivity as inter
    monkeypatch.setattr(inter, "lookup_user_email", lambda repo, uid: "intruder@example.com")
    archived = []
    async def fake_archive(app, owner, ch): archived.append((owner, ch))
    monkeypatch.setattr(inter, "_soft_archive_dm", fake_archive)
    eph = []
    async def fake_eph(url, text, blocks=None): eph.append(text)
    monkeypatch.setattr(inter.sender, "send_ephemeral", fake_eph)
    app = SimpleNamespace(state=SimpleNamespace(chat_repo=SimpleNamespace(_conn=object())))
    it = inter.Interaction(action_id=inter.blocks.ACTION_NEW_SESSION, slack_user_id="U2",
                           channel_id="D1", response_url="https://r",
                           value={"channel_id": "D1", "owner": "a@example.com"})
    asyncio.run(inter._on_new_session(app, it))
    assert archived == []
    assert any("belongs to" in t or "only" in t.lower() for t in eph)
```
- [ ] Run it, expect FAIL (`NotImplementedError`): `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_new_session" -v`.
- [ ] Add the import to `services/slack_bot/interactivity.py`:
```python
from services.slack_bot.commands import _soft_archive_dm
```
- [ ] Replace the `_on_new_session` stub body:
```python
async def _on_new_session(app, it: Interaction) -> None:
    repo = app.state.chat_repo
    clicker_email = lookup_user_email(repo, it.slack_user_id)
    owner = it.value.get("owner", "")
    channel_id = it.value.get("channel_id", "")
    if not clicker_email or clicker_email != owner:
        await sender.send_ephemeral(
            it.response_url, "This session belongs to someone else; only its owner can reset it."
        )
        return
    await _soft_archive_dm(app, owner, channel_id)
    await sender.send_ephemeral(
        it.response_url, "Started a fresh session — your next message begins anew."
    )
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "on_new_session" -v`.
- [ ] Commit: `git add services/slack_bot/interactivity.py tests/test_slack_interactivity.py && git commit -m "slack: implement New-session button (owner-gated soft-archive)"`

---

## Task 8 — `SlackSinkBridge`: emit buttons on DM replies + Stop-button lifecycle

**Files:**
- `services/slack_bot/sink.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4: `SlackSinkBridge` gains `chat_id`, `owner`, `web_base`. On the first `assistant_message`-bearing post of a turn it posts via `post_thread_reply_with_blocks` with **three** kinds of blocks appended onto the bot reply (this is the producer the spec requires — "everywhere a bot reply appears"):
1. `stop_button_blocks(...)` — the Stop button + the reply section.
2. `continue_on_web_block(...)` — the deep-link button (omitted when no `web_base`).
3. `new_session_block(...)` — the New-session button (DM only).

It stores the returned `ts`; on `cancelled` / `error` / **`done`** (the runner's turn-end frame, `app/chat/runner.py`), `update_message(channel, ts, final_text, blocks=[])` strips all buttons. `cancel()` is idempotent so Stop is always safe.

To keep the existing Phase-0 `SlackSinkBridge(channel=..., thread_ts=...)` call site and tests green, `chat_id` / `owner` / `web_base` are **keyword args with defaults**; when `chat_id` is empty the bridge falls back to the plain `send_thread_reply` behavior (no buttons).

- [ ] Write failing tests for button emission + both strip paths (cancelled **and** happy-path done). Append to `tests/test_slack_interactivity.py`:
```python
def test_sink_emits_all_buttons_then_strips_on_cancelled(monkeypatch):
    from services.slack_bot import sink as sink_mod, blocks

    posts, updates = [], []
    async def fake_post_blocks(ch, ts, text, bs):
        posts.append((ch, ts, text, bs))
        return "msg-1"
    async def fake_update(ch, ts, text, bs):
        updates.append((ch, ts, text, bs))
    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_post_blocks)
    monkeypatch.setattr(sink_mod, "update_message", fake_update)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(
            channel="D1", thread_ts="1.1", chat_id="s1",
            owner="a@example.com", web_base="https://host.example",
        )
        await bridge.send_json({"type": "assistant_message", "content": "thinking..."})
        await bridge.send_json({"type": "cancelled"})
        await bridge.close()

    asyncio.run(_run())
    assert len(posts) == 1
    emitted = posts[0][3]
    action_ids = [
        e["action_id"]
        for b in emitted if b["type"] == "actions"
        for e in b["elements"] if "action_id" in e
    ]
    # Stop + New-session carry callbacks; Continue-on-web is a link (no action_id).
    assert blocks.ACTION_STOP in action_ids
    assert blocks.ACTION_NEW_SESSION in action_ids
    has_link = any(
        e.get("url", "").endswith("/chat?session=s1")
        for b in emitted if b["type"] == "actions" for e in b["elements"]
    )
    assert has_link
    # cancelled stripped the buttons via chat.update (blocks == []).
    assert updates and updates[-1][1] == "msg-1" and updates[-1][3] == []


def test_sink_strips_buttons_on_done(monkeypatch):
    """Happy path: turn-end `done` frame strips the Stop button (primary
    spec requirement 'removes it at turn end'). runner.py emits {'type':'done'}."""
    from services.slack_bot import sink as sink_mod

    updates = []
    async def fake_post_blocks(ch, ts, text, bs):
        return "msg-1"
    async def fake_update(ch, ts, text, bs):
        updates.append((ch, ts, text, bs))
    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_post_blocks)
    monkeypatch.setattr(sink_mod, "update_message", fake_update)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(
            channel="D1", thread_ts="1.1", chat_id="s1", owner="a@example.com", web_base="",
        )
        await bridge.send_json({"type": "assistant_message", "content": "answer"})
        await bridge.send_json({"type": "done"})
        await bridge.close()

    asyncio.run(_run())
    assert updates == [("D1", "msg-1", "answer", [])]


def test_sink_without_chat_id_keeps_plain_behavior(monkeypatch):
    """Back-compat: no chat_id → plain send_thread_reply, no blocks."""
    from services.slack_bot import sink as sink_mod
    sent = []
    async def fake_send(ch, ts, text): sent.append((ch, ts, text))
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "assistant_message", "content": "hello"})
        await bridge.close()

    asyncio.run(_run())
    assert sent == [("D1", "1.1", "hello")]
```
- [ ] Run it, expect FAIL (`TypeError: unexpected keyword 'chat_id'` then assertion fail): `.venv/bin/pytest tests/test_slack_interactivity.py -k "sink_emits or strips_buttons_on_done or plain_behavior" -v`.
- [ ] Edit `services/slack_bot/sink.py` — replace the import line `from services.slack_bot.sender import send_thread_reply` with:
```python
from services.slack_bot.blocks import (
    continue_on_web_block,
    new_session_block,
    stop_button_blocks,
)
from services.slack_bot.sender import (
    post_thread_reply_with_blocks,
    send_thread_reply,
    update_message,
)
```
- [ ] Replace `SlackSinkBridge.__init__`:
```python
    def __init__(
        self,
        *,
        channel: str,
        thread_ts: str,
        chat_id: str = "",
        owner: str = "",
        web_base: str = "",
    ) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._chat_id = chat_id
        self._owner = owner
        self._web_base = web_base
        self._closed = asyncio.Event()
        # ts of the current turn's button-bearing post, if any. Set on the
        # first assistant_message of a turn; cleared when the button is
        # stripped on cancelled / error / done (turn end).
        self._stop_msg_ts: str | None = None
        self._stop_msg_text: str = ""
```
- [ ] Replace `SlackSinkBridge.send_json` (and add `_turn_blocks` + `_strip_stop_button`):
```python
    def _turn_blocks(self, content: str) -> list[dict]:
        """Reply section + Stop + Continue-on-web (if web_base) + New-session.

        This is the producer that emits the interactive buttons onto every
        DM bot reply (spec §4 "everywhere a bot reply appears")."""
        blocks = stop_button_blocks(text=content, chat_id=self._chat_id, owner=self._owner)
        link = continue_on_web_block(web_base=self._web_base, chat_id=self._chat_id)
        if link is not None:
            blocks.append(link)
        blocks.append(new_session_block(channel_id=self._channel, owner=self._owner))
        return blocks

    async def send_json(self, data: dict) -> None:
        t = data.get("type")
        if t == "assistant_message":
            content = data.get("content", "")
            if not content:
                return
            # With a chat_id we emit the interactive buttons on the streaming
            # reply and strip the Stop button at turn end. Without one, keep
            # the plain path (back-compat for callers that don't wire buttons).
            if self._chat_id and self._stop_msg_ts is None:
                ts = await post_thread_reply_with_blocks(
                    self._channel, self._thread_ts, content, self._turn_blocks(content),
                )
                self._stop_msg_ts = ts
                self._stop_msg_text = content
            else:
                await send_thread_reply(self._channel, self._thread_ts, content)
        elif t == "error":
            kind = data.get("kind", "")
            msg = data.get("message", "")
            await send_thread_reply(
                self._channel, self._thread_ts, f":warning: {kind}: {msg}".strip(": ")
            )
            await self._strip_stop_button()
        elif t == "cancelled":
            await send_thread_reply(self._channel, self._thread_ts, "_(stopped)_")
            await self._strip_stop_button()
        elif t == "done":
            await self._strip_stop_button()
        # ready, runner_ready, token, tool_call, tool_result: silently ignored

    async def _strip_stop_button(self) -> None:
        """Edit the turn's button-bearing post to remove the Stop button.

        Idempotent: a no-op once already stripped or if no button was posted.
        """
        if self._stop_msg_ts is None:
            return
        ts, text = self._stop_msg_ts, self._stop_msg_text
        self._stop_msg_ts = None
        self._stop_msg_text = ""
        await update_message(self._channel, ts, text, [])
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "sink_emits or strips_buttons_on_done or plain_behavior" -v`.
- [ ] Run the existing Phase-0 sink suite to confirm back-compat (the `done` frame was previously ignored; the plain-path tests construct without `chat_id` so they keep ignoring it): `.venv/bin/pytest tests/test_slack_bot.py -k "sink" -v`.
- [ ] Commit: `git add services/slack_bot/sink.py tests/test_slack_interactivity.py && git commit -m "slack: emit interactive buttons on DM replies + Stop-button lifecycle"`

---

## Task 9 — `POST /api/slack/interactivity` endpoint (verify + ack-then-async)

**Files:**
- `app/api/slack.py` (Modify)
- `tests/test_slack_interactivity.py` (Test)

Per spec §4: verify HMAC on the **raw body** (Slack signs the urlencoded bytes), parse the form, `_schedule(_run_logged(dispatch_interaction(...)))`, return an empty 200. A bad/missing signature returns 401 before any work (a forged click never runs). `_schedule` + `_run_logged` are the Task-0 helpers in `services/slack_bot/events.py`. `verify_slack_signature(secret, ts, sig, body)` is the existing helper in `services/slack_bot/sigverify.py` (already imported in `app/api/slack.py`).

> **Test-hygiene note:** the `_schedule`-patch test below must drain the **inner** `dispatch_interaction` coroutine as well as the outer `_run_logged` wrapper, or the inner coroutine leaks a `RuntimeWarning: coroutine was never awaited`. The patch runs `_run_logged` to completion (which awaits the inner coroutine) inside a fresh event loop rather than `.close()`-ing the outer wrapper.

- [ ] Write a failing test for the endpoint. Append to `tests/test_slack_interactivity.py`:
```python
def _signed_form_request(monkeypatch, payload_json, *, good_sig=True):
    """Build a signed urlencoded interactivity (body, ts, sig)."""
    import hashlib, hmac, time
    from urllib.parse import urlencode
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    body = urlencode({"payload": payload_json}).encode()
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(b"shh", base, hashlib.sha256).hexdigest()
    if not good_sig:
        sig = "v0=deadbeef"
    return body, ts, sig


def test_interactivity_endpoint_bad_signature_401(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.slack import router
    app = FastAPI()
    app.include_router(router)
    body, ts, sig = _signed_form_request(monkeypatch, "{}", good_sig=False)
    client = TestClient(app)
    r = client.post("/api/slack/interactivity", content=body,
                    headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                             "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 401


def test_interactivity_endpoint_acks_and_schedules(monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.api.slack as slack_api

    scheduled = []
    # Drain the scheduled wrapper coroutine to completion in a throwaway loop
    # so neither the outer _run_logged wrapper nor the inner dispatch coroutine
    # leaks a 'coroutine was never awaited' RuntimeWarning.
    def _drain(coro):
        scheduled.append(coro)
        asyncio.new_event_loop().run_until_complete(coro)
    monkeypatch.setattr(slack_api, "_schedule", _drain)
    dispatched = []
    async def fake_dispatch(app, interaction):
        dispatched.append(interaction.action_id)
    monkeypatch.setattr(slack_api, "dispatch_interaction", fake_dispatch)

    app = FastAPI()
    app.include_router(slack_api.router)
    from services.slack_bot import blocks
    payload = json.dumps({
        "type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
        "response_url": "https://r",
        "actions": [{"action_id": blocks.ACTION_STOP, "value": blocks.encode_value({"chat_id": "s1"})}],
    })
    body, ts, sig = _signed_form_request(monkeypatch, payload, good_sig=True)
    r = TestClient(app).post("/api/slack/interactivity", content=body,
                             headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                                      "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    assert r.content in (b"", b"null")  # empty 200 ack
    assert len(scheduled) == 1          # exactly one dispatch scheduled
    assert dispatched == [blocks.ACTION_STOP]  # inner coroutine ran to completion
```
- [ ] Run it, expect FAIL (404 — route missing): `.venv/bin/pytest tests/test_slack_interactivity.py -k "interactivity_endpoint" -v`.
- [ ] Edit `app/api/slack.py` — extend imports (the events helpers were already imported in Task 0; add the JSON/form + interactivity ones):
```python
import json
from urllib.parse import parse_qs

from fastapi import Response

from services.slack_bot.interactivity import dispatch_interaction, parse_interaction
```
- [ ] Add the endpoint after `slack_events`:
```python
@router.post("/interactivity")
async def slack_interactivity(request: Request):
    body = await request.body()                       # raw bytes — Slack signs these
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    interaction = parse_interaction(json.loads(form["payload"]))
    _schedule(_run_logged(dispatch_interaction(request.app, interaction)))
    return Response(status_code=200)                  # empty 200 ack; message unchanged
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_interactivity.py -k "interactivity_endpoint" -v`.
- [ ] Commit: `git add app/api/slack.py tests/test_slack_interactivity.py && git commit -m "slack: add /api/slack/interactivity endpoint (verify + ack-then-async)"`

---

## Task 10 — Stop integration test + 3s-ack regression test

**Files:**
- `tests/test_slack_interactivity.py` (Test)

Two end-to-end-ish locks. First, a Stop **integration** test that joins the two halves the unit tests mock apart — `_on_stop` → a real `ChatManager`-shaped cancel → the sink's `cancelled`-driven strip — so the contract between handler and sink (the `cancelled` frame strips the button) is exercised together, not just in isolation. Second, the 3s-ack regression: a slow handler must not block the HTTP response.

- [ ] Write the Stop integration test. Append to `tests/test_slack_interactivity.py`:
```python
def test_stop_then_cancelled_strips_button_integration(monkeypatch):
    """Join _on_stop and the sink: owner clicks Stop → cancel() broadcasts a
    `cancelled` frame to the sink → sink strips the Stop button. Drives both
    halves through one fake live session rather than mocking each in isolation."""
    from types import SimpleNamespace
    from services.slack_bot import interactivity as inter, sink as sink_mod

    # --- sink half: capture the button post + the strip update ---
    updates = []
    async def fake_post_blocks(ch, ts, text, bs):
        return "msg-1"
    async def fake_update(ch, ts, text, bs):
        updates.append((ch, ts, text, bs))
    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_post_blocks)
    monkeypatch.setattr(sink_mod, "update_message", fake_update)
    async def fake_send_reply(ch, ts, text):  # for the "_(stopped)_" line
        pass
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send_reply)

    # --- manager half: cancel() forwards a `cancelled` frame to the bound sink ---
    class FakeManager:
        def __init__(self): self.sink = None
        async def cancel(self, chat_id):
            await self.sink.send_json({"type": "cancelled"})

    monkeypatch.setattr(inter, "lookup_user_email", lambda repo, uid: "a@example.com")
    mgr = FakeManager()
    app = SimpleNamespace(state=SimpleNamespace(
        chat_repo=SimpleNamespace(_conn=object()), chat_manager=mgr))

    async def _run():
        bridge = sink_mod.SlackSinkBridge(
            channel="D1", thread_ts="1.1", chat_id="s1", owner="a@example.com", web_base="",
        )
        mgr.sink = bridge
        # post a turn so there's a button to strip
        await bridge.send_json({"type": "assistant_message", "content": "working"})
        it = inter.Interaction(action_id=inter.blocks.ACTION_STOP, slack_user_id="U1",
                               channel_id="D1", response_url="https://r",
                               value={"chat_id": "s1", "owner": "a@example.com"})
        await inter._on_stop(app, it)

    asyncio.run(_run())
    # cancel() -> cancelled frame -> button stripped (blocks == [])
    assert updates == [("D1", "msg-1", "working", [])]


def test_interactivity_endpoint_does_not_block_on_slow_handler(monkeypatch):
    import json, time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.api.slack as slack_api

    async def slow_dispatch(app, interaction):
        await asyncio.sleep(5)  # > Slack's 3s budget
    # Use the real _schedule (create_task) so the slow handler runs detached;
    # the test asserts the response returns long before the 5s sleep completes.
    monkeypatch.setattr(slack_api, "dispatch_interaction", slow_dispatch)

    app = FastAPI()
    app.include_router(slack_api.router)
    from services.slack_bot import blocks
    payload = json.dumps({
        "type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
        "response_url": "https://r",
        "actions": [{"action_id": blocks.ACTION_STOP, "value": "{}"}],
    })
    body, ts, sig = _signed_form_request(monkeypatch, payload, good_sig=True)
    t0 = time.monotonic()
    r = TestClient(app).post("/api/slack/interactivity", content=body,
                             headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                                      "Content-Type": "application/x-www-form-urlencoded"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 3.0, f"endpoint blocked {elapsed:.2f}s on slow handler"
```
- [ ] Run them, expect PASS (against the Task-8 sink + Task-9 ack-then-async endpoint): `.venv/bin/pytest tests/test_slack_interactivity.py -k "stop_then_cancelled or does_not_block" -v`.
- [ ] Commit: `git add tests/test_slack_interactivity.py && git commit -m "slack: Stop integration test + 3s-ack regression test"`

---

## Task 11 — Manifest: enable interactivity + Request URL + documented stanzas

**Files:**
- `services/slack_bot/manifest.yaml` (Modify)
- `docs/cloud-chat.md` (Modify)

Per spec §4 + §1: interactions arrive at a separate Request URL. Flip the manifest's interactivity block on and add the URL. Keep the vendor-agnostic `YOUR-AGNES-HOST` placeholder (matching the file's existing convention). The spec's two documented HTTP/Socket stanzas do not exist on `main` (a Phase-0 deliverable), so this task **creates** them in `docs/cloud-chat.md` rather than editing nonexistent ones.

- [ ] Edit `services/slack_bot/manifest.yaml` — replace:
```yaml
  interactivity:
    is_enabled: false
```
with:
```yaml
  interactivity:
    is_enabled: true
    request_url: "https://YOUR-AGNES-HOST/api/slack/interactivity"
```
- [ ] Read `docs/cloud-chat.md` around the manifest reference (line ~67) to find the insertion point: `.venv/bin/pytest --collect-only -q >/dev/null 2>&1; grep -n "manifest" docs/cloud-chat.md` (use Read on the surrounding lines before editing).
- [ ] In `docs/cloud-chat.md`, append a new subsection documenting the two transport stanzas (HTTP default vs Socket Mode). Add this Markdown block:
```markdown
### Manifest stanzas: HTTP vs Socket Mode

Two transports, two manifest shapes. Pick the one matching your
`chat.slack.transport` setting. Replace `<your-host>` with your public
Agnes hostname.

**HTTP (default).** Slack delivers events, slash commands and interactivity
over HTTPS to your public endpoints:

```yaml
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
    request_url: "https://<your-host>/api/slack/interactivity"
  socket_mode_enabled: false
```

**Socket Mode (optional).** All three event classes arrive over one
WebSocket; no public `request_url` is needed — interactivity routes through
the same `dispatch_interaction`:

```yaml
settings:
  event_subscriptions:
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```
```
- [ ] Verify no customer-specific tokens slipped in: `git diff services/slack_bot/manifest.yaml docs/cloud-chat.md | grep -iE "keboola|groupon|[0-9]{1,3}(\.[0-9]{1,3}){3}" || echo CLEAN`
- [ ] Commit: `git add services/slack_bot/manifest.yaml docs/cloud-chat.md && git commit -m "slack: enable interactivity in manifest + document HTTP/Socket stanzas"`

---

## Task 12 — Full-suite gate + CHANGELOG bullet

**Files:**
- `CHANGELOG.md` (Modify)

- [ ] Run the full suite (this is what CI runs): `.venv/bin/pytest tests/ --tb=short -n auto -q`. All green for code you touched. If an unrelated failure appears, confirm it reproduces on a clean branch (`git stash`) and note it; do not block on pre-existing failures.
- [ ] Read the top of `CHANGELOG.md` to find the `## [Unreleased]` header before editing.
- [ ] Add a bullet under `## [Unreleased]` in `CHANGELOG.md` (under `### Added`; create the subsection if absent):
```markdown
### Added
- **Slack Block Kit interactivity.** Bot DM replies now carry interactive
  buttons, delivered via a new signature-verified `POST /api/slack/interactivity`
  endpoint (ack-then-async, empty 200): **Stop** (owner-gated, cancels the live
  turn), **Continue on web** (deep link to `/chat?session=<id>`), and **New
  session** (owner-gated soft-archive, shared path with `/agnes-new`). The Stop
  button is posted on the first assistant turn and stripped when the turn ends
  (`done`), errors, or is cancelled. A **Share to channel** consumer is also
  added (promotes an answer to a public in-thread post — allowlist re-checked at
  click time, audited as `slack_share`); its producer attaches with the slash
  `/agnes` ephemeral surface in a later change. New leaf `blocks.py` builders +
  `interactivity.py` parser/router; `sender.py` gains block/update/channel/
  ephemeral/`response_url` primitives; `binding.py` gains a per-channel
  allowlist check; the Slack events webhook now acks-then-processes. Manifest
  enables interactivity and documents HTTP vs Socket Mode stanzas.
```
- [ ] Verify the CHANGELOG entry is vendor-agnostic (no real hosts/IDs): `git diff CHANGELOG.md | grep -iE "keboola|groupon|[0-9]{1,3}(\.[0-9]{1,3}){3}" || echo CLEAN` (placeholders like `<your-host>` / `YOUR-AGNES-HOST` are fine).
- [ ] Commit: `git add CHANGELOG.md && git commit -m "docs: changelog for Slack Block Kit interactivity"`

---

## Coverage check against spec §4 / §8

- **Stop button** (owner-gated cancel, idempotent, sink strips button) → Tasks 4, 8; joined end-to-end in Task 10 (`test_stop_then_cancelled_strips_button_integration`).
- **Stop strip at turn end** (the primary "removes it at turn end" requirement) → Task 8 `test_sink_strips_buttons_on_done` exercises the happy-path `done` frame (the runner emits `{"type":"done"}`, `app/chat/runner.py`); `cancelled` and `error` strips also covered.
- **Continue-on-web** (pure link, no callback, deep link) → builder Task 1; **emitted onto every DM reply** by the sink in Task 8 (`test_sink_emits_all_buttons_then_strips_on_cancelled` asserts the `/chat?session=s1` link is present); dispatcher ignores the (never-POSTed) link in Task 3.
- **New-session** (owner-gated, shared `_soft_archive_dm`) → handler Task 7; helper Task 0; **emitted onto every DM reply** by the sink in Task 8 (asserts `ACTION_NEW_SESSION` present).
- **Share-to-channel** (click-time `is_channel_allowlisted` re-check, public post, ephemeral clear, audit, >2000-char tokenized via TTL map) → consumer Tasks 5, 6. **Producer scope note:** attaching `share_to_channel_blocks` to a `/agnes` answer lives with `EphemeralCommandSink` in the slash-commands phase (does not exist on `main`); this phase builds and tests the consumer + token store, and Task 5 documents the deferral explicitly.
- **Endpoint:** raw-body verify, `parse_qs`, `payload` JSON, `_schedule`/`_run_logged`, empty 200 → Task 9; ack-then-async helpers themselves → Task 0.
- **Error handling:** 401 before any work (Task 9), empty 200 so no Slack retry (Tasks 9/10), each dispatch wrapped via `_run_logged` (Tasks 0/9, `test_run_logged_swallows_exceptions`), out-of-allowlist Share never posts publicly (Task 6), `response_url` expiry tolerated after a public share (Task 6).
- **§8 unit-test list:** `blocks.py` exact JSON (Task 1), parse/dispatch routing (Task 3), sig-verify bad→401 no handler (Task 9), Stop non-owner→ephemeral no cancel (Task 4), Share non-allowlisted→no public post (Task 6), Share allowlisted→one post + ephemeral clear + one audit row (Task 6), sink Stop-button lifecycle incl. turn-end (Task 8), 3s-ack regression (Task 10).

**Dual-backend / RBAC / migration scope:** this phase adds **no** `app/chat/persistence.py` method, **no** `*_pg.py` sibling, **no** schema change, and **no** new `ResourceType` (`SLACK_CHANNEL` is owned by the mentions phase; `is_channel_allowlisted` here reads the `resource_grants` table directly and does not introduce the enum/spec). It reuses the existing `write_audit` (audit_log) and `ChatManager.cancel`. The endpoint is **signature-gated at the transport boundary** rather than via `require_admin`/`require_resource_access`, matching the existing `/api/slack/events` pattern — Block Kit clicks are not authenticated FastAPI requests, so authorization is enforced **inside each handler** (binding lookup + owner check + click-time `is_channel_allowlisted`). No DuckDB↔PG contract test is therefore required for Phase 3.
