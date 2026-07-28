# Phase 2 — Slash Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add Slack slash commands `/agnes <q>`, `/agnes-new`, `/agnes-status`, and `/agnes help` behind a signature-verified `POST /api/slack/commands` endpoint that acks within 3 s and delivers results asynchronously via Slack `response_url`.

**Architecture:** Slash commands arrive as `x-www-form-urlencoded` POSTs to a dedicated Request URL (separate from the Events endpoint) with payload fields `command`, `text`, `user_id`, `channel_id`, `response_url`. The HTTP endpoint verifies the HMAC on the raw body, parses the form, schedules `dispatch_command` via `create_task` (ack-then-async), and returns a ≤3 s ack body. `dispatch_command` routes on `cmd["command"]` to one coroutine per command; each delivers its answer ephemerally through the caller's `response_url`. `/agnes` runs on the invoker's *persistent* DM session (resolved via `conversations.open` so the session keys on the user's IM channel, not the source public channel) so the same answer also surfaces on web `/chat`; a transient `EphemeralCommandSink` posts the next assistant turn ephemerally then closes.

**Tech Stack:** Python 3, FastAPI, `httpx` (async Slack Web API calls), DuckDB-backed `ChatRepository`, `ChatManager`, `pytest` (`-n auto`).

---

## Assumptions / dependencies on earlier phases

Phase 0 (Transport + ack fix) is *intended* to add to `services/slack_bot/events.py`:
- a module-level detached-task set + `_schedule(coro)` helper that does `task = asyncio.create_task(coro); _BG_TASKS.add(task); task.add_done_callback(_BG_TASKS.discard)`;
- `_run_logged(coro)` — wraps a coroutine in try/except, logs unhandled exceptions, posts a best-effort ephemeral on failure.

**Verified ground truth at plan-authoring time:** `grep -n "_run_logged\|def _schedule" services/slack_bot/events.py` returns *nothing* — only `_handle_dm` exists in that module. This phase therefore **does not depend on Phase 0** and defines its own `_schedule` + `_run_logged` locally in `commands.py` (Task 4) and wires them in `app/api/slack.py` (Task 5). If Phase 0 later lands its own copies in `events.py`, the local ones in `commands.py` remain correct and independent (they are module-private; no import collision). Do not import from `events.py`.

The existing `verify_slack_signature(signing_secret, timestamp, signature, body) -> bool` lives in `services/slack_bot/sigverify.py` and is reused unchanged. The mention/allowlist work (Phase 1) is **not** a dependency of this phase.

---

## File Structure

**Created**
- `services/slack_bot/commands.py` — `dispatch_command(app, cmd)` router + four handler coroutines (`_cmd_agnes`, `_cmd_new`, `_cmd_status`, `_help_body`), plus local `_schedule` + `_run_logged` (best-effort-ephemeral backstop). Pure routing + per-command logic; no FastAPI.
- `tests/test_slack_commands.py` — unit tests for the command router, handlers, the endpoint signature gate, `EphemeralCommandSink`, and `_run_logged`.

**Modified**
- `app/api/slack.py` — add `POST /api/slack/commands` (verify signature on raw body, parse form, schedule `_run_logged(dispatch_command(...))`, return 3 s ack body).
- `services/slack_bot/sender.py` — add `send_ephemeral(response_url, text, blocks=None)` and `open_im(slack_user_id) -> Optional[str]`.
- `services/slack_bot/sink.py` — add `EphemeralCommandSink` (transient sink that posts the next assistant turn to a `response_url` then self-closes).
- `app/chat/manager.py` — add public `active_count_for_user(self, user_email) -> int` wrapper over the private `_active_count_for_user`.
- `services/slack_bot/manifest.yaml` — add a `slash_commands` block declaring the three registered commands.
- `docs/cloud-chat.md` — mirror the `slash_commands` stanza + slash-command usage in the "Slack install" section.
- `CHANGELOG.md` — `[Unreleased]` Added bullet.

---

## Task 1 — `active_count_for_user` public wrapper on ChatManager

Single-source the concurrency-cap predicate so `/agnes-status` reports exactly what `create_session` enforces.

**Files:**
- Modify: `app/chat/manager.py`
- Test: `tests/test_chat_manager.py`

Steps:

- [ ] Read the existing private method to confirm the signature. In `app/chat/manager.py` it is:
  ```python
  def _active_count_for_user(self, user_email: str) -> int:
      return sum(
          1
          for s in self._live.values()
          if s.user_email == user_email
          and s.state in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE)
      )
  ```
- [ ] Append a failing test to `tests/test_chat_manager.py`. First find an existing fixture/helper that builds a `ChatManager` (run `grep -n "ChatManager(" tests/test_chat_manager.py` and reuse the same construction). Add:
  ```python
  def test_active_count_for_user_matches_private(monkeypatch):
      from types import SimpleNamespace
      from app.chat.manager import ChatManager
      from app.chat.types import SessionState

      mgr = ChatManager.__new__(ChatManager)  # bypass __init__; we set only _live
      mgr._live = {
          "a": SimpleNamespace(user_email="x@e.com", state=SessionState.ACTIVE),
          "b": SimpleNamespace(user_email="x@e.com", state=SessionState.IDLE),
          "c": SimpleNamespace(user_email="y@e.com", state=SessionState.ACTIVE),
          "d": SimpleNamespace(user_email="x@e.com", state=SessionState.DEAD),
      }
      assert mgr.active_count_for_user("x@e.com") == 2
      assert mgr.active_count_for_user("x@e.com") == mgr._active_count_for_user("x@e.com")
  ```
- [ ] Run it, expect FAIL (no `active_count_for_user` attribute):
  `.venv/bin/pytest tests/test_chat_manager.py::test_active_count_for_user_matches_private -v`
  Expected: `AttributeError: 'ChatManager' object has no attribute 'active_count_for_user'`.
- [ ] Add the public wrapper directly under `_active_count_for_user` in `app/chat/manager.py`:
  ```python
  def active_count_for_user(self, user_email: str) -> int:
      """Public wrapper over the private cap predicate so callers
      (e.g. /agnes-status) report exactly what create_session enforces."""
      return self._active_count_for_user(user_email)
  ```
- [ ] Run it, expect PASS:
  `.venv/bin/pytest tests/test_chat_manager.py::test_active_count_for_user_matches_private -v`
- [ ] Commit:
  ```bash
  git add app/chat/manager.py tests/test_chat_manager.py
  git commit -m "chat: public active_count_for_user wrapper for slash-status"
  ```

---

## Task 2 — Sender helpers: `send_ephemeral` + `open_im`

Both ride the Slack Web API independent of inbound transport. `send_ephemeral` POSTs to the slash command's `response_url`; `open_im` resolves a user's DM channel id via `conversations.open`.

**Files:**
- Modify: `services/slack_bot/sender.py`
- Test: `tests/test_slack_commands.py` (Created)

Steps:

- [ ] Create `tests/test_slack_commands.py` with the first three failing tests:
  ```python
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
  ```
- [ ] Run, expect FAIL (functions don't exist):
  `.venv/bin/pytest tests/test_slack_commands.py -k "send_ephemeral or open_im" -v`
  Expected: `AttributeError: module 'services.slack_bot.sender' has no attribute 'send_ephemeral'`.
- [ ] Add both helpers to `services/slack_bot/sender.py` (the module already imports `httpx`, `logging`, `os`):
  ```python
  from typing import Optional


  async def send_ephemeral(
      response_url: str, text: str, blocks: Optional[list] = None,
  ) -> None:
      """Deliver an ephemeral message to a slash command's response_url.

      response_url is limited to ~30 min / 5 posts — single-shot use only.
      No bot token needed: the URL itself authorizes the post.
      """
      payload: dict = {"response_type": "ephemeral", "text": text}
      if blocks is not None:
          payload["blocks"] = blocks
      async with httpx.AsyncClient(timeout=10) as client:
          await client.post(response_url, json=payload)


  async def open_im(slack_user_id: str) -> Optional[str]:
      """Resolve a user's DM channel id via conversations.open.

      A slash command fired in a public channel carries that channel's id,
      not the DM channel — keying a SLACK_DM session on it would break
      dedup. Returns the IM channel id, or None on missing token / error.
      """
      token = os.environ.get("SLACK_BOT_TOKEN")
      if not token:
          logger.error("SLACK_BOT_TOKEN missing — cannot open IM")
          return None
      async with httpx.AsyncClient(timeout=10) as client:
          resp = await client.post(
              "https://slack.com/api/conversations.open",
              headers={"Authorization": f"Bearer {token}"},
              json={"users": slack_user_id},
          )
      try:
          data = resp.json()
      except Exception:
          logger.exception("conversations.open returned non-JSON")
          return None
      if not data.get("ok"):
          logger.error("conversations.open failed: %s", data.get("error"))
          return None
      return data.get("channel", {}).get("id")
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "send_ephemeral or open_im" -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/sender.py tests/test_slack_commands.py
  git commit -m "slack: send_ephemeral + open_im sender helpers"
  ```

---

## Task 3 — `EphemeralCommandSink`

A transient duck-typed sink: it posts the *next* `assistant_message` of a turn to the caller's `response_url`, then closes. It drops the chatty frames (`token`, `ready`, `tool_call`, …) exactly like `SlackSinkBridge`, and forwards `error`/`cancelled` so a budget/rate failure is still surfaced once.

**Files:**
- Modify: `services/slack_bot/sink.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests to `tests/test_slack_commands.py`:
  ```python
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
  ```
- [ ] Run, expect FAIL:
  `.venv/bin/pytest tests/test_slack_commands.py -k ephemeral_command_sink -v`
  Expected: `AttributeError: module 'services.slack_bot.sink' has no attribute 'EphemeralCommandSink'`.
- [ ] Add to `services/slack_bot/sink.py`. First add the import at the top alongside the existing `from services.slack_bot.sender import send_thread_reply`:
  ```python
  from services.slack_bot.sender import send_ephemeral, send_thread_reply
  ```
  (replace the existing single-name import line). Then append the class:
  ```python
  class EphemeralCommandSink:
      """One-shot sink for slash commands.

      Posts the FIRST assistant_message of the turn to the caller's
      response_url, then ignores further frames. error/cancelled are also
      surfaced once so a budget/rate failure is visible. Never stays
      attached — the session's permanent sink (web/DM) keeps streaming.
      """

      def __init__(self, *, response_url: str) -> None:
          self._response_url = response_url
          self._delivered = False
          self._closed = asyncio.Event()

      async def send_json(self, data: dict) -> None:
          if self._delivered:
              return
          t = data.get("type")
          if t == "assistant_message":
              content = data.get("content", "")
              if content:
                  self._delivered = True
                  await send_ephemeral(self._response_url, content)
          elif t == "error":
              kind = data.get("kind", "")
              msg = data.get("message", "")
              self._delivered = True
              await send_ephemeral(
                  self._response_url, f":warning: {kind}: {msg}".strip(": ")
              )
          elif t == "cancelled":
              self._delivered = True
              await send_ephemeral(self._response_url, "_(stopped)_")
          # ready / runner_ready / token / tool_call / tool_result / done: ignored

      async def receive_json(self) -> dict:
          await self._closed.wait()
          return {"type": "_closed"}

      async def close(self) -> None:
          self._closed.set()
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k ephemeral_command_sink -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/sink.py tests/test_slack_commands.py
  git commit -m "slack: EphemeralCommandSink for one-shot slash answers"
  ```

---

## Task 4 — `dispatch_command` router + `/agnes help` + local `_schedule`/`_run_logged`

Start with the router skeleton, the simplest command (`/agnes help`, synchronous, no session), and the local `_schedule` + `_run_logged` helpers this phase owns (Phase 0's copies are confirmed absent from `events.py`).

**Files:**
- Create: `services/slack_bot/commands.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests to `tests/test_slack_commands.py`:
  ```python
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
  ```
- [ ] Run, expect FAIL (module does not exist):
  `.venv/bin/pytest tests/test_slack_commands.py -k "help_body or routes_unknown or run_logged" -v`
  Expected: `ModuleNotFoundError: No module named 'services.slack_bot.commands'`.
- [ ] Create `services/slack_bot/commands.py` with the router, the help body, the local scheduler + logged-wrapper, and stubs for the three async handlers:
  ```python
  """Slack slash-command dispatcher — routes /agnes* commands to handlers.

  Each handler delivers its answer asynchronously via the command's
  response_url (30-min / 5-post limited → single-shot). /agnes help is the
  only synchronous path (its body rides the 3 s ack).

  This module owns its own _schedule + _run_logged (Phase 0's copies live
  in events.py but are not depended upon here — verified absent at authoring
  time; keeping them local makes this phase self-contained).
  """
  from __future__ import annotations

  import asyncio
  import logging
  from typing import Any, Optional

  from services.slack_bot.sender import send_ephemeral

  logger = logging.getLogger(__name__)

  _BG_TASKS: set = set()


  def _schedule(coro) -> None:
      """Fire-and-forget a coroutine, keeping a strong ref so the GC can't
      cancel an in-flight dispatch."""
      task = asyncio.create_task(coro)
      _BG_TASKS.add(task)
      task.add_done_callback(_BG_TASKS.discard)


  async def _run_logged(coro, *, response_url: Optional[str] = None) -> None:
      """Run a dispatch coroutine, swallowing + logging any unhandled
      exception. Because the endpoint acks before dispatch, an exception
      here never triggers a Slack retry — this is the only recovery path,
      so on failure post a best-effort ephemeral to the caller's
      response_url (if one was supplied)."""
      try:
          await coro
      except Exception:
          logger.exception("unhandled exception in slash-command dispatch")
          if response_url:
              try:
                  await send_ephemeral(
                      response_url,
                      ":warning: Something went wrong handling that command. "
                      "Please try again.",
                  )
              except Exception:
                  logger.exception("failed to post error ephemeral")


  def _help_body() -> str:
      return (
          "*Agnes slash commands*\n"
          "• `/agnes <question>` — ask Agnes; the answer also appears on web /chat.\n"
          "• `/agnes-new` — archive your current Agnes DM session and start fresh.\n"
          "• `/agnes-status` — show your active session count and cap.\n"
          "• `/agnes help` — show this message."
      )


  async def dispatch_command(app, cmd: dict[str, Any]) -> None:
      command = (cmd.get("command") or "").strip()
      if command == "/agnes":
          await _cmd_agnes(app, cmd)
      elif command == "/agnes-new":
          await _cmd_new(app, cmd)
      elif command == "/agnes-status":
          await _cmd_status(app, cmd)
      else:
          logger.info("unknown slash command: %s", command)


  async def _cmd_agnes(app, cmd: dict) -> None:  # implemented in Task 6
      raise NotImplementedError


  async def _cmd_new(app, cmd: dict) -> None:  # implemented in Task 7
      raise NotImplementedError


  async def _cmd_status(app, cmd: dict) -> None:  # implemented in Task 8
      raise NotImplementedError
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "help_body or routes_unknown or run_logged" -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/commands.py tests/test_slack_commands.py
  git commit -m "slack: dispatch_command router + /agnes help + local _schedule/_run_logged"
  ```

---

## Task 5 — `POST /api/slack/commands` endpoint (signature gate + ack-then-async)

Verify HMAC on the raw body, parse the `x-www-form-urlencoded` form, schedule `_run_logged(dispatch_command(...))` via the local `_schedule`, and return the 3 s ack. `/agnes help` answers synchronously in the ack body. The `_run_logged` wrap is the best-effort-ephemeral backstop for any unhandled handler exception (per-command errors already reach `response_url` via the sink, matching the existing `_handle_dm` precedent; this catches the *unexpected* ones).

**Files:**
- Modify: `app/api/slack.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests. These exercise the endpoint through FastAPI's `TestClient`, monkeypatching signature verification and `dispatch_command`:
  ```python
  def _sign_ok(monkeypatch):
      import services.slack_bot.sigverify as sv
      monkeypatch.setattr(sv, "verify_slack_signature", lambda *a, **k: True)
      import app.api.slack as slack_api
      monkeypatch.setattr(slack_api, "verify_slack_signature", lambda *a, **k: True)
      monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")


  def _make_client(monkeypatch, scheduled):
      from types import SimpleNamespace
      from fastapi import FastAPI
      from fastapi.testclient import TestClient
      import app.api.slack as slack_api

      async def fake_dispatch(app, cmd):
          scheduled.append(cmd)

      monkeypatch.setattr(slack_api, "dispatch_command", fake_dispatch)
      app = FastAPI()
      app.include_router(slack_api.router)
      app.state.chat_repo = SimpleNamespace()
      app.state.chat_manager = SimpleNamespace()
      return TestClient(app)


  def test_commands_bad_signature_401(monkeypatch):
      import app.api.slack as slack_api
      monkeypatch.setattr(slack_api, "verify_slack_signature", lambda *a, **k: False)
      monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")
      scheduled: list = []
      client = _make_client(monkeypatch, scheduled)
      r = client.post("/api/slack/commands", data={"command": "/agnes", "text": "hi"})
      assert r.status_code == 401
      assert scheduled == []  # forged command never dispatched


  def test_commands_help_is_synchronous(monkeypatch):
      _sign_ok(monkeypatch)
      scheduled: list = []
      client = _make_client(monkeypatch, scheduled)
      r = client.post(
          "/api/slack/commands",
          data={"command": "/agnes", "text": "help", "user_id": "U1",
                "channel_id": "C1", "response_url": "https://r/1"},
      )
      assert r.status_code == 200
      assert "/agnes-new" in r.json()["text"]
      assert scheduled == []  # help did no async work


  def test_commands_schedules_dispatch(monkeypatch):
      _sign_ok(monkeypatch)
      scheduled: list = []
      client = _make_client(monkeypatch, scheduled)
      r = client.post(
          "/api/slack/commands",
          data={"command": "/agnes", "text": "what is mrr", "user_id": "U1",
                "channel_id": "C1", "response_url": "https://r/1"},
      )
      assert r.status_code == 200
      assert len(scheduled) == 1
      assert scheduled[0]["command"] == "/agnes"
      assert scheduled[0]["text"] == "what is mrr"
  ```
- [ ] Run, expect FAIL (404 — no `/commands` route):
  `.venv/bin/pytest tests/test_slack_commands.py -k "commands_" -v`
  Expected: `assert 404 == 401` (route missing).
- [ ] Edit `app/api/slack.py`. Add imports near the top (the file already imports `os`, `logging`, `HTTPException`, `Request`, `verify_slack_signature`):
  ```python
  from urllib.parse import parse_qs

  from services.slack_bot.commands import (
      _help_body,
      _run_logged,
      _schedule,
      dispatch_command,
  )
  ```
  (Note: `_schedule` and `_run_logged` come from `commands.py` — this phase's own copies, verified self-contained in Task 4 — *not* from `events.py`.)
- [ ] Add the endpoint to `app/api/slack.py`:
  ```python
  @router.post("/commands")
  async def slack_commands(request: Request):
      body = await request.body()
      ts = request.headers.get("X-Slack-Request-Timestamp", "")
      sig = request.headers.get("X-Slack-Signature", "")
      secret = os.environ.get("SLACK_SIGNING_SECRET", "")
      if not secret or not verify_slack_signature(secret, ts, sig, body):
          raise HTTPException(401, "bad_signature")
      form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
      command = (form.get("command") or "").strip()
      text = (form.get("text") or "").strip()
      # /agnes help answers synchronously in the 3 s ack — no session, no async.
      if command == "/agnes" and text in ("", "help"):
          return {"response_type": "ephemeral", "text": _help_body()}
      # Wrap the dispatch in _run_logged so an UNHANDLED handler exception
      # posts a best-effort ephemeral instead of vanishing (per-command errors
      # already reach response_url via the sink — this is the backstop).
      _schedule(_run_logged(dispatch_command(request.app, form),
                            response_url=form.get("response_url")))
      return {"response_type": "ephemeral", "text": "_Working on it…_"}
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "commands_" -v`
- [ ] Commit:
  ```bash
  git add app/api/slack.py tests/test_slack_commands.py
  git commit -m "slack: POST /api/slack/commands endpoint (sig gate + ack-then-async)"
  ```

---

## Task 6 — `/agnes <q>` handler (persistent DM session + ephemeral answer)

Resolve the invoker → email; gate on binding + CHAT grant; resolve the IM channel via `open_im`; `create_session(SLACK_DM)` (dedups); attach an `EphemeralCommandSink` only if no permanent sink exists; send the user turn; the sink delivers the answer once via `response_url`.

**Files:**
- Modify: `services/slack_bot/commands.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests. They use a `SimpleNamespace` app like the existing `tests/test_slack_bot.py::_build_slack_app_state` pattern:
  ```python
  def _agnes_app(monkeypatch, *, bound=True, can_chat=True):
      from types import SimpleNamespace
      import duckdb
      from src.db import _ensure_schema
      from app.chat.persistence import ChatRepository
      from app.chat.types import ChatSession, Surface
      from datetime import datetime, timezone
      from services.slack_bot.binding import _ensure_table

      conn = duckdb.connect(":memory:")
      _ensure_schema(conn)
      conn.execute("INSERT INTO users(id, email, name) VALUES ('uid1','bob@example.com','Bob')")
      repo = ChatRepository(conn)
      _ensure_table(conn)
      if bound:
          conn.execute("UPDATE users SET slack_user_id='U1' WHERE email='bob@example.com'")

      created: list = []
      attached: list = []
      sent: list = []

      async def create_session(*, user_email, surface, slack_channel_id=None, **kw):
          s = ChatSession(
              id="dm-1", user_email=user_email, surface=surface,
              slack_channel_id=slack_channel_id, slack_thread_ts=None, title=None,
              started_at=datetime.now(timezone.utc), last_message_at=None,
              message_count=0, archived=False,
          )
          created.append(s)
          return s

      async def attach(chat_id, sink):
          attached.append((chat_id, sink))
          await sink.send_json({"type": "assistant_message", "content": "the answer"})

      async def send_user_message(chat_id, text):
          sent.append((chat_id, text))

      mgr = SimpleNamespace(
          list_live=lambda: [], create_session=create_session, attach=attach,
          send_user_message=send_user_message,
          _config=SimpleNamespace(concurrency_per_user=3),
          _created=created, _attached=attached, _sent=sent,
      )
      app = SimpleNamespace(state=SimpleNamespace(
          chat_repo=repo, chat_manager=mgr, public_url="https://agnes.example.com"))

      import app.auth.access as _access
      monkeypatch.setattr(_access, "can_access", lambda *a, **k: can_chat)
      import services.slack_bot.commands as cmds
      async def fake_open_im(uid): return "D1"
      monkeypatch.setattr(cmds, "open_im", fake_open_im)
      return app, cmds


  def test_agnes_happy_path_keys_on_im_channel(monkeypatch):
      app, cmds = _agnes_app(monkeypatch)
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes", "text": "what is mrr", "user_id": "U1",
             "channel_id": "C_PUBLIC", "response_url": "https://r/1"}

      async def _run():
          await cmds.dispatch_command(app, cmd)
          import asyncio as _a; await _a.sleep(0.1)
      __import__("asyncio").run(_run())

      mgr = app.state.chat_manager
      assert mgr._created[0].slack_channel_id == "D1"   # IM channel, NOT C_PUBLIC
      assert mgr._sent == [("dm-1", "what is mrr")]
      assert eph == [("https://r/1", "the answer")]


  def test_agnes_unbound_user_gets_code(monkeypatch):
      app, cmds = _agnes_app(monkeypatch, bound=False)
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes", "text": "hi", "user_id": "U_NEW",
             "channel_id": "C1", "response_url": "https://r/2"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph and "6-digit" in eph[0][1]
      assert app.state.chat_manager._created == []   # no session for unbound


  def test_agnes_no_chat_grant_denied(monkeypatch):
      app, cmds = _agnes_app(monkeypatch, can_chat=False)
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes", "text": "hi", "user_id": "U1",
             "channel_id": "C1", "response_url": "https://r/3"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph and "admin" in eph[0][1].lower()
      assert app.state.chat_manager._created == []


  def test_agnes_cap_hit_ephemeral(monkeypatch):
      app, cmds = _agnes_app(monkeypatch)
      from app.chat.manager import ConcurrencyCapHit
      async def boom(**kw): raise ConcurrencyCapHit("at cap")
      app.state.chat_manager.create_session = boom
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes", "text": "hi", "user_id": "U1",
             "channel_id": "C1", "response_url": "https://r/4"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph and "/agnes-new" in eph[0][1]
  ```
- [ ] Run, expect FAIL (`_cmd_agnes` raises `NotImplementedError`):
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_happy or agnes_unbound or agnes_no_chat or agnes_cap" -v`
- [ ] Implement `_cmd_agnes` in `services/slack_bot/commands.py`. Add imports at the top of the module:
  ```python
  from services.slack_bot.binding import issue_verification_code, lookup_user_email
  from services.slack_bot.sender import open_im, send_ephemeral
  from services.slack_bot.sink import EphemeralCommandSink
  ```
  Replace the `_cmd_agnes` stub with:
  ```python
  def _is_attached(mgr, chat_id: str) -> bool:
      return any(live.chat_id == chat_id for live in mgr.list_live())


  async def _cmd_agnes(app, cmd: dict) -> None:
      from app.auth.access import can_access
      from app.chat.manager import ConcurrencyCapHit
      from app.chat.types import Surface
      from app.resource_types import ResourceType
      from src.repositories.users import UserRepository

      repo = app.state.chat_repo
      mgr = app.state.chat_manager
      slack_user_id = cmd.get("user_id", "")
      text = (cmd.get("text") or "").strip()
      response_url = cmd.get("response_url", "")

      user_email = lookup_user_email(repo, slack_user_id)
      if user_email is None:
          code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
          public_url = getattr(app.state, "public_url", "")
          setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
          await send_ephemeral(
              response_url,
              "To use Agnes from Slack, bind your identity first:\n"
              f"1. Visit {setup_link} while logged in.\n"
              f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes).",
          )
          return

      _u = UserRepository(repo._conn).get_by_email(user_email)
      if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", repo._conn):
          await send_ephemeral(
              response_url,
              "You don't have access to Agnes chat yet — ask an admin to grant "
              "your group access on /admin/access.",
          )
          return

      im_channel = await open_im(slack_user_id)
      if im_channel is None:
          await send_ephemeral(response_url, ":warning: Couldn't open a DM channel. Try again.")
          return

      try:
          session = await mgr.create_session(
              user_email=user_email, surface=Surface.SLACK_DM, slack_channel_id=im_channel,
          )
      except ConcurrencyCapHit:
          cap = mgr._config.concurrency_per_user
          await send_ephemeral(
              response_url,
              f"You're at your session limit ({cap}); run `/agnes-new` to free one.",
          )
          return

      # Attach a one-shot ephemeral sink only if no permanent sink (web/DM)
      # is already pumping — response_url is single-shot and the persistent
      # sink keeps streaming on web/DM.
      if not _is_attached(mgr, session.id):
          sink = EphemeralCommandSink(response_url=response_url)
          asyncio.create_task(mgr.attach(session.id, sink))
          await asyncio.sleep(0.1)  # let attach() set up the pump + emit ready
      await mgr.send_user_message(session.id, text)
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_happy or agnes_unbound or agnes_no_chat or agnes_cap" -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/commands.py tests/test_slack_commands.py
  git commit -m "slack: /agnes handler — persistent DM session + ephemeral answer"
  ```

---

## Task 7 — `/agnes-new` handler (soft-archive DM session)

Resolve IM channel → look up the live DM session via **`repo.get_slack_dm_session`** → `mgr.kill` + `repo.archive_session` → ephemeral confirm. Next `/agnes` creates a fresh row (`get_slack_dm_session` filters `archived=FALSE`). `repo.get_slack_dm_session(slack_channel_id)` is confirmed to exist on `ChatRepository` (`app/chat/persistence.py:163`); the handler calls it on the **repo** and `kill` on the **manager**.

**Files:**
- Modify: `services/slack_bot/commands.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests (final form — `get_slack_dm_session` and `archive_session` are both stubbed on the **repo**, which is exactly what the handler calls; `kill` is the only manager stub):
  ```python
  def test_agnes_new_archives_existing(monkeypatch):
      app, cmds = _agnes_app(monkeypatch)
      mgr = app.state.chat_manager

      from app.chat.types import ChatSession, Surface
      from datetime import datetime, timezone
      existing = ChatSession(
          id="dm-old", user_email="bob@example.com", surface=Surface.SLACK_DM,
          slack_channel_id="D1", slack_thread_ts=None, title=None,
          started_at=datetime.now(timezone.utc), last_message_at=None,
          message_count=1, archived=False,
      )
      killed: list = []
      archived: list = []

      # Handler calls these on the REPO:
      app.state.chat_repo.get_slack_dm_session = lambda ch: existing if ch == "D1" else None
      app.state.chat_repo.archive_session = lambda cid: archived.append(cid)
      # Handler calls kill on the MANAGER:
      async def kill(chat_id, *, reason): killed.append((chat_id, reason))
      mgr.kill = kill

      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes-new", "text": "", "user_id": "U1",
             "channel_id": "C1", "response_url": "https://r/5"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))

      assert killed == [("dm-old", "agnes_new")]
      assert archived == ["dm-old"]
      assert eph and "fresh" in eph[0][1].lower()


  def test_agnes_new_no_existing_still_confirms(monkeypatch):
      app, cmds = _agnes_app(monkeypatch)
      app.state.chat_repo.get_slack_dm_session = lambda ch: None
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes-new", "text": "", "user_id": "U1",
             "channel_id": "C1", "response_url": "https://r/6"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph  # always confirms
  ```
- [ ] Run, expect FAIL (`_cmd_new` raises `NotImplementedError`):
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_new" -v`
- [ ] Implement the shared soft-archive helper + `_cmd_new` in `services/slack_bot/commands.py` (replace the `_cmd_new` stub):
  ```python
  async def _soft_archive_dm(app, slack_user_id: str) -> bool:
      """Resolve the caller's IM channel, kill + archive any live DM session.

      Returns True if a session was archived, False if none existed. Shared
      by /agnes-new and (Phase 3) the New-session button.
      """
      repo = app.state.chat_repo
      mgr = app.state.chat_manager
      im_channel = await open_im(slack_user_id)
      if im_channel is None:
          return False
      existing = repo.get_slack_dm_session(im_channel)
      if existing is None:
          return False
      try:
          await mgr.kill(existing.id, reason="agnes_new")
      except Exception:
          logger.exception("kill failed for %s during /agnes-new", existing.id)
      repo.archive_session(existing.id)
      return True


  async def _cmd_new(app, cmd: dict) -> None:
      slack_user_id = cmd.get("user_id", "")
      response_url = cmd.get("response_url", "")
      # Binding/grant are enforced on the next /agnes; /agnes-new is a no-op
      # for unbound users (no DM session can exist), so we skip the gate here.
      archived = await _soft_archive_dm(app, slack_user_id)
      if archived:
          await send_ephemeral(response_url, "Archived your Agnes session — your next `/agnes` starts fresh.")
      else:
          await send_ephemeral(response_url, "No active Agnes session to archive — your next `/agnes` starts fresh.")
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_new" -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/commands.py tests/test_slack_commands.py
  git commit -m "slack: /agnes-new soft-archives the DM session"
  ```

