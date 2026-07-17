# Per-user MCP credential onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a signed-in user self-serve their own credential for a `per_user`
MCP source — connect, replace, test, remove — from a web page, and make the
agent tell an unconnected user exactly where to do it.

**Architecture:** One PR. A new `connect_hint` column on `mcp_sources` (schema
v91→v92) carries per-source token instructions. `GET …/my-secret` gains
`updated_at`. A new `POST …/my-secret/test` endpoint verifies the caller's own
credential upstream behind scope+grant+rate-limit+credential gates, mirrored by a
CLI command and an MCP tool. `PerUserCredentialMissing` becomes a web-first,
deep-linked remedy that survives every transport. A `/me/connections` page ties
it together.

**Tech Stack:** Python 3.12, FastAPI, DuckDB + Postgres dual backend, Jinja2
(`base_page.html`), Typer CLI, FastMCP, pytest.

Spec: `docs/superpowers/specs/2026-07-17-per-user-mcp-connect-onboarding-design.md`.

## Global Constraints

- **Dual-backend parity.** Any repo method added to a DuckDB repo gets its
  matching Postgres sibling in the same change. `PerUserSecretsRepository` lives
  in `app/secrets_vault.py` (DuckDB) + `PerUserSecretsPgRepository` in
  `src/repositories/secrets_vault_pg.py` (PG). `MCPSourceRepository` lives in
  `src/repositories/mcp_sources.py` + `_pg` sibling.
- **Ratchet gap.** `tests/db_pg/test_repo_method_parity.py` scans only
  `src/repositories/`, so the `mcp_user_secrets` cluster (DuckDB half in
  `app/secrets_vault.py`) is **not** covered mechanically. The manual test
  `tests/db_pg/test_parity_mcp_user_secrets.py` is the only guard — extend it.
- **Reach repos through the factory** (`per_user_secrets_repo()`,
  `mcp_sources_repo()`), never instantiate a repo class directly.
- **Vendor-agnostic.** No customer/brand/source name baked into code, templates,
  or copy. Placeholders only.
- **Web pages** extend `base_page.html`; page CSS in `{% block head_extra %}`;
  the route MUST pass a chrome context (via `_build_context`) or the page renders
  with no CSS and no nav while tests stay green.
- **Command-UX standard** for the new CLI: positional term, `--json`, a
  "not found" error that hints the next step. Never a new boolean scope flag.
- **Triple-surface ratchet.** A new `/api/*` endpoint is BLOCKING on a CLI
  command + an MCP tool that reach it — all in this PR.
- **CHANGELOG.** Add a bullet under `## [Unreleased]` in the same PR.
- **Run the full suite before every push:** `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- **No AI attribution** in commits or PR.

---

### Task 1: `connect_hint` column on `mcp_sources` (schema v91→v92)

**Files:**
- Modify: `src/db.py:50` (`SCHEMA_VERSION`), add `_v91_to_v92` after `_v90_to_v91` (`src/db.py:5812`), wire it at both ladder call sites (`src/db.py:6248`, `src/db.py:6483`)
- Create: `migrations/versions/0039_mcp_connect_hint_v92.py`
- Modify: `src/repositories/mcp_sources.py:28` (`upsert`), `src/repositories/mcp_sources_pg.py` (`upsert` sibling)
- Modify: `app/api/admin_mcp.py` — `CreateMCPSourceRequest` / `UpdateMCPSourceRequest` models + `create_mcp_source` (`:371`) + `update_mcp_source` (`:449`) to accept and thread `connect_hint`
- Modify: `app/web/templates/admin_mcp_source_detail.html` — an admin field for `connect_hint`
- Test: `tests/test_db_schema_version.py` (already gates version parity), `tests/db_pg/` mcp_sources parity test if present, plus a focused repo round-trip test

**Interfaces:**
- Produces: `mcp_sources` rows carry `connect_hint: Optional[str]`; `mcp_sources_repo().get(id)` returns it in the dict; `upsert(..., connect_hint=None)` persists it.

- [ ] **Step 1: Write the failing test** — repo round-trips the field on both backends.

Add to the existing mcp_sources parity/round-trip test (or create `tests/test_mcp_sources_connect_hint.py`):

```python
def test_connect_hint_round_trips(system_db):
    from src.repositories.mcp_sources import MCPSourceRepository
    repo = MCPSourceRepository(system_db)
    repo.upsert(id="s1", name="src_one", transport="stdio", command="x",
                scope="per_user", connect_hint="Generate a token in Settings → API.")
    got = repo.get("s1")
    assert got["connect_hint"] == "Generate a token in Settings → API."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_sources_connect_hint.py -v`
Expected: FAIL — `upsert() got an unexpected keyword argument 'connect_hint'` (or KeyError on the dict).

- [ ] **Step 3: Add the DuckDB migration step.** In `src/db.py`, bump `SCHEMA_VERSION = 92` and add after `_v90_to_v91`:

```python
def _v91_to_v92(conn: duckdb.DuckDBPyConnection) -> None:
    """v92: mcp_sources.connect_hint — per-source, admin-authored instructions
    telling a user where to obtain their personal token for a per_user source.
    Rendered through app/markdown_render.render_safe on the connect page."""
    conn.execute("ALTER TABLE mcp_sources ADD COLUMN IF NOT EXISTS connect_hint VARCHAR")
    conn.execute("UPDATE schema_version SET version = 92")
