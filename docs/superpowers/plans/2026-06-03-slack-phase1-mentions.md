# Phase 1 — Channel mentions + threads + SLACK_CHANNEL allowlist Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** A bound, chat-authorized Slack user who `@agnes`-mentions the bot in an allowlisted channel gets a public in-thread reply backed by a persistent `Surface.SLACK_THREAD` session owned by the mention starter; the per-channel allowlist is a `ResourceType.SLACK_CHANNEL` registry entry (default-deny, no DB migration).

**Architecture:** A new `ResourceType.SLACK_CHANNEL` (registry-only) makes each channel grantable as `(Everyone, slack_channel, <channel_id>)`. A direct-grant allowlist helper `is_channel_allowlisted` deliberately bypasses `can_access` so the Admin god-mode short-circuit can never auto-open a channel. The current stub `_handle_mention` is replaced with a full flow (loop-guard → allowlist → binding → CHAT grant → thread-session create/reuse-or-reject → attach sink → send message), reusing the existing `ChatManager` Slack-thread dedup and `SlackSinkBridge`.

**Tech Stack:** Python 3.11 (FastAPI, DuckDB + Postgres parity layer), `httpx` for outbound Slack Web API, pytest (`-n auto`).

---

## File Structure

**Modified**
- `app/resource_types.py` — add `SLACK_CHANNEL` enum member, `_slack_channel_blocks` projection (grant rows *are* the allowlist), and its `ResourceTypeSpec` registry entry.
- `services/slack_bot/binding.py` — add `is_channel_allowlisted(conn, channel_id)` (direct `resource_grants` lookup scoped to the `Everyone` group; no `can_access`).
- `services/slack_bot/events.py` — replace the stub `_handle_mention`; add `_strip_bot_mention(text, bot_user_id)`.
- `services/slack_bot/sender.py` — add `send_ephemeral_to_user(channel, slack_user_id, text)` (calls `chat.postEphemeral`). **Named `_to_user` (not `send_ephemeral`) on purpose:** Phase 2 (slash, spec line 189) adds a different `send_ephemeral(response_url, text, blocks=None)` to this same module; the two signatures are incompatible, so Phase 1 must not squat the bare name.
- `services/slack_bot/sink.py` — `SlackSinkBridge.__init__` gains optional `chat_id` (forward-compat for Phase 3; defaulted, no behavior change).
- `app/main.py` — resolve and stash `app.state.slack_bot_user_id` once at startup via Slack `auth.test`.
- `CHANGELOG.md` — `[Unreleased]` bullet.

**Created**
- `services/slack_bot/identity.py` — `async def resolve_bot_user_id() -> str | None` (single `auth.test` call; isolated for unit-testability).

---

## Task 1 — `ResourceType.SLACK_CHANNEL` registry entry

**Files:**
- Modify: `app/resource_types.py`
- Test: `tests/test_resource_types.py`

The allowlist has **no domain table** — the grant rows themselves are the allowlist, so `_slack_channel_blocks` projects `DISTINCT resource_id FROM resource_grants WHERE resource_type='slack_channel'`. Admins add a channel by pasting its id into the create-grant form. The `system_conn` fixture (defined in `tests/test_resource_types.py`, backed by `seeded_app`) gives a fully-migrated system DB with the seeded `Everyone`/`Admin` groups.