---

## Task 8 — `/agnes-status` handler (read-only count + deep link)

Ephemeral: `active_count_for_user(email)` / `config.concurrency_per_user` plus a `<public_url>/chat` deep link. Gated on binding (unbound → code); no CHAT-grant check needed (read-only count, no session spawn) — but unbound users can't have a count, so we surface the binding prompt.

**Files:**
- Modify: `services/slack_bot/commands.py`
- Test: `tests/test_slack_commands.py`

Steps:

- [ ] Append failing tests:
  ```python
  def test_agnes_status_reports_count_and_cap(monkeypatch):
      app, cmds = _agnes_app(monkeypatch)
      mgr = app.state.chat_manager
      mgr.active_count_for_user = lambda email: 2
      mgr._config = __import__("types").SimpleNamespace(concurrency_per_user=3)
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

      cmd = {"command": "/agnes-status", "text": "", "user_id": "U1",
             "channel_id": "C1", "response_url": "https://r/7"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph
      body = eph[0][1]
      assert "2" in body and "3" in body
      assert "https://agnes.example.com/chat" in body


  def test_agnes_status_unbound_gets_code(monkeypatch):
      app, cmds = _agnes_app(monkeypatch, bound=False)
      eph: list = []
      async def fake_eph(url, text, blocks=None): eph.append((url, text))
      monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)
      cmd = {"command": "/agnes-status", "text": "", "user_id": "U_NEW",
             "channel_id": "C1", "response_url": "https://r/8"}
      __import__("asyncio").run(cmds.dispatch_command(app, cmd))
      assert eph and "6-digit" in eph[0][1]
  ```
