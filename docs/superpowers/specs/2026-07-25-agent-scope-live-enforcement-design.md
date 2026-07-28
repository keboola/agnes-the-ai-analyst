# Agent Scope — Live Enforcement (V1d) — Design

Date: 2026-07-25
Status: validated design (closes the HIGH finding from the V1 `/agnes-review`)

## The problem

V1a–V1c shipped per-agent scope (`plugins_mode`/`connections_mode`/
`tables_mode`/`memory_mode` ∈ `all|selected`, enumerated in `agent_scope`) and
the spec, CHANGELOG, and PR all claim the invariant:

> **An agent can never exceed its owner**: effective capability =
> owner's RBAC grants ∩ agent scope, applied live at the authorization seams.

**Only the outer half is true.** The intersection is *computed* and
*audit-snapshotted* (`agent_scope_snapshots`), but never *enforced*:

- `app/api/broker.py::_mint_identity_jwt` mints the replayed request's
  identity with `{sub=owner_id, email, scope="chat", chat_session_id}` — **no
  agent identity**. Every downstream RBAC check (`can_access`,
  `get_accessible_tables`, `resolve_user_marketplace`, MCP tool resolution)
  therefore authorizes against the **owner's full grants**.
- `app/chat/agent_profile.py`'s own docstring admits it: *"this snapshot is
  audit-only … nothing in this module (or in `_spawn_live`) subsets the
  workspace materialization by the computed scope. Live seam enforcement is
  V1b work."* That work was never scheduled into any V1b/V1c task — a
  planning gap, not an implementation slip.

**Consequence.** A `'selected'`-scoped agent — and the agent PAT issued for
it, which V1a deliberately restricts to `'selected'`-only agents precisely
*because* it is meant to be a constrained credential — grants its holder the
owner's entire table/plugin/connection surface. An operator who scopes an
agent to two tables and hands the PAT to an automation is wrong about what
they handed over. This is a confused-deputy gap.

## The key insight: the seam already exists

Agnes already solves exactly this shape for **co-drive sessions**. A
co-session is driven by 2+ humans and must run at the *intersection* of their
grants, never any one participant's full set:

- `app/auth/session_principal.py::SessionPrincipal` — a frozen dataclass
  carrying `intersection: dict[resource_type -> frozenset[resource_id]]`.
- `src/grant_intersection.py::compute_grant_intersection` — builds it from
  `_allowed_ids_for_user` (the no-admin-short-circuit grant primitive),
  **fail-closed**: an unknown participant yields `{}`.
- `app/auth/pat_resolver.py:159-176` — on a `typ="co_session"` token the
  resolver returns a `SessionPrincipal` instead of a user dict, rebuilt live
  per request (no grants baked into the JWT — no stale-replay window).
- Every consumer already honors it: `get_accessible_tables` (`src/rbac.py`)
  branches on `isinstance(user, SessionPrincipal)` and returns the
  intersection's `ResourceType.TABLE` set; `can_access` has a principal
  branch; `require_admin` **hard-denies** a principal before any
  `is_user_admin` check.

So the fix is **not** three new `agent_id` checks sprinkled across the
marketplace, MCP, and table seams (each a new place to get wrong). It is to
route agent-scoped sessions through the *same, already-audited* choke point:
mint a principal whose intersection is `(owner grants ∩ agent scope)`.

One mechanism, already fail-closed, already admin-denying, already respected
by all three seams.

## Design

### 1. `AgentPrincipal` — a sibling of `SessionPrincipal`

Add to `app/auth/session_principal.py`:

```python
@dataclass(frozen=True)
class AgentPrincipal:
    """Auth subject of a live agent-scoped session.

    Effective authority = owner's grants ∩ the agent's declared scope.
    Never the owner's full set, never the Admin god-mode short-circuit —
    an agent is a *restriction* of its owner, never an elevation.
    """
    session_id: str
    agent_id: str
    owner_user_id: str
    owner_email: str
    intersection: dict[str, frozenset[str]]
```