- [ ] Write a failing test. Append to `tests/test_resource_types.py`:
```python
class TestSlackChannelBlocks:
    def test_enum_member_and_spec_registered(self):
        from app.resource_types import RESOURCE_TYPES, ResourceType
        assert ResourceType.SLACK_CHANNEL.value == "slack_channel"
        spec = RESOURCE_TYPES[ResourceType.SLACK_CHANNEL]
        assert spec.display_name == "Slack channels"
        assert spec.id_format == "<channel_id>"

    def test_in_enabled_resource_types(self):
        from app.resource_types import enabled_resource_types, ResourceType
        keys = {s.key for s in enabled_resource_types()}
        assert ResourceType.SLACK_CHANNEL in keys

    def test_projects_seeded_grant(self, system_conn):
        from app.resource_types import _slack_channel_blocks
        gid = system_conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Everyone'"
        ).fetchone()[0]
        system_conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_sc1', ?, 'slack_channel', 'C123')",
            [gid],
        )
        blocks = _slack_channel_blocks(system_conn)
        items = [it for b in blocks for it in b["items"]]
        assert any(it["resource_id"] == "C123" for it in items)

    def test_empty_when_no_grants(self, system_conn):
        from app.resource_types import _slack_channel_blocks
        system_conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = 'slack_channel'"
        )
        assert _slack_channel_blocks(system_conn) == []
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_resource_types.py::TestSlackChannelBlocks -v` → fails with `AttributeError: SLACK_CHANNEL` (enum member missing).
- [ ] Add the enum member. In `app/resource_types.py`, in `class ResourceType(StrEnum)`, after `CHAT = "chat"` (line 50):
```python
    SLACK_CHANNEL = "slack_channel"
```
- [ ] Add the projection delegate. In `app/resource_types.py`, after `_chat_blocks` (after line 372, before the `# Registry` divider comment at line 375):
```python
# ---------------------------------------------------------------------------
# Slack channel allowlist projection
# ---------------------------------------------------------------------------


def _slack_channel_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project the per-channel mention allowlist.

    There is **no domain table** — the ``resource_grants`` rows themselves are
    the allowlist. An admin enables Agnes in a channel by pasting its channel
    id (e.g. ``C0123ABCD``) into the create-grant form on /admin/access; that
    writes ``(Everyone, slack_channel, <channel_id>)``. We project the distinct
    granted channel ids so the admin UI can list what is currently enabled.
    Empty allowlist → no block (default-deny).
    """
    rows = conn.execute(
        """SELECT DISTINCT resource_id
           FROM resource_grants
           WHERE resource_type = 'slack_channel'
           ORDER BY resource_id"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "slack_channels",
        "name": "Slack channels",
        "items": [
            {
                "resource_id": r[0],
                "name": r[0],
                "category": "slack_channel",
                "description": "Channel where @agnes mentions are answered.",
            }
            for r in rows
        ],
    }]
```
- [ ] Register the spec. In `app/resource_types.py`, in the `RESOURCE_TYPES` dict, after the `ResourceType.CHAT` entry (after line 442, before the closing `}` on line 443):
```python
    ResourceType.SLACK_CHANNEL: ResourceTypeSpec(
        key=ResourceType.SLACK_CHANNEL,
        display_name="Slack channels",
        description=(
            "A Slack channel where @agnes mentions are answered. Grant "
            "(Everyone, slack_channel, <channel_id>) to enable Agnes there; "
            "with no grant the channel is silent (default-deny)."
        ),
        id_format="<channel_id>",
        list_blocks=_slack_channel_blocks,
    ),
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_resource_types.py::TestSlackChannelBlocks -v`.
- [ ] Commit: `git add app/resource_types.py tests/test_resource_types.py && git commit -m "Add ResourceType.SLACK_CHANNEL registry entry for mention allowlist"`

---

## Task 2 — `is_channel_allowlisted` (direct-grant, no admin short-circuit)

**Files:**
- Modify: `services/slack_bot/binding.py`
- Test: `tests/test_slack_bot.py`

Security-critical: the check is a direct `resource_grants` lookup scoped to the **Everyone** group. It must **not** call `can_access`, because `can_access` returns `True` for any admin — which would let an admin's mention make Agnes post in any channel they happen to be in. Channel openness is a property of the channel, not of a user group. The `conn` fixture (line 22 of `tests/test_slack_bot.py`) runs `_ensure_schema`, which seeds the `Everyone` and `Admin` groups.

- [ ] Write a failing test. Append to `tests/test_slack_bot.py`:
```python
class TestChannelAllowlist:
    def _everyone_gid(self, conn):
        return conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Everyone'"
        ).fetchone()[0]

    def test_default_deny(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted
        assert is_channel_allowlisted(conn, "C_NEW") is False

    def test_true_after_everyone_grant(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted
        gid = self._everyone_gid(conn)
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_a', ?, 'slack_channel', 'C_OK')",
            [gid],
        )
        assert is_channel_allowlisted(conn, "C_OK") is True

    def test_admin_grant_does_not_open_channel(self, conn):
        """A grant to the Admin group (not Everyone) must NOT allowlist —
        proves we do not use can_access (no admin short-circuit)."""
        from services.slack_bot.binding import is_channel_allowlisted
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_admin', ?, 'slack_channel', 'C_ADMIN')",
            [admin_gid],
        )
        assert is_channel_allowlisted(conn, "C_ADMIN") is False
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_slack_bot.py::TestChannelAllowlist -v` → `ImportError: cannot import name 'is_channel_allowlisted'`.
- [ ] Implement the helper. In `services/slack_bot/binding.py`, after `lookup_user_email` (after line 67):
```python
def is_channel_allowlisted(conn: duckdb.DuckDBPyConnection, channel_id: str) -> bool:
    """True iff the Everyone group holds (slack_channel, channel_id).

    Direct grant lookup — deliberately does NOT use ``can_access`` so the
    Admin god-mode short-circuit cannot auto-open a channel. Channel openness
    is a property of the channel (an Everyone grant), not of the mentioning
    user's group. Default-deny: no grant → False.
    """
    row = conn.execute(
        """SELECT 1
           FROM resource_grants rg
           JOIN user_groups ug ON ug.id = rg.group_id
           WHERE ug.name = 'Everyone'
             AND rg.resource_type = 'slack_channel'
             AND rg.resource_id = ?
           LIMIT 1""",
        [channel_id],
    ).fetchone()
    return row is not None
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_bot.py::TestChannelAllowlist -v`.
- [ ] Commit: `git add services/slack_bot/binding.py tests/test_slack_bot.py && git commit -m "Add is_channel_allowlisted direct-grant check (no admin short-circuit)"`