- [ ] Run, expect FAIL (`_cmd_status` raises `NotImplementedError`):
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_status" -v`
- [ ] Implement `_cmd_status` in `services/slack_bot/commands.py` (replace the stub):
  ```python
  async def _cmd_status(app, cmd: dict) -> None:
      repo = app.state.chat_repo
      mgr = app.state.chat_manager
      slack_user_id = cmd.get("user_id", "")
      response_url = cmd.get("response_url", "")

      user_email = lookup_user_email(repo, slack_user_id)
      if user_email is None:
          code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
          public_url = getattr(app.state, "public_url", "")
          setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
          await send_ephemeral(
              response_url,
              "Bind your Slack identity to Agnes first:\n"
              f"1. Visit {setup_link} while logged in.\n"
              f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes).",
          )
          return

      active = mgr.active_count_for_user(user_email)
      cap = mgr._config.concurrency_per_user
      public_url = getattr(app.state, "public_url", "")
      chat_link = f"{public_url}/chat" if public_url else "/chat"
      await send_ephemeral(
          response_url,
          f"*Agnes status* — active sessions: *{active}* / {cap}\n"
          f"Open the full chat UI: {chat_link}",
      )
  ```
- [ ] Run, expect PASS:
  `.venv/bin/pytest tests/test_slack_commands.py -k "agnes_status" -v`
