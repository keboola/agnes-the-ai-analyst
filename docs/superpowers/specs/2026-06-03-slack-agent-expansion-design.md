---
title: Slack Agent Expansion — Transport, Mentions, Slash, Interactivity, Cross-Surface & Live Co-Drive
date: 2026-06-03
status: draft for review
---

# Slack Agent Expansion

## Decisions (recap)

These are locked from prior brainstorming and are designed *to*, not relitigated here:

- **Transport (B):** keep the HTTP Events API as default, *add* Socket Mode as an optional second transport selectable via `instance.yaml` `chat.slack.transport: http|socket`. `slack_sdk`'s `SocketModeClient` is an **optional** dependency. Both transports funnel into the *one* existing dispatcher. Pattern everywhere: **ack within 3s, then process async** (`asyncio.create_task`) — this also fixes a latent duplicate-session bug in the current `_handle_dm`.
- **Mentions:** new `ResourceType.SLACK_CHANNEL` (registry-only, **no DB migration**). Per-channel allowlist = a `resource_grant` `(Everyone, SLACK_CHANNEL, <channel_id>)`, default-deny. `_handle_mention` reply is public in-thread; the thread is a `Surface.SLACK_THREAD` session keyed `(channel_id, thread_ts)`, owned by the starter.
- **Slash commands:** `/agnes <q>`, `/agnes-new`, `/agnes-status`, `/agnes help`. All ack ≤3s + async delivery via `response_url`. `/agnes` runs on the invoker's persistent DM session so the answer also appears on web `/chat`.
- **Interactivity (Block Kit):** Stop, Continue-on-web (deep link), Share-to-channel (allowlisted channels only), New-session.
- **Web badge:** a "Slack" pill in the `/chat` sidebar for `slack_dm` / `slack_thread` sessions (JS render only).
- **Co-presence — FULL LIVE CO-DRIVE:** two+ principals drive one live session cross-surface. Authorization = **intersection** of all participants' grants. Fresh ephemeral shared workspace (never a personal one). Fork-on-invite. Multi-sink fan-out. Sequenced **last**.
- **Data model:** DuckDB ladder `v68 → v69` in `src/db.py` + matching Alembic step reaching the same endpoint. Every new repo method gets its `_pg.py` sibling + cross-engine contract test in the same change.
- **Conventions (hard):** vendor-agnostic OSS (no customer names/hosts/IDs/tokens anywhere); `CHANGELOG.md` bullet under `[Unreleased]` for user-visible behavior; gate endpoints with `require_admin` / `require_resource_access`; new `ResourceType` needs a `ResourceTypeSpec`; web pages extend `base_page.html`/`base_ds.html`; no AI attribution in commits/PRs.

---

## Architecture overview

```
                          ┌──────────────────────── Slack ────────────────────────┐
                          │  events   slash-cmds   interactivity   socket-mode WS   │
                          └───┬──────────┬──────────────┬─────────────────┬─────────┘
                  HTTP        │          │              │        Socket Mode (opt)
        POST /api/slack/events│  /commands  /interactivity         SocketModeDispatcher
        (verify HMAC, ack 200)│  (verify)   (verify)               (xapp- token, ack envelope)
                              │          │              │                 │
                              ▼          ▼              ▼                 ▼
                    ┌─────────────────────────────────────────────────────────────┐
                    │  asyncio.create_task(_run_logged(...))   ── ack-then-async ── │
                    └───┬───────────────┬─────────────────┬───────────────────────┘
                        ▼               ▼                 ▼
                 dispatch_event   dispatch_command   dispatch_interaction
                  (events.py)      (commands.py)      (interactivity.py)
                        │               │                 │
                        └───────────────┴─────────────────┘
                                        ▼
                         identity (binding) + RBAC gate
              lookup_user_email → can_access(CHAT) / SLACK_CHANNEL allowlist
                                        ▼
                              ChatManager  (app/chat/manager.py)
                  create_session · attach (MULTI-SINK) · send_user_message(sender_email)
                                        │
                          runner subprocess (1 stdin, 1 stdout)
                                        │  frames broadcast to N sinks
                        ┌───────────────┴───────────────┐
                        ▼                                ▼
                  web WS sink                     SlackSinkBridge / EphemeralCommandSink
                  (/chat)                         (chat.postMessage / postEphemeral)
```

Outbound (replies) **always** rides the Web API (`chat.postMessage` / `postEphemeral` / `response_url`), independent of inbound transport. So `sender.py`, `sink.py`, `binding.py` are inbound-transport-agnostic.

---

## 1. Transport abstraction (HTTP + Socket Mode)

**Goal.** Add Socket Mode as an optional inbound transport selectable per instance, without forking handler logic. Both transports funnel through the existing `dispatch_event` / `dispatch_command` / `dispatch_interaction` routers. HTTP stays default.

**Latent bug fixed here (both transports).** Today `_handle_dm` is `await`ed inside the HTTP handler before the 202 returns, but it spawns an E2B sandbox (>3s) — blowing Slack's 3s budget, triggering retries, and relying on dedup as the only backstop. We make **ack-then-async** a first-class contract: handlers `create_task` the dispatch and return immediately.

### Components

