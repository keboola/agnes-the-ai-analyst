# Per-user MCP credential fail-closed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `scope='per_user'` MCP source must never lend its shared credential to an *identified* caller who has no personal credential — such calls fail closed. The shared credential remains available only to the caller-less materialize path.

**Architecture:** One behavioral change in `connectors/mcp/client.py::_lookup_secret_for_source` (the secret-resolution precedence), a defense-in-depth 403 guard in `app/api/mcp_passthrough.py::invoke_passthrough_tool`, and tests (unit + cross-engine contract). No schema change — `mcp_user_secrets` already exists.

**Tech Stack:** Python, FastAPI, DuckDB + Postgres (dual backend via repo factory), pytest.

## Global Constraints

- Dual-backend parity: any state read goes through the repo factory (`per_user_secrets_repo()` / `shared_secrets_repo()`), never a raw connection. Cross-engine behavior asserted in `tests/db_pg/`.
- No AI attribution in commits or PRs.
- CHANGELOG bullet under `[Unreleased]` in the same PR; release-cut is the isolated last commit (patch bump by default).
- Run the full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- Vendor-agnostic: no customer/CRM-specific names in code, comments, tests, or CHANGELOG — the fix is generic Universal-MCP behavior.

---

### Task 1: Fail-closed in `_lookup_secret_for_source`

**Files:**
- Modify: `connectors/mcp/client.py` (`_lookup_secret_for_source`, ~lines 96–120)
- Test: `tests/test_mcp_user_secrets.py` (update one existing test, add one)

**Interfaces:**
- Consumes: `per_user_secrets_repo().get(source_id, user_id)`, `shared_secrets_repo().get(source_id)` (existing, factory-routed).
- Produces: `_lookup_secret_for_source(source: dict, *, caller_user_id: str|None) -> str|None` — unchanged signature; new behavior: `scope='per_user'` + `caller_user_id` truthy + no per-user row → returns `None` (does NOT consult shared or env).

- [ ] **Step 1: Update the existing precedence test to encode fail-closed**

In `tests/test_mcp_user_secrets.py`, `test_lookup_per_user_wins_over_shared_when_scope_per_user`, change the final assertion (currently `== "shared-fallback"`):

```python
    src = {"id": "src_pu", "scope": "per_user"}
    assert _lookup_secret_for_source(src, caller_user_id="analyst1") == "analyst-own"
    # Fail-closed: an identified caller with no per-user row must NOT borrow
    # the shared credential on a per_user source.
    assert _lookup_secret_for_source(src, caller_user_id="admin1") is None
```

- [ ] **Step 2: Add a no-leak test (shared row present, caller without a row)**

Append to `tests/test_mcp_user_secrets.py`:

```python
def test_lookup_per_user_no_row_does_not_leak_shared(seeded_app):
    """per_user source + identified caller + no per-user row → None, even when
    a shared vault row exists. The shared credential is not borrowed."""
    from connectors.mcp.client import _lookup_secret_for_source

    _seed_per_user_source()
    client = seeded_app["client"]
    client.put(
        "/api/admin/mcp-sources/src_pu/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "shared-fallback"},
    )
    src = {"id": "src_pu", "scope": "per_user"}
    assert _lookup_secret_for_source(src, caller_user_id="nobody") is None


def test_lookup_per_user_materialize_uses_shared(seeded_app):
    """per_user source + no caller (materialize job) → shared fallback stays."""
    from connectors.mcp.client import _lookup_secret_for_source

    _seed_per_user_source()
    client = seeded_app["client"]
    client.put(
        "/api/admin/mcp-sources/src_pu/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "shared-fallback"},
    )
    src = {"id": "src_pu", "scope": "per_user"}
    assert _lookup_secret_for_source(src, caller_user_id=None) == "shared-fallback"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets.py -k "lookup" -v`
Expected: `test_lookup_per_user_no_row_does_not_leak_shared` and the updated assertion FAIL (current code returns `"shared-fallback"`); `test_lookup_per_user_materialize_uses_shared` PASSES.

