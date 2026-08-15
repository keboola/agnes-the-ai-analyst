# Keboola multi-project auth & auto-connect — design

Builds on the single-project Keboola OAuth provider
(`2026-08-12-keboola-auth-provider-design.md`). Goal: after a Keboola OAuth
sign-in, Agnes discovers every project the user can reach on the stack and —
per the admin's chosen mode — connects them (source connection, vaulted
project-scoped token, chat tools, RBAC groups, semantic layer) with no
hand-copied tokens anywhere.

## Modes (`auth.keboola.multi_project_mode`, switch `keboola_multi_project_mode`)

| Mode | Behavior |
|---|---|
| `disabled` (default) | The single-project provider exactly as shipped — nothing discovers, nothing provisions. Admin-managed connections only. A `single` value from older configs resolves here (same behavior) via the switch's invalid-value fallback. |
| `select` | Discovery at login; the filtered project list + the OAuth access token are stashed vault-encrypted per user (15-min TTL) and the user imports chosen projects via `GET/POST /api/auth/keboola/projects`. Already-imported projects still get membership-synced at every login. |
| `auto` | Trusted auto-provision: every allowed project is connected/refreshed on each login. |

`auth.keboola.project_id` composes orthogonally: a concrete id keeps the
single-project verify gate (PR #1288) and narrows discovery to that project;
`"*"` or unset under an active mode is the **wildcard** — the login trust
boundary becomes *"introspect lists ≥ 1 project with an allowed role"*, which
therefore **fails closed**: an introspect failure or an empty filtered list
rejects the login. Provisioning, by contrast, is per-project best-effort and
never blocks a login that passed its gates.

The wildcard boundary is deliberately **stack-shaped, not organization-shaped**:
a Keboola role is per-project and every user is admin of their own project, so
on a shared multi-tenant stack the wildcard admits any user of the stack
regardless of `allowed_roles` — the roles filter narrows which of a user's
projects take part, never which organization may sign in. The wildcard is for
dedicated (single-organization) stacks; a shared stack pins a concrete
`project_id`. Documented as a CAUTION in `config/instance.yaml.example`, the
switch description and `docs/feature-flags.md` (Devin Review, tenth round).

## Platform APIs (OAuth host; real but publicly undocumented — parsed defensively)

- `GET /v1/auth/token/introspect` (Bearer access token) →
  `{"projects": [{id, name, role}, …]}` — the discovery read.
- `POST /v1/auth/pat/exchange` (Bearer) with
  `{"scope": {"projects": [<id>]}, "readOnly": bool}` → a project-scoped PAT
  (`token`/`pat`/`value` keys accepted; 404/405 retries `/v1/auth/pat` once).
  `readOnly=false` only for the `admin` role.

Both live in `app/auth/providers/keboola_projects.py`, HTTP through
`_fetch_*` only (tests monkeypatch them), same https-only + SSRF
validate-at-use posture as `keboola_verify`, no token material in logs.

## Verify-gate changes (`keboola_verify.py`)

Under the wildcard the OAuth path skips the project binding **and** the
home-project role gate — `admin.role` on `/tokens/verify` is the role in the
token's *home* project only, and a user who is admin of project A must not
be turned away because the OAuth token's home project B lists them as guest.
`filter_projects` enforces `allowed_roles` across every project instead. The
`X-StorageApi-Token` header path keeps the role gate (a plain Storage token
cannot call introspect) and, on a wildcard instance, accepts existing users'
master tokens from any project.

## Provisioning (`app/auth/keboola_provisioning.py`)

One connection per `(stack_url, project_id)` — the identity the semantic
layer already dedupes on. Per discovered project: ensure group → find/create
connection → mint PAT → verify it against the stack (refuse on project
mismatch) → vault as storage token (+ master slot when `isMasterToken`;
non-master logs the semantic-layer skip) → propagate to the derived
chat-tools source's copy. **Credential ownership:** rotation only on rows
this same user auto-provisioned (`config.user_email`); anything else only
ever gets an *empty* slot filled — a human-stored credential is never
overwritten, and admin-managed connections keep working untouched.

RBAC: `kbc-{project_id}-{role}` groups (`ensure(created_by="system:keboola-sync")`),
membership diffed per login on `source='keboola_sync'` rows only. Tool
grants per role — admin everything, other roles non-mutating only (the
passthrough policy gate additionally refuses mutating tools to non-admin
Agnes users). Grants are **not** revoked by the sync: `tool_grants` carries
no provenance, so deletion could destroy admin-made grants — losing a
project upstream removes the *membership*, which is the per-user lever.

The slow tail — chat-tools enable (MCP introspection; only when no derived
source exists, so an admin's deliberate off-switch is never re-enabled) and
the semantic-layer refresh (shared single-flight guard in
`keboola_semantic_layer_refresh.py`) — runs as a post-response
`BackgroundTask`.

## Out of scope / follow-ups

- Cross-stack: `source_connections.config.stack_url` already carries the
  stack, but each stack needs its own OAuth client + login flow (Keboola
  tokens are stack-scoped). Phase 2.
- A web page for the select-mode import (the REST surface is the contract;
  see `docs/api-reference.md` §`/api/auth/keboola`).
- Connection/grant garbage collection for projects no user can reach any
  more — needs a cross-user reconcile, not a per-login sync.
- Deployment values (Helm chart/env for a managed instance) live in the
  operator's own infra repo; Agnes only defines the config surface
  (`AGNES_KEBOOLA_MULTI_PROJECT_MODE`, `auth.keboola.*`).