---

## Task 3 — `_strip_bot_mention` text cleaner

**Files:**
- Modify: `services/slack_bot/events.py`
- Test: `tests/test_slack_bot.py`

`app_mention` text arrives as e.g. `"<@U07BOT> what is revenue?"`. We strip the bot's own `<@…>` token (resolved at startup, Task 5) and trim, so the runner gets clean prose. The type hint `str | None` is safe at runtime because `events.py` already declares `from __future__ import annotations` (line 2 of the existing file), which stringizes all annotations.

- [ ] Write a failing test. Append to `tests/test_slack_bot.py`:
```python
class TestStripBotMention:
    def test_strips_leading_mention(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("<@U07BOT> what is revenue?", "U07BOT") == "what is revenue?"

    def test_strips_mid_text_mention(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("hey <@U07BOT> hello", "U07BOT") == "hey  hello".strip()

    def test_no_bot_id_returns_trimmed(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("  hello  ", None) == "hello"

    def test_handles_angle_with_label(self):
        from services.slack_bot.events import _strip_bot_mention
        assert _strip_bot_mention("<@U07BOT|agnes> hi", "U07BOT") == "hi"
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_slack_bot.py::TestStripBotMention -v` → `ImportError: cannot import name '_strip_bot_mention'`.
- [ ] Implement. In `services/slack_bot/events.py`, add `import re` to the top imports (after `import logging` on line 5), then add after `_is_attached` (after line 25):
```python
def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the bot's own ``<@ID>`` / ``<@ID|label>`` mention token(s) from
    an app_mention text body and return the trimmed remainder.

    ``bot_user_id`` None (not yet resolved) → just trim — never echo the raw
    ``<@…>`` token into the runner.
    """
    if not text:
        return ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    return text.strip()
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_bot.py::TestStripBotMention -v`.
- [ ] Commit: `git add services/slack_bot/events.py tests/test_slack_bot.py && git commit -m "Add _strip_bot_mention helper for app_mention text"`

---

## Task 4 — `send_ephemeral_to_user` outbound helper

**Files:**
- Modify: `services/slack_bot/sender.py`
- Test: `tests/test_slack_bot.py`

All mention denials are **ephemeral** (visible only to the mentioning user), never public — avoids leaking channel-enablement or thread ownership into the channel. Slack's `chat.postEphemeral` requires `channel` + `user` (the Slack user id) + `text`.

**Naming:** the helper is `send_ephemeral_to_user`, NOT `send_ephemeral`. Spec Section 3 (Phase 2, slash commands — line 189) introduces a different `send_ephemeral(response_url, text, blocks=None)` into this same module that POSTs `{"response_type":"ephemeral",...}` to a `response_url`. The two signatures are incompatible; keeping distinct names prevents a Phase-2 collision.

- [ ] Write a failing test. Append to `tests/test_slack_bot.py`:
```python
class TestSendEphemeralToUser:
    def test_posts_ephemeral_with_user_and_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        captured = {}

        class _FakeResp:
            pass

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return _FakeResp()

        monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _FakeClient)
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))
        assert captured["url"].endswith("/chat.postEphemeral")
        assert captured["json"] == {"channel": "C1", "user": "U1", "text": "nope"}
        assert captured["headers"]["Authorization"] == "Bearer xoxb-test"

    def test_no_token_is_noop(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        # Must not raise even though no HTTP client is patched.
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_slack_bot.py::TestSendEphemeralToUser -v` → `AttributeError: module 'services.slack_bot.sender' has no attribute 'send_ephemeral_to_user'`.
- [ ] Implement. In `services/slack_bot/sender.py`, after `send_thread_reply` (after line 22):
```python
async def send_ephemeral_to_user(channel: str, slack_user_id: str, text: str) -> None:
    """Post an ephemeral message visible only to ``slack_user_id`` in
    ``channel`` via chat.postEphemeral. Used for all mention denials so we
    never leak channel-enablement or thread ownership into the channel.

    Distinct from the Phase-2 ``send_ephemeral(response_url, ...)`` helper —
    that one POSTs to a slash-command response_url, this one calls the Web API.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error("SLACK_BOT_TOKEN missing — cannot post ephemeral")
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://slack.com/api/chat.postEphemeral",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "user": slack_user_id, "text": text},
        )
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_bot.py::TestSendEphemeralToUser -v`.
- [ ] Commit: `git add services/slack_bot/sender.py tests/test_slack_bot.py && git commit -m "Add send_ephemeral_to_user helper (chat.postEphemeral) for mention denials"`