- [ ] **Step 4: Apply the fix**

In `connectors/mcp/client.py`, inside the `if source_id:` / `try:` block, replace the per-user branch + shared fallback:

```python
            from src.repositories import per_user_secrets_repo, shared_secrets_repo

            if scope == "per_user" and caller_user_id:
                value = per_user_secrets_repo().get(source_id, caller_user_id)
                if value:
                    return value
                # Fail closed: an identified caller on a per_user source must
                # NOT borrow the shared credential (or the env-var one). Only
                # the caller-less materialize path (caller_user_id is None)
                # reaches the shared fallback below.
                return None
            # scope='shared', or per_user materialize (caller_user_id is None)
            value = shared_secrets_repo().get(source_id)
            if value:
                return value
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add connectors/mcp/client.py tests/test_mcp_user_secrets.py
git commit -m "fix(mcp): per_user sources fail closed for tokenless identified callers"
```

---

### Task 2: Defense-in-depth 403 at the passthrough endpoint

**Files:**
- Modify: `app/api/mcp_passthrough.py` (`invoke_passthrough_tool`, before the `call_tool_async` forward)
- Test: `tests/test_mcp_user_secrets.py` (or the passthrough test module if the fixture lives there)

**Interfaces:**
- Consumes: `per_user_secrets_repo().get(source_id, user_id)`, the already-resolved `source` dict and `user` dict in the handler.
- Produces: `403` with `detail` naming the source and the `agnes mcp my-secret set <source>` remedy when a `per_user` source has no per-user row for the caller; no upstream forward occurs.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_user_secrets.py` (reuse `_seed_per_user_source` + a passthrough tool seed; mirror the existing passthrough fixtures — if a passthrough tool helper does not exist in this module, add a minimal `POST /api/admin/mcp-tools` seed with `mode="passthrough"` and a grant to the analyst's group):

```python
def test_passthrough_per_user_no_secret_returns_403(seeded_app, monkeypatch):
    """Granted caller, per_user source, no personal secret → 403 with remedy;
    the upstream connector is never called."""
    import connectors.mcp.client as mcp_client

    _seed_per_user_source()
    # seed a passthrough tool on src_pu granted to the analyst's group here
    # (see existing passthrough seeds in this module / test_admin_mcp_vault.py)
    tool_id = _seed_passthrough_tool(seeded_app, source_id="src_pu")

    called = {"n": 0}
    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("upstream must not be called")
    monkeypatch.setattr(mcp_client, "call_tool_async", _boom)

    r = seeded_app["client"].post(
        f"/api/mcp/passthrough/tools/{tool_id}/call",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"arguments": {}},
    )
    assert r.status_code == 403
    assert "my-secret" in r.json()["detail"]
    assert called["n"] == 0
```

(If `_seed_passthrough_tool` does not already exist, define it in this module using the same admin-token `POST /api/admin/mcp-tools` + `/grants` calls the other passthrough tests use.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets.py::test_passthrough_per_user_no_secret_returns_403 -v`
Expected: FAIL (currently the call proceeds to the connector).

- [ ] **Step 3: Add the guard in `invoke_passthrough_tool`**

In `app/api/mcp_passthrough.py`, after `source` is fetched and validated and before the `call_tool_async(...)` block, insert:

```python
    # Fail-closed guard for per-user sources: an interactive caller (admin
    # included — data scoping is per identity) must have their own credential.
    # Without it the call would connect anonymously; refuse with an actionable
    # message instead of an opaque upstream auth error.
    if (source.get("scope") or "shared").lower() == "per_user":
        from src.repositories import per_user_secrets_repo

        if not per_user_secrets_repo().get(source["id"], user["id"]):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"no personal credential for source {source.get('name') or source['id']!r}. "
                    f"Run `agnes mcp my-secret set {source.get('name') or source['id']}` "
                    f"to connect your own account."
                ),
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets.py::test_passthrough_per_user_no_secret_returns_403 -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/mcp_passthrough.py tests/test_mcp_user_secrets.py
git commit -m "fix(mcp): passthrough endpoint 403s per_user calls without a personal credential"
```

