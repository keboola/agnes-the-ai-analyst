# Three-Plane Wave 2-F — Gateway / Chat HA (WS D)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make chat replica-safe: session routing leases (`chat:{id} → gateway`), a WS frame envelope with monotonic ids + Redis-Streams outbound replay, an inbound command stream (user→runner across gateways), cross-gateway claim-then-respawn takeover (v1 = destroy+respawn+context-replay, NOT live-process handoff), owner-scoped reapers, and absorbing the standalone `ws_gateway`/`telegram` notification path into the coordination fabric — so the `UVICORN_WORKERS>1`/multi-replica chat gate can finally be lifted.

**Architecture:** All new shared state rides the wave-2C `CoordinationBackend` (memory default = today's single-process behavior byte-for-byte; redis = replica-safe). E2B sandboxes already survive process death (`sandbox_id`/runner refs persist in the chat DB); this wave adds the *routing* layer so any gateway can reclaim and respawn. Chat frame streams were explicitly deferred here from WS C — this is where they land. Spec §3.5. The `UVICORN_WORKERS>1` chat disable + the wave-1 gateway-role gate stay until the final task flips them, behind the redis backend only.

**Tech Stack:** Python/FastAPI WebSockets, CoordinationBackend (memory/redis Streams), E2B provider, pytest + fakeredis.

## Global Constraints

- Default (memory coordination, single process) = today's chat behavior byte-for-byte; existing chat E2E/unit suites pass unchanged. The gate lift is redis-only and opt-in.
- Every new shared-state piece rides `app.coordination` — NO new module-level dicts for cross-process state (the standing ratchet).
- Disposability: a Redis FLUSHALL mid-chat must degrade to "client reconnects, session respawns, recent context replayed" — never a crash; document at each site.
- CoordinationUnavailable in a WS path → graceful coded close (reuse the 4503 pattern from WS C-2), never an unhandled traceback.
- Full suite + m-tier smoke (Docker permitting) before push; CHANGELOG in the final task; vendor-agnostic.

---

### Task 1: Session routing leases + registry

**Files:** Create `app/chat/routing.py`; modify `app/chat/manager.py` (grep where a session goes live / is registered in `_live`). Test `tests/test_chat_routing.py`.

**Produces:** `claim_session(chat_id, gateway_id, ttl_s) -> bool` (lease `chat:{chat_id}` via coordination), `renew_session`, `release_session`, `owner_of(chat_id) -> gateway_id | None`, `this_gateway_id()`. ChatManager claims the routing lease when it brings a session live and renews it on a heartbeat; releases on teardown. Memory mode: process-local lease always granted ⇒ single owner = today.

- [ ] Tests: two managers, one chat_id → exactly one owner; owner heartbeat renews; owner death → lease expires → other can claim; memory-mode always-owner.
- [ ] Commit `feat(chat): session routing leases`

### Task 2: Frame envelope + monotonic ids

**Files:** `app/chat/manager.py` (frame emission — grep the sink/frame send path + `turn_buffer`), the WS send in `app/api/chat.py`, `SlackSinkBridge` (grep); the web client JS (grep the chat WS client in templates/static). Test.

**Produces:** every outbound frame gains `{seq: int, id: str}` (monotonic per session). A `FrameSequencer` (per session, seq stored in coordination so it survives respawn — `incr(chat-seq:{id})`). Client tracks last-seen seq. This task only ADDS the envelope + client tracking (replay wiring is task 3); behavior otherwise unchanged.

- [ ] Tests: frames carry increasing seq; seq persists across a simulated respawn (counter in coordination, not memory); client parse tolerant of the new fields (old frames without seq still handled during rollout — additive).
- [ ] Commit `feat(chat): monotonic frame envelope`

### Task 3: Outbound replay via Redis Streams

**Files:** `app/chat/routing.py` / a new `app/chat/replay.py`; `app/api/chat.py` WS reconnect path. Test.

**Produces:** each session's outbound frames are also published to a coordination stream `chat-out:{id}` (`MAXLEN ~1000`); on WS (re)connect the client sends `last_seq`, the gateway replays stream entries after `last_seq` before resuming live. Memory backend: the stream is an in-process deque (synchronous) → single-process reconnect replays from it. Bounded; FLUSHALL → stream empty → client does a full-refresh (documented fallback).

- [ ] Tests: emit N frames → reconnect with last_seq=k → receive frames k+1..N in order; empty stream after reset → client gets a full-refresh signal not an error; MAXLEN caps memory.
- [ ] Commit `feat(chat): outbound frame replay on reconnect`

### Task 4: Inbound command stream (user → runner across gateways)

**Files:** `app/chat/manager.py` (`send_user_message` — grep; it writes to the runner stdin held in-process), `app/api/chat.py` `/join` co-drive path, `services/slack_bot/events.py` (Slack sender). Test.

**Produces:** a per-session inbound command stream `chat-in:{id}`; a producer (any gateway / Slack handler / co-drive joiner) publishes user input there; the lease-holding gateway consumes and feeds its local runner stdin. Ordered, at-least-once with frame-id dedup. Memory mode: synchronous in-process delivery to the single owner = today. A message arriving at a non-owner gateway is published, not dropped.

- [ ] Tests: message published on gateway B while owner is gateway A → A's runner receives it; ordering preserved; dedup on redelivery; memory-mode direct delivery.
- [ ] Commit `feat(chat): inbound command routing across gateways`

### Task 5: Cross-gateway claim-then-respawn takeover

**Files:** `app/chat/manager.py` (the resume/respawn path — grep `_known_protocol_sessions`, `resume`, `_respawn`, the runner-protocol ticket guard). Test.

**Produces:** when a gateway receives a WS connect for a `chat_id` it doesn't own (owner lease held elsewhere or expired): it claims the routing lease (task 1), and — because the runner-protocol ticket guard makes foreign *live* resume unsafe — it **destroys the old sandbox runner and respawns** a fresh one, replaying recent turn context (the existing last-N-turns replay). This is the v1 semantic stated in the spec (NOT live handoff). The E2B sandbox_id from the DB is used to destroy the orphan. Document the trade-off (in-flight agent turn is lost; same as today's cross-restart recovery).

