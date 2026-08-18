# Agent Schedules — scheduled runs for agent profiles

**Date:** 2026-08-17
**Status:** approved (design review in-session)

## Motivation

Agent profiles can be invoked programmatically (`POST /api/v1/agents/{slug}/responses`),
but nothing inside Agnes can invoke them *on a cadence*. The original
agent-profiles design (2026-07-21) deliberately assumed cron lives outside the
product ("hand a single credential to an automation — cron, n8n — and get
answers"). That stops short for autonomous-agent workloads where the whole
point is unattended operation: a knowledge-management agent that runs a
morning briefing at 07:00 weekdays, lighter consolidation passes mid-day, a
self-improvement pass at night, and research sessions on demand — each run
type with its own prompt and its own cadence.

This feature makes schedules a property of the agent, owned and managed by the
agent's owner, executed by Agnes's existing scheduler + worker machinery.

## Non-goals

- No new execution engine. Runs go through the existing `agent_response`
  background job kind — model pinning, token budgets, scope enforcement,
  memory notebooks, artifact harvest, and webhooks all apply unchanged.
- No per-schedule result inbox/UI. V1 delivery = the job result +
  the agent's existing `job.completed` / `job.failed` webhooks +
  `last_status` / `last_job_id` on the schedule row.
- No builder-page UI panel in V1 (REST + CLI only; UI is a fast follow).
- No change to `bootstrap_marketplace` semantics (instance-wide flag; a
  scheduled agent that needs marketplace plugin skills requires it enabled).

## Storage

New table `agent_schedules`, both backends (DuckDB ladder step + Alembic
twin), repos `agent_schedules.py` / `agent_schedules_pg.py` behind the
factory, cross-engine contract test.

| column         | type      | notes |
|----------------|-----------|-------|
| `id`           | TEXT PK   | uuid |
| `agent_id`     | TEXT      | FK → `agents.id`; schedules die with the agent (delete cascade in repo `delete_for_agent`) |
| `name`         | TEXT      | run-type label, unique per agent (e.g. `morning-briefing`); single safe path-ish segment, ≤64 chars |
| `schedule`     | TEXT      | existing grammar — `every Nm`/`every Nh`, `daily HH:MM[,HH:MM]` UTC, `cron <5-field>`; validated with `src.scheduler.is_valid_schedule` at write |
| `prompt`       | TEXT      | the `input` sent to the agent, non-empty |
| `enabled`      | BOOLEAN   | default TRUE |
| `last_run_at`  | TIMESTAMP | set when the dispatcher claims the row |
| `last_status`  | TEXT      | `enqueued` / `failed_enqueue` (job-terminal states live on the job + webhooks) |
| `last_job_id`  | TEXT      | last enqueued job id, for `GET /api/v1/jobs/{id}` |
| `created_at`, `updated_at` | TIMESTAMP | |

Cap: ≤20 schedules per agent (400 `schedule_limit` beyond), mirroring the
"unattended fan-out needs a ceiling" concern.

## Dispatch

`POST /api/v1/agents/run-due` — gated like other scheduler-driven sweeps
(`require_admin`; the scheduler token resolves to an Admin-group synthetic
user). Modeled on `POST /api/scripts/run-due`:

1. Walk `enabled=TRUE` rows joined to live agents (`status='active'`).
2. `is_table_due(schedule, last_run_at)` decides due-ness (same catch-up
   semantics as every other schedule in the product), with one deliberate
   deviation: `last_run_at` is stamped at creation, so a brand-new schedule
   anchors its cadence there and never fires an immediate catch-up run — an
   unattended agent run spends tokens; the create-then-instant-fire surprise
   a data sync tolerates is wrong here.
3. Atomic claim: repo `claim_for_run(id, now)` updates `last_run_at` only if
   unchanged since read (optimistic, single-writer per row) — a concurrent
   sweep can't double-fire.
4. Enqueue the existing `agent_response` job kind, `mode="fresh"`, with the
   **agent owner's** `owner_user_id`/`owner_email` in the payload — the exact
   shape `_run_agent_response` already consumes. This is what makes
   scheduler-initiated runs work at all: the public `/responses` endpoint
   resolves agents by (caller, slug) and the scheduler owns no agents, so the
   sweep enqueues directly instead of impersonating.
5. `idempotency_key = "agent-schedule:<schedule_id>:<floor(now/60)>"` — a
   retried sweep within the same minute dedupes at the jobs table.
6. Record `last_status` / `last_job_id`; per-row failures are logged and
   skipped, never abort the sweep.

One new row in the scheduler's `build_jobs()`: `agents:run-due`, `every 1m`,
gated on `SCHEDULER_AGENT_SCHEDULES` (default on, like scripts), hitting the
endpoint above. The scheduler still owns no DB access; due-evaluation lives
server-side.

Concurrency: enqueueing N due runs on one tick is safe — the LIGHT job lane
throttles execution (2 slots) and per-user session caps still apply
(`ConcurrencyCapHit` → job retry/backoff as today).

## Owner surface

REST, owner-gated exactly like the existing agent CRUD — `require_session_token`,
which rejects every PAT flavor (plain user PATs included), matching
`agents_admin.py` / `agent_webhooks.py`. The CLI therefore needs an
interactive `agnes auth` session, same as the rest of agent management:

- `GET    /api/v1/agents/{slug}/schedules`
- `POST   /api/v1/agents/{slug}/schedules`   `{name, schedule, prompt, enabled?}`
- `PATCH  /api/v1/agents/{slug}/schedules/{schedule_id}`
- `DELETE /api/v1/agents/{slug}/schedules/{schedule_id}`

Validation: `is_valid_schedule` (400 `invalid_schedule` naming the accepted
grammar — hint the `cron ` prefix, the known footgun), non-empty prompt,
unique `name` per agent (409 `schedule_name_taken`).

CLI (`cli/commands/agent.py` family):

```
agnes agent schedule list <slug>
agnes agent schedule add <slug> --name morning-briefing \
    --schedule "cron 0 7 * * 1-5" --prompt "/scout-briefing"
agnes agent schedule remove <slug> <name>
agnes agent schedule enable|disable <slug> <name>
```

Flag vocabulary per the command-UX standard; "not found" errors hint the next
step.

## Testing

- Contract test `tests/db_pg/test_agent_schedules_contract.py` driving both
  backends through create/list/update/claim/delete.
- Endpoint tests: CRUD auth matrix (owner ok, other user 404, agent PAT 403),
  validation errors, run-due sweep (due row enqueues the right job payload +
  idempotency key; not-due row untouched; disabled row untouched; claim is
  atomic under a simulated concurrent sweep).
- Scheduler row: `build_jobs()` includes `agents:run-due` with the env gate.
- CLI: happy path + invalid schedule message.

## Worked example (first user: an autonomous knowledge agent)

One agent profile with schedule rows:

| name | schedule | prompt |
|---|---|---|
| morning-briefing | `cron 0 7 * * 1-5` | invoke the briefing skill |
| weekend-briefing | `cron 0 9 * * 0,6` | invoke the briefing skill (weekend mode) |
| consolidation | `cron 0 11,14,17 * * 1-5` | invoke the consolidation skill |
| dream | `cron 0 21 * * *` | invoke the self-improvement skill |

For the runs to see marketplace plugin skills, the instance enables
`chat.bootstrap_marketplace` and the plugin is granted + subscribed for the
owner and included in the agent's plugin scope. State the agent keeps between
runs beyond its memory notebook should live in a git repo its prompt tells it
to clone/push — sandboxes are ephemeral.