---

## Task 5 — Resolve and stash `slack_bot_user_id` at startup

**Files:**
- Create: `services/slack_bot/identity.py`
- Modify: `app/main.py`
- Test: `tests/test_slack_bot.py`

`_strip_bot_mention` and the bot-loop-guard need the bot's own user id. Resolve it once via Slack `auth.test` at startup and stash on `app.state.slack_bot_user_id`. Isolated in `identity.py` so it is unit-testable without the full lifespan.

- [ ] Write a failing test. Append to `tests/test_slack_bot.py`:
```python
class TestResolveBotUserId:
    def test_returns_user_id_on_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self): return {"ok": True, "user_id": "U07BOT"}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None):
                assert url.endswith("/auth.test")
                return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) == "U07BOT"

    def test_returns_none_on_not_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self): return {"ok": False, "error": "invalid_auth"}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None): return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) is None

    def test_returns_none_without_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert asyncio.run(ident.resolve_bot_user_id()) is None
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_slack_bot.py::TestResolveBotUserId -v` → `ModuleNotFoundError: No module named 'services.slack_bot.identity'`.
- [ ] Create `services/slack_bot/identity.py`:
```python
"""Resolve the bot's own Slack user id once at startup via auth.test.

Stashed on app.state.slack_bot_user_id so the mention loop-guard and
_strip_bot_mention can recognise (and ignore) the bot's own posts.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def resolve_bot_user_id() -> str | None:
    """Return the bot's Slack user id (``user_id`` from auth.test), or None
    if the token is missing or Slack returns ``ok=false``. Never raises —
    a failure just leaves loop-guard/strip in their None-safe fallback."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.warning("SLACK_BOT_TOKEN missing — cannot resolve bot user id")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
    except Exception:
        logger.exception("auth.test failed — bot user id unresolved")
        return None
    if not data.get("ok"):
        logger.warning("auth.test returned ok=false: %s", data.get("error"))
        return None
    return data.get("user_id")
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_bot.py::TestResolveBotUserId -v`.
- [ ] Wire it into startup. Find the chat-init block in the lifespan: `grep -n "CHAT-INIT\|chat_manager\|app.state.chat_repo" app/main.py | head`. Immediately after the chat-init block finishes (after `app.state.chat_manager` / `app.state.chat_repo` are assigned), add:
```python
    # --- SLACK-INIT: resolve bot user id once (mention loop-guard / strip) ---
    app.state.slack_bot_user_id = None
    try:
        from services.slack_bot.identity import resolve_bot_user_id
        app.state.slack_bot_user_id = await resolve_bot_user_id()
        if app.state.slack_bot_user_id:
            logger.info("slack bot user id resolved: %s", app.state.slack_bot_user_id)
    except Exception:
        logger.exception("SLACK-INIT failed (non-fatal); bot user id unresolved")
    # --- end SLACK-INIT ------------------------------------------------------
```
- [ ] Verify the surrounding lifespan block still parses by importing the app: `.venv/bin/python -c "import app.main"` (expect no error), then run the full slack suite: `.venv/bin/pytest tests/test_slack_bot.py -q`.
- [ ] Commit: `git add services/slack_bot/identity.py app/main.py tests/test_slack_bot.py && git commit -m "Resolve and stash slack_bot_user_id at startup"`

---

## Task 6 — `SlackSinkBridge` accepts optional `chat_id`

**Files:**
- Modify: `services/slack_bot/sink.py`
- Test: `tests/test_slack_bot.py`

The mention flow constructs the bridge with `chat_id=session.id`. Phase 3 (Stop-button lifecycle) needs `chat_id` on the bridge; we add it now as an optional kwarg with no behavior change so Phase 1's attach call already passes it. The current `__init__` is `def __init__(self, *, channel: str, thread_ts: str) -> None:` (line 34) setting `self._channel`, `self._thread_ts`, `self._closed`.

- [ ] Write a failing test. Append to `tests/test_slack_bot.py`:
```python
class TestSinkBridgeChatId:
    def test_chat_id_stored_and_optional(self):
        from services.slack_bot.sink import SlackSinkBridge
        b1 = SlackSinkBridge(channel="C1", thread_ts="111.0", chat_id="sess_1")
        assert b1._chat_id == "sess_1"
        b2 = SlackSinkBridge(channel="C1", thread_ts="111.0")
        assert b2._chat_id is None
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_slack_bot.py::TestSinkBridgeChatId -v` → `TypeError: __init__() got an unexpected keyword argument 'chat_id'`.
- [ ] Implement. In `services/slack_bot/sink.py`, replace the `__init__` (lines 34-37):
```python
    def __init__(self, *, channel: str, thread_ts: str, chat_id: str | None = None) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._chat_id = chat_id
        self._closed = asyncio.Event()
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_slack_bot.py::TestSinkBridgeChatId -v`.
- [ ] Commit: `git add services/slack_bot/sink.py tests/test_slack_bot.py && git commit -m "SlackSinkBridge accepts optional chat_id (forward-compat)"`

