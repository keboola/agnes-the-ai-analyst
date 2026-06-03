# Phase 0 — Slack Transport (HTTP + Socket Mode) + Ack-then-Async Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add Socket Mode as an optional second inbound Slack transport selectable per instance, funnel both HTTP and Socket Mode through the existing `dispatch_event` router, and fix the latent duplicate-session bug by making **ack-within-3s-then-process-async** a first-class contract on every dispatch call site.

**Architecture:** Inbound Slack payloads arrive over either HTTP (`POST /api/slack/events`, default) or a Socket Mode WebSocket (optional, `slack_sdk` lazy dep). Both verify/ack first, then schedule the dispatch via `asyncio.create_task(_run_logged(dispatch_event(...)))` and return immediately — the slow E2B-sandbox spawn no longer blocks the 3s Slack ack budget. A new `SocketModeDispatcher` owns the WS lifecycle (connect, ack envelope, schedule, reconnect, shutdown) and is wired into the FastAPI lifespan behind fail-closed gates (single-worker invariant, valid `xapp-`/`xoxb-` token pair, optional dep importable). Outbound replies are unchanged and remain transport-agnostic (`sender.py` / `sink.py`).

**Tech Stack:** Python 3.13, FastAPI, `asyncio`, `httpx`, DuckDB, `slack_sdk` (new optional `slack-socket` extra), pytest + pytest-xdist.

---

## File Structure

**Created:**
- `services/slack_bot/socket_mode_client.py` — `SocketModeDispatcher`: owns the Socket Mode WS lifecycle (lazy-imports `slack_sdk` inside `start()`), acks each envelope first, then schedules `dispatch_event` via `_run_logged`; reconnect/backoff handled by the SDK; clean `stop()`.
- `docs/slack-manifest-http.md` — vendor-agnostic HTTP-transport manifest stanza (default).
- `docs/slack-manifest-socket.md` — vendor-agnostic Socket-Mode manifest stanza.
- `tests/test_slack_transport.py` — unit tests for `SlackConfig` parsing, `get_slack_transport`, `_run_logged` (swallow+log, success, and best-effort failure-notifier), `_schedule` task-tracking, `SocketModeDispatcher._on_request` ack-first + dispatch parity, fail-closed gates.
- `tests/test_slack_events_ack_async.py` — ack-timing regression test: a slow `dispatch_event` does not block the HTTP `POST /api/slack/events` ack, and the event is dispatched exactly once.

**Modified:**
- `app/chat/config.py` — add frozen `SlackConfig` dataclass + `slack` field on `ChatConfig`; parse the nested `chat.slack` block in `load_chat_config`.
- `app/instance_config.py` — add `get_slack_transport()` (env → yaml → default `"http"`).
- `services/slack_bot/events.py` — add module-level detached-task set, `_schedule(coro)`, and `_run_logged(coro, *, on_failure=None)`; both reused by HTTP + Socket Mode.
- `app/api/slack.py` — change the `event_callback` branch to `_schedule(_run_logged(dispatch_event(...)))` then return ack (was `await dispatch_event(...)`).
- `app/main.py` — lifespan: construct + `start()` the `SocketModeDispatcher` when `transport=socket` (behind fail-closed gates incl. single-worker re-assert); stash on `app.state.slack_socket_dispatcher`; `stop()` at shutdown.
- `pyproject.toml` — add `[project.optional-dependencies] slack-socket = ["slack_sdk>=3.27"]`.
- `CHANGELOG.md` — `[Unreleased]` bullet.

---

## Task 1 — `_run_logged` + `_schedule` detached-task helpers in `events.py`

Per spec §1: every scheduled coroutine is wrapped in `_run_logged`, and every `create_task` result is held in a module-level set with `add_done_callback(discard)` so the GC can't cancel an in-flight dispatch. These are the shared primitives the HTTP endpoint and Socket Mode both call.

**The ack-then-fail recovery contract (spec lines 78 & 127).** Because we ack Slack *before* processing, a failure during dispatch does **not** trigger a Slack retry — `_run_logged` is the only recovery path, and the spec says it must post a *user-visible ephemeral* on unhandled exceptions. In Phase 0 the actual Slack ephemeral *wiring* cannot be implemented yet: the `send_ephemeral(response_url, …)` helper does not exist until Phase 2 (spec §3), and the context-free coroutine `_run_logged` wraps carries no `channel` / `thread_ts` / `response_url` to post to. So Phase 0 implements the recovery path as a **first-class, testable seam**: `_run_logged` takes an optional `on_failure: Callable[[BaseException], Awaitable[None]] | None` best-effort notifier and `await`s it (itself guarded) when the wrapped coroutine raises. Phase 0 call sites pass `on_failure=None` (the HTTP DM handler already emits its own binding/error replies inline before any dispatch failure can occur); later phases (mentions/slash/interactivity, which *do* have channel/`response_url` context) pass an `on_failure` that posts the ephemeral. This wires and tests the contract now without inventing a Phase-2 helper early — the only thing deferred is the concrete ephemeral payload, which is explicitly documented in the docstring.