- [ ] Tests: session owned by A (lease), connect lands on B → B claims lease, calls provider.destroy(old sandbox_id) + spawns fresh + replays context; A's stale renew fails (lease gone) and A tears down cleanly; the runner-protocol guard is respected (no foreign live-resume attempted).
- [ ] Commit `feat(chat): cross-gateway claim-then-respawn takeover`

### Task 6: Absorb ws_gateway notification path into coordination

**Files:** `services/ws_gateway/` (the standalone service + its in-memory `connections` dict + UDS dispatch), the notify producers (grep `dispatch_to_ws_gateway`), `app/api/chat.py` or a gateway-role notification endpoint. Test.

**Produces:** desktop/browser notifications route through a coordination pub/sub channel `notify:{user}` instead of the in-memory dict + Unix-socket dispatch; the gateway role subscribes and fans out to that user's connected WS. The standalone `ws_gateway` service + its compose entry are removed (its role folds into the gateway process). Memory mode: synchronous local fan-out = today (single process). Telegram's UDS dispatch likewise switches to the channel.

- [ ] Tests: a notification published for user U → a WS held by the gateway for U receives it; cross-replica (publish on A, WS on B) delivered via redis; memory-mode local delivery; no remaining import of the removed UDS dispatch.
- [ ] Commit `feat(chat): absorb ws_gateway notifications into coordination pub/sub`

### Task 7: Lift the multi-worker/replica chat gate (redis-only) + Slack webhook producer

**Files:** `app/main.py` (the `UVICORN_WORKERS>1` chat disable + gateway-role gate — grep), `services/slack_bot/events.py` (webhook-mode handlers). Test.

**Produces:** when `coordination.backend=redis` (and PG app-state, per the guard), chat is ALLOWED with >1 worker / role-split — because tickets (WS C), routing/replay/inbound (tasks 1-5), and notifications (task 6) are all now coordination-backed. Memory/single-process keeps the existing gate (no behavior change for S tier). Slack HTTP webhook handlers become thin producers: resolve session → publish to `chat-in:{id}`; the owning gateway consumes (already built task 4).

- [ ] Tests: redis backend + workers=2 → chat_manager enabled (not None); memory backend + workers=2 → still disabled (unchanged); Slack webhook handler publishes to the inbound stream instead of touching a local runner.
- [ ] Commit `feat(chat): allow multi-replica chat on the redis coordination backend`

### Task 8: m-tier chat E2E + docs + CHANGELOG + suite

**Files:** `scripts/dev/mtier-smoke.sh` (a chat reconnect+replay + kill-gateway-mid-session assertion, Docker-gated — static if daemon down), `docs/DEPLOYMENT.md` + `docs/architecture.md` (chat HA section: routing leases, replay, respawn-takeover semantics, the gate-lift condition), `CHANGELOG.md`.

- [ ] Full suite green; m-tier smoke chat scenario added (live run deferred if Docker down — note it in the bod-3 checklist).
- [ ] Commit `docs: chat HA / gateway role (wave 2F)`

## Self-review notes

Deferred (say so): true live foreign-gateway resume (needs a persisted relay-protocol-version column — spec follow-up; v1 is respawn+replay); per-user chat concurrency cap becoming lease-derived across replicas (it stayed process-local in WS C-4 — now that routing leases exist, count `chat:{*}` leases per user — do it here IF cheap, else note as a small follow-up); WS D does NOT build DuckLake/signed-URLs (WS E/F).