| Component | File | Responsibility |
|---|---|---|
| `dispatch_event/command/interaction` | `services/slack_bot/{events,commands,interactivity}.py` | Pure routing by payload type; transport-agnostic. |
| HTTP endpoints | `app/api/slack.py` | Verify HMAC, answer `url_verification`, **schedule** dispatch via `create_task`, return ack. |
| `SocketModeDispatcher` | `services/slack_bot/socket_mode_client.py` (**new**) | Own the WS lifecycle: connect, **ack envelope first**, then schedule dispatch; reconnect/backoff; clean shutdown. |
| `SlackConfig` | `app/chat/config.py` | Parse nested `chat.slack` block incl. `transport`. Tokens are **not** stored here (read from env at use site). |
| `get_slack_transport()` | `app/instance_config.py` (**new**) | `SLACK_TRANSPORT` env → `chat.slack.transport` → default `"http"`. |
| Lifespan wiring | `app/main.py` | If `socket`: validate tokens, construct dispatcher, `await start()`, store on `app.state`, tear down at shutdown. |
| `_run_logged(coro)` | `services/slack_bot/events.py` | Wrap every scheduled coroutine: try/except-log + best-effort ephemeral on failure. Used at *all* dispatch call sites. |

### Config

```python
@dataclass(frozen=True)
class SlackConfig:
    transport: str = "http"   # "http" | "socket"; unknown → log + treat as "http"

@dataclass(frozen=True)
class ChatConfig:
    ...
    slack: SlackConfig = field(default_factory=SlackConfig)
```

Tokens (`SLACK_BOT_TOKEN` `xoxb-`, `SLACK_APP_TOKEN` `xapp-`, `SLACK_SIGNING_SECRET`) are read from env at use site — kept out of the frozen config and out of any `/admin/server-config` echo.

### Socket Mode listener (the funnel)

```python
async def _on_request(self, client, req):
    await client.send_socket_mode_response(           # 1. ACK FIRST (<3s)
        SocketModeResponse(envelope_id=req.envelope_id))
    if req.type == "events_api" and req.payload.get("type") == "event_callback":
        self._schedule(_run_logged(dispatch_event(self._app, req.payload["event"])))
    elif req.type == "slash_commands":
        self._schedule(_run_logged(dispatch_command(self._app, req.payload)))
    elif req.type == "interactive":
        self._schedule(_run_logged(dispatch_interaction(self._app, parse_interaction(req.payload))))
```

`SocketModeRequest.payload` for `events_api` is byte-identical to the HTTP webhook's `payload` — **no event-shape translation layer**, that's the whole point.

### HTTP webhook change

```python
if payload.get("type") == "event_callback":
    _schedule(_run_logged(dispatch_event(request.app, payload["event"])))
    return {"ok": True}                                # was: await dispatch_event(...)
```

`url_verification` and signature checks unchanged.

### Hard constraints

- **Single-worker invariant.** `app/main.py` already disables chat when `UVICORN_WORKERS > 1`. Socket Mode hard-requires it (one WS; N workers fracture dedup). The socket branch re-asserts this and refuses to start otherwise (log + disable).
- **Fail-closed gates** at lifespan init: `transport=socket` requires a valid `xapp-`/`xoxb-` token pair and the `slack-socket` extra importable; any miss → log + disable Slack, never start a dead WS or crash the app.
- **Lazy optional dep.** `slack_sdk` is imported **inside** `SocketModeDispatcher.start()`, never at module top. `pyproject.toml`: `[project.optional-dependencies] slack-socket = ["slack_sdk>=3.27"]`. `ImportError` → actionable fail-closed message.
- **Detached-task leakage.** Keep strong references to every `create_task` result in a module-level set with `add_done_callback(discard)` so the GC can't cancel an in-flight dispatch.
- **Ack-then-fail semantics.** Because we ack before processing, a failure during dispatch does *not* trigger a Slack retry — `_run_logged` is the only recovery path and must post a user-visible ephemeral on unhandled exceptions.

### Manifest

Ship **two documented manifest stanzas** in `docs/` (HTTP vs Socket) rather than one dual-mode file (avoids stale `request_url` foot-guns). Vendor-agnostic: `https://<your-host>/api/slack/events`.

---

## 2. Channel mentions, threads, allowlist (`ResourceType.SLACK_CHANNEL`)

**Goal.** A bound, chat-authorized user `@agnes`-mentions the bot in a channel; Agnes replies publicly in-thread; the thread becomes a persistent `Surface.SLACK_THREAD` session owned by the starter; gated by a per-channel allowlist as `resource_grants` under a new `ResourceType.SLACK_CHANNEL` (default-deny, **no migration**). Replaces the current stub `_handle_mention`.

### Registry wiring (`app/resource_types.py`)

Three edits, per the module's four-step recipe:

1. `SLACK_CHANNEL = "slack_channel"` enum member.
2. `_slack_channel_blocks(conn)` projection — **no domain table**; the grant rows *are* the allowlist, so project `DISTINCT resource_id FROM resource_grants WHERE resource_type='slack_channel'` (admins add by pasting a channel ID).
3. `ResourceTypeSpec(key=SLACK_CHANNEL, display_name="Slack channels", id_format="<channel_id>", list_blocks=_slack_channel_blocks)`.

### Allowlist semantics (security-critical)

The allowlist grant is `(Everyone, SLACK_CHANNEL, <channel_id>)` — channel openness is a property of the channel, not of a user group. **Check it with a direct `resource_grants` lookup scoped to the `Everyone` group — NOT `can_access(user_id, …)`** — because `can_access` short-circuits `True` for admins, which would let any admin's mention make Agnes post in any channel they're in. New helper:

```python
# services/slack_bot/binding.py
def is_channel_allowlisted(conn, channel_id: str) -> bool:
    """True iff the Everyone group holds (SLACK_CHANNEL, channel_id).
    Direct grant lookup — deliberately does NOT use can_access (no admin short-circuit)."""
```

### Flow (one mention)