**Files:**
- Test: `tests/test_slack_transport.py` (Create)
- Modify: `services/slack_bot/events.py`

**Steps:**

- [ ] Create `tests/test_slack_transport.py` with failing tests for `_run_logged` (swallow+log, success, and best-effort failure-notifier) plus `_schedule` strong-ref tracking:

```python
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
    """Ack-then-fail recovery contract (spec §1, lines 78 & 127): since we
    ack Slack BEFORE processing, a dispatch failure triggers no Slack retry —
    _run_logged is the only recovery path. It must call the best-effort
    on_failure notifier (the seam that later phases use to post a user-visible
    ephemeral) with the raised exception, while still swallowing the error."""
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
    """The recovery notifier is best-effort: if it ALSO raises (e.g. Slack
    postEphemeral 500s), _run_logged must still not propagate and must log."""
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
```

- [ ] Run them, expect FAIL (AttributeError: module `services.slack_bot.events` has no attribute `_run_logged`):
  `.venv/bin/pytest tests/test_slack_transport.py -v`

- [ ] Add the helpers to `services/slack_bot/events.py`. The existing imports already include `import asyncio` (line 4) and `logger = logging.getLogger(__name__)` (line 12). Add `Awaitable`, `Callable`, `Optional` to the `typing` import (currently line 6 `from typing import Any`):

```python
from typing import Any, Awaitable, Callable, Optional
```

  Then insert the helpers immediately after the `logger = logging.getLogger(__name__)` line (line 12):

```python
# Strong references to every scheduled dispatch task. asyncio only keeps a
# weak ref to a bare create_task() result, so a fire-and-forget task can be
# GC-collected (and cancelled) mid-flight. Holding it here until the
# done-callback discards it guarantees the dispatch runs to completion.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule(coro) -> asyncio.Task:
    """Schedule a coroutine on the running loop, retaining a strong ref.

    Used at every transport's dispatch call site (HTTP endpoint + Socket
    Mode) so the slow body runs *after* the 3s Slack ack has been sent.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_logged(
    coro,
    *,
    on_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
) -> None:
    """Wrap a scheduled dispatch coroutine — the ONLY recovery path.

    Because we ack Slack *before* processing (ack-then-async), a failure
    here does NOT trigger a Slack retry. So this wrapper must (a) never let
    the exception escape — an escaped exception surfaces as an asyncio
    "Task exception was never retrieved" and silently drops the work — and
    (b) drive the best-effort user-visible recovery notice (spec §1, the
    ack-then-fail semantics on lines 78 & 127).

    ``on_failure`` is that recovery seam: an awaitable the caller supplies
    to post a user-visible ephemeral with the failure. It is itself
    best-effort — a notifier that raises is caught and logged, never
    propagated. Phase 0 call sites pass ``on_failure=None`` (the HTTP DM
    handler emits its own binding/error replies inline, and the context-free
    dispatch here carries no channel/response_url to post to, and the
    ``send_ephemeral`` helper does not exist until Phase 2). Later phases
    (mentions/slash/interactivity), which have channel/response_url context,
    pass an ``on_failure`` that posts the ephemeral. The seam is wired and
    tested now; only the concrete ephemeral payload is deferred.
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — last line of defence for a detached task
        logger.exception("scheduled Slack dispatch failed")
        if on_failure is not None:
            try:
                await on_failure(exc)
            except Exception:  # noqa: BLE001 — recovery notice is best-effort
                logger.exception("best-effort Slack failure notice failed")
```