---

## Task 7 — Replace stub `_handle_mention` with the full flow

**Files:**
- Modify: `services/slack_bot/events.py`
- Test: `tests/test_slack_bot.py`

Implements the spec flow (Section 2 steps 2-9). Order matters: bot loop-guard → allowlist (ephemeral deny) → binding (ephemeral code) → CHAT grant (ephemeral deny) → thread-session reuse-or-create (ephemeral ownership reject) → attach (not awaited, 3s-ack) → send. All denials are **ephemeral** via `send_ephemeral_to_user`.

**Verified ground truth used below (do not re-derive):**
- `ChatManager.send_user_message(self, chat_id: str, text: str)` — `app/chat/manager.py:410`. **No `sender_email` param** (it arrives only in Phase 5a). So the call is `await mgr.send_user_message(session.id, clean)` — no kwarg.
- `ChatManager.create_session(*, user_email, surface, slack_channel_id=None, slack_thread_ts=None, title=None)` — `app/chat/manager.py:105`. It internally dedups `SLACK_THREAD` via `get_slack_thread_session`, so the handler does not need to.
- `ChatSession` fields — `app/chat/types.py:23` — `id, user_email, surface, slack_channel_id, slack_thread_ts, title, started_at, last_message_at, message_count, archived`. **No `state`, no `created_at`; the timestamp is `started_at`.**
- `chat_sessions` columns — `src/db.py:1118` — `id, user_email, surface, slack_channel_id, slack_thread_ts, title, started_at, last_message_at, message_count, archived`. **The timestamp column is `started_at`, not `created_at`.**
- `users.slack_user_id` does NOT exist after `_ensure_schema`. It is added lazily by `services.slack_bot.binding._ensure_table` (`binding.py:26-28`). Any test that INSERTs or SELECTs `slack_user_id` MUST call `_ensure_table(conn)` first (the existing tests at lines 235-238 / 283-285 already do this).

We use a shared `_FakeApp` / `_FakeMgr` test harness defined once (Task 7a), then the test cases.

### 7a — Test harness + bot loop-guard

- [ ] Append the harness + first test to `tests/test_slack_bot.py`:
```python
class _FakeApp:
    """Mimics the bits of `app` _handle_mention touches."""
    class _State:
        pass

    def __init__(self, conn, mgr, *, bot_user_id="U07BOT", public_url="https://example.com"):
        self.state = _FakeApp._State()
        self.state.chat_repo = _RepoStub(conn)
        self.state.chat_manager = mgr
        self.state.slack_bot_user_id = bot_user_id
        self.state.public_url = public_url


class _FakeMgr:
    def __init__(self):
        self.created = []
        self.sent = []
        self.attached = []
        self._live = []

    def list_live(self):
        return self._live

    async def create_session(self, **kw):
        from app.chat.types import ChatSession, Surface
        from datetime import datetime
        sess = ChatSession(
            id="sess_new",
            user_email=kw["user_email"],
            surface=kw["surface"],
            slack_channel_id=kw.get("slack_channel_id"),
            slack_thread_ts=kw.get("slack_thread_ts"),
            title=None,
            started_at=datetime.now(),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        self.created.append(sess)
        return sess

    async def attach(self, chat_id, sink):
        self.attached.append((chat_id, sink))

    async def send_user_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_mention_bot_loop_guard_returns_silently(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    posts = []
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: posts.append(a))
    conn = duckdb.connect(":memory:"); _ensure_schema(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"bot_id": "B1", "channel": "C1", "ts": "1.0", "user": "U07BOT"}))
    assert posts == [] and mgr.created == []
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest "tests/test_slack_bot.py::test_mention_bot_loop_guard_returns_silently" -v` → fails because `events.py` does not yet import `send_ephemeral_to_user`, so the `monkeypatch.setattr(ev, "send_ephemeral_to_user", ...)` raises `AttributeError: <module 'services.slack_bot.events'> has no attribute 'send_ephemeral_to_user'`.

### 7b — Implement the handler

