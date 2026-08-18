# Agent surface bindings and grant-based mutating MCP tools

**Date:** 2026-08-17
**Status:** approved
**Driving use case:** hosting a fully autonomous "announcement channel → CMS
draft" agent (a release-notes / changelog drafting agent) entirely on Agnes:
mentioned in a Slack channel it drafts a post via a write-capable MCP tool and
replies in-thread; on a weekday schedule it sweeps the channel unprompted.
Nothing below is specific to that agent — each feature closes a generic gap in
the agent-profiles platform.

## Problem

Agent profiles (spec 2026-07-21) give Agnes named, scoped, budgeted agents with
a public runtime API. Three gaps keep a real autonomous agent from running on
them:

1. **Slack sessions are agent-less.** `services/slack_bot/events.py` never
   passes `agent_id` to `ChatManager.create_session`, so a channel mention
   always runs the generic profile — no persona, no scope, no budget
   attribution. There is no channel→agent mapping anywhere.
2. **Agents cannot invoke write-capable MCP tools, structurally.**
   `check_mutating` (`app/api/mcp_policy.py`) is admin-or-bust, and
   `caller_authority` pins `AgentPrincipal.is_admin = False` unconditionally.
   Any agent whose job includes a side effect (create a CMS draft, post a
   message) is impossible, no matter how the operator scopes it. The policy
   module reserves the fix in its own docstring: "a future iteration can
   replace the admin-or-bust check with a separate `mutating_grant` row".
3. **No scheduled agent runs.** The scheduler's job list is hardcoded; the
   only way to run an agent unattended is an external cron calling the
   `/responses` API — which re-introduces the external-infra dependency the
   agent platform exists to remove.

## Design

### 1. Slack channel → agent routing (`agent_scope` item type `slack_channel`)

A channel binding is a row `agent_scope(agent_id, 'slack_channel',
<channel_id>)`, written through the existing `PUT /api/v1/agents/{id}/scope`
(and `agnes agent scope set`). No DDL change: `agent_scope` has no CHECK
constraint; item types are validated by `_ITEM_TYPES` in
`app/api/agents_admin.py`, which gains `'slack_channel'`.

Semantics:

- **One agent per channel.** The scope PUT rejects (`409
  slack_channel_taken`) a `slack_channel` item already bound to a *different,
  non-deleted* agent. Lookup helper: `agents_repo().agent_for_scope_item(
  item_type, item_id)` (new, both backends, contract-tested).
- **Routing.** `_handle_mention` resolves the binding after the existing
  allowlist/identity/CHAT-grant gates. Bound channel → `create_session(...,
  agent_id=<bound>)`; unbound channel → today's behavior, bit for bit. The
  existing thread-session dedup wins over the binding: an existing thread
  session keeps whatever agent it was created with.
- **Slack context header.** A routed session's *first* turn (and only the
  first) is prefixed with a bracketed context line —
  `[slack context: channel=<id> thread_ts=<ts> message_ts=<ts> sender=<@Uxx>]`
  — so an agent granted Slack MCP tools can operate on the correct thread.
  Non-routed sessions get no header (no behavior change).
- **Acknowledgement reaction.** When (and only when) a mention routes to a
  bound agent, the bot immediately adds an `eyes` reaction to the mentioning
  message (best-effort; failures logged, never block the session). Requires
  the Slack app's `reactions:write` scope; documented in both Slack manifest
  docs.
- **DMs are out of scope** — bindings are channel-shaped; a DM has no
  channel identity an admin would bind.

The scope-intersection enforcement paths (`compute_agent_intersection`,
marketplace/table/connection filters) key off their own item types and ignore
`slack_channel` rows by construction; a binding grants the agent no data
authority.

**Trust model — a binding is a deliberately SHARED surface.** Any channel
member who passes the Slack gates (admin-controlled channel allowlist +
identity binding + CHAT grant) can invoke the bound agent, and the turn runs
with the AGENT's authority — owner grants ∩ agent scope, including any
`allow_mutating` tools and the agent's memory notebook — not the mentioning
user's. That is the point of a channel service agent (the driving use case:
anyone on the team asks it to draft), and it is consented twice: the owner
consents by writing the binding; the admin consents by allowlisting the
channel for the Slack surface at all. Owners should bind only channels where
"anyone here may drive my agent" is intended, and narrow the agent's scope
accordingly. A per-binding `shared` flag / mentioner-allowlist is deferred
until a second consumer needs it.