```

Wire it at both ladder sites (mirror how `_v90_to_v91(conn)` is called):

```python
            _v91_to_v92(conn)
```

- [ ] **Step 4: Add the Alembic revision.** Create `migrations/versions/0039_mcp_connect_hint_v92.py`:

```python
"""mcp_sources.connect_hint (schema v92)"""
from alembic import op
import sqlalchemy as sa

revision = "0039_mcp_connect_hint_v92"
down_revision = "0038_store_lint_v91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_sources", sa.Column("connect_hint", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_sources", "connect_hint")
```

- [ ] **Step 5: Thread `connect_hint` through both repo `upsert`s.** In
`src/repositories/mcp_sources.py::upsert`, add `connect_hint: Optional[str] = None`
to the keyword-only params, add `connect_hint` to the INSERT column list + values
list, and add `connect_hint = excluded.connect_hint` to the `ON CONFLICT` SET.
Apply the identical change to `src/repositories/mcp_sources_pg.py::upsert` (PG
placeholder style). `get`/`list_all` already `SELECT *`-shape the row dict, so
the field surfaces automatically — verify by reading those methods.

- [ ] **Step 6: Thread through the admin API + template.** Add
`connect_hint: Optional[str] = None` to `CreateMCPSourceRequest` and
`UpdateMCPSourceRequest` in `app/api/admin_mcp.py`, pass it into the
`repo.upsert(...)` calls in `create_mcp_source` and `update_mcp_source`
(update: only when the field is present in the partial payload). Add a labelled
`<textarea name="connect_hint">` to `admin_mcp_source_detail.html` following the
existing field markup.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_sources_connect_hint.py tests/test_db_schema_version.py -v`
Expected: PASS (round-trip green; DuckDB ladder and Alembic both report v92).

- [ ] **Step 8: Commit**

```bash
git add src/db.py migrations/versions/0039_mcp_connect_hint_v92.py \
        src/repositories/mcp_sources.py src/repositories/mcp_sources_pg.py \
        app/api/admin_mcp.py app/web/templates/admin_mcp_source_detail.html \
        tests/test_mcp_sources_connect_hint.py
git commit -m "feat(mcp): add mcp_sources.connect_hint (schema v92)"
```

---

### Task 2: `updated_at` on the per-user secret status

**Files:**
- Modify: `app/secrets_vault.py:262` (`PerUserSecretsRepository`) — add `get_updated_at`
- Modify: `src/repositories/secrets_vault_pg.py:157` (`PerUserSecretsPgRepository`) — matching sibling
- Modify: `app/api/mcp_user_secrets.py:40` (`HasSecretResponse`) + `:82` (`get_my_secret_status`)
- Test: `tests/db_pg/test_parity_mcp_user_secrets.py` (the only guard for this cluster)

**Interfaces:**
- Produces: `per_user_secrets_repo().get_updated_at(source_id, user_id) -> Optional[str]` (ISO-8601 string or None); `GET …/my-secret` response gains `updated_at: str | None`.

- [ ] **Step 1: Write the failing test** — extend `tests/db_pg/test_parity_mcp_user_secrets.py`:

```python
def test_get_updated_at_present_and_absent(per_user_secrets_repo_both):
    repo = per_user_secrets_repo_both
    assert repo.get_updated_at("srcX", "userX") is None
    repo.upsert("srcX", "userX", "tok")
    assert repo.get_updated_at("srcX", "userX") is not None
```

(Match the file's existing fixture names — it drives both backends through a
`TestClient`/repo fixture; mirror whatever the neighboring tests use.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/db_pg/test_parity_mcp_user_secrets.py -k updated_at -v`
Expected: FAIL — `AttributeError: 'PerUserSecretsRepository' object has no attribute 'get_updated_at'`.

- [ ] **Step 3: Add `get_updated_at` to the DuckDB repo** (`app/secrets_vault.py`):

```python
    def get_updated_at(self, source_id: str, user_id: str) -> Optional[str]:
        """ISO-8601 timestamp of the last upsert, or None if not connected.
        Never returns the secret value."""
        row = self.conn.execute(
            "SELECT updated_at FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?",
            [source_id, user_id],
        ).fetchone()
        return row[0].isoformat() if row and row[0] is not None else None
```

- [ ] **Step 4: Add the matching PG sibling** (`src/repositories/secrets_vault_pg.py`, PG placeholder + cursor style used by the neighboring methods):

```python
    def get_updated_at(self, source_id: str, user_id: str) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM mcp_user_secrets WHERE source_id = %s AND user_id = %s",
                (source_id, user_id),
            )
            row = cur.fetchone()
        return row[0].isoformat() if row and row[0] is not None else None
```

- [ ] **Step 5: Surface it in the endpoint.** In `app/api/mcp_user_secrets.py`, add
`updated_at: Optional[str] = None` to `HasSecretResponse` and populate it in
`get_my_secret_status`:

```python
    return HasSecretResponse(
        has_secret=per_user_secrets_repo().has(source_id, user["id"]),
        source_scope=(source.get("scope") or "shared"),
        updated_at=per_user_secrets_repo().get_updated_at(source_id, user["id"]),
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/db_pg/test_parity_mcp_user_secrets.py -v`
Expected: PASS on both backends.

- [ ] **Step 7: Commit**

```bash
git add app/secrets_vault.py src/repositories/secrets_vault_pg.py \
        app/api/mcp_user_secrets.py tests/db_pg/test_parity_mcp_user_secrets.py
git commit -m "feat(mcp): expose updated_at on per-user secret status"
```

---

### Task 3: Web-first, deep-linked remedy that survives every transport

**Files:**
- Modify: `app/api/mcp_policy.py:104` (`PerUserCredentialMissing`) + `:120` (`enforce_per_user_credential` raise site)
- Test: `tests/test_mcp_policy.py` (or the file that already exercises `enforce_per_user_credential`), plus per-transport propagation tests where they live (`tests/test_mcp_passthrough_api.py`, MCP HTTP/streamable test modules)

**Interfaces:**
- Consumes: `app.instance_config.get_public_url() -> str`.
- Produces: `PerUserCredentialMissing(source_label: str, source_id: str)`; its message is web-first when a public URL is set, CLI-fallback otherwise. Both strings are module constants.

- [ ] **Step 1: Write the failing test:**

```python
def test_remedy_is_web_first_with_deep_link(monkeypatch):
    import app.api.mcp_policy as p
    monkeypatch.setattr(p, "get_public_url", lambda: "https://agnes.example")
    exc = p.PerUserCredentialMissing(source_label="CRM", source_id="abc123")
    msg = str(exc)
    assert "https://agnes.example/me/connections?source=abc123" in msg
    assert "abc123" in msg and "CRM" in msg

def test_remedy_falls_back_to_cli_without_public_url(monkeypatch):
    import app.api.mcp_policy as p
    monkeypatch.setattr(p, "get_public_url", lambda: "")
    msg = str(p.PerUserCredentialMissing(source_label="CRM", source_id="abc123"))
    assert "agnes mcp my-secret set" in msg
    assert "http" not in msg  # no broken link
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_policy.py -k remedy -v`
Expected: FAIL — `PerUserCredentialMissing.__init__() got an unexpected keyword argument 'source_id'`.

- [ ] **Step 3: Rewrite the exception + raise site.** In `app/api/mcp_policy.py`:

```python
from app.instance_config import get_public_url

_REMEDY_WEB = (
    "You are not connected to {label!r}. Open "
    "{base}/me/connections?source={sid} and add your token, then try again."
)
_REMEDY_CLI = (
    "You are not connected to {label!r}. Run "
    "`agnes mcp my-secret set {sid}` to connect your own account."
)


class PerUserCredentialMissing(Exception):
    def __init__(self, source_label: str, source_id: str):
        self.source_label = source_label
        self.source_id = source_id
        base = get_public_url()
        if base:
            msg = _REMEDY_WEB.format(label=source_label, base=base, sid=source_id)
        else:
            msg = _REMEDY_CLI.format(label=source_label, sid=source_id)
        super().__init__(msg)
```

Update the raise site in `enforce_per_user_credential` to pass both:

```python
    raise PerUserCredentialMissing(
        source_label=source.get("name") or source["id"],
        source_id=source["id"],
    )
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_policy.py -k remedy -v`
Expected: PASS.

- [ ] **Step 5: Add per-transport propagation tests.** Assert the message text
reaches the caller as a readable tool error on each path. In
`tests/test_mcp_passthrough_api.py` add a REST test: an unconnected caller on a
per_user tool → `403`, and `response.json()["detail"]` contains
`/me/connections?source=`. Add/extend the stdio, SSE, and streamable transport
tests (mirror the existing passthrough transport tests) to assert the same
substring survives in the returned tool-error text. Fix any path that flattens
the message to an opaque error.

- [ ] **Step 6: Run the propagation tests**

Run: `.venv/bin/pytest tests/test_mcp_passthrough_api.py -k "per_user or remedy" -v`
Expected: PASS across REST/stdio/SSE/streamable.

- [ ] **Step 7: Commit**

```bash
git add app/api/mcp_policy.py tests/test_mcp_policy.py tests/test_mcp_passthrough_api.py
git commit -m "feat(mcp): web-first deep-linked per-user credential remedy"
```

---

### Task 4: `POST …/my-secret/test` with all gates

**Files:**
- Modify: `connectors/mcp/client.py:254` (`list_tools_async`) + `:264` (`list_tools`) — add keyword-only `caller_user_id`
- Modify: `app/api/mcp_user_secrets.py` — new `test_my_secret` route + a module `_TEST_CONNECTION_RATE_LIMIT_PM` constant + a `_redact_then_truncate` helper
- Test: `tests/test_mcp_user_secrets_test_endpoint.py`

**Interfaces:**
- Consumes: `_visible_passthrough_tools(user)` (`app/api/mcp_passthrough.py`), `check_rate_limit`, `enforce_per_user_credential`, `PerUserCredentialMissing` (`app/api/mcp_policy.py`), `list_tools_async(source, *, caller_user_id=...)`.
- Produces: `POST /api/mcp/sources/{source_id}/my-secret/test` → `{ok: bool, tool_count: int | None, message: str}`.

- [ ] **Step 1: Thread `caller_user_id` into `list_tools_async` first.** In
`connectors/mcp/client.py`, add a keyword-only `caller_user_id: Optional[str] = None`
to `list_tools_async` and `list_tools`, mirroring `call_tool_async`'s shape, and
pass it into `_open_session(source, caller_user_id=caller_user_id)`. Verify
`introspect_source_async` (in `connectors/mcp/extractor.py`) still calls
`list_tools_async(source)` positionally — the new keyword-only param leaves it
untouched.

- [ ] **Step 2: Write the failing gate tests** in
`tests/test_mcp_user_secrets_test_endpoint.py`. Seed a per_user source + a shared
source + grants, then:

```python
def test_test_endpoint_rejects_shared_scope(seeded_app):
    r = seeded_app["client"].post(
        "/api/mcp/sources/SHARED_ID/my-secret/test",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"})
    assert r.status_code == 400  # nothing personal to test

def test_test_endpoint_requires_grant(seeded_app):
    r = seeded_app["client"].post(
        "/api/mcp/sources/PERUSER_UNGRANTED_ID/my-secret/test",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"})
    assert r.status_code == 403

def test_test_endpoint_no_credential_403_no_upstream(seeded_app):
    with patch("connectors.mcp.client.list_tools_async") as up:
        r = seeded_app["client"].post(
            "/api/mcp/sources/PERUSER_GRANTED_ID/my-secret/test",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"})
    assert r.status_code == 403
    assert "/me/connections?source=" in r.json()["detail"]
    up.assert_not_called()  # gated before any upstream call
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets_test_endpoint.py -v`
Expected: FAIL — route `404` (not implemented).

- [ ] **Step 4: Implement the endpoint** in `app/api/mcp_user_secrets.py`:

```python
_TEST_CONNECTION_RATE_LIMIT_PM = 6  # explicit, positive — check_rate_limit no-ops on None/<=0


def _redact_then_truncate(text: str, token: str, limit: int = 300) -> str:
    if token:
        text = text.replace(token, "***")   # redact on the FULL string first
    return text[:limit]                      # then truncate


class TestResult(BaseModel):
    ok: bool
    tool_count: Optional[int] = None
    message: str


@router.post("/{source_id}/my-secret/test", response_model=TestResult)
async def test_my_secret(source_id: str, user: dict = Depends(get_current_user)) -> TestResult:
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.api.mcp_policy import check_rate_limit, enforce_per_user_credential
    from connectors.mcp.client import list_tools_async
    from src.repositories import per_user_secrets_repo

    source = mcp_sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    # Gate 2: shared source has no "my credential" to test — and would resolve
    # the operator's shared secret. Reject before any upstream call.
    if (source.get("scope") or "shared").lower() != "per_user":
        raise HTTPException(status_code=400, detail="source_scope_not_per_user")
    # Gate 3: same grant filter the page uses — no second intersection.
    granted_source_ids = {t["source_id"] for t in _visible_passthrough_tools(user)}
    if source_id not in granted_source_ids:
        raise HTTPException(status_code=403, detail="not_granted")
    # Gate 4: explicit positive cap or it silently no-ops.
    check_rate_limit(source_id, user["id"], _TEST_CONNECTION_RATE_LIMIT_PM)
    # Gate 5: fail closed with the remedy for an unconnected caller.
    enforce_per_user_credential(source, user["id"])

    token = per_user_secrets_repo().get(source_id, user["id"]) or ""
    try:
        tools = await list_tools_async(source, caller_user_id=user["id"])
    except Exception as exc:
        return TestResult(ok=False, tool_count=None,
                          message=_redact_then_truncate(str(exc), token))
    return TestResult(ok=True, tool_count=len(tools), message="ok")
```

Map `PerUserCredentialMissing` → 403 (mirror the passthrough endpoint's existing
handler) and `RateLimited` → 429 with `Retry-After` if `check_rate_limit` raises
that type; reuse the exact pattern from `app/api/mcp_passthrough.py`.

- [ ] **Step 5: Add the ok-path, rate-limit, redaction, and mutation tests:**

```python
def test_test_endpoint_ok(seeded_app):
    _store_secret("PERUSER_GRANTED_ID", "analyst1", "tok")
    with patch("connectors.mcp.client.list_tools_async", new=AsyncMock(return_value=[1, 2])):
        r = seeded_app["client"].post(..., headers=...)
    assert r.status_code == 200 and r.json() == {"ok": True, "tool_count": 2, "message": "ok"}

def test_test_endpoint_redacts_token_before_truncation(seeded_app):
    _store_secret("PERUSER_GRANTED_ID", "analyst1", "SEKRET")
    boom = AsyncMock(side_effect=RuntimeError("401 bad token SEKRET " + "x" * 500))
    with patch("connectors.mcp.client.list_tools_async", new=boom):
        r = seeded_app["client"].post(..., headers=...)
    body = r.json()
    assert body["ok"] is False and "SEKRET" not in body["message"]

def test_test_endpoint_over_rate_limit_429(seeded_app):
    _store_secret("PERUSER_GRANTED_ID", "analyst1", "tok")
    with patch("connectors.mcp.client.list_tools_async", new=AsyncMock(return_value=[])):
        for _ in range(_TEST_CONNECTION_RATE_LIMIT_PM):
            seeded_app["client"].post(..., headers=...)
        r = seeded_app["client"].post(..., headers=...)
    assert r.status_code == 429

def test_client_list_tools_without_caller_id_does_not_use_shared_for_per_user(...):
    # Mutation guard: calling list_tools_async on a per_user source WITHOUT
    # caller_user_id must not silently resolve the shared credential.
    ...
```

Reset rate buckets with `reset_rate_buckets_for_tests()` around the 429 test.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_user_secrets_test_endpoint.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add connectors/mcp/client.py app/api/mcp_user_secrets.py \
        tests/test_mcp_user_secrets_test_endpoint.py
git commit -m "feat(mcp): per-user my-secret test endpoint with scope/grant/rate/credential gates"
```

---

### Task 5: CLI `agnes mcp my-secret test` + MCP tool (triple surface)

**Files:**
- Modify: `cli/commands/mcp.py:80` area (add a `test` command to `my_secret_app`)
- Modify: `app/api/mcp/foundation_tools.py` — add a `my_secret_test` tool + append its name to `FOUNDATION_TOOL_NAMES`
- Test: `tests/test_cli_mcp_my_secret.py`, `tests/test_mcp_tool_parity.py` (parity gate — should stay green once the tool is added)

**Interfaces:**
- Consumes: `POST /api/mcp/sources/{source_id}/my-secret/test`.
- Produces: `agnes mcp my-secret test <source> [--json]`; MCP tool `my_secret_test(source_id: str) -> dict`.

- [ ] **Step 1: Write the failing CLI test:**

```python
def test_my_secret_test_command(monkeypatch, capsys):
    from cli.commands import mcp as mcpcmd
    monkeypatch.setattr(mcpcmd, "api_post_json",
                        lambda path, body=None: {"ok": True, "tool_count": 3, "message": "ok"})
    mcpcmd.my_secret_test(source="src1", json_out=False)
    assert "ok" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_mcp_my_secret.py -k test_command -v`
Expected: FAIL — `module 'cli.commands.mcp' has no attribute 'my_secret_test'`.

- [ ] **Step 3: Add the CLI command** to `cli/commands/mcp.py` under `my_secret_app`:

```python
@my_secret_app.command("test")
def my_secret_test(
    source: str = typer.Argument(..., help="MCP source id or name"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Verify your stored credential works against the upstream source."""
    resp = api_post_json(f"/api/mcp/sources/{source}/my-secret/test", None)
    if json_out:
        typer.echo(json.dumps(resp))
        return
    if resp.get("ok"):
        typer.echo(f"ok — {resp.get('tool_count')} tools reachable")
    else:
        # command-UX: a failure hints the next step
        typer.echo(f"not working: {resp.get('message')}")
        typer.echo("Reconnect with: agnes mcp my-secret set " + source)
```

- [ ] **Step 4: Add the MCP tool** in `app/api/mcp/foundation_tools.py` (mirror an
existing POST-backed tool; use the provided `base_url`/`headers_fn`), and append
`"my_secret_test"` to `FOUNDATION_TOOL_NAMES`:

```python
    @mcp.tool()
    async def my_secret_test(source_id: str) -> dict:
        """Verify the caller's stored credential for a per_user MCP source."""
        return await _post(f"/api/mcp/sources/{source_id}/my-secret/test", {})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_mcp_my_secret.py tests/test_mcp_tool_parity.py -v`
Expected: PASS (CLI works; MCP parity gate green with the new tool registered on every transport).

- [ ] **Step 6: Commit**

```bash
git add cli/commands/mcp.py app/api/mcp/foundation_tools.py tests/test_cli_mcp_my_secret.py
git commit -m "feat(mcp): agnes mcp my-secret test CLI + MCP tool"
```

---

### Task 6: `/me/connections` web page

**Files:**
- Create: `app/web/templates/me_connections.html`
- Modify: `app/web/router.py` — add the `/me/connections` route near `/mcp-connect` (`:998`)
- Test: `tests/test_me_connections_page.py`, plus the design-system contract already runs via `tests/test_design_system_contract.py`

**Interfaces:**
- Consumes: `_visible_passthrough_tools(user)`, `mcp_sources_repo()`, `per_user_secrets_repo()`, `app.markdown_render.render_safe`, `_build_context`.
- Produces: `GET /me/connections` HTML; per-source cards for granted per_user sources with connect/replace/test/remove controls calling the existing `my-secret` endpoints.

- [ ] **Step 1: Write the failing route test:**

```python
def test_connections_page_grant_filtered_and_styled(seeded_app):
    r = seeded_app["client"].get("/me/connections",
                                 headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"})
    assert r.status_code == 200
    html = r.text
    assert "/static/" in html                     # chrome context present (CSS wired)
    assert "GRANTED_PERUSER_SOURCE_NAME" in html   # a granted per_user source shows
    assert "UNGRANTED_SOURCE_NAME" not in html     # ungranted source hidden
    assert "SHARED_SOURCE_NAME" not in html        # shared source never listed
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_me_connections_page.py -v`
Expected: FAIL — route `404`.

- [ ] **Step 3: Add the route** in `app/web/router.py`:

```python
@router.get("/me/connections", response_class=HTMLResponse)
async def me_connections_page(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Self-service page: connect / replace / test / remove your own credential
    for the per_user MCP sources you are granted."""
    from app.api.mcp_passthrough import _visible_passthrough_tools
    from app.markdown_render import render_safe

    granted_ids = {t["source_id"] for t in _visible_passthrough_tools(user)}
    sources = []
    for src in mcp_sources_repo().list_all(enabled_only=True):
        if src["id"] not in granted_ids:
            continue
        if (src.get("scope") or "shared").lower() != "per_user":
            continue
        sources.append({
            "id": src["id"],
            "name": src["name"],
            "transport": src.get("transport"),
            "hint_html": render_safe(src.get("connect_hint")),
            "has_secret": per_user_secrets_repo().has(src["id"], user["id"]),
            "updated_at": per_user_secrets_repo().get_updated_at(src["id"], user["id"]),
        })
    highlight = request.query_params.get("source") or ""
    ctx = _build_context(
        request, user=user, conn=conn,
        is_admin=is_user_admin(user["id"], conn),
        sources=sources, highlight_source=highlight,
    )
    return templates.TemplateResponse(request, "me_connections.html", ctx)
```

- [ ] **Step 4: Create the template** `app/web/templates/me_connections.html`. Extends
`base_page.html`; hero title "My connections"; page CSS in `{% block head_extra %}`
(no raw `#hex`, no `var(--primary)` — use `var(--ds-*)`); one card per source with
a `type="password" autocomplete="off"` input, **Connect** (`PUT`) when
`not has_secret`, and **Replace token** / **Test** (`POST …/test`) /
**Remove** (`DELETE`, confirm) when connected; render `{{ source.hint_html | safe }}`;
show `Updated {{ source.updated_at }}`; include the security + removal-semantics
copy from the spec verbatim; JS scrolls to `#source-{{ highlight_source }}`. All
network calls hit the existing `/api/mcp/sources/{id}/my-secret[/test]` endpoints
with the caller's session.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_me_connections_page.py tests/test_design_system_contract.py -v`
Expected: PASS (grant-filtered list; page styled; DS contract clean).

- [ ] **Step 6: Commit**

```bash
git add app/web/router.py app/web/templates/me_connections.html tests/test_me_connections_page.py
git commit -m "feat(web): /me/connections self-service per-user credential page"
```

---

### Task 7: connect_hint safe-render test, CHANGELOG, full suite, browser + E2E verify

**Files:**
- Test: `tests/test_me_connections_page.py` (add the sanitizer assertion)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the `connect_hint` sanitizer test** — a dangerous scheme must
not survive `render_safe`:

```python
def test_connect_hint_strips_dangerous_scheme(seeded_app):
    _set_connect_hint("GRANTED_PERUSER_SOURCE_ID",
                      "[x](javascript:alert(1)) <script>alert(1)</script>")
    r = seeded_app["client"].get("/me/connections", headers=...)
    assert "javascript:" not in r.text
    assert "<script>" not in r.text
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_me_connections_page.py -k dangerous -v`
Expected: PASS (render_safe + nh3 strip it).

- [ ] **Step 3: Add the CHANGELOG bullet** under `## [Unreleased]` → `### Added`:

```markdown
- Self-service per-user MCP credential management — a `/me/connections` page to
  connect, replace, test, and remove your own token for `per_user` MCP sources;
  a `POST /api/mcp/sources/{id}/my-secret/test` endpoint with a matching
  `agnes mcp my-secret test` CLI command and MCP tool; and an actionable,
  web-linked error when an unconnected caller invokes a per_user tool.
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: all green (fix anything you touched; note unrelated pre-existing fails per the release rules).

- [ ] **Step 5: Browser + real-agent verification** (per `e2e = real user path`).
Start the dev server, sign in, open `/me/connections`; confirm the page is styled,
lists only granted per_user sources, and Connect/Test/Remove hit the endpoints
(check network + console). Then drive one real chat turn against a per_user source
with **no** stored credential and confirm the agent relays the web-linked remedy
(not an opaque error) on at least the web-chat transport. Screenshot the page and
the chat reply.

- [ ] **Step 6: Commit**

```bash
git add tests/test_me_connections_page.py CHANGELOG.md
git commit -m "test(mcp): connect_hint sanitizer + changelog for per-user connect onboarding"
```

- [ ] **Step 7: Release-cut check.** If this PR lands the only `## [Unreleased]`
content, add the release-cut commit (bump `pyproject.toml`, rename `[Unreleased]`
→ the new version, add a fresh empty `[Unreleased]`) as the LAST commit — after
re-checking the live `pyproject.toml` version against `origin/main` to avoid a
version-race collision.

---

## Notes for the implementer

- **Out of scope (tracked follow-up):** `GET`/`PUT`/`DELETE …/my-secret` are not
  grant-gated today (any signed-in user can learn `has_secret` + `source_scope`
  for an arbitrary source id). Deliberately accepted for this PR; do **not** widen
  scope to fix it here. The new `test` endpoint **is** grant-gated.
- **Do not touch `connectors/mcp/extractor.py`.** The test endpoint routes through
  `client.list_tools_async` directly (the `classify_mcp_source` precedent), so the
  extractor and the two admin endpoints that depend on its caller-less behaviour
  stay untouched.
- **Mandatory review loop before merge:** `/agnes-review` → fix → Devin → CI all
  green, then merge → tag → watch post-merge `release.yml` smoke.
```