- [ ] Commit:
  ```bash
  git add services/slack_bot/commands.py tests/test_slack_commands.py
  git commit -m "slack: /agnes-status read-only count + deep link"
  ```

---

## Task 9 — Manifest + docs: `slash_commands` stanza

Declare the three registered commands so operators installing from manifest get the slash UI, and mirror them in `docs/cloud-chat.md` (which already documents the manifest install and the `YOUR-AGNES-HOST` placeholder at lines 65-74). Vendor-agnostic placeholder host only.

**Files:**
- Modify: `services/slack_bot/manifest.yaml`
- Modify: `docs/cloud-chat.md`
- Test: none (static config; YAML-parse check below)

Steps:

- [ ] Read the current `services/slack_bot/manifest.yaml`. Add a top-level `slash_commands` block after the `oauth_config` block (and before `settings:`). Include an inline comment about the Socket-Mode variant:
  ```yaml
  # Slash commands. Under Socket Mode, omit each `url:` — commands arrive
  # over the socket and route through the same dispatch_command.
  slash_commands:
    - command: /agnes
      url: "https://YOUR-AGNES-HOST/api/slack/commands"
      description: Ask Agnes a data question
      usage_hint: "<your question> | help"
      should_escape: false
    - command: /agnes-new
      url: "https://YOUR-AGNES-HOST/api/slack/commands"
      description: Archive your Agnes session and start fresh
      should_escape: false
    - command: /agnes-status
      url: "https://YOUR-AGNES-HOST/api/slack/commands"
      description: Show your active Agnes session count and cap
      should_escape: false
  ```
  Leave `oauth_config.scopes.bot` unchanged — slash commands require no extra scope.