- [ ] Update imports in `services/slack_bot/events.py`. Replace the three `from services.slack_bot.*` import lines (lines 8-10) with:
```python
from services.slack_bot.binding import (
    issue_verification_code,
    is_channel_allowlisted,
    lookup_user_email,
)
from services.slack_bot.sender import send_ephemeral_to_user, send_thread_reply
from services.slack_bot.sink import SlackSinkBridge
```
- [ ] Replace the entire stub `_handle_mention` body (lines 84-96) with this complete implementation. Note the ownership-reject branch SELECTs `users.slack_user_id` to render `<@owner>`, falling back to a neutral phrase when the owner has no bound Slack id:
```python
async def _handle_mention(app, event: dict) -> None:
    """Channel @agnes mention → public in-thread reply on a persistent
    SLACK_THREAD session owned by the mention starter. Gated by the
    per-channel allowlist (default-deny). All denials are ephemeral.
    """
    # 2. Bot loop-guard: ignore our own / any bot's posts.
    bot_user_id = getattr(app.state, "slack_bot_user_id", None)
    if event.get("bot_id") or (bot_user_id and event.get("user") == bot_user_id):
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    slack_user_id = event.get("user")
    text = event.get("text", "")
    repo = app.state.chat_repo
    conn = repo._conn

    # 3. Allowlist (direct Everyone grant — never can_access).
    if not is_channel_allowlisted(conn, channel):
        await send_ephemeral_to_user(
            channel, slack_user_id, "Agnes isn't enabled in this channel."
        )
        return

    # 4. Identity binding.
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        setup_link = f"{public_url}/setup?slack=1" if public_url else "/setup?slack=1"
        await send_ephemeral_to_user(
            channel, slack_user_id,
            (
                "To use Agnes here, bind your Slack identity:\n"
                f"1. Visit {setup_link} while logged in.\n"
                f"2. Paste this 6-digit code: *{code}* (expires in 10 minutes)."
            ),
        )
        return

    # 5. CHAT grant.
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from src.repositories.users import UserRepository
    _u = UserRepository(conn).get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", conn):
        await send_ephemeral_to_user(
            channel, slack_user_id,
            "You don't have access to Agnes chat yet — ask an admin to grant "
            "your group access on /admin/access.",
        )
        return

    # 6. Thread session: reuse or create; reject if owned by someone else.
    mgr = app.state.chat_manager
    from app.chat.types import Surface
    existing = repo.get_slack_thread_session(channel, thread_ts)
    if existing is not None and existing.user_email != user_email:
        owner_row = conn.execute(
            "SELECT slack_user_id FROM users WHERE email = ?",
            [existing.user_email],
        ).fetchone()
        owner_ref = f"<@{owner_row[0]}>" if owner_row and owner_row[0] else "another user"
        await send_ephemeral_to_user(
            channel, slack_user_id, f"This thread belongs to {owner_ref}."
        )
        return
    session = await mgr.create_session(
        user_email=user_email,
        surface=Surface.SLACK_THREAD,
        slack_channel_id=channel,
        slack_thread_ts=thread_ts,
    )

    # 7. Strip our own mention token.
    clean = _strip_bot_mention(text, bot_user_id)

    # 8. Attach (NOT awaited — keep the 3s ack budget).
    if not _is_attached(mgr, session.id):
        sink = SlackSinkBridge(channel=channel, thread_ts=thread_ts, chat_id=session.id)
        asyncio.create_task(mgr.attach(session.id, sink))
        await asyncio.sleep(0.1)

    # 9. Inject the user turn. send_user_message(chat_id, text) — no sender_email
    #    (per-sender attribution arrives with Phase 5a's multi-sink refactor).
    await mgr.send_user_message(session.id, clean)
```
- [ ] Run 7a, expect PASS: `.venv/bin/pytest "tests/test_slack_bot.py::test_mention_bot_loop_guard_returns_silently" -v`.

### 7c — Allowlist-deny and unbound tests

- [ ] Append tests. Both prime `slack_user_id` via `_ensure_table` so the lazy column exists (the unbound test's `lookup_user_email` SELECT and the handler's binding path both touch it):
```python
def test_mention_not_allowlisted_ephemeral_deny(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table
    posts = []
    async def _fake_ep(ch, u, txt): posts.append((ch, u, txt))
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn); _ensure_table(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_X", "ts": "1.0", "user": "U1", "text": "<@U07BOT> hi"}))
    assert posts and "isn't enabled" in posts[0][2]
    assert mgr.created == []


def test_mention_unbound_user_gets_code(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table
    posts = []
    async def _fake_ep(ch, u, txt): posts.append((ch, u, txt))
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn); _ensure_table(conn)
    gid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg1', ?, 'slack_channel', 'C_OK')", [gid])
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "1.0", "user": "U_NEW", "text": "<@U07BOT> hi"}))
    assert posts and "6-digit code" in posts[0][2]
    assert mgr.created == []
```
- [ ] Run, expect PASS: `.venv/bin/pytest "tests/test_slack_bot.py::test_mention_not_allowlisted_ephemeral_deny" "tests/test_slack_bot.py::test_mention_unbound_user_gets_code" -v`.

