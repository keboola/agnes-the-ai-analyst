# Chat sessions decoupled from the WebSocket: pause/resume lifecycle

**Date:** 2026-06-10
**Status:** approved design, pre-implementation

## Problem

A web-chat run is killed the moment the browser WebSocket disconnects:
`ws_stream` cancels the attach task on `WebSocketDisconnect`
(`app/api/chat.py`), and `attach()`'s `finally` block calls
`kill(reason="ws_disconnect")` (`app/chat/manager.py`). Streaming `token`
frames are never persisted — only the final `assistant_message` frame is
written to `chat_messages` — so a reload or tab close mid-turn destroys the
runner, loses the partial answer entirely, and the user returns to a chat
showing only their own question.

Two further latent defects share the same root:

- `chat.e2b_kill_on_ws_disconnect` is parsed, tested, and documented, but the
  kill path never reads it — `false` has no effect.
- Nothing extends the sandbox timeout during an active session, so a chat
  active for longer than `sandbox_timeout_seconds` dies mid-conversation and
  limps on through crash-respawn, losing the agent's in-memory context.

## Goal

The session, not the WebSocket, owns the runner lifecycle. A disconnected
turn always completes and persists. A session with no connected clients is
**paused** (E2B sandbox snapshot — filesystem + memory + running processes),
not killed, and resumes with the agent's full in-memory context when the user
returns. The WebSocket becomes a pure sink.

E2B facts this design relies on (verified against the v2 SDK reference and
the persistence docs):

- `await sandbox.beta_pause()` — beta; snapshots memory + filesystem,
  including running processes. Paused sandboxes are retained indefinitely and
  bill storage only.
- `AsyncSandbox.connect(sandbox_id)` — auto-resumes a paused sandbox (~1 s).
- `sandbox.commands.connect(pid, on_stdout=…, on_stderr=…)` — reattaches to a
  running process with streaming callbacks; `commands.send_stdin(pid, data)`
  writes to it (the provider already uses `send_stdin` today).
- `AsyncSandbox.beta_create(…, auto_pause=True)` — sandbox pauses instead of
  dying when its timeout expires; covers server crashes.
- Pause severs all open connections; the command-handle streams must be
  re-established after resume. This is the riskiest assumption and gets a
  dedicated real-E2B spike before the main build (see Verification).

## Design

### 1. State model & persistence

`SessionState` gains `PAUSED` alongside `ACTIVE`/`DEAD`. `chat_sessions`
gains three nullable columns:

| column | type | written |
|---|---|---|
| `sandbox_id` | VARCHAR | at spawn; cleared on real kill |
| `runner_pid` | INTEGER | at spawn; cleared on real kill |
| `sandbox_paused_at` | TIMESTAMP | at pause; cleared on resume |

Persisting these means a paused session survives a server restart: the
manager can resume from the repo row even when no `LiveSession` exists.

Migration ships in both ladders in the same PR (DuckDB `_vN_to_v(N+1)` step
in `src/db.py` + Alembic), with matching repo methods in
`src/repositories/chat_sessions*.py` (DuckDB + `_pg` sibling) and an extended
cross-engine contract test.

### 2. Attach becomes a decision tree

Today `attach()` unconditionally spawns a runner. New order of checks:

1. **Live `ACTIVE`** → seat the WS as a sink, replay the current-turn buffer
   (§4), no spawn.
2. **Live `PAUSED`, or no live entry but the repo row has `sandbox_id`** →
   resume: `AsyncSandbox.connect(sandbox_id)` →
   `commands.connect(runner_pid, callbacks)` → rebuild the handle/adapters →
   restart pump/wait tasks → seat the sink.
3. **Resume fails** (sandbox deleted, pid gone, API error) → clear the
   persisted sandbox columns and fall back to a fresh spawn + replay of the
   recent user turns (the existing crash-respawn mechanics).
4. **Nothing exists** → today's fresh-spawn path.

### 3. Detach: sink removal, linger, pause

`ws_stream` stops cancelling the attach task on disconnect; the manager owns
the session tasks. When a WS drops, only its sink is removed. When the
*last* sink leaves:

- an in-flight turn always runs to completion and persists as usual;
- then a **linger** window (`chat.detach_linger_seconds`, default 60) absorbs
  quick reloads/navigation without pause/resume churn;
- then `beta_pause()`. On pause failure (it is a beta API) → fall back to
  kill with partial-save (§4), identical data-safety to the no-pause design.