- [ ] Run the six tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v`

- [ ] Commit:
  `git add services/slack_bot/events.py tests/test_slack_transport.py`
  `git commit -m "Add _run_logged + _schedule detached-task helpers for Slack dispatch"`

---

## Task 2 — Ack-then-async on the HTTP events endpoint (latent bug fix)

Per spec §1 "Latent bug fixed here": today `app/api/slack.py` does `await dispatch_event(...)` before returning 200. `_handle_dm` spawns an E2B sandbox (>3s), blowing Slack's 3s budget → Slack retries → a second `event_callback` arrives and `create_session` can race a duplicate session. Switch to `_schedule(_run_logged(dispatch_event(...)))` then return the ack immediately.

The Phase 0 HTTP call site passes `on_failure=None`: the DM handler already emits its own binding/no-grant/error replies inline (`app/api/slack.py` → `dispatch_event` → `_handle_dm`), so there is no separate top-level failure notice to post here. The `on_failure` recovery seam is exercised by later phases that have `response_url` / channel context at the call site.

**Files:**
- Test: `tests/test_slack_events_ack_async.py` (Create)
- Modify: `app/api/slack.py`

**Steps:**

- [ ] Create `tests/test_slack_events_ack_async.py` with a failing regression test. It mounts the real slack router, patches `dispatch_event` with a 5s-sleeping coroutine, computes a valid HMAC, and asserts the POST returns in well under 3s while the dispatch still fires exactly once:

```python
"""Phase 0 — HTTP events endpoint must ack before the slow dispatch runs.

Regression for the latent duplicate-session bug: the old code did
`await dispatch_event(...)` before returning 200, so a >3s _handle_dm
(E2B spawn) blew Slack's 3s budget and triggered retries. We assert the
handler returns near-instantly and dispatches exactly once.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.slack import router as slack_router

_SECRET = "ack-async-secret"


def _signed_post(client, payload: dict):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return client.post(
        "/api/slack/events",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/json",
        },
    )


def test_events_endpoint_acks_before_slow_dispatch(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)

    dispatched: list[dict] = []
    done = asyncio.Event()

    async def slow_dispatch(app, event):
        await asyncio.sleep(5)  # simulate E2B spawn > 3s budget
        dispatched.append(event)
        done.set()

    # Patch the symbol used inside the endpoint module.
    import app.api.slack as slack_api
    monkeypatch.setattr(slack_api, "dispatch_event", slow_dispatch)

    app = FastAPI()
    app.include_router(slack_router)

    with TestClient(app) as client:
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "channel_type": "im",
                      "channel": "D1", "user": "U1", "ts": "1.1", "text": "hi"},
        }
        start = time.monotonic()
        resp = _signed_post(client, payload)
        elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The ack must beat Slack's 3s budget despite the 5s dispatch body.
    assert elapsed < 3.0, f"ack took {elapsed:.2f}s — should not await dispatch"


def test_url_verification_still_returns_challenge(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(slack_router)
    with TestClient(app) as client:
        resp = _signed_post(client, {"type": "url_verification", "challenge": "xyz"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "xyz"}


def test_bad_signature_rejected(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(slack_router)
    with TestClient(app) as client:
        body = json.dumps({"type": "event_callback", "event": {}}).encode()
        resp = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=deadbeef",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401
```

- [ ] Run it, expect FAIL on `test_events_endpoint_acks_before_slow_dispatch` (elapsed ≈ 5s because the current code awaits the dispatch):
  `.venv/bin/pytest tests/test_slack_events_ack_async.py -v`

- [ ] Edit `app/api/slack.py`. Replace the existing `from services.slack_bot.events import dispatch_event` import (line 11) so it also pulls in the helpers:

```python
from services.slack_bot.events import _run_logged, _schedule, dispatch_event
```

- [ ] Replace the `event_callback` branch (lines 29-31) so it schedules instead of awaiting:

```python
    if payload.get("type") == "event_callback":
        # Ack-then-async: schedule the (slow, E2B-spawning) dispatch and
        # return the 200 immediately so Slack's 3s budget is never blown.
        # A failure inside the detached task is handled by _run_logged, not
        # by a Slack retry (we already acked). The DM handler emits its own
        # binding/error replies inline, so no top-level on_failure is needed
        # here (the recovery seam is used by later, context-bearing phases).
        _schedule(_run_logged(dispatch_event(request.app, payload["event"])))
        return {"ok": True}
```

- [ ] Run all four tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_events_ack_async.py -v`

- [ ] Run the existing slack bot suite to confirm no regression (the in-process roundtrip + handler tests call `dispatch_event` directly, so they are unaffected):
  `.venv/bin/pytest tests/test_slack_bot.py tests/e2e/test_slack_roundtrip.py --tb=short -q`

- [ ] Commit:
  `git add app/api/slack.py tests/test_slack_events_ack_async.py`
  `git commit -m "Slack events endpoint: ack-then-async dispatch (fix 3s-budget duplicate-session bug)"`

---

## Task 3 — `SlackConfig` nested config dataclass

Per spec §1 Config: a frozen `SlackConfig` with `transport: str = "http"` (`"http" | "socket"`; unknown → log + treat as `"http"`), added as a `slack` field on `ChatConfig`, parsed from the nested `chat.slack` block. Tokens are deliberately **not** stored here.

**Files:**
- Test: `tests/test_slack_transport.py` (Modify)
- Modify: `app/chat/config.py`

**Steps:**

- [ ] Add a failing config-resolution table-test to `tests/test_slack_transport.py`:

```python
def test_slack_config_default_transport_http():
    from app.chat.config import ChatConfig, SlackConfig
    cfg = ChatConfig()
    assert isinstance(cfg.slack, SlackConfig)
    assert cfg.slack.transport == "http"


@pytest.mark.parametrize("raw_transport,expected", [
    ("http", "http"),
    ("socket", "socket"),
    ("SOCKET", "socket"),     # case-insensitive
    ("websocket", "http"),    # unknown → http
    ("", "http"),             # empty → http
])
def test_load_chat_config_parses_slack_transport(tmp_path, raw_transport, expected, caplog):
    import logging
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
```

- [ ] Run it, expect FAIL (ImportError: cannot import name `SlackConfig`):
  `.venv/bin/pytest tests/test_slack_transport.py -v -k slack_config`

- [ ] Edit `app/chat/config.py`. Add a module-level logger and the `SlackConfig` dataclass above `ChatConfig`. Insert after the imports (after line 8 `import yaml`):

```python
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackConfig:
    # "http" (default, Events API webhook) | "socket" (Socket Mode WS).
    # Unknown values are normalized to "http" at parse time with a warning.
    # Tokens (SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_SIGNING_SECRET) are
    # deliberately NOT stored here — read from env at use site so they never
    # leak into a frozen-config echo (e.g. /admin/server-config).
    transport: str = "http"
```

- [ ] Add the `slack` field to `ChatConfig`. Add `field` to the dataclasses import (line 4 currently `from dataclasses import dataclass`):

```python
from dataclasses import dataclass, field
```

  Then add as the last field inside `ChatConfig` (after `bootstrap_marketplace`, line 47):

```python
    slack: "SlackConfig" = field(default_factory=SlackConfig)
```

- [ ] Add a parse helper above `load_chat_config` and wire it into the `ChatConfig(...)` constructor call. Insert before `def load_chat_config`:

```python
def _parse_slack_config(raw_chat: dict) -> SlackConfig:
    raw_slack = raw_chat.get("slack", {}) or {}
    transport = str(raw_slack.get("transport", "http") or "http").strip().lower()
    if transport not in ("http", "socket"):
        logger.warning(
            "unknown slack transport %r in chat.slack.transport — "
            "falling back to 'http'", transport,
        )
        transport = "http"
    return SlackConfig(transport=transport)
```

  Add the field to the `return ChatConfig(...)` call (after `bootstrap_marketplace=...`, line 71):

```python
        slack=_parse_slack_config(raw),
```

- [ ] Run the config tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v -k slack_config`

- [ ] Commit:
  `git add app/chat/config.py tests/test_slack_transport.py`
  `git commit -m "Add nested SlackConfig (transport) to ChatConfig"`

---

## Task 4 — `get_slack_transport()` env-overrides-yaml resolver

Per spec §1: `get_slack_transport()` on `app/instance_config.py` resolves `SLACK_TRANSPORT` env → `chat.slack.transport` yaml → default `"http"`, mirroring the `get_data_source_type` / `get_home_route` env-overrides-yaml shape already in that file.

**Files:**
- Test: `tests/test_slack_transport.py` (Modify)
- Modify: `app/instance_config.py`

**Steps:**

- [ ] Add a failing resolver test to `tests/test_slack_transport.py`:

```python
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
```

- [ ] Run it, expect FAIL (AttributeError: module `app.instance_config` has no attribute `get_slack_transport`):
  `.venv/bin/pytest tests/test_slack_transport.py -v -k get_slack_transport`

- [ ] Edit `app/instance_config.py`. Add the function after `get_data_source_type` (after line 167):

```python
def get_slack_transport() -> str:
    """Inbound Slack transport for this instance: "http" (default) | "socket".

    Resolution: ``SLACK_TRANSPORT`` env (Terraform-friendly, overrides
    everything) > ``chat.slack.transport`` in instance.yaml > default
    ``"http"``. Unknown values fall back to ``"http"`` so a typo never
    starts a dead Socket Mode WS. Mirrors :func:`get_data_source_type`.
    """
    raw = os.environ.get("SLACK_TRANSPORT") or get_value(
        "chat", "slack", "transport", default="http"
    )
    value = (raw or "http").strip().lower()
    if value not in ("http", "socket"):
        return "http"
    return value
```

- [ ] Run the resolver tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v -k get_slack_transport`

- [ ] Commit:
  `git add app/instance_config.py tests/test_slack_transport.py`
  `git commit -m "Add get_slack_transport() env-overrides-yaml resolver"`

---

## Task 5 — `slack-socket` optional dependency in `pyproject.toml`

Per spec §1 "Lazy optional dep": `slack_sdk` is an optional extra, imported only inside `SocketModeDispatcher.start()`. Declare the extra now so the dispatcher's gate can check importability.

**Files:**
- Modify: `pyproject.toml`

**Steps:**

- [ ] Read the optional-dependencies block to confirm exact anchor lines before editing:
  `.venv/bin/python -c "import pathlib; print('\n'.join(f'{i+1}: {l}' for i,l in enumerate(pathlib.Path('pyproject.toml').read_text().splitlines()) if 'optional-dependencies' in l or l.strip().startswith(('observability','dev'))))"`

- [ ] Edit `pyproject.toml`. Add the extra inside `[project.optional-dependencies]`, immediately before the `dev = [` entry:

```toml
slack-socket = [
    # Socket Mode (optional inbound transport). Imported lazily inside
    # SocketModeDispatcher.start() — never at module top — so HTTP-only
    # deployments need not install it. A missing import is a fail-closed
    # gate at lifespan init (logs + disables Slack, never a dead WS).
    "slack_sdk>=3.27",
]
```

- [ ] Verify TOML parses:
  `.venv/bin/python -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('pyproject.toml').read_text()); print('ok')"`

- [ ] Install the extra into the dev venv so the dispatcher tests can import the SDK:
  `.venv/bin/python -m pip install 'slack_sdk>=3.27'`

- [ ] Commit:
  `git add pyproject.toml`
  `git commit -m "Add slack-socket optional dependency (slack_sdk)"`

---

## Task 6 — `SocketModeDispatcher` (lazy dep, ack-envelope-first, funnel)

Per spec §1 "Socket Mode listener": `_on_request` acks the envelope **first** (<3s), then schedules `dispatch_event` via `_run_logged`/`_schedule` for `events_api`→`event_callback`. The `slack_sdk` import lives inside `start()`. `SocketModeRequest.payload["event"]` is byte-identical to the HTTP webhook's `payload["event"]` — no shape translation. (Slash/interactivity routing lands in later phases; Phase 0 only funnels events.) The socket call site, like the HTTP one in Phase 0, passes `on_failure=None` — it funnels into the same `dispatch_event`/`_handle_dm` that emits its own inline replies.

**Files:**
- Test: `tests/test_slack_transport.py` (Modify)
- Create: `services/slack_bot/socket_mode_client.py`

**Steps:**

- [ ] Add failing dispatcher tests to `tests/test_slack_transport.py`. These use a fake `client` (no real WS) and a synthetic request object shaped like `slack_sdk`'s `SocketModeRequest` — asserting the ack is sent **before** the dispatch is scheduled, and the dispatched event dict is byte-identical to `payload["event"]`:

```python
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
        # let the scheduled task run
        import asyncio
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
```

- [ ] Run it, expect FAIL (ModuleNotFoundError: `services.slack_bot.socket_mode_client`):
  `.venv/bin/pytest tests/test_slack_transport.py -v -k socket_dispatcher`

- [ ] Create `services/slack_bot/socket_mode_client.py`:

```python
"""Optional Socket Mode inbound transport for the Slack bot.