- [ ] Read `docs/cloud-chat.md` lines 65-89 (the "Slack install" + "Cost & limits" sections). Insert a new subsection after the existing "## Slack install" numbered list (after the line `which they paste at \`/setup\` while logged into Agnes.`, before `## Cost & limits`):
  ```markdown
  ### Slash commands

  The manifest also registers three slash commands, all pointed at
  `https://YOUR-AGNES-HOST/api/slack/commands` (a separate Request URL
  from the Events endpoint):

  | Command | What it does |
  |---|---|
  | `/agnes <question>` | Asks Agnes; runs on your persistent DM session, so the answer also appears on web `/chat`. |
  | `/agnes-new` | Archives your current Agnes DM session so the next `/agnes` starts fresh. |
  | `/agnes-status` | Shows your active session count vs. the per-user cap, plus a `/chat` deep link. |
  | `/agnes help` | Lists these commands (answered inline, no async work). |

  Each command acks within Slack's 3 s budget and delivers its answer
  asynchronously (ephemerally) via the command's `response_url`. Under
  Socket Mode the commands arrive over the socket instead of the HTTP
  Request URL — no manifest `url:` is needed in that mode.
  ```
- [ ] Verify the manifest still parses:
  `.venv/bin/python -c "import yaml; yaml.safe_load(open('services/slack_bot/manifest.yaml'))"`
  Expected: no output (valid YAML).
- [ ] Commit:
  ```bash
  git add services/slack_bot/manifest.yaml docs/cloud-chat.md
  git commit -m "slack: declare /agnes slash commands in manifest + docs"
  ```

---

## Task 10 — Full suite + CHANGELOG bullet

**Files:**
- Modify: `CHANGELOG.md`
- Test: full suite

Steps:

- [ ] Run the entire suite (this is what CI runs):
  `.venv/bin/pytest tests/ --tb=short -n auto -q`
  Expected: all green. If a failure is in code you touched, fix it before continuing. If it is pre-existing/unrelated, confirm with `git stash` it reproduces on the clean branch and note it — do not block on it.
- [ ] Add a bullet under `## [Unreleased]` in `CHANGELOG.md`, in the `### Added` group (create the group if absent, keeping Added/Changed/Fixed/Internal ordering):
  ```markdown
  ### Added
  - Slack slash commands: `/agnes <question>` (runs on your persistent DM session so the answer also appears on web `/chat`), `/agnes-new` (archive the current DM session), `/agnes-status` (active session count vs cap + a `/chat` deep link), and `/agnes help`. New signature-verified `POST /api/slack/commands` endpoint acks within 3 s and delivers answers asynchronously via Slack `response_url`.
  ```
