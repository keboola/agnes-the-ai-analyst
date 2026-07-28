# Agent Scope — Live Enforcement (V1d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "agent ⊆ owner" invariant real — a `'selected'`-scoped agent's brokered requests must be authorized against (owner grants ∩ agent scope), not the owner's full grants.

**Spec:** `docs/superpowers/specs/2026-07-25-agent-scope-live-enforcement-design.md` — read it first, especially §2 (why one choke point, not three checks) and §3 (fail-closed rules + the default-agent carve-out).

**Architecture:** Reuse the co-drive authorization mechanism. A new `AgentPrincipal` (sibling of the existing `SessionPrincipal`) carries a precomputed `intersection` map; the broker mints an `agent_session` token for narrowing agents; the PAT resolver rebuilds the intersection live per request. The table and marketplace seams already honor a principal — they only need their isinstance widened. MCP connections get the one genuinely new filter.

**Tech Stack:** FastAPI, existing auth stack (`app/auth/access.py`, `pat_resolver.py`, `session_principal.py`), `src/grant_intersection.py` as the precedent, DuckDB + Postgres via the repo factory, pytest.

## Global Constraints

- **Fail closed, always.** Every unknown (missing agent row, soft-deleted agent, missing owner, unrecognized mode value) resolves to *deny*, never to "all". `compute_grant_intersection` (`src/grant_intersection.py`) is the reference implementation — an unknown participant returns `{}`.
- **Never widen.** Every entry in the intersection map is `owner_set & agent_set`. A scope row naming a resource the owner lacks must NOT appear in the result. There must be a test for exactly this.
- **No admin god-mode for a principal.** `require_admin` hard-denies `SessionPrincipal` today; it must deny `AgentPrincipal` identically. `get_accessible_tables` must never return `None` (the admin "all" sentinel) for a principal.
- **No grants baked into the JWT.** The agent-session token carries only `chat_session_id` + a synthetic `sub`, exactly like `mint_co_session_jwt` — the resolver recomputes per request so revocation has no replay window.
- **Web chat must not change.** The default agent (all four modes `'all'`) keeps the current owner-identity path; a regression test asserts this.
- **Dual-backend discipline** (CLAUDE.md): any new repo method lands in both `src/repositories/X.py` and `X_pg.py` with a contract test; reach repos only through the factory (`tests/test_backend_split_guard.py` is a static ratchet).
- **Route-auth guard + triple-surface + docs-coverage** gates stay green; no new public routes are expected in this plan.
- **Vendor-agnostic**; CHANGELOG bullet in the same PR (Task 6); no AI attribution in commits.
- Verification loop: prefer `.venv/bin/pytest <focused files> -q` per task; a bounded full run (`-n 4`, this box flakes above that) at Task 6. Known-environmental failure: `tests/test_cli_init.py::test_shortcut_windows_writes_cmd_shim` (needs Python 3.13).

---

### Task 1: `AgentPrincipal` + the `Principal` union

**Files:**
- Modify: `app/auth/session_principal.py`
- Test: `tests/test_agent_principal.py`

**Interfaces:**
- Produces:
  - `AgentPrincipal` frozen dataclass: `session_id: str`, `agent_id: str`, `owner_user_id: str`, `owner_email: str`, `intersection: dict[str, frozenset[str]]`.
  - `Principal = Union[SessionPrincipal, AgentPrincipal]` — the type later tasks use for "restricted principal" isinstance checks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_principal.py
"""AgentPrincipal is a frozen, restricted auth subject (V1d)."""
import pytest


def test_agent_principal_is_frozen_and_carries_intersection():
    from app.auth.session_principal import AgentPrincipal

    p = AgentPrincipal(
        session_id="c1",
        agent_id="a1",
        owner_user_id="u1",
        owner_email="owner@example.com",
        intersection={"table": frozenset({"t1"})},
    )
    assert p.intersection["table"] == frozenset({"t1"})
    with pytest.raises(Exception):  # frozen dataclass
        p.agent_id = "a2"  # type: ignore[misc]