### 7d — Happy path, same-thread reuse, ownership reject, 3s-ack guard

- [ ] Append the seed helpers. `_seed_bound_chat_user` calls `_ensure_table(conn)` FIRST so the lazy `slack_user_id` column exists before the INSERT (without this the INSERT raises `BinderException: column slack_user_id not found`). The `chat_sessions` seeds use `started_at` (the real column — there is no `created_at`):
```python
def _seed_bound_chat_user(conn, *, email="u@x", slack_id="U_OK"):
    """Seed a user bound to slack_id, in Everyone, with a CHAT grant.
    Primes the lazy users.slack_user_id column first (binding._ensure_table)."""
    from services.slack_bot.binding import _ensure_table
    _ensure_table(conn)  # adds users.slack_user_id if missing
    uid = f"uid_{slack_id}"
    conn.execute("DELETE FROM users WHERE email = ?", [email])
    conn.execute(
        "INSERT INTO users(id, email, name, slack_user_id) VALUES (?, ?, 'U', ?)",
        [uid, email, slack_id],
    )
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'system_seed')",
        [uid, egid],
    )
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg_chat', ?, 'chat', 'chat') ON CONFLICT DO NOTHING", [egid])
    return uid


def _allow_channel(conn, channel="C_OK"):
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg_ch', ?, 'slack_channel', ?)", [egid, channel])


def test_mention_happy_path_creates_thread_and_sends(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.1", "user": "U_OK", "text": "<@U07BOT> revenue?"}))
    assert len(mgr.created) == 1
    assert mgr.created[0].surface.value == "slack_thread"
    assert mgr.created[0].slack_thread_ts == "9.1"
    assert mgr.attached and mgr.attached[0][0] == "sess_new"
    assert mgr.sent and mgr.sent[0][1] == "revenue?"


def test_mention_ownership_reject_ephemeral(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    posts = []
    async def _fake_ep(ch, u, txt): posts.append(txt)
    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn)
    _seed_bound_chat_user(conn, email="owner@x", slack_id="U_OWNER")
    _seed_bound_chat_user(conn, email="other@x", slack_id="U_OTHER")
    _allow_channel(conn)
    # pre-existing thread session owned by owner@x (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_owned', 'owner@x', 'slack_thread', 'C_OK', '9.2', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.2", "user": "U_OTHER", "text": "<@U07BOT> hi"}))
    # owner has a bound slack id → rendered as <@U_OWNER>
    assert posts and "belongs to <@U_OWNER>" in posts[0]
    assert mgr.created == []
```

Note: `_seed_bound_chat_user` is called twice in the ownership test; its `'rg_chat'` grant uses `ON CONFLICT DO NOTHING` so the second call's duplicate CHAT grant is a no-op (the PK is the grant `id`).

- [ ] Run, expect PASS: `.venv/bin/pytest "tests/test_slack_bot.py::test_mention_happy_path_creates_thread_and_sends" "tests/test_slack_bot.py::test_mention_ownership_reject_ephemeral" -v`.

- [ ] Add the same-thread-reuse + 3s-ack-guard tests. Append:
```python
def test_mention_same_thread_reuses_session(monkeypatch):
    """A second mention in the same thread by the OWNER must NOT be rejected
    (no ownership reject) and proceeds to send. (Real dedup to a single row is
    ChatManager.create_session's job via get_slack_thread_session; the handler
    only enforces the owner check.)"""
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    # existing session owned by the SAME user (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_mine', 'u@x', 'slack_thread', 'C_OK', '9.3', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.3", "user": "U_OK", "text": "<@U07BOT> again"}))
    assert mgr.sent and mgr.sent[0][1] == "again"


def test_mention_attach_not_awaited_returns_under_budget(monkeypatch):
    """attach() blocks forever; the handler must still return promptly because
    attach is create_task'd, not awaited (3s-ack contract)."""
    import asyncio
    import services.slack_bot.events as ev
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = duckdb.connect(":memory:"); _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)

    blocker = asyncio.Event()  # never set

    class _BlockingMgr(_FakeMgr):
        async def attach(self, chat_id, sink):
            self.attached.append((chat_id, sink))
            await blocker.wait()  # would hang if awaited

    mgr = _BlockingMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    async def _run():
        await asyncio.wait_for(
            ev._handle_mention(app, {"channel": "C_OK", "ts": "9.4", "user": "U_OK", "text": "<@U07BOT> q"}),
            timeout=2.0,
        )
    asyncio.run(_run())
    assert mgr.sent  # handler reached step 9 despite attach blocking
```
- [ ] Run, expect PASS: `.venv/bin/pytest "tests/test_slack_bot.py::test_mention_same_thread_reuses_session" "tests/test_slack_bot.py::test_mention_attach_not_awaited_returns_under_budget" -v`.
- [ ] Run the whole slack suite to confirm no regression in the existing DM/sink tests: `.venv/bin/pytest tests/test_slack_bot.py -q`.
- [ ] Commit: `git add services/slack_bot/events.py tests/test_slack_bot.py && git commit -m "Implement channel mention flow: allowlist, thread session, ephemeral denials"`