- [ ] Verify the bullet placement:
  `grep -n "Unreleased\|/agnes" CHANGELOG.md | head`
- [ ] Commit:
  ```bash
  git add CHANGELOG.md
  git commit -m "changelog: Slack slash commands"
  ```

---

## Notes for the implementer

- **No new repo method** is introduced in this phase — `/agnes-new` reuses the existing `ChatRepository.archive_session` and `get_slack_dm_session` (both already have PG siblings via the `_sessions_pg` delegation in `app/chat/persistence.py`). The dual-backend rule therefore needs no new `_pg.py` method or `tests/db_pg/` contract test for this phase. If you find yourself adding a repo method, stop and add its `_pg.py` sibling + a `tests/db_pg/` contract test in the same task per the repo's dual-backend discipline.
- **No new endpoint authz gate beyond signature verification.** `/api/slack/commands` authenticates the *caller* via Slack's HMAC (not Agnes's `require_admin`/`require_resource_access`) because the caller is Slack, not an Agnes-session principal; per-user authorization happens inside `_cmd_agnes` via the `can_access(..., ResourceType.CHAT.value, "chat", ...)` check (mirroring `_handle_dm`). Do not add `Depends(get_current_user)` to the slash endpoint — Slack POSTs carry no Agnes session cookie.
- **`_schedule`/`_run_logged` are this phase's own** (in `commands.py`), confirmed self-contained because `services/slack_bot/events.py` has no such helpers at authoring time. The endpoint always wraps dispatch in `_run_logged(...)` so an unhandled handler exception posts a best-effort ephemeral to `response_url` instead of vanishing — each `_cmd_*` already surfaces its own user-facing errors (binding/grant/cap), so this is purely the backstop for the *unexpected*. Do **not** import these from `events.py` even if Phase 0 later adds copies there.
- **Vendor-agnostic:** every example host is `YOUR-AGNES-HOST` / `<your-host>` / `agnes.example.com`. No real hostnames, tokens, or workspace IDs anywhere in code, manifest, docs, or commit messages.
- **`response_url` is single-shot** (30 min / 5 posts) by Slack's contract — `EphemeralCommandSink` delivers exactly the first assistant turn and then closes; long-running `/agnes` turns continue streaming on web/DM via the session's permanent sink, which is the intended behavior.