Detach policy knob: `chat.on_detach: pause | kill` (default `pause`; `kill`
preserves today's cost-minimizing behavior for operators who prefer it).
`chat.e2b_kill_on_ws_disconnect` is deprecated: it keeps parsing, maps to
`on_detach=kill` when explicitly set to `true` in the operator's YAML, and
logs a deprecation warning. New code reads only `on_detach`.

### 4. Current-turn buffer, replay, partial save

`LiveSession` keeps the frames of the in-progress turn (`token`,
`tool_call`, …) in memory; the buffer is cleared when the turn's
`assistant_message` arrives. Uses:

- **Replay:** a newly seated sink receives the buffered frames before
  joining the broadcast list (same serialization as `add_sink`'s
  replay-then-append), so a mid-turn reconnect picks up exactly where the
  run is.
- **Partial save:** any forced death mid-turn (pause-failure kill, paused-TTL
  reaping, `max_session_seconds`, crash limit, explicit kill) persists the
  accumulated text to `chat_messages` flagged as interrupted (reuse
  `tool_calls`-style metadata or a marker in content per implementation
  plan; no schema change required beyond §1).

### 5. Reaper, limits, timeout heartbeat

- `idle_ttl_seconds` (30 min) now **pauses** instead of kills (when
  `on_detach=pause`).
- New `chat.paused_ttl_seconds` (default 7 days): paused sessions older than
  this are really killed (sandbox deleted, columns cleared) so storage does
  not accumulate forever. The reaper sweep handles this from repo rows, not
  just in-memory state.
- `max_session_seconds` (4 h) remains a hard ceiling on *active* runtime;
  pause stops the clock (track accumulated active seconds on the live
  session instead of comparing wall-clock `started_at`).
- Sandboxes are created with `auto_pause=True`, and the manager extends the
  sandbox timeout (`set_timeout`) on a heartbeat while sinks are attached.
  This also fixes the pre-existing >30-min active-session death.
- `shutdown()` pauses live sessions instead of killing them — a server
  restart no longer destroys conversations.

### 6. Other surfaces

- **`send_user_message` resumes on demand:** a message arriving for a
  `PAUSED` session (Slack DM after hours, web reader-loop race) triggers the
  §2 resume path first. Slack's permanent `SlackSinkBridge` means Slack
  sessions only ever pause via `idle_ttl`.
- **Co-sessions:** orphan = last sink of any kind departed; otherwise
  unchanged.
- **Cancel** (`mgr.cancel`) behaves as today; it only ever arrives over a
  live sink.

### 7. Provider interface

`SandboxProvider` gains:

- `pause(handle) -> None` — snapshot & detach; raises on failure.
- `resume(sandbox_id: str, runner_pid: int, *, env, …) -> SandboxHandle` —
  reconnect + reattach streams, returning a handle indistinguishable from
  `spawn()`'s.

E2B implementation per the SDK calls above; the mock/fake provider used by
the test suite implements both in-memory so the full lifecycle state machine
is unit-testable. The environment must actually install `e2b>=2.0.0`
(pyproject already pins it; stale venvs hold 1.x which lacks pause).

### 8. Frontend

Minimal: history already loads over REST and replayed frames use the same
wire format. Add a "resuming session…" status while attach performs a
resume, and a paused badge in the session list (driven by
`sandbox_paused_at` in the sessions listing).

## Implementation order

Riskiest-first, each phase independently testable:

1. **Spike (gated real-E2B test):** create sandbox → run a stdin-echo
   process → `beta_pause()` → `connect()` → `commands.connect(pid)` →
   `send_stdin` → assert output flows again. Skipped without `E2B_API_KEY`.
   If the reattach semantics fail here, the design falls back to
   "pause + fresh-runner-respawn on resume" before any manager work begins.
2. **Schema & parity:** migration + repo methods + contract tests (§1).
3. **Turn buffer + replay + partial save** (§4) — valuable alone, no E2B
   dependency.
4. **Provider pause/resume** (§7) + mock provider.
5. **Manager decoupling:** detach/linger/pause, attach decision tree,
   resume-on-message, shutdown-pauses, reaper/limits/heartbeat (§§2-3, 5-6).
6. **Config knobs + deprecation** (§3, §5), API/`ws_stream` change, frontend
   touches (§8).
7. **Docs:** `docs/cloud-chat.md` lifecycle section rewrite; CHANGELOG.

## Verification

- **Unit/integration (mock provider):** disconnect mid-turn → turn completes
  and persists; reconnect mid-turn → buffered frames replayed once, no
  duplicates; last-sink detach → linger → pause; attach to paused → resume;
  resume failure → fallback spawn + history replay; reaper pause vs
  paused-TTL kill; partial save on every forced-death path; `on_detach=kill`
  preserves legacy behavior; contract tests for the new columns on both
  backends.
- **Gated real-E2B tests:** the phase-1 spike plus one end-to-end
  pause/resume of the actual runner template; run locally/CI only when
  `E2B_API_KEY` is present.
- **Manual acceptance on a dev instance:** the original repro — ask a
  long-running question, reload mid-answer → answer continues streaming;
  close the tab, return after >linger → session resumes (~1 s) with context
  intact ("what did I ask first?"); return after `paused_ttl` → graceful
  fresh session with persisted history. Extend
  `tests/e2e/acceptance/MANUAL_RUNBOOK.md` accordingly (its current
  assertion 7 — "session killed on disconnect" — inverts).
- Full suite `.venv/bin/pytest tests/ --tb=short -n auto -q` green before
  every push, per release process.

## Out of scope

- Multi-instance shared session registry (the manager remains in-process).
- Pausing Slack-only sessions on any trigger other than `idle_ttl`.
- Cross-restart recovery of *running* (non-paused) sessions; `auto_pause`
  converts crash-orphans into paused sandboxes, which the normal resume
  path then picks up.