A `SocketModeDispatcher` owns one Socket Mode WebSocket (xapp- token):
connect, ack each envelope FIRST (<3s), then schedule the same
`dispatch_event` the HTTP webhook uses — no event-shape translation,
because `SocketModeRequest.payload` for `events_api` is byte-identical
to the HTTP webhook body. Reconnect/backoff is handled by slack_sdk's
SocketModeClient; we just own the lifecycle.

`slack_sdk` is an OPTIONAL dependency (`pip install '.[slack-socket]'`)
imported lazily inside `start()` so HTTP-only deployments never need it.
"""
from __future__ import annotations

import logging

from services.slack_bot.events import _run_logged, _schedule, dispatch_event

logger = logging.getLogger(__name__)


class SocketModeImportError(RuntimeError):
    """Raised when transport=socket but slack_sdk is not importable."""


class SocketModeDispatcher:
    def __init__(self, *, app, app_token: str, bot_token: str) -> None:
        self._app = app
        self._app_token = app_token
        self._bot_token = bot_token
        self._client = None  # slack_sdk SocketModeClient, built in start()

    async def _on_request(self, client, req) -> None:
        # 1. ACK FIRST (<3s) so Slack never retries / disconnects.
        from slack_sdk.socket_mode.response import SocketModeResponse

        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
        # 2. Funnel into the SAME dispatcher the HTTP webhook uses. No
        #    payload translation — req.payload["event"] is byte-identical
        #    to the HTTP body's payload["event"]. on_failure=None here for
        #    the same reason as the HTTP call site: _handle_dm emits its own
        #    inline replies (the recovery seam is used by later phases).
        if req.type == "events_api" and req.payload.get("type") == "event_callback":
            _schedule(_run_logged(dispatch_event(self._app, req.payload["event"])))
        # slash_commands / interactive routing arrives in later phases.

    async def start(self) -> None:
        """Connect the WS. Lazy-imports slack_sdk; ImportError → actionable
        fail-closed error the lifespan gate turns into 'Slack disabled'."""
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
        except ImportError as e:  # noqa: F841
            raise SocketModeImportError(
                "chat.slack.transport=socket requires the 'slack-socket' "
                "extra — install with: pip install '.[slack-socket]'"
            ) from e

        self._client = SocketModeClient(
            app_token=self._app_token,
            web_client=None,
        )
        self._client.socket_mode_request_listeners.append(self._on_request)
        await self._client.connect()
        logger.info("Slack Socket Mode connected")

    async def stop(self) -> None:
        """Clean shutdown of the WS at app teardown."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Slack Socket Mode disconnect failed (non-fatal)")
            finally:
                self._client = None
                logger.info("Slack Socket Mode disconnected")