1. `channel = event["channel"]`; `thread_ts = event.get("thread_ts") or event["ts"]`.
2. **Bot loop-guard:** if `event.get("bot_id")` or the sender is our own bot user → return silently.
3. `is_channel_allowlisted(conn, channel)`? no → ephemeral "Agnes isn't enabled in this channel."
4. `user_email = lookup_user_email(repo, event["user"])`; `None` → ephemeral binding instructions + 6-digit code (reuse `issue_verification_code`).
5. `can_access(user_id, CHAT, "chat", conn)`? no → ephemeral "ask an admin for chat access."
6. `existing = repo.get_slack_thread_session(channel, thread_ts)`:
   - none → `mgr.create_session(user_email, surface=SLACK_THREAD, slack_channel_id=channel, slack_thread_ts=thread_ts)` (starter = owner).
   - exists & `owner != user_email` → ephemeral "This thread belongs to `<@owner>`." Drop.
7. `clean = _strip_bot_mention(text, app.state.slack_bot_user_id)`.
8. if not attached: `create_task(mgr.attach(session.id, SlackSinkBridge(channel, thread_ts, chat_id=session.id)))` — **not awaited** (3s-ack).
9. `await mgr.send_user_message(session.id, clean, sender_email=user_email)`.

`bot_user_id` is resolved once at startup via `auth.test` and stashed on `app.state.slack_bot_user_id`.

All denials are **ephemeral**, never public (avoids leaking existence/ownership into channels).

---

## 3. Slash commands

Slash commands arrive as `x-www-form-urlencoded` POST to a **separate Request URL**, not the Events endpoint, with a different payload shape (`command`, `text`, `user_id`, `channel_id`, `response_url`). They get their own endpoint + dispatcher.

### Components

| Unit | File | Responsibility |
|---|---|---|
| `POST /api/slack/commands` | `app/api/slack.py` | Verify signature on raw body, parse form, `create_task(dispatch_command)`, return 3s ack body. |
| `dispatch_command(app, cmd)` | `services/slack_bot/commands.py` (**new**) | Route on `cmd["command"]`. |
| handlers | `services/slack_bot/commands.py` | One coroutine each; deliver async via `response_url`. |
| `send_ephemeral(response_url, text, blocks=None)` | `services/slack_bot/sender.py` | POST `{"response_type":"ephemeral",...}`. |
| `open_im(slack_user_id)` | `services/slack_bot/sender.py` | `conversations.open` → DM channel id. |
| `EphemeralCommandSink` | `services/slack_bot/sink.py` | Transient sink that posts the next assistant turn ephemerally then closes. |
| `active_count_for_user(email)` | `app/chat/manager.py` | Public wrapper over `_active_count_for_user` (single-source the cap predicate). |

### Behavior