---

### Task 3: Cross-engine contract coverage

**Files:**
- Modify: `tests/db_pg/test_parity_mcp_user_secrets.py`

**Interfaces:**
- Consumes: the parity harness's parametrized `(backend)` fixture already used in that file; `_lookup_secret_for_source`.

- [ ] **Step 1: Read the existing parity harness**

Run: `.venv/bin/pytest tests/db_pg/test_parity_mcp_user_secrets.py -v --collect-only`
Read the file to learn its fixture names (how it seeds a source + shared secret + per-user row on each backend).

- [ ] **Step 2: Add the fail-closed contract test**

Add a test that, for each backend, seeds a `per_user` source with a shared secret but no per-user row for a given caller, and asserts `_lookup_secret_for_source(src, caller_user_id=<caller>) is None`; and with `caller_user_id=None` asserts it returns the shared value. Follow the file's existing parametrization exactly (do not invent a new fixture shape):

```python
def test_per_user_no_row_fails_closed_both_backends(<existing fixture params>):
    # seed source scope='per_user' + shared secret, NO per-user row, per the
    # harness's existing seed helpers
    src = {"id": <seeded_source_id>, "scope": "per_user"}
    assert _lookup_secret_for_source(src, caller_user_id="nobody") is None
    assert _lookup_secret_for_source(src, caller_user_id=None) == <shared_value>
```

- [ ] **Step 3: Run the contract test on both backends**

Run: `.venv/bin/pytest tests/db_pg/test_parity_mcp_user_secrets.py -v`
Expected: PASS on both DuckDB and Postgres params.

- [ ] **Step 4: Commit**

```bash
git add tests/db_pg/test_parity_mcp_user_secrets.py
git commit -m "test(mcp): cross-engine contract for per_user fail-closed resolution"
```

---

### Task 4: CHANGELOG + release-cut

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`

- [ ] **Step 1: Add the CHANGELOG bullet (before release-cut, under `[Unreleased] > Fixed`)**

```markdown
### Fixed
- **Security:** `scope='per_user'` MCP sources no longer fall back to the shared service credential for an *identified* caller who has not set their own credential — such passthrough calls now fail closed (the endpoint returns 403 with a `agnes mcp my-secret set <source>` remedy) instead of silently borrowing the shared credential and exposing whatever it can see. The shared credential remains available only to the caller-less materialize path.
```

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: all pass (note any pre-existing unrelated failures per RELEASING.md).

- [ ] **Step 3: Release-cut (isolated last commit)**

Determine the next patch version from current `pyproject.toml` (re-check right before, since `main` moves): rename `## [Unreleased]` → `## [X.Y.Z] - <date>`, add a new empty `## [Unreleased]`, bump `pyproject.toml` version.

```bash
git add CHANGELOG.md            # bullet
git commit -m "fix(mcp): <one-line>"   # if the bullet wasn't already in Task 1-3 commits
git add CHANGELOG.md pyproject.toml
git commit -m "release: X.Y.Z"
```

- [ ] **Step 4: Push, PR, review loop, merge, tag**

Open the PR; run `/agnes-review`; wait for Devin Review + CI green; address any findings; merge (squash); create the `vX.Y.Z` release on the merge commit; watch post-merge `release.yml` (smoke green + rollback skipped).

---

## Rollout (operator — separate from this code PR, gated)

Do NOT do these as part of the code PR. After the fix ships and the boxes carry it:

1. **Blocking prerequisite:** verify the upstream MCP applies per-identity RLS — two accounts with different upstream access, each with their own stored token, must get different data through the passthrough. Until verified, the source stays `scope='shared'` and admin-only.
2. Switch the source to `scope='per_user'` (keep the shared vault secret for materialize).
3. Onboard: each analyst runs `agnes mcp my-secret set <source>`.
4. Widen the tool grants — tokenless grantees now see nothing (fail-closed).