**Why a distinct class, not a reused `SessionPrincipal`:** the two carry
different identity (one agent vs N participants), and downstream code that
means "co-drive" must not silently start matching agent sessions. Both are
handled by a shared `isinstance(user, (SessionPrincipal, AgentPrincipal))`
guard at each consumer, so there is exactly one place per seam to update and
a `Principal` union type makes the intent explicit.

Introduce `Principal = SessionPrincipal | AgentPrincipal` and switch existing
`isinstance(user, SessionPrincipal)` checks to the union **where the semantic
is "restricted principal"** (deny admin, use intersection) — and leave them
alone where the semantic is genuinely co-drive-specific.

### 2. `compute_agent_intersection` — owner grants ∩ agent scope

New `src/agent_scope_intersection.py`:

```python
def compute_agent_intersection(
    owner_user_id: str, agent_row: dict, conn=None
) -> dict[str, frozenset[str]]:
```

For each `ResourceType`:
1. `owner = _allowed_ids_for_user(owner_user_id, rt.value, conn)` — the same
   no-short-circuit primitive `compute_grant_intersection` uses.
2. If the agent's mode for that resource type is `'all'` → the agent does not
   narrow it → result is `owner`.
3. If `'selected'` → result is `owner & {ids from agent_scope for that
   item_type}`.

**Mode → resource-type mapping** (the `agent_scope.item_type` vocabulary is
narrower than `ResourceType`):

| agent mode column   | `agent_scope.item_type` | `ResourceType`       |
|---------------------|-------------------------|----------------------|
| `tables_mode`       | `table`                 | `TABLE`              |
| `plugins_mode`      | `plugin`                | `MARKETPLACE_PLUGIN` |
| `memory_mode`       | `memory_domain`         | `MEMORY_DOMAIN`      |
| `connections_mode`  | `connection`            | *(see below)*        |

`connection` has **no `ResourceType` member** — per-user MCP connections are
authorized by a different mechanism (`tool_registry` passthrough ACLs keyed on
groups). Two consequences, both explicit:

- Connection narrowing is **not** delivered by the intersection map; it needs
  its own filter at the MCP tool-resolution seam (Task 4).
- **Resource types the agent does not model at all** (`DATA_PACKAGE`,
  `RECIPE`, `COLLECTION`, `SLACK_CHANNEL`, `KNOWLEDGE_DIGEST`, `DATA_APP`,
  `MEMORY_ITEM`, `CHAT`) pass through as the **owner's** set. This is
  deliberate and must be documented: an agent narrows what it declares, and
  nothing else. It never *widens* (every entry is an intersection with the
  owner's set), so the "agent ⊆ owner" invariant holds for every resource
  type, including ones the agent has no vocabulary for.

**Fail-closed rules** (mirroring `compute_grant_intersection`):
- Unknown/missing agent row → `{}` (deny everything).
- Owner not found → `{}`.
- An unrecognized mode value (neither `all` nor `selected`) → treat as
  `'selected'` **with an empty allowlist** → `frozenset()` for that type. Note
  this is the *opposite* of `compute_effective_scope`'s current audit-only
  behavior, which fails **open** to `"all"` with a warning. Enforcement must
  fail closed; the audit helper is being brought in line (Task 2) so the two
  cannot disagree.

### 3. The broker mints an agent principal

`app/api/broker.py::_mint_identity_jwt` gains a third branch, ordered
**after** the co-session branch (a co-session is never agent-scoped):

- co-session → `mint_co_session_jwt` (unchanged)
- solo session **with `agent_id`** → `mint_agent_session_jwt(session_id)`:
  `typ="agent_session"`, synthetic `sub=f"agent-session:{session_id}"`, **no
  real user id**, `extra_claims={"scope": "chat", "chat_session_id": ...}`.
  Like the co-session token it bakes in **no grants** — the resolver rebuilds
  the intersection live per request, so revoking a grant or narrowing the
  agent takes effect on the next request with no stale-replay window.
- solo session without `agent_id` → today's owner identity JWT (unchanged —
  this is the legacy/no-agent path).