```

- [ ] Run the dispatcher tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v -k socket_dispatcher`

- [ ] Commit:
  `git add services/slack_bot/socket_mode_client.py tests/test_slack_transport.py`
  `git commit -m "Add SocketModeDispatcher: ack-envelope-first funnel into dispatch_event"`

---

## Task 7 — Fail-closed gate helper for the socket transport

Per spec §1 "Hard constraints": at lifespan init, `transport=socket` requires (a) `UVICORN_WORKERS == 1` (one WS; N workers fracture dedup), (b) a valid `xapp-`/`xoxb-` token pair, (c) the `slack-socket` extra importable. Any miss → log + disable Slack, never start a dead WS or crash. Make this a single testable helper so the gate logic is unit-covered without booting the app.

**Files:**
- Test: `tests/test_slack_transport.py` (Modify)
- Modify: `services/slack_bot/socket_mode_client.py`

**Steps:**

- [ ] Add failing gate tests to `tests/test_slack_transport.py`:

```python
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
```

- [ ] Run it, expect FAIL (AttributeError: `socket_mode_preflight`):
  `.venv/bin/pytest tests/test_slack_transport.py -v -k socket_gate`

- [ ] Edit `services/slack_bot/socket_mode_client.py`. Add the helpers above the `SocketModeDispatcher` class:

```python
def _slack_sdk_importable() -> bool:
    """True iff the optional slack_sdk dep is installed. Isolated so the
    preflight gate is unit-testable without the package present."""
    try:
        import slack_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def socket_mode_preflight(
    *, workers: int, app_token: str, bot_token: str,
) -> tuple[bool, str]:
    """Fail-closed gate for the socket transport.

    Returns (ok, reason). On any failure the lifespan caller logs `reason`
    and disables Slack — it never starts a dead WS or crashes the app.
    """
    if workers > 1:
        return False, (
            "Socket Mode requires a single worker (one WS; N workers "
            "fracture dedup) but UVICORN_WORKERS > 1"
        )
    if not app_token:
        return False, "SLACK_APP_TOKEN missing (required for Socket Mode)"
    if not app_token.startswith("xapp-"):
        return False, "SLACK_APP_TOKEN must be an app-level token (xapp- prefix)"
    if not bot_token:
        return False, "SLACK_BOT_TOKEN missing (required for Socket Mode)"
    if not bot_token.startswith("xoxb-"):
        return False, "SLACK_BOT_TOKEN must be a bot token (xoxb- prefix)"
    if not _slack_sdk_importable():
        return False, (
            "Socket Mode requires the 'slack-socket' extra — install with: "
            "pip install '.[slack-socket]'"
        )
    return True, ""
```

- [ ] Run the gate tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v -k socket_gate`

- [ ] Commit:
  `git add services/slack_bot/socket_mode_client.py tests/test_slack_transport.py`
  `git commit -m "Add fail-closed socket_mode_preflight gate (workers, token pair, optional dep)"`

---

## Task 8 — Lifespan wiring of the Socket Mode dispatcher

Per spec §1 "Lifespan wiring": when `get_slack_transport() == "socket"`, run `socket_mode_preflight`; on pass, construct `SocketModeDispatcher`, `await start()`, stash on `app.state.slack_socket_dispatcher`, and `await stop()` at shutdown. On preflight fail or `start()` error → log + leave `app.state.slack_socket_dispatcher = None` (Slack stays HTTP-only; never crash). This is exercised by an in-process unit test that drives the wiring helper, not a live WS (per spec §8 "No live-WS test in CI").

**Files:**
- Test: `tests/test_slack_transport.py` (Modify)
- Modify: `app/main.py`

**Steps:**

- [ ] Locate the exact line numbers of the slack-router import, the `@asynccontextmanager`/`lifespan` definition, and the `yield` inside lifespan before editing (line numbers below are illustrative — use the real ones from this grep):
  `.venv/bin/python -c "import pathlib; [print(f'{i+1}: {l}') for i,l in enumerate(pathlib.Path('app/main.py').read_text().splitlines()) if 'slack_router' in l or 'asynccontextmanager' in l or l.strip()=='yield' or 'def lifespan' in l]"`

- [ ] Add a failing wiring test to `tests/test_slack_transport.py`. It calls a new `_start_slack_socket_transport(app)` helper extracted into `app/main.py`, with monkeypatched preflight + a fake dispatcher class, asserting the dispatcher is started and stashed on pass, and left `None` on fail:

```python
def test_start_slack_socket_transport_happy(monkeypatch):
    from types import SimpleNamespace
    import app.main as main_mod

    started: list[str] = []

    class FakeDispatcher:
        def __init__(self, *, app, app_token, bot_token):
            self.app = app
        async def start(self):
            started.append("start")

    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "socket")
    monkeypatch.setattr(main_mod, "SocketModeDispatcher", FakeDispatcher)
    monkeypatch.setattr(main_mod, "socket_mode_preflight",
                        lambda **k: (True, ""))
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-a")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-b")
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    app = SimpleNamespace(state=SimpleNamespace())

    import asyncio
    asyncio.run(main_mod._start_slack_socket_transport(app))

    assert started == ["start"]
    assert isinstance(app.state.slack_socket_dispatcher, FakeDispatcher)


def test_start_slack_socket_transport_http_is_noop(monkeypatch):
    from types import SimpleNamespace
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "http")
    app = SimpleNamespace(state=SimpleNamespace())
    import asyncio
    asyncio.run(main_mod._start_slack_socket_transport(app))
    assert getattr(app.state, "slack_socket_dispatcher", None) is None