The identity is whole, not split: a routed session is created AS THE OWNER
(session row, sandbox workspace, rails, personal `CLAUDE.local.md`, and
brokered authority all resolve from the owner), identical to the agent's
API/scheduled runs. The mentioner's identity gates participation (channel
allowlist + Slack binding + CHAT grant) and rides along as sender
attribution — the first turn's `[slack context: … sender=…]` header, a
`[slack sender=…]` prefix on follow-ups — but never shapes the workspace.
Any gated channel member may continue a routed thread (the
reviewer-asks-for-a-revision flow); agent-less threads still belong to
whoever started them.

### 2. Grant-based mutating MCP tools (`tool_grants.allow_mutating`)

`tool_grants` gains `allow_mutating BOOLEAN NOT NULL DEFAULT FALSE`
(migration v121, both ladders — renumbered from v120 when the parallel agent-schedules migration merged first with that number). Policy change in
`enforce_passthrough_access` / `check_mutating`:

- Admin callers: unchanged (always allowed).
- Non-admin callers (users *and* `AgentPrincipal`s resolving to their owner's
  groups): a mutating tool call passes iff at least one of the caller's
  groups holds a `tool_grants` row for that tool with `allow_mutating=TRUE`.
  The plain (non-mutating) grant check is unchanged and still required.
- `AgentPrincipal.is_admin` stays pinned `False`; connection-scope and
  rate-limit gates unchanged. Net: an agent can call exactly the write tools
  its owner's groups were explicitly granted, and only on MCP sources inside
  its declared connection scope.

Admin surface: `POST /api/admin/mcp-tools/{tool_id}/grants` accepts an
optional `allow_mutating` flag (default false, preserving today's payloads);
re-POSTing an existing grant updates the flag. Grant listings include the
flag. CLI mirror on the existing `agnes admin mcp` tool-grant commands.

### 3. Scheduled agent runs — OUT OF SCOPE HERE

Scheduled/unattended agent runs are being designed and implemented in a
parallel effort; this spec deliberately does not touch them. The driving use
case's daily sweep consumes that feature once it lands; until then an external
cron can call `POST /api/v1/agents/{slug}/responses` with an agent PAT.

## What is deliberately NOT built

- **Vendored Ghost/Slack MCP servers.** Write-capable external tools arrive
  via the existing Universal MCP passthrough (`mcp_sources` +
  `tool_registry`); the servers themselves are deployment artifacts, not
  Agnes code. Feature 2 is what makes them agent-reachable.
- **Per-agent egress / sandbox secrets.** Passthrough tools execute
  server-side; the sandbox never needs the CMS credential or new egress.
- **`agents.surfaces` activation.** The builder's JSON stays decorative;
  bindings live in `agent_scope` where scope tooling, snapshots, and audit
  already exist. (If the builder later publishes surfaces, it can write
  scope rows.)
- **DM routing, per-mention agent selection, multi-agent channels** — YAGNI
  until a second consumer exists.
- **Hard-fail approval mode for unattended runs** (`on_approval_required=
  'fail'`) stays reserved in the agent-API spec; not needed for v1.

## Testing

- Contract tests (both backends): `agent_for_scope_item`;
  `tool_grants.allow_mutating` round-trip.
- Policy unit tests: mutating gate — admin, granted-group user, ungranted
  user, `AgentPrincipal` with/without owner-group mutating grant.
- Slack routing tests (extend `tests/test_slack_*`): bound channel passes
  `agent_id` + injects context header + adds reaction; unbound unchanged;
  existing-thread dedup wins; reaction failure non-fatal.
- Scope PUT: `slack_channel` accepted, cross-agent conflict → 409, same-agent
  re-PUT idempotent.
- Migration: `tests/test_db_schema_version.py` gate; wal-recovery runbook
  version bump.
- Triple-surface ratchet classifications for every new endpoint; OpenAPI
  snapshot refresh.

## Implementation order

1. Migration v121 (DuckDB `_v120_to_v121` + Alembic twin) —
   `tool_grants.allow_mutating`; runbook version bump. (Coordinate: the
   parallel scheduled-runs effort may also bump the schema; whichever merges
   second renumbers.)
2. Repos + contract tests (agents `agent_for_scope_item`; tool_registry
   mutating-grant support).
3. Policy: grant-based mutating gate + tests.
4. API: scope item type + 409; tool-grant flag.
5. Slack: routing + context header + reaction.
6. CLI + docs (`docs/agent-recipes/announcement-drafting-agent.md`, Slack
   manifest scope note) + CHANGELOG.
7. Verify loop (`scripts/verify_syncmap.py` → touched guards → full suite) →
   review.