---

## Task 8 — Full-suite gate + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`
- Test: (full suite)

- [ ] Run the full suite (this is what CI runs): `.venv/bin/pytest tests/ --tb=short -n auto -q`. All green. If a failure is unrelated to this diff, confirm it reproduces on a clean tree (`git stash`) and note it; do not block on it. Fix anything you touched.
- [ ] Add the changelog bullet. In `CHANGELOG.md`, under `## [Unreleased]`, in the `### Added` group (create it if absent, ordered Added/Changed/Fixed/Internal):
```markdown
- Slack channel mentions: `@agnes` in an allowlisted channel now opens a public in-thread session owned by the mention starter, gated by a new `slack_channel` resource type (default-deny; admins enable a channel by granting `(Everyone, slack_channel, <channel_id>)` on /admin/access). Denials are ephemeral.
```
- [ ] Commit: `git add CHANGELOG.md && git commit -m "Changelog: channel mentions + slack_channel allowlist"`

---

## Notes for the implementer

- **No DB migration in this phase.** `ResourceType.SLACK_CHANNEL` is registry-only — `resource_grants` already stores arbitrary `(resource_type, resource_id)` strings (mirroring `marketplace_plugin`). Do not add a `_vN_to_v(N+1)` step or an Alembic revision.
- **No new repo method.** This phase only *reads* existing tables (`resource_grants`, `users`, `chat_sessions` via the existing `get_slack_thread_session`, which already has its PG path at `app/chat/persistence.py:178-181`). The dual-backend parity rule does not trigger — no method is added to `app/chat/persistence.py`, so no `_pg.py` sibling or `tests/db_pg/` contract test is required here.
- **No new HTTP endpoint.** The mention arrives through the existing `POST /api/slack/events` → `dispatch_event` path, which is HMAC-verified in `app/api/slack.py`. No `require_admin` / `require_resource_access` wiring is needed; the authorization gate for mentions is the in-handler `is_channel_allowlisted` + `can_access(CHAT)` pair.
- **`users.slack_user_id` is lazy.** It does not exist after `_ensure_schema` — it is added by `services.slack_bot.binding._ensure_table` (binding.py:26-28). Production primes it the first time any unbound user DMs/mentions the bot (the handler's `issue_verification_code` → `_ensure_table` path). In tests you must call `_ensure_table(conn)` before INSERTing/SELECTing the column — the seed helpers and 7c tests above do this; do not drop those calls.
- **`send_ephemeral` name is reserved for Phase 2.** This phase's helper is `send_ephemeral_to_user` (Web API `chat.postEphemeral`). Phase 2's `send_ephemeral(response_url, text, blocks=None)` (spec line 189) is a different function in the same module. Do not rename Phase 1's helper to `send_ephemeral`.
- **`send_user_message` has no `sender_email`.** Live signature is `send_user_message(self, chat_id, text)` (manager.py:410). Per-sender attribution is Phase 5a. Keep the call as `await mgr.send_user_message(session.id, clean)`.
- **`ChatSession` / `chat_sessions` timestamp column is `started_at`** (types.py:31, db.py:1125) — there is no `state` or `created_at` anywhere in this schema. All session seeds and the `_FakeMgr.create_session` literal use `started_at`.
- **Vendor-agnostic:** every channel id in tests is a placeholder (`C_OK`, `C123`, `C_ADMIN`); every email is `@x` / `example.com`; no real workspace, host, or token appears.
- **Phase-0 dependency:** the ack-then-async refactor of the HTTP events endpoint is Phase 0, not this phase. This handler is 3s-safe regardless (attach is `create_task`'d, never awaited — Task 7b step 8 + the guard test in 7d), so it is correct under both the current `await dispatch_event(...)` endpoint and the Phase-0 scheduled variant.

Files touched (all absolute):
- `app/resource_types.py`
- `services/slack_bot/binding.py`
- `services/slack_bot/events.py`
- `services/slack_bot/sender.py`
- `services/slack_bot/sink.py`
- `services/slack_bot/identity.py` (new)
- `app/main.py`
- `tests/test_slack_bot.py`
- `tests/test_resource_types.py`
- `CHANGELOG.md`