def test_principal_union_covers_both():
    from app.auth.session_principal import AgentPrincipal, Principal, SessionPrincipal

    agent = AgentPrincipal("c1", "a1", "u1", "o@example.com", {})
    co = SessionPrincipal("c2", ["u1"], ["o@example.com"], {})
    for p in (agent, co):
        assert isinstance(p, Principal.__args__)  # both members of the union
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_principal.py -q`
Expected: FAIL (`ImportError: cannot import name 'AgentPrincipal'`).

- [ ] **Step 3: Implement**

Append to `app/auth/session_principal.py` (match the existing module's docstring style — explain *why*, not *what*):

```python
@dataclass(frozen=True)
class AgentPrincipal:
    """Auth subject of a live agent-scoped session (V1d).

    Effective authority = the owner's grants ∩ the agent's declared scope.
    Never the owner's full set, never the Admin god-mode short-circuit — an
    agent is a *restriction* of its owner, never an elevation. Like
    ``SessionPrincipal`` the intersection is rebuilt live per request (the
    token bakes in no grants), so revoking a grant or narrowing the agent
    takes effect on the next request with no stale-replay window.
    """

    session_id: str
    agent_id: str
    owner_user_id: str
    owner_email: str
    intersection: dict[str, frozenset[str]]


#: Either restricted principal. Consumers that mean "not a full user dict —
#: use the intersection, deny admin" should branch on this union, not on one
#: member, so a new principal kind cannot silently bypass a seam.
Principal = Union[SessionPrincipal, AgentPrincipal]
```

Add `from typing import Union` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_agent_principal.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auth/session_principal.py tests/test_agent_principal.py
git commit -m "feat(auth): AgentPrincipal — restricted auth subject for agent-scoped sessions"
```

---

### Task 2: `compute_agent_intersection` (+ align the audit helper's fail direction)

**Files:**
- Create: `src/agent_scope_intersection.py`
- Modify: `app/chat/agent_profile.py` (`compute_effective_scope` fails closed on an unrecognized mode, matching enforcement)
- Test: `tests/test_agent_scope_intersection.py`

**Interfaces:**
- Consumes: `app.auth.access._allowed_ids_for_user` (the no-admin-short-circuit grant primitive), `agents_repo().get_scope`, `app.resource_types.ResourceType`.
- Produces:
  - `compute_agent_intersection(owner_user_id: str, agent_row: dict, conn=None) -> dict[str, frozenset[str]]`
  - `MODE_TO_RESOURCE_TYPE: dict[str, tuple[str, str]]` — mode column → (`agent_scope.item_type`, `ResourceType` value), for reuse by the seams:
    `{"tables_mode": ("table", "table"), "plugins_mode": ("plugin", "marketplace_plugin"), "memory_mode": ("memory_domain", "memory_domain")}`
    (`connections_mode` is deliberately absent — `connection` has no `ResourceType`; Task 5 filters it separately.)
  - `agent_narrows(agent_row) -> bool` — True when ANY of the four mode columns is `'selected'`. The broker uses this for the default-agent carve-out.

Semantics (from the spec §2, normative):
- For each `ResourceType`, start from `_allowed_ids_for_user(owner_user_id, rt.value, conn)`.
- Mode `'all'` (or a resource type the agent does not model) → owner's set unchanged.
- Mode `'selected'` → `owner_set & {item_id for that item_type in agent_scope}`.
- Missing/empty `agent_row`, missing owner id → `{}` (deny everything).
- Unrecognized mode value → `frozenset()` for that type (fail CLOSED).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_scope_intersection.py
"""compute_agent_intersection: owner ∩ agent scope, fail-closed (V1d)."""
import pytest


def _agent(**over):
    row = {
        "id": "a1", "owner_user_id": "u1",
        "tables_mode": "all", "plugins_mode": "all",
        "connections_mode": "all", "memory_mode": "all",
    }
    row.update(over)
    return row