`app/auth/pat_resolver.py` grows the matching `typ="agent_session"` branch:
load the session → its `agent_id` → the agent row → the owner → return
`AgentPrincipal(intersection=compute_agent_intersection(...))`. Fail closed on
every missing link (session, agent, soft-deleted agent, owner).

**Default-agent carve-out.** Every user has a lazily-seeded default agent with
all four modes `'all'`. `compute_agent_intersection` for such an agent returns
exactly the owner's sets — mathematically identical to today's behavior — but
it would still route web chat through the principal path, changing the
`user` type every existing chat consumer sees. To keep the blast radius at
zero for the default web-chat path, the broker mints an agent-session token
**only when the agent actually narrows something** (any of the four modes is
`'selected'`). An all-`'all'` agent keeps the current owner-identity JWT.
Documented as an optimization with an identical-authority argument, not a
security exception.

### 4. The three seams

With the principal in place, two of the three seams need **no new logic** —
only their existing `SessionPrincipal` branch widened to the union:

- **Tables** — `src/rbac.py::get_accessible_tables` already returns
  `user.intersection[TABLE]` + internal tables for a principal. Widening the
  isinstance covers `/api/query`, catalog, and every table read.
- **Plugins/marketplace** — `src/marketplace_filter.py::resolve_user_marketplace`
  resolves via `_user_group_ids` + `resource_grants`. It needs a principal
  branch that filters to `intersection[MARKETPLACE_PLUGIN]` instead.
- **MCP connections** — the one genuinely new filter (no `ResourceType`).
  At tool resolution, when the caller is an `AgentPrincipal` whose
  `connections_mode == 'selected'`, keep only tools whose source id is in the
  agent's `connection` scope rows.

And the guard that must **not** be forgotten: `require_admin` currently
hard-denies `SessionPrincipal`. It must deny `AgentPrincipal` too — an agent
must never reach an admin endpoint, even when its owner is an admin. This is
the single most important line in the change.

### 5. What this does *not* change

