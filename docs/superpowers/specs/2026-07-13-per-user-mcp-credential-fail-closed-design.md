# Per-user MCP credential passthrough — fail-closed design

**Date:** 2026-07-13
**Status:** Approved (brainstorm), pending implementation plan

## Problem

Universal-MCP sources can be registered with `scope='per_user'` so that each
analyst's tool calls forward under *their own* upstream credential (RFC #461 §4
per-user credential passthrough). The intent is that the upstream system (e.g. a
CRM with row-level security) authenticates the individual analyst and returns
only the data that person is entitled to.

The current secret-resolution logic (`connectors/mcp/client.py`
`_lookup_secret_for_source`) defeats that intent. Its precedence is:

1. per-user secret (`mcp_user_secrets`) keyed on `(source_id, caller_user_id)` —
   only when `scope='per_user'` **and** a caller id was threaded through;
2. **shared secret** (`mcp_secrets`) keyed on `source_id`;
3. env var named by `auth_secret_env`.

Step 2 is an unconditional fallback. So for a `per_user` source, an
**authenticated caller who has not set their own token silently falls through to
the shared service credential** — and sees everything that shared credential can
see. A source that was configured precisely to scope data per-user instead
leaks the full shared view to any granted-but-tokenless caller. This is a
data-exposure bug, not a convenience feature.

At the same time, the shared fallback is *required* for one legitimate path:
scheduled materialize jobs run with **no calling user** (`caller_user_id=None`)
and must use the shared credential to produce their snapshot tables. The current
code lumps "no caller (materialize)" and "caller with no personal row" into the
same shared fallback; only the first is legitimate.

## Goal

Make `scope='per_user'` sources fail closed for interactive callers without a
personal credential, while keeping the shared credential available to the
caller-less materialize path.

Non-goals: changing `scope='shared'` behavior; per-source opt-in flags (a
`per_user` source that leaks to shared is a bug, so the fix is unconditional for
`per_user`); redacting or filtering upstream responses beyond existing PII
redaction; any change to how the upstream system itself enforces RLS.

## Design

### Secret-resolution matrix (`per_user` source)

| Caller context | Today | After |
|---|---|---|
| Materialize job (`caller_user_id=None`) | shared | **shared** (unchanged) |
| Interactive caller **with** a per-user row | own token | own token (unchanged) |
| Interactive caller **without** a per-user row | ⚠️ shared (leak) | **no token → fail closed** |

Precise rule in `_lookup_secret_for_source`:

- `scope='per_user'` **and** `caller_user_id is not None`:
  - return the per-user secret if present;
  - otherwise return `None` **and do not consult the shared/env paths**. A
    per-user source with an identified caller never borrows the shared
    credential.
- `scope='per_user'` **and** `caller_user_id is None` (materialize): unchanged —
  fall through to shared, then env.
- `scope='shared'`: unchanged — shared, then env.

Returning `None` for the tokenless interactive caller means the connector makes
an unauthenticated upstream connect (matching `auth_method='none'`), which the
upstream rejects — but we do better than relying on that: the passthrough
endpoint fails the call explicitly (below).

### Defense in depth — explicit endpoint error

`app/api/mcp_passthrough.py` `invoke_passthrough_tool`: when the resolved source
is `scope='per_user'` and the authenticated caller has **no** `mcp_user_secrets`
row, return `403` with an actionable body before forwarding — e.g.
`{"detail": "no personal credential for source '<name>'. Run `agnes mcp
my-secret set <source>` to connect your own account."}`. This gives the analyst
a clear next step instead of an opaque upstream auth failure, and guarantees the
tokenless path can never reach the upstream even if a future refactor changes
the lookup.

Both layers are covered by tests so neither can silently regress.

### Components touched

- `connectors/mcp/client.py` — `_lookup_secret_for_source` precedence change
  (the single behavioral fix). No signature change.
- `app/api/mcp_passthrough.py` — pre-forward per-user-secret existence check +
  actionable 403.
- `src/repositories/` — `per_user_secrets_repo` read path already exists and is
  factory-routed (DuckDB + `_pg` sibling). No new method expected; confirm the
  existence check (`get` returning falsy) works identically on both backends and
  is covered by the cross-engine contract test.
- Tests — see below.

No schema migration: `mcp_user_secrets` already exists; this is behavior only.

## Rollout (operational — outside the code change, gated on verification)

1. **Verify upstream RLS (blocking prerequisite).** Confirm the upstream MCP
   (the customer CRM in the motivating case) issues per-user credentials bound
   to each person's permissions and applies row-level security on them. Verify
   empirically: two accounts with different upstream access → the passthrough
   returns different data under each one's stored token. Until this passes, the
   source stays `scope='shared'` and admin-only. This is a real risk: if the
   upstream treats all tokens as equivalent, per-user scoping is cosmetic.
2. Switch the source to `scope='per_user'` (keep the shared vault secret — the
   materialize path still needs it).
3. Onboard analysts: each stores their own credential via
   `agnes mcp my-secret set <source>`.
4. Grants can then be widened to the appropriate group — a tokenless grantee
   sees nothing (fail-closed), so a broad grant no longer implies broad data
   exposure.

The rollout steps are operator actions, documented here but not part of the code
PR. The code PR is safe to ship independently: it only tightens `per_user`
behavior and changes nothing for existing `shared` sources.

## Testing

Unit / contract (the behavioral matrix, both backends where state is involved):

- Materialize path (`caller_user_id=None`) on a `per_user` source → resolves the
  shared secret (regression guard: materialize must keep working).
- Interactive caller **with** a per-user row → resolves their token, never the
  shared one (assert the returned value is the per-user value even when a shared
  row also exists).
- Interactive caller **without** a per-user row on a `per_user` source →
  `_lookup_secret_for_source` returns `None` and the shared/env paths are **not**
  consulted (assert no shared value leaks even when a shared row exists).
- `scope='shared'` source → unchanged (shared then env).
- `invoke_passthrough_tool`: `per_user` source + granted caller + no personal
  secret → `403` with the actionable message, and **no** upstream forward
  happens (mock the connector; assert it was not called).
- Cross-engine contract test extended so the per-user-vs-shared resolution is
  asserted on both DuckDB and Postgres.

CHANGELOG entry under `[Unreleased] > Fixed` (security-relevant), release-cut as
the isolated last commit per RELEASING.md.

## Risks

- **Upstream RLS assumption (highest).** Covered by the blocking verification in
  Rollout step 1.
- **Behavior change for any existing `per_user` source.** Any source already on
  `scope='per_user'` that was (knowingly or not) relying on the shared fallback
  for tokenless callers will start failing those calls. Audit existing
  `per_user` sources before shipping; today there are none in the OSS default
  and the motivating source is still `shared`, so blast radius is expected to be
  zero — confirm at implementation time.
- **Materialize regression.** Guarded by the explicit `caller_user_id=None →
  shared` test.