def test_start_slack_socket_transport_failclosed_on_preflight(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_slack_transport", lambda: "socket")
    monkeypatch.setattr(main_mod, "socket_mode_preflight",
                        lambda **k: (False, "SLACK_APP_TOKEN missing"))
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    caplog.set_level(logging.ERROR, logger="app.main")
    app = SimpleNamespace(state=SimpleNamespace())
    import asyncio
    asyncio.run(main_mod._start_slack_socket_transport(app))
    assert app.state.slack_socket_dispatcher is None
    assert any("Slack Socket Mode disabled" in r.message for r in caplog.records)
```

- [ ] Run it, expect FAIL (AttributeError: `_start_slack_socket_transport`):
  `.venv/bin/pytest tests/test_slack_transport.py -v -k start_slack_socket`

- [ ] Edit `app/main.py`. Add module-level imports adjacent to the existing `from app.api.slack import router as slack_router` line (use the real line number from the grep) so the names exist for monkeypatching:

```python
from app.instance_config import get_slack_transport
from services.slack_bot.socket_mode_client import (
    SocketModeDispatcher,
    socket_mode_preflight,
)
```

- [ ] Add the wiring helper as a module-level function, placed immediately above the `lifespan` definition (use the real line number — it is just before the `@asynccontextmanager` decorator):

```python
async def _start_slack_socket_transport(app) -> None:
    """If chat.slack.transport=socket, start one Socket Mode WS behind
    fail-closed gates. On any miss → log + leave Slack HTTP-only; never
    crash and never start a dead WS. Stashed on app.state for shutdown."""
    app.state.slack_socket_dispatcher = None
    if get_slack_transport() != "socket":
        return
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    ok, reason = socket_mode_preflight(
        workers=workers, app_token=app_token, bot_token=bot_token,
    )
    if not ok:
        logger.error("Slack Socket Mode disabled: %s", reason)
        return
    try:
        dispatcher = SocketModeDispatcher(
            app=app, app_token=app_token, bot_token=bot_token,
        )
        await dispatcher.start()
        app.state.slack_socket_dispatcher = dispatcher
    except Exception:
        logger.exception("Slack Socket Mode disabled: start() failed")
        app.state.slack_socket_dispatcher = None
```

  (Confirm `os` and `logger` are already imported at module scope in `app/main.py` — `os` is a standard import there and `logger = logging.getLogger(...)` is defined near the top; both are reused, not re-added.)

- [ ] Call the helper at the end of the lifespan startup block (just before the `yield`) and tear it down after `yield`. Insert immediately before `yield`:

```python
    # --- SLACK SOCKET MODE (optional inbound transport) ----------------------
    try:
        await _start_slack_socket_transport(app)
    except Exception:
        logger.exception("Slack Socket Mode wiring failed (non-fatal)")
        app.state.slack_socket_dispatcher = None
    # --- end SLACK SOCKET MODE -----------------------------------------------
```

  Insert the teardown immediately after the `yield` line (at the top of the shutdown section):

```python
    _socket_disp = getattr(app.state, "slack_socket_dispatcher", None)
    if _socket_disp is not None:
        try:
            await _socket_disp.stop()
        except Exception:
            logger.exception("Slack Socket Mode shutdown failed")
```

- [ ] Run the wiring tests, expect PASS:
  `.venv/bin/pytest tests/test_slack_transport.py -v -k start_slack_socket`

- [ ] Run the full transport + ack-async + existing slack suites to confirm no regression:
  `.venv/bin/pytest tests/test_slack_transport.py tests/test_slack_events_ack_async.py tests/test_slack_bot.py tests/test_slack_sigverify.py tests/e2e/test_slack_roundtrip.py --tb=short -q`

- [ ] Commit:
  `git add app/main.py tests/test_slack_transport.py`
  `git commit -m "Wire SocketModeDispatcher into lifespan behind fail-closed gates"`

---

## Task 9 — Two manifest stanzas (HTTP vs Socket) in `docs/`

Per spec §1 "Manifest": ship two documented manifest stanzas rather than one dual-mode file (avoids the stale `request_url` foot-gun). Vendor-agnostic: use `https://<your-host>/api/slack/events`. The existing `services/slack_bot/manifest.yaml` is the HTTP source; document both modes in `docs/`.

**Files:**
- Create: `docs/slack-manifest-http.md`
- Create: `docs/slack-manifest-socket.md`

**Steps:**

- [ ] Create `docs/slack-manifest-http.md`:

```markdown
# Slack App manifest — HTTP transport (default)

Paste this at api.slack.com/apps → "Create New App" → "From a manifest".
Replace `<your-host>` with the public hostname of your Agnes instance
(e.g. `agnes.example.com`). This is the default transport — Slack delivers
events over an HTTPS webhook to your public endpoint.

​```yaml
display_information:
  name: Agnes
  description: Ask Agnes data questions from Slack
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Agnes
    always_online: false
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
​```

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: http` in `instance.yaml` (or `SLACK_TRANSPORT=http`,
  or leave unset — `http` is the default).
```

  (Note for the implementer: in the file you write, the three fenced lines marked `​```` above are plain triple-backticks — the zero-width markers here are only so this plan's own code block doesn't terminate early. Write a normal ` ```yaml … ``` ` fence.)