- **`/agnes <q>`** — runs on the invoker's **persistent DM session** so the answer also surfaces on web/DM. Resolve the user's IM channel via `open_im` (a slash fired in a public channel carries that channel's id, not the DM channel — keying the session on it would break dedup). `create_session(surface=SLACK_DM, slack_channel_id=<IM channel>)` dedups to the existing live session. Attach a transient `EphemeralCommandSink(response_url)` only if no permanent sink exists; deliver the answer ephemerally via `response_url`; do not stay attached (the persistent sink keeps streaming on web/DM). `response_url` is 30-min / 5-post limited — single-shot only.
- **`/agnes help`** — `cmd["text"].strip() in ("", "help")` returns the help body synchronously in the 3s ack; no session, no async work.
- **`/agnes-new`** — resolve IM channel → `get_slack_dm_session` → if found, `mgr.kill(id)` + `repo.archive_session(id)`; ephemeral confirm. Next `/agnes`/DM creates a fresh row (`get_slack_dm_session` filters `archived=FALSE`).
- **`/agnes-status`** — ephemeral, read-only: `active_count_for_user(email)` / `config.concurrency_per_user` + a `<public_url>/chat` deep link.

### Error handling

- Bad/missing signature → 401 `bad_signature` (mirrors events).
- Unbound → ephemeral code + setup link (reuse `_handle_dm`'s branch).
- No CHAT grant → ephemeral "ask an admin."
- `ConcurrencyCapHit` → ephemeral "at your session limit ({cap}); /agnes-new to free one."
- Budget/rate `RuntimeError` (raised after the structured `error` frame is emitted) → forwarded by the sink to `response_url`; dispatcher swallows the already-surfaced error.

### Manifest & config

`slash_commands` block declaring `/agnes`, `/agnes-new`, `/agnes-status` with `url: https://<your-host>/api/slack/commands`. Under Socket Mode the `url` is omitted and commands arrive over the socket → same `dispatch_command`.

---

## 4. Interactivity / Block Kit buttons

Four buttons. Interactions arrive as `x-www-form-urlencoded` with a single `payload` JSON field at a **separate Request URL**.

| Button | Action | Where |
|---|---|---|
| **Stop** | `ChatManager.cancel(chat_id)` → existing `cancelled` frame. Sink attaches the button on a streaming reply, removes it at turn end. | DM + allowlisted mention threads |
| **Continue-on-web** | Pure link button `url=<web_base>/chat?session=<id>` — no callback. | everywhere a bot reply appears |
| **Share-to-channel** | Promote an ephemeral `/agnes` answer to a public in-thread post — **only in allowlisted channels**, re-checked at click time. | ephemeral `/agnes` answers in allowlisted channels |
| **New-session** | Soft-archive current DM session (shared helper with `/agnes-new`). | DM threads |

### Components

```
services/slack_bot/
├── interactivity.py   NEW — parse_interaction + dispatch_interaction (verify is in app/api/slack.py)
├── blocks.py          NEW — pure Block Kit builders (no I/O)
├── sender.py          EXTEND — post_thread_reply_with_blocks(→ts), update_message, respond_via_response_url
└── sink.py            EXTEND — Stop-button lifecycle on SlackSinkBridge
app/api/slack.py       EXTEND — POST /api/slack/interactivity
```

`blocks.py` is leaf (imports nothing from the others); every interactive element carries a structured JSON `value` so handlers never re-parse free text.

### Endpoint

```python
@router.post("/interactivity")
async def slack_interactivity(request: Request):
    body = await request.body()                       # raw bytes — Slack signs these
    if not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    interaction = parse_interaction(json.loads(form["payload"]))
    _schedule(_run_logged(dispatch_interaction(request.app, interaction)))
    return Response(status_code=200)                  # empty 200 = ack, message unchanged
```

### Per-button rules

- **Stop:** `_on_stop` resolves clicker → owner email; non-owner in a shared thread → ephemeral "belongs to @X". Otherwise `mgr.cancel(chat_id)` (idempotent) + sink strips the button via `update_message`.
- **Share-to-channel:** `_on_share` (1) resolves clicker→email (unbound → deny); (2) **re-resolves `require_resource_access(SLACK_CHANNEL, <channel_id from signature-verified payload>)` at click time** — never trusts the payload channel for posting; not allowlisted → ephemeral "not here"; (3) public `chat.postMessage`; (4) clear the ephemeral via `response_url`; (5) `write_audit("slack_share", actor, channel)`. Long answers (>2000-char `value` cap) are tokenized into a small in-memory TTL map; only the token rides in `value`.
- **New-session:** owner-gated; route through the shared `_soft_archive_dm(app, owner_email, channel_id)` helper (same as `/agnes-new`).

### Sink Stop-button lifecycle

`SlackSinkBridge` gains `chat_id` (passed at construction in `_handle_dm`/mention). On the first `assistant_message`-bearing post of a turn, post with `stop_button_blocks(...)` and store the returned `ts`; on `cancelled`/turn-end, `update_message(channel, ts, final_text, blocks=[])` to strip it. `cancel()` is idempotent so Stop is always safe.

### Error handling

401 before any work on bad signature (forged click never runs); empty 200 ack so a handler exception never becomes a Slack retry; each `_on_*` wrapped in try/except → ephemeral "something went wrong"; out-of-allowlist Share never posts publicly; `response_url` expiry → fall back to `chat.postEphemeral`.

---

## 5. Web badge, cross-surface visibility, deep link

Three web-UI capabilities, ordered by independence; (3) degrades gracefully to (1)+(2) on an older server.

### 5.1 Surface badge (JS render only)

One change to `_makeSidebarItem(s)` in `app/web/static/js/chat.js`: when `s.surface` is `"slack_dm"`/`"slack_thread"`, append a non-interactive `.cloud-chat-surface-badge` pill with text "Slack" (text, not an icon — no brand asset bundled, satisfies the design-system contract). `list_sessions` already emits `surface`. New CSS class in `style-custom.css` uses **design tokens only** (`var(--ds-*)`, never raw `#hex`, never `var(--primary)`) — `tests/test_design_system_contract.py` enforces this. `undefined` surface → no pill (fail-closed).

### 5.2 Deep link `/chat?session=<id>`

- Router (`chat_page()`): `ctx["initial_session_id"] = request.query_params.get("session")` — **does not** 404 on unknown/forbidden ids; RBAC is enforced when JS calls the session-scoped endpoints (existing ownership guards). Page always renders.
- Template exposes it as a DOM hook; chat.js reads it once on boot and, after the sidebar cache is populated, `requestAnimationFrame(() => openSession(id))` — guarded by `&& !currentChatId` and consumed once (`_initialSessionId = null`) so a later sidebar refresh can't re-hijack the view. Unknown/forbidden id → `target` undefined → no-op.

### 5.3 Co-presence web surface

The render half of co-drive (security model in §6): a "Co-drive" pill + participant-avatar cluster in the sidebar; per-message sender attribution in `renderMessage` when `m.sender_email !== currentUserEmail`; an "Invite" affordance on an owned session; a "Fork" affordance for collaborators; a new `session_participants` WS frame re-renders the roster (full re-render, self-healing). All co fields are **optional** in JS → graceful degradation on older servers.

---

## 6. Live co-drive co-presence

> **Sequencing.** Heaviest piece; depends on the v69 schema and the multi-sink `ChatManager` refactor. Ship Slack-core (§1–§5) first. Land co-drive **last**. The v69 migration and multi-sink fan-out are the only pieces that land *early* (single-principal Slack threads also use multi-sink for cross-surface).

**Goal.** Two+ authenticated principals drive one live session simultaneously, cross-surface. Either sees the same streamed frames, either can send a turn, either can Stop. Authorization is the **intersection** of all participants' grants.

**Non-goals.** OT text co-editing, cursor presence, voice, cross-instance co-drive, retroactive downgrade of a personal session (we fork instead).

### 6.1 Security model (HARD REQUIREMENTS)

The adversarial review found the model is **only sound after** the requirements below are met. Each is part of the design, not an open question. **Co-drive must not ship until every item in §6.1 and §6.2 is implemented and tested.**

#### SR-1 — Session principal, intersection, no admin short-circuit

The auth subject of a co-session is a `SessionPrincipal` (`app/auth/session_principal.py`, new):

```python
@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    participant_user_ids: list[str]
    participant_emails: list[str]
    intersection: dict[str, frozenset[str]]   # resource_type -> allowed resource_ids
```

`src/grant_intersection.py` (new) `compute_grant_intersection(participant_emails, conn)`: per `ResourceType`, the set-**intersection** of each participant's allowed `resource_id`s. **Must NOT apply the Admin god-mode short-circuit.** Implementation: a private `_allowed_ids_for_user(user_id, resource_type, conn)` in `access.py` computes the real grant set *without* the admin short-circuit; both `can_access` (union/admin path) and `compute_grant_intersection` call it (one code path, so admin-leak can't reappear by drift). An admin participant contributes the full set → `intersection(full, non_admin) == non_admin`. **Fail-closed:** any participant with zero groups, or empty participant list → empty intersection → deny.

`can_access_session(participant_user_ids, resource_type, resource_id, conn) -> bool` returns `resource_id in intersection.get(resource_type, frozenset())`. It **must not** call `is_user_admin` / `can_access`. (PR checklist item: *"can_access_session does not call is_user_admin / can_access"*.)

#### SR-2 — SessionPrincipal flows through EVERY data-authz call site, not just two dependencies (CRITICAL)

The runner reaches data via the agnes CLI hitting `/api/data`, `/api/sync/manifest`, `/api/catalog`, `/api/v2/{scan,sample,schema}` — these call `can_access_table(user, ...)` and `resolver.stack(user["id"], ...)` directly, **not** through `require_resource_access`. Patching only the two FastAPI dependencies leaves ~283 call sites authorizing against one participant's full grants (and re-triggering admin short-circuit). **Required:** make `SessionPrincipal` a first-class subject through one chokepoint — `can_access_table`, `StackResolver.stack`, and the `/api/sync` manifest builder accept *either* a user dict *or* a `SessionPrincipal` and dispatch to `can_access_session` with no admin short-circuit. Audit every `user["id"]` / `can_access_table(user, ...)` site and route it through that chokepoint. **Gate:** a contract test that a co-session token hitting `/api/data`, `/api/sync/manifest`, `/api/v2/scan` for a resource only one participant holds returns 403. *Until every data-read path is SessionPrincipal-aware, do not ship co-drive.*

#### SR-3 — Fail-closed at the resolver, not at the minter (CRITICAL)

`resolve_token_to_user` (`app/auth/pat_resolver.py`) branches on the token:
- `typ="co_session"` → recompute participants **live** from `chat_session_participants WHERE left_at IS NULL`, build a `SessionPrincipal`.
- Any token bearing `chat_session_id` whose session row has `is_co_session=TRUE` but is a plain single-user token (no `typ="co_session"`) → **FAIL CLOSED** (`invalid_token`). A co-session can never be driven by a `sub=user_id` token, regardless of `_spawn_runner` correctness. This is defense-in-depth independent of the minter.
- `sub` of a co-session JWT is the synthetic `"session:<id>"` (never a user UUID), so `get_by_id` cannot resolve it to a user dict.

`require_resource_access` dispatches: `dict` → `can_access(user["id"], …)`; `SessionPrincipal` → `can_access_session(...)`. `require_admin` **hard-denies** a `SessionPrincipal` (403) before any `is_user_admin` call.

#### SR-4 — Co-session JWT carries NO participant identity (CRITICAL)

`mint_co_session_jwt(session_id, ttl=3600)` carries **only** `chat_session_id` + `typ="co_session"` + synthetic `sub`. **No `participants` email list** is baked in — the resolver reads `chat_session_participants` live as the sole source of truth. This eliminates both the stale-grant replay window (a removed participant can't widen the intersection via an old token) and the per-call-DB-hit-vs-baked-claim ambiguity.

#### SR-5 — No JWT seed fallback for co-sessions (HIGH)

`_spawn_runner`'s `except ValueError → AGNES_SESSION_JWT_SEED` fallback **must not** apply to co-sessions. Wrap `mint_co_session_jwt` so a failure re-raises and aborts the spawn (emit an `error` frame to all sinks); never inject a seed token (which would carry no co claims and could resolve to an admin in dev/test setups). The respawn path (§6.4) uses the same co branch.

#### SR-6 — Ephemeral workspace boundary (CRITICAL + HIGH)

A co-session **must not** mount any participant's personal workspace. `ChatManager.attach` branches on `session.is_co_session` **before** calling `prepare_session_dir`; for co-sessions it calls `prepare_ephemeral_session_dir(chat_id, participant_emails, intersection)` (`app/chat/workdir.py`, new), which:
- creates a **fresh** dir with **no symlinks** to any personal workspace;
- copies **only** intersection-filtered `.claude/skills` + `.claude/agents` (`intersection["marketplace_plugin"]`);
- writes a fresh RBAC-filtered `CLAUDE.md`; **never** includes `CLAUDE.local.md` in any form (no symlink, no copy);
- fresh empty `memory/` and shared `work/`.

`upload_workspace` operates on this ephemeral dir, not the owner's personal root. `download_workspace` is **skipped** when `session.ephemeral` is true. *Recommendation:* remove `CLAUDE.local.md` from `prepare_session_dir`'s unconditional symlink list and make the personal override opt-in, so a missed co branch cannot leak it.

#### SR-7 — Plugin/skill set fixed to the NARROWEST lifetime intersection (MEDIUM)

Plugins/skills are loaded into the running agent at spawn. If a higher-privilege participant present at spawn later leaves, already-loaded plugins remain usable. **Required:** on any participant-set change (join/leave), tear down and **re-spawn** the runner with the recomputed intersection (the fork-on-invite pattern already implies a fresh spawn; apply it to leave too). Document that plugin/skill availability tracks the live intersection at each spawn/respawn.

#### SR-8 — Fork does NOT blind-clone the transcript (HIGH × 2)

The original single-principal transcript may contain rows a low-grant invitee can't query; the live intersection only constrains *future* queries, so a blind clone leaks historical results verbatim via the seeded transcript + multi-sink replay. **Required default:** do **not** copy raw messages into a co-session a lower-grant principal can read. Instead seed with a **server-generated summary produced under the intersection principal**, OR require the inviter to explicitly select carry-over messages with an explicit warning, OR start fresh. Whatever the choice, audit that the transcript was exposed, and never make silent full-clone the default.

#### SR-9 — Join gated on live membership; leave tears down the sink atomically (CRITICAL + MEDIUM)

- **Join:** the WS ticket is issued **only** after `get_session_participants(session_id)` contains the caller with `left_at IS NULL`; membership is **re-verified inside `add_sink`**, not just at ticket mint. A bare `CHAT` grant must not let a user attach to an arbitrary co-session id.
- **Leave / owner-removes-participant:** atomically stamp `left_at`, **remove the leaver's `SinkEntry` from `live.sinks`, and `close()` that sink within the same handler before returning** — ordered so no frame broadcasts to a sink whose principal is no longer a participant. Do **not** rely on the next data-query recompute (fan-out frames bypass the data-grant path). One source of truth (read once) for both the fan-out set and the authz set. **Gate:** a test asserting zero frames reach a leaver's sink after `leave` returns.

#### SR-10 — Per-sender budgets, caps, rate limits (HIGH)

All budget/rate/concurrency checks in `send_user_message` currently key on `live.user_email` (the owner). **Required:** thread `sender_email` through and check against the **sender**: per-participant active-session counting (a co-session counts against *every* active participant's cap), per-sender daily-token + rate-window checks before injecting the turn, and token-spend attribution to the sender. **Gate:** a test that a capped/rate-limited collaborator is rejected on their own turn while the owner's turns still pass.

#### SR-11 — Crash-respawn is co-aware and re-authorized (MEDIUM)

`_wait_for_exit_and_respawn` must route through the same co branch (`mint_co_session_jwt` with the current live participant set), carry `sender_email` on replayed turns, and **skip replay** of turns authored by participants now `left_at`-stamped (or re-authorize each replayed turn against the current intersection before injecting).

#### SR-12 — Slack binding hardening (HIGH, also helps DM/mention/slash)

`binding.issue_verification_code` currently issues unlimited 6-digit codes in a global namespace with no tie to the redeemer. **Required:** at most one active code per `slack_user_id` (DELETE prior on re-issue), throttle issuance per `slack_user_id`, per-code attempt lockout on redeem, audit every redeem, and show the `slack_user_id` being bound for confirmation at `/setup`. For co-drive: **pin `participant_user_ids` by `user_id` at JOIN time** (store `user_id` in `chat_session_participants`) so a later re-binding cannot retroactively change an active participant's identity mid-session.

### 6.2 Stdin serialization (HIGH)

`send_user_message` has `await` points before `stdin.write` + `await stdin.drain()`; two participants' fire-and-forget tasks can interleave partial JSON lines on the shared stdin. **Required:** add `_stdin_lock: asyncio.Lock` to `LiveSession` and hold it across the **write+drain pair** so the injection is atomic w.r.t. the event loop.

### 6.3 Multi-sink fan-out

- `LiveSession.ws` → `sinks: list[SinkEntry]` where `SinkEntry = (participant_email, sink_obj)` (duck-typed: web WS or `SlackSinkBridge`).
- `attach(chat_id, ws, *, is_primary=True)` spawns runner + pump (existing lifecycle, blocks). `add_sink(chat_id, ws, participant_email)` appends, **replays persisted history to the new sink before appending it to the broadcast list** (so a late joiner doesn't miss in-flight frames — analogous to crash-respawn replay; replay+append serialized so the pump doesn't double-send), sends `ready`, returns. Last sink dropping does **not** kill a co-session; only `kill()`/owner-leave ends it.
- `_pump_subprocess_to_ws` snapshots `list(live.sinks)`, sends per sink, collects `dead_sinks`, removes + `create_task(sink.close())` after the loop. **Persistence stays singular:** exactly one `append_message` per `assistant_message`, one `write_audit` per `tool_call`, regardless of sink count.
- `send_user_message(chat_id, text, *, sender_email=None)` persists with `sender_email or live.user_email`; injects under the `_stdin_lock`.
- `cancel()` broadcasts the synthetic `tool_result` + `cancelled` to all sinks.

### 6.4 Fork-on-invite lifecycle

```
A's personal session S0 (untouched)  ──invite──▶  S1 (is_co_session=TRUE, ephemeral=TRUE)
  POST /api/chat/{S0}/invite {invitee_email}        participants: (A,'owner'),(B,'collaborator')
    require: caller owns S0 AND invitee independently has CHAT access
    seed: intersection-filtered summary (SR-8), NOT raw clone
    workspace: fresh ephemeral, intersection-filtered (SR-6)
    runner: mint_co_session_jwt(S1) (SR-4, SR-5)
  join → add_sink (membership-checked, SR-9)
  leave → left_at + sink teardown + respawn under new intersection (SR-7, SR-9)
  end → owner-leave kills + discards ephemeral dir; save-on-end is opt-in, owner-only
```

Endpoints in `app/api/chat_copresence.py` (new), all gated. `/fork?owner=<email>` is the collaborator-initiated variant (copy S1 transcript into a private single-principal session). **Owner-leave ends the co-session** (kill + discard). **Save-on-end is dropped from v1** per the data-leak review — default mandatory discard; if reintroduced later, it is opt-in and owner-only.

---

## 7. Data model, migrations, dual-backend (v68 → v69)

Additive-only: three columns (safe defaults) + one table. Forward-safe on populated prod DBs, no downtime. Both ladders reach the same endpoint.

### Schema

```sql
CREATE TABLE IF NOT EXISTS chat_session_participants (
    id          VARCHAR PRIMARY KEY,
    session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
    user_email  VARCHAR NOT NULL,
    user_id     VARCHAR NOT NULL,            -- pinned at join (SR-12)
    role        VARCHAR NOT NULL,            -- 'owner' | 'collaborator'
    joined_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    left_at     TIMESTAMP,                   -- NULL = active
    UNIQUE (session_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_chat_session_participants_user
    ON chat_session_participants(user_email, session_id);

ALTER TABLE chat_messages ADD COLUMN sender_email  VARCHAR;                       -- nullable, backfilled
ALTER TABLE chat_sessions ADD COLUMN is_co_session BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_sessions ADD COLUMN ephemeral     BOOLEAN NOT NULL DEFAULT FALSE;
```

Backfill `sender_email` for existing `role='user'` rows to the session owner (all pre-v69 sessions are single-principal).

### Migrations

- **DuckDB** `src/db.py`: bump `SCHEMA_VERSION = 69`, add `_v68_to_v69(conn)`, dispatch it at **both** ladder invocation sites, mirror DDL into `_SYSTEM_SCHEMA` (so fresh installs build v69 directly; migration is a no-op there). Guard each `ADD COLUMN` (PRAGMA check) — migration may re-run on a partial DB.
- **Alembic** `migrations/versions/0016_cloud_chat_v69.py` (`down_revision="0015_cloud_chat_v68"`): same endpoint; PG uses `ondelete="CASCADE"` on the participants FK + the index; same backfill `UPDATE`.
- **DuckDB has no `ON DELETE CASCADE`** → `ChatRepository.hard_delete_user_sessions` gains, as its **first** step, `DELETE FROM chat_session_participants WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_email = ?)` (before messages/sessions). Same step in the ephemeral-GC sweep. PG's FK cascade also handles it; the explicit DuckDB delete makes intent visible and avoids a mid-transaction FK violation + partial-delete ghost.
- Do **not** touch `last_message_at` / `message_count` (DuckDB read-time derivation via `_SESSION_SELECT`/`_SESSION_GROUP`; PG write-time maintenance) — `sender_email` rides the existing INSERT.
- `tests/test_db_schema_version.py` updated to expect 69 and assert the new table/columns exist on a freshly-migrated DuckDB.

### Dataclasses & repo parity

`app/chat/types.py`: `ChatSession.is_co_session/ephemeral` (default False), `ChatMessage.sender_email` (Optional), new `SessionParticipant`. Add `s.is_co_session, s.ephemeral` to `_SESSION_SELECT` + `_SESSION_GROUP`.

Every new method lands in **both** `app/chat/persistence.py` (DuckDB) and a new `src/repositories/chat_session_participants_pg.py` in the same PR, with cross-engine contract tests in `tests/db_pg/test_chat_pg.py`: `add_session_participant`, `get_session_participants`, `remove_participant`, `update_participant_role`, `list_sessions_for_participant`, `fork_session_as_co_session`. The fork primitive must be atomic (PG transaction; DuckDB ordered so a partial failure leaves only a harmless empty GC-able ephemeral session).

---

## 8. Testing

### Unit

- **Transport:** dispatcher parity (synthetic `SocketModeRequest` → `_on_request` acks envelope **before** scheduling, and `dispatch_event` receives a dict byte-identical to the HTTP extraction); ack-timing regression (patch `create_session` to sleep 5s → HTTP handler returns ≪3s, session created once — fails against the old `await` code); config resolution table-test; fail-closed gates (missing/`xapp-`-invalid token, `slack_sdk` absent); detached-task error path (`_run_logged` logs + best-effort ephemeral).
- **Mentions:** `is_channel_allowlisted` (default-deny; True after Everyone grant; **still False for an admin** — proves no `can_access`); `_handle_mention` per error-table row + happy path + same-thread reuse + 3s-ack guard (attach blocks on an unset `Event`, handler still returns); `_strip_bot_mention` table-test; RBAC registry contract (`SLACK_CHANNEL` in `enabled_resource_types()`, `id_format`, `_slack_channel_blocks` projects a seeded grant).
- **Slash:** signature gate; routing; `/agnes` happy/unbound/no-grant (IM channel keys the session, not the source channel); `/agnes help` synchronous; `/agnes-new` kill+archive; `/agnes-status` count matches the cap predicate; `EphemeralCommandSink` frame filter; cap-hit ephemeral.
- **Interactivity:** `blocks.py` exact JSON; `parse_interaction`/`dispatch_interaction` routing; endpoint sig-verify (bad → 401, no handler); Stop non-owner → ephemeral, no cancel; Share non-allowlisted → no public post; Share allowlisted → one post + ephemeral clear + one audit row; sink Stop-button lifecycle; 3s-ack regression.
- **Web:** badge present for slack surfaces / absent otherwise; deep-link auto-open once / no-op on unknown / no clobber when `currentChatId` set; design-system contract sweep over new CSS.
- **Co-presence security (the gates — must all pass before merge):**
  - intersection: two non-admins → overlap; **admin + non-admin → non-admin's set** (no admin leak); grantless participant → deny-all; leave grows / join shrinks-or-equal.
  - resolver fails closed: a single-user JWT against an `is_co_session=TRUE` session → rejected (SR-3); co JWT carries no participant identity (SR-4).
  - `require_admin` rejects a `SessionPrincipal`; `can_access_session` does not call `is_user_admin`/`can_access`.
  - **data-path coverage (SR-2):** co token → 403 on `/api/data`, `/api/sync/manifest`, `/api/v2/scan` for a single-participant-only resource.
  - workspace boundary (SR-6): no `CLAUDE.local.md` symlink, only intersection plugins, fresh memory, `download_workspace` skipped when `ephemeral`.
  - join membership-gated; **left participant's open sink receives zero frames after leave returns** (SR-9); per-sender cap/rate rejection (SR-10); stdin serialization (no interleaving under concurrent sends).
  - fork: S0 unchanged; S1 two participant rows + co/ephemeral flags; **seed is summary, not raw clone** (SR-8); respawn co-aware (SR-11); binding rate-limit + one-active-code + audit (SR-12).

### Dual-backend contract (`tests/db_pg/`)

Parametrized over DuckDB + PG: add/list/remove/role participants; `fork_session_as_co_session`; `sender_email` round-trip; flags default False (back-compat); `hard_delete_user_sessions` cascades participants on **both** engines (DuckDB explicit delete, PG FK); co-session coexists with the owner's `slack_dm`/`slack_thread`/web sessions. `tests/test_db_schema_version.py` confirms both ladders reach v69.

### E2E (skip-by-default in CI)

- No live-WS test in CI — Socket Mode parity is covered by stubbed-`client` unit tests; any live test behind an env-flagged, skip-by-default marker.
- Dual-transport parity: a single `dispatch_event(app, {"type":"app_mention", …})` test covers both transports.

---

## 9. Phasing

1. **Phase 0 — Transport + ack fix (foundation).** Nested `SlackConfig`, `get_slack_transport`, `SocketModeDispatcher` (lazy optional dep, fail-closed gates, single-worker re-assert), `_run_logged`, ack-then-async on the HTTP events endpoint. Ships the latent-bug fix immediately. Two manifest stanzas in `docs/`.
2. **Phase 1 — Mentions + allowlist.** `ResourceType.SLACK_CHANNEL` + `_slack_channel_blocks` + spec (no migration); `is_channel_allowlisted`; implement `_handle_mention`; `_strip_bot_mention`; `send_ephemeral`; stash `slack_bot_user_id`.
3. **Phase 2 — Slash commands.** `/api/slack/commands`; `dispatch_command` + four handlers; `open_im`; `EphemeralCommandSink`; `active_count_for_user`; manifest `slash_commands`.
4. **Phase 3 — Interactivity.** `/api/slack/interactivity`; `interactivity.py` + `blocks.py`; `sender`/`sink` extensions; Stop lifecycle; Share-to-channel with **click-time** allowlist re-check + audit.
5. **Phase 4 — Web badge + deep link.** `_makeSidebarItem` pill; `chat_page` query param + JS one-shot auto-open. (Independent of Slack-core; can land in parallel.)
6. **Phase 5 — Co-presence (last).** Split:
   - **5a (lands with/after Phase 0–1, reused by single-principal threads):** v69 schema migration (both ladders) + multi-sink `ChatManager.attach`/fan-out + `_stdin_lock`.
   - **5b:** `SessionPrincipal`, `grant_intersection`, `can_access_session`, `mint_co_session_jwt`, resolver co-branch + fail-closed, **SR-2 data-path chokepoint**, ephemeral workspace builder, fork-on-invite, invite/join/leave API, per-sender budgets, binding hardening, co-aware respawn, audit. **Co-drive does not ship until every SR-* gate test in §8 is green.**

Each phase: a `CHANGELOG.md` bullet under `[Unreleased]`, RBAC-gated endpoints, dual-backend parity for any new repo method, vendor-agnostic content.

---

## 10. Risks / open questions

- **SR-2 blast radius.** Routing ~283 data-authz call sites through a SessionPrincipal-aware chokepoint is the largest single risk in co-drive; mis-sequencing it (shipping any co-session route before the chokepoint) is a privilege-escalation hole. Hard gate: the 403 data-path contract test must exist and pass before any co route merges.
- **Fork seeding UX (SR-8).** Summary-vs-explicit-selection-vs-fresh is a product decision; all three are secure, but they differ in usefulness. Default chosen: intersection-produced summary. Needs a product call before 5b.
- **Per-sender billing/cap accounting (SR-10).** Counting one co-session against every active participant's cap changes user-visible cap math; confirm the desired product behavior (and the `/agnes-status` wording) with operators.
- **Respawn-on-leave cost (SR-7).** Re-spawning the runner on every leave to re-fix the plugin set is correct but expensive for large sessions; acceptable for chat-rate traffic, revisit if it bites.
- **`mint_session_jwt` scope is decorative (out of co-drive scope but flagged).** The single-user runner token (`scope="chat"`) is read only for BQ budget annotation; a compromised runner could reach admin endpoints if the user is an admin. Recommend (separate work) enforcing `scope="chat"` → reject on `require_admin`-gated paths. Likewise the `cowork`/`cowork-bundle` PAT scopes are unenforced full PATs — either enforce a setup-endpoint allowlist in `pat_resolver.py` or rename the scope to stop implying restriction. Both are pre-existing and should be tracked separately, but are listed so they aren't lost.
- **Slack `response_url` limits.** 30-min / 5-post ceiling means slash answers are single-shot; long-running `/agnes` turns surface fully only on web/DM. Acceptable by design (ephemeral is a convenience surface).
- **Manifest drift.** Two stanzas reduce the stale-`request_url` foot-gun but require operators to pick the right one; document loudly.