- **Agent PAT authentication** (`typ="agent_pat"`) at the public `/api/v1`
  edge is unchanged: those requests still resolve to the owner's user dict for
  *endpoint* authorization (ownership checks, CHAT grant). The intersection
  governs what the **sandbox** can reach through the broker — which is where
  data, plugins, and tools are actually consumed. (A follow-up may narrow the
  edge too; it is not required to close this finding, because the edge routes
  only ever operate on the caller's own agents.)
- The audit snapshot (`agent_scope_snapshots`) stays — it now records what was
  *enforced*, not merely what was computed.

## Testing

The invariant deserves adversarial tests, not just happy paths:

- **Intersection unit tests**: `'all'` → owner's set verbatim; `'selected'` →
  strict subset; a scope row naming a table the owner does **not** have →
  absent from the result (agent cannot widen); unknown mode → empty; missing
  agent/owner → `{}`.
- **Admin denial**: an `AgentPrincipal` whose owner **is an admin** hitting an
  admin endpoint → 403, and `get_accessible_tables` → the intersection, never
  `None` (the admin "all" sentinel). This is the elevation test that matters.
- **Seam tests**: a selected-scope agent's brokered `/api/query` for a
  non-scoped table → denied; the same query as the owner directly → allowed
  (proving the restriction is the agent's, not a broken grant).
- **Regression**: the default all-`'all'` agent and a plain no-agent session
  behave exactly as before (web chat unchanged); a co-session still resolves
  to `SessionPrincipal` with its own intersection.
- Dual-backend contract coverage for any new repo read.

## Rollout

This lands on the same PR as V1a–V1c (#1034), because merging that PR without
it would ship the false claim. The CHANGELOG bullet for agent scope must not
describe enforcement until this is in.

## Known remaining gaps (recorded at Task 6, not closed by this wave)

The "Agent ⊆ owner" invariant holds for everything this design models, but
Tasks 4–5's own self-reviews surfaced four gaps deliberately left open —
recorded here so they aren't lost between waves:

- **Bundled workspace-template skills are a `∪` outside the intersection.**
  The skills baked into the spawned workspace template (not the curated
  marketplace) are not gated by `plugins_mode`/`agent_scope` at all — every
  agent, however narrowly scoped, gets the same template baseline. Only the
  curated-marketplace and Store-install plugin surfaces are intersected.
- **Internal-table names/schemas remain visible even when content is
  blocked.** `get_accessible_tables` always includes `INTERNAL_TABLES` for a
  principal (by design — row-level RBAC applies at query time, per
  `src/rbac.py::can_access_table`'s docstring), so an `AgentPrincipal` can
  see that `agnes_sessions`/`agnes_usage`/`agnes_audit` exist and their
  schemas, even when its own row-level view yields zero rows. Metadata
  visibility, not data leakage — but worth naming explicitly.
- **Data packages pass through as the owner's grants, not a subscribed
  stack.** `ResourceType.DATA_PACKAGE` has no `agents.*_mode` column
  (§2's mode table), so `compute_agent_intersection` passes it through
  verbatim as the owner's set (the "resource type the agent does not model"
  branch) — an agent inherits its owner's *entire* data-package stack
  (required ∪ subscribed), not some agent-specific subset. Table-level
  narrowing still applies on top via the `TABLE` intersection, so this is
  bounded, but it means `tables_mode='selected'` is the only real table-axis
  lever today.
- **The Store-install filter is correct but barely exercised on the live
  path.** `agent_scope_filter`'s Store-install half
  (`plugins_mode`/`'plugin'`) is unit-tested (`tests/test_agent_scope_mcp.py`,
  `tests/test_agent_scope_seams.py`) but sees little real traffic today:
  `bootstrap_marketplace` (the flag that seeds a sandbox's marketplace
  registration) is off by default, and even when on, a spawned sandbox
  generally cannot reach `/marketplace.git/*` directly (network egress is
  broker-mediated). Correctness is proven at the unit level; end-to-end
  live-path coverage for this specific axis is a follow-up, not a blocker —
  the seam that matters most (tables, via `/api/query`) has the full E2E
  proof in `tests/test_agent_scope_e2e.py`.

**Bug found and fixed while building the Task 6 E2E proof (not a scope
gap, a pre-existing crash):** `app/api/query.py`'s non-internal query path
called `user.get(...)` unconditionally in several audit-log / BQ-quota-key
/ BQ-path-admin-check spots, and `src/audit_helpers.py::client_kind_from_user`
did the same. Neither had ever been exercised by a *successful* (200,
non-internal-table) request under a restricted principal (`SessionPrincipal`
or `AgentPrincipal`) before this suite — co-session tests only covered the
`/api/data/*/check-access` and denied-request paths, and no agent-session
request existed until V1d. The result: a properly-scoped agent (or a
co-session) could reach an in-scope local table and still get a crash
(`400 Query error: 'AgentPrincipal' object has no attribute 'get'`) instead
of its rows. Fixed via a principal-aware `_identity_for_audit` helper in
`app/api/query.py` (used only for audit/bookkeeping identity, never for an
authorization decision) plus a `PRINCIPAL_TYPES` guard on the one
authorization-relevant call site (`_bq_guardrail_inputs`'s `is_admin` check,
which must stay `False` for any principal — resolving it via the owner's id
would have reintroduced admin inheritance). The identical `user.get(...)`
pattern in `app/api/v2_scan.py`, `v2_sample.py`, `v2_schema.py`, and
`app/api/data.py`'s audit-log/quota-key calls was swept in this PR too —
they now route through the shared `src/audit_helpers.identity_for_audit`
(owner identity for an `AgentPrincipal`, `None` for a co-session); the
only remaining dict-style access in those files sits inside a
principal-guarded `else` branch (`v2_sample.py`'s admin check, which must
stay dict-only so a narrowed principal cannot inherit the owner's admin
bit).