def test_all_mode_returns_owner_set_verbatim(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user",
                        lambda uid, rt, conn=None: frozenset({"t1", "t2"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset())
    out = mod.compute_agent_intersection("u1", _agent())
    assert out["table"] == frozenset({"t1", "t2"})


def test_selected_mode_narrows_to_subset(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user",
                        lambda uid, rt, conn=None: frozenset({"t1", "t2", "t3"}))
    monkeypatch.setattr(mod, "_agent_scope_ids",
                        lambda aid, it, conn=None: frozenset({"t2"}) if it == "table" else frozenset())
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t2"})


def test_agent_can_never_widen_beyond_owner(monkeypatch):
    """A scope row naming a table the OWNER lacks must not appear."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user",
                        lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids",
                        lambda aid, it, conn=None: frozenset({"t1", "SECRET"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t1"})
    assert "SECRET" not in out["table"]


def test_unrecognized_mode_fails_closed(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user",
                        lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t1"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="bogus"))
    assert out.get("table", frozenset()) == frozenset()


@pytest.mark.parametrize("owner,agent_row", [("", _agent()), ("u1", None), ("u1", {})])
def test_missing_inputs_deny_everything(owner, agent_row):
    from src.agent_scope_intersection import compute_agent_intersection

    assert compute_agent_intersection(owner, agent_row) == {}


def test_agent_narrows_flag():
    from src.agent_scope_intersection import agent_narrows

    assert agent_narrows(_agent()) is False
    assert agent_narrows(_agent(plugins_mode="selected")) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_scope_intersection.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement `src/agent_scope_intersection.py`**

Module-level helpers `_allowed_ids_for_user` (imported from `app.auth.access`) and `_agent_scope_ids(agent_id, item_type, conn=None)` (reads `agents_repo().get_scope(agent_id)` through the factory and filters by `item_type`) must be module attributes so the tests above can monkeypatch them — mirror how `src/grant_intersection.py` structures its helpers. Docstring must state the fail-closed contract and the "narrows only what it declares; never widens" rule from the spec.

- [ ] **Step 4: Align the audit helper's fail direction**

`app/chat/agent_profile.py::compute_effective_scope` currently logs a warning and treats an unrecognized mode as `"all"` (fails **open**). Enforcement fails closed; the audit view must not disagree with what is enforced. Change it to report the restrictive result and keep the warning. Update whichever test in `tests/test_agent_profile_spawn.py` pins the old fail-open behavior, and say so in the commit body — this is a deliberate behavior change, not an incidental one.

- [ ] **Step 5: Run to verify both pass**

Run: `.venv/bin/pytest tests/test_agent_scope_intersection.py tests/test_agent_profile_spawn.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent_scope_intersection.py app/chat/agent_profile.py tests/test_agent_scope_intersection.py tests/test_agent_profile_spawn.py
git commit -m "feat(auth): compute_agent_intersection — owner grants ∩ agent scope, fail-closed"
```

---

### Task 3: Broker mints an agent-session token; resolver returns `AgentPrincipal`

**Files:**
- Modify: `app/api/broker.py` (`_mint_identity_jwt`), `app/auth/access.py` (add `mint_agent_session_jwt` beside `mint_co_session_jwt`), `app/auth/pat_resolver.py` (new `typ="agent_session"` branch)
- Test: `tests/test_agent_session_principal.py`

**Interfaces:**
- Consumes: `compute_agent_intersection`, `agent_narrows` (Task 2); `AgentPrincipal` (Task 1); `chat_session_repo()`, `agents_repo()`, `users_repo()`.
- Produces:
  - `mint_agent_session_jwt(session_id: str, *, ttl: int = 3600) -> str` — `typ="agent_session"`, `sub=f"agent-session:{session_id}"`, `email=""`, `extra_claims={"scope": "chat", "chat_session_id": session_id}`. **No grants, no real user id** — same contract as `mint_co_session_jwt`.
  - Resolver branch: `typ="agent_session"` → session → `agent_id` → agent row → owner → `AgentPrincipal(...)`. Fail closed (`return None, "invalid_token"`) if the session is missing, has no `agent_id`, the agent row is missing or soft-deleted, or the owner is missing.
  - `_mint_identity_jwt` branch order: co-session (unchanged) → solo **with a narrowing agent** (`agent_narrows`) → plain owner identity (unchanged).

- [ ] **Step 1: Write the failing tests**

Cover: a narrowing agent's session mints `typ="agent_session"`; the resolver returns an `AgentPrincipal` whose `intersection` matches `compute_agent_intersection`; an all-`'all'` (default) agent still mints the plain owner JWT (**the web-chat regression guard**); a session with no `agent_id` unchanged; each fail-closed path (missing session / missing agent / soft-deleted agent / missing owner) → `(None, "invalid_token")`; the minted token carries **no** grant claims (assert the decoded payload has no intersection/scope-list keys).

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_agent_session_principal.py -q`

- [ ] **Step 3: Implement** the mint helper, the broker branch, and the resolver branch. Place the resolver branch next to the existing `co_session` branch and reuse its structure (including `_stash_payload`).

- [ ] **Step 4: Run to pass** — `.venv/bin/pytest tests/test_agent_session_principal.py tests/test_broker_routes.py tests/test_agent_pat.py -q`

- [ ] **Step 5: Commit**

```bash
git add app/api/broker.py app/auth/access.py app/auth/pat_resolver.py tests/test_agent_session_principal.py
git commit -m "feat(broker): mint agent-session identity; resolver returns AgentPrincipal"
```

---

### Task 4: Widen the principal-aware seams — admin denial, tables, marketplace

**Files:**
- Modify: `app/auth/access.py` (`require_admin`, `can_access`, and any other `isinstance(user, SessionPrincipal)` site whose meaning is "restricted principal"), `src/rbac.py` (`get_accessible_tables`), `src/marketplace_filter.py` (`resolve_user_marketplace`)
- Test: `tests/test_agent_scope_seams.py`

**Interfaces:**
- Consumes: `Principal` union (Task 1), `AgentPrincipal.intersection`.
- Produces: every "restricted principal" branch honors `AgentPrincipal` identically to `SessionPrincipal`; `resolve_user_marketplace` gains a principal branch filtering to `intersection["marketplace_plugin"]`.

**THE critical line:** `require_admin` must hard-deny `AgentPrincipal` **before** any `is_user_admin` lookup — an agent owned by an admin must not inherit admin. Write that test first.

- [ ] **Step 1: Write the failing tests**

```python
def test_admin_owned_agent_principal_is_denied_admin():
    """An agent whose OWNER is an admin must never reach an admin endpoint."""
    # build an AgentPrincipal for an admin owner, call require_admin -> 403

def test_get_accessible_tables_never_returns_all_for_agent_principal():
    """None is the admin 'all' sentinel — a principal must get a concrete list."""

def test_get_accessible_tables_returns_intersection_for_agent_principal():
    # intersection {"table": {"t1"}} -> ["t1"] (+ internal tables), never t2
```

Plus: marketplace resolution for a selected-plugins agent returns only the scoped plugins; an `'all'`-plugins agent sees the owner's set.

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_agent_scope_seams.py -q`

- [ ] **Step 3: Implement.** Grep for every `isinstance(..., SessionPrincipal)` in `app/` and `src/`; for each, decide explicitly whether it means "restricted principal" (→ widen to the union) or "co-drive specifically" (→ leave alone) and note the decision in the commit body. Do not blanket-replace.

- [ ] **Step 4: Run to pass** — `.venv/bin/pytest tests/test_agent_scope_seams.py tests/test_cosession_auth.py tests/test_rbac.py tests/test_marketplace_filter.py -q` (adjust to the real file names present in `tests/`).

- [ ] **Step 5: Commit**

```bash
git add app/auth/access.py src/rbac.py src/marketplace_filter.py tests/test_agent_scope_seams.py
git commit -m "feat(rbac): honor AgentPrincipal at the admin, table, and marketplace seams"
```

---

### Task 5: MCP connection filtering

**Files:**
- Modify: the MCP tool-resolution path (find it: `app/api/mcp/tools_generator.py` and/or `app/api/mcp_http.py` — trace how passthrough tools are filtered per caller today, `src/repositories/tool_registry.py::list_passthrough_for_groups` is the grant read)
- Test: `tests/test_agent_scope_mcp.py`

**Interfaces:**
- Consumes: `AgentPrincipal`, the agent's `connection` scope rows (`agents_repo().get_scope(agent_id)` filtered to `item_type='connection'`), `connections_mode`.
- Produces: when the caller is an `AgentPrincipal` with `connections_mode == 'selected'`, only tools belonging to a scoped MCP source are exposed/callable. `'all'` → the owner's full set (unchanged).

**Enforce at the call seam, not only at listing.** Hiding a tool from the list is discovery, not authorization — a sandboxed agent that names an unlisted tool directly must still be refused. Implement the filter where the tool is resolved for execution, and cover both in tests.

- [ ] **Step 1: Write the failing tests** — a selected-connections agent lists only its scoped tools; invoking a non-scoped tool by name is refused; an `'all'` agent is unaffected.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to pass** — `.venv/bin/pytest tests/test_agent_scope_mcp.py tests/test_mcp_http.py tests/test_mcp_tool_parity.py -q`
- [ ] **Step 5: Commit**

```bash
git add <files> tests/test_agent_scope_mcp.py
git commit -m "feat(mcp): restrict passthrough tools to an agent's scoped connections"
```

---

### Task 6: End-to-end proof, docs, CHANGELOG

**Files:**
- Test: `tests/test_agent_scope_e2e.py`
- Modify: `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md` (§4/§5 now describe reality), `app/chat/agent_profile.py` (drop the "audit-only … V1b work" docstring — it is no longer true), `docs/api-reference.md`, `CHANGELOG.md`, `CLAUDE.md` if the agent subsection claims enforcement

**Interfaces:** consumes everything above.

- [ ] **Step 1: The end-to-end test that proves the finding is closed**

Drive a real brokered request for a selected-scope agent and assert the denial, plus the control that the same request as the owner succeeds:

```python
def test_scoped_agent_cannot_query_a_table_outside_its_scope():
    # owner has t1 + t2; agent scoped to t1 only
    # brokered /api/query for t2 as the agent -> denied
    # same query as the owner directly -> allowed
```

Also: the default all-`'all'` agent behaves exactly as before (web chat regression), and the audit snapshot now matches what is enforced.

- [ ] **Step 2: Run to verify it fails on the pre-Task-3 behavior** (if the earlier tasks are already in, assert it passes and note that the pre-fix tree was verified by the reviewer's finding).

- [ ] **Step 3: Docs** — the spec's §4/§5 currently describe enforcement that did not exist; rewrite them to describe what now does, and add a short "V1d" note recording that V1a–V1c shipped the computation and V1d shipped the enforcement. Remove the stale "audit-only" docstring. CHANGELOG bullet under `## [Unreleased]`:

```markdown
### Fixed
- Agent scope is now enforced at request time, not merely recorded. A
  `selected`-scoped agent's brokered requests are authorized against
  (owner grants ∩ agent scope) via a restricted `AgentPrincipal`, so an
  agent PAT can no longer reach tables, plugins, or MCP tools outside the
  agent's declared scope. Agents never inherit their owner's admin
  authority. Previously the scope was computed and audit-snapshotted but
  the brokered request still ran with the owner's full grants.
```

- [ ] **Step 4: Full suite (bounded)**

Run: `.venv/bin/pytest tests/ --tb=line -n 4 -q`
Triage anything failing by re-running it in isolation before calling it a regression.

- [ ] **Step 5: Commit + push**

```bash
git add <files>
git commit -m "test(security): end-to-end agent-scope enforcement proof + docs"
git push origin HEAD:refs/heads/zs/agent-profiles-and-agent-api
```

---

## Execution notes

- Task order is dependency order (1 → 2 → 3 → 4 → 5 → 6). Tasks 4 and 5 are independent of each other once 3 lands.
- After Task 6, re-run `/agnes-review` — the RBAC reviewer's HIGH finding should flip to satisfied, and the release-cut must be re-cut as the final commit on the PR (a separate rules-reviewer finding).
- The other outstanding `/agnes-review` findings (doc drift on `docs/architecture.md:340`, missing schema/Alembic ids in the CHANGELOG `[0.77.0]` section, stale `_AGENT_SESSION_REASON` text, Alembic revision-id/filename mismatch) are cheap and can be folded into Task 6's docs commit.