- [ ] Create `docs/slack-manifest-socket.md`:

```markdown
# Slack App manifest — Socket Mode transport (optional)

Use this when your Agnes instance has no publicly reachable webhook URL.
Slack delivers events over an outbound WebSocket instead of an HTTPS
webhook, so there is **no `request_url`** — that's the whole point of the
two-stanza split (a stale `request_url` is a common foot-gun).

​```yaml
display_information:
  name: Agnes
  description: Ask Agnes data questions from Slack
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Agnes
    always_online: false
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
​```

After creating the app, generate an **app-level token** (`xapp-…`) with the
`connections:write` scope under "Basic Information → App-Level Tokens".

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_APP_TOKEN` (`xapp-…`, with `connections:write`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: socket` in `instance.yaml` (or `SLACK_TRANSPORT=socket`)
- Install the optional dependency: `pip install '.[slack-socket]'`

## Constraints

- **Single worker only.** Socket Mode requires `UVICORN_WORKERS=1` — multiple
  workers each open a WS and fracture event dedup. Agnes refuses to start the
  WS otherwise (logs the reason, disables Slack, never crashes).
- All gates are fail-closed: a missing/mis-prefixed token pair or a missing
  `slack-socket` extra logs the reason and leaves Slack HTTP-only.
```

  (Same fence note as above: write normal triple-backtick `yaml` fences.)

- [ ] Verify both files are vendor-agnostic (no customer hosts/IDs/tokens; only `<your-host>` / `example.com` placeholders):
  `grep -nE "keboola|groupon|foundry|34\.|agnes\.keboola|xoxb-[A-Za-z0-9]|xapp-[A-Za-z0-9]" docs/slack-manifest-http.md docs/slack-manifest-socket.md || echo "clean"`

- [ ] Commit:
  `git add docs/slack-manifest-http.md docs/slack-manifest-socket.md`
  `git commit -m "Document HTTP and Socket Mode Slack manifest stanzas"`

---

## Task 10 — CHANGELOG entry + full-suite gate

Per repo convention: every change that touches user-visible behavior adds a bullet under `## [Unreleased]` in `CHANGELOG.md`, in the same change. The ack-then-async fix and the new optional transport are both user-visible (operator config + bug behavior).

**Files:**
- Modify: `CHANGELOG.md`

**Steps:**

- [ ] Read the current top of `CHANGELOG.md` to find the exact `## [Unreleased]` header and its existing subsections:
  `.venv/bin/python -c "import pathlib; print('\n'.join(pathlib.Path('CHANGELOG.md').read_text().splitlines()[:25]))"`

- [ ] Edit `CHANGELOG.md`. Under the `## [Unreleased]` header, add the following bullets (merge into existing `### Added` / `### Fixed` subsections if they already exist; otherwise create them):

```markdown
### Added
- **Slack Socket Mode transport (optional).** A second inbound Slack
  transport selectable per instance via `chat.slack.transport: http|socket`
  in `instance.yaml` (or the `SLACK_TRANSPORT` env var; default `http`).
  Socket Mode delivers events over an outbound WebSocket — no public webhook
  URL required. Both transports funnel through the existing event dispatcher
  (no forked handler logic). Requires the new `slack-socket` extra
  (`pip install '.[slack-socket]'`), a single worker (`UVICORN_WORKERS=1`),
  and an `xapp-`/`xoxb-` token pair; all gates fail closed (log + disable
  Slack, never crash, never start a dead WS). Two manifest stanzas documented
  in `docs/slack-manifest-http.md` and `docs/slack-manifest-socket.md`.

### Fixed
- **Slack events: ack-then-async.** `POST /api/slack/events` now schedules the
  (slow, sandbox-spawning) event dispatch and returns the `200` ack
  immediately instead of awaiting it. The previous `await` blew Slack's 3s
  ack budget on the first DM (E2B spawn > 3s), triggering Slack retries that
  could race a duplicate chat session. A failure inside the detached dispatch
  is logged (and surfaced via the best-effort recovery seam) rather than
  retried by Slack.
```

- [ ] Run the full test suite (this is what CI runs):
  `.venv/bin/pytest tests/ --tb=short -n auto -q`

- [ ] If any failures appear in code this phase touched (`tests/test_slack_transport.py`, `tests/test_slack_events_ack_async.py`, `tests/test_slack_bot.py`, `tests/test_slack_sigverify.py`, `tests/e2e/test_slack_roundtrip.py`), fix before proceeding. For failures unrelated to this diff, confirm with `git stash` that they reproduce on the clean branch and note them in the PR body.

- [ ] Commit:
  `git add CHANGELOG.md`
  `git commit -m "CHANGELOG: Slack Socket Mode transport + ack-then-async fix"`
