# Semantic-Layer Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-connection Keboola master token (UI + API + CLI), multi-project semantic-layer sync with per-source provenance/prune isolation, and a new `/admin/semantic-layer` admin page.

**Architecture:** The master token is a second vault secret under the derived key `{connection_id}:master`. A nullable `source_ref` column (migration v105→v106, both ladders) gives every synced metric/glossary row a connection identity; `sync_semantic_layer()` becomes a loop over all master-token connections with per-`source_ref` prune scoping. A new admin page renders derived sources + last-run status.

**Tech Stack:** FastAPI, DuckDB + Postgres (dual-backend repos), Alembic + `src/db.py` migration ladder, Jinja2 design-system pages, Typer CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-07-28-semantic-layer-sources-design.md` — read it first; it is the contract. Review-team findings are already folded in.

## Global Constraints

- Every repo method change lands in BOTH backends in the same task (`src/repositories/X.py` + `X_pg.py`); reach repos only via factory functions (`metric_repo()`, `glossary_repo()`, `connection_secrets_repo()`) — never `get_system_db()` or direct instantiation.
- Secrets never on argv, in URLs, or in logs; error text through `KeboolaStorageClient._redact()`.
- Vault reads for status use `.has()`, never `.get()`.
- New web page: `{% extends "base_page.html" %}`, ctx from `_build_context(request, user=user)`, `ds.*` macros, `var(--ds-*)` tokens; no `.container:has()`, no bare `:root{}`, no raw `#hex`, no `var(--primary)`.
- Vendor-agnostic: no customer names/hosts anywhere, including tests and commit messages. No AI attribution in commits.
- Full suite before every push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- CHANGELOG bullet(s) under `## [Unreleased]` land in Task 9, same PR.

---

### Task 1: Migration v105→v106 — `source_ref` on `metric_definitions` + `glossary_terms`

**Files:**
- Modify: `src/db.py` (four touch points: `SCHEMA_VERSION`, step fn, two ladder call sites, two snapshot DDL blocks)
- Create: `migrations/versions/0053_semantic_source_ref_v106.py`
- Modify: `src/models/config.py` (both ORM classes)
- Test: `tests/test_db_schema_version.py` (existing gate), `tests/db_pg/test_schema_parity.py` (existing gate)

**Interfaces:**
- Produces: nullable `source_ref VARCHAR` column on both tables, both engines. Later tasks read/write it via repo kwargs.

- [ ] **Step 1: Write the failing test** — add to `tests/test_keboola_semantic_layer_sync.py`:

```python
def test_metric_definitions_has_source_ref_column(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('metric_definitions')").fetchall()}
    gcols = {r[1] for r in conn.execute("PRAGMA table_info('glossary_terms')").fetchall()}
    conn.close()
    assert "source_ref" in cols
    assert "source_ref" in gcols
```

- [ ] **Step 2: Run it** — `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py::test_metric_definitions_has_source_ref_column -x -q` → FAIL (`source_ref` missing).
- [ ] **Step 3: DuckDB side** in `src/db.py`:
  1. `SCHEMA_VERSION = 105` → `106` (line ~51).
  2. Snapshot DDL: in BOTH `CREATE TABLE IF NOT EXISTS metric_definitions` blocks' column lists (there are two copies, ~line 459 and ~line 3805 — grep `metric_definitions (`) and the `glossary_terms` block (~line 1537), add `source_ref      VARCHAR,` after the `source` column.
  3. New step function next to `_v104_to_v105` (~line 6851):

```python
def _v105_to_v106(conn: duckdb.DuckDBPyConnection) -> None:
    """v105→v106: nullable ``source_ref`` on metric_definitions +
    glossary_terms — per-connection provenance for the multi-project
    semantic-layer sync (2026-07-28 spec). No-op on fresh installs
    (snapshot DDL already declares the column)."""
    for table in ("metric_definitions", "glossary_terms"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if "source_ref" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source_ref VARCHAR")
    conn.execute("UPDATE schema_version SET version = 106")
```

  4. **BOTH ladder call sites** (recurring footgun — do not miss either):
     - sequential upgrade branch (~line 7563, pattern `if current < 105: _v104_to_v105(conn)`): append `if current < 106: _v105_to_v106(conn)`.
     - fresh-install/self-heal chronological branch (~line 7297, unconditional list ending `_v104_to_v105(conn)`): append `_v105_to_v106(conn)`.
- [ ] **Step 4: PG side**:
  1. Create `migrations/versions/0053_semantic_source_ref_v106.py` (clone header style from `0052_sessions_uploaded_v105.py`; `down_revision` = revision id inside 0052):

```python
"""v106: source_ref on metric_definitions + glossary_terms."""
import sqlalchemy as sa
from alembic import op

revision = "0053_semantic_source_ref_v106"
down_revision = "<revision id from 0052 — read the file>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric_definitions", sa.Column("source_ref", sa.String(), nullable=True))
    op.add_column("glossary_terms", sa.Column("source_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("glossary_terms", "source_ref")
    op.drop_column("metric_definitions", "source_ref")
```

  2. `src/models/config.py`: in `MetricDefinition` AND `GlossaryTerm`, after the `source` column add:

```python
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 5: Run the gates** — `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py::test_metric_definitions_has_source_ref_column tests/test_db_schema_version.py tests/db_pg/test_schema_parity.py -q` → PASS.
- [ ] **Step 6: Commit** — `git add src/db.py migrations/versions/0053_semantic_source_ref_v106.py src/models/config.py tests/test_keboola_semantic_layer_sync.py && git commit -m "feat(db): v106 — source_ref provenance column on metric_definitions + glossary_terms"`

---

### Task 2: Repo methods — `source_ref` round-trip + exact-name lookups, both backends

**Files:**
- Modify: `src/repositories/metrics.py`, `src/repositories/metrics_pg.py`
- Modify: `src/repositories/glossary.py`, `src/repositories/glossary_pg.py`
- Test: `tests/db_pg/test_glossary_contract.py` (extend), `tests/db_pg/test_config_pg.py` (add cross-engine fixture)

**Interfaces:**
- Produces (both backends, identical signatures — `tests/db_pg/test_repo_method_parity.py` AST-checks the pair):
  - `MetricRepository.create(..., source_ref: Optional[str] = None, ...)` — persisted + upserted like `source`.
  - `MetricRepository.find_by_name(name: str) -> Optional[Dict[str, Any]]` — exact match on `name`, first row or None.
  - `GlossaryRepository.create(..., source_ref: Optional[str] = None, ...)` — same.
  - `GlossaryRepository.find_by_term(term: str) -> Optional[Dict[str, Any]]` — exact match on `term`.
- Consumes: Task 1 columns.

- [ ] **Step 1: Failing contract tests.** In `tests/db_pg/test_config_pg.py` add a `repo` fixture parametrized `["duckdb", "pg"]` cloned from `tests/db_pg/test_glossary_contract.py` (`_make_duckdb_repo` builds `MetricRepository` over `_ensure_schema`'d DuckDB; `_make_pg_repo` runs Alembic `upgrade head` + `MetricPgRepository`). Then:

```python
def test_metric_source_ref_roundtrip(repo):
    repo.create(
        id="keboola/m1/mrr", name="mrr", display_name="MRR", category="revenue",
        sql="SELECT 1", source="keboola_semantic_layer", source_ref="conn-a",
    )
    row = repo.get("keboola/m1/mrr")
    assert row["source_ref"] == "conn-a"
    assert repo.find_by_name("mrr")["id"] == "keboola/m1/mrr"
    assert repo.find_by_name("nope") is None
```

  In `tests/db_pg/test_glossary_contract.py` add the mirror test (`source_ref="conn-a"` on `create`, `find_by_term("Monthly Recurring Revenue")` returns the row, `find_by_term("nope")` is None).
- [ ] **Step 2: Run** — `.venv/bin/pytest tests/db_pg/test_config_pg.py -k source_ref -q tests/db_pg/test_glossary_contract.py -q` → FAIL.
- [ ] **Step 3: DuckDB implementations.** `metrics.py`: add `source_ref: Optional[str] = None` to `create()` kwargs; add `source_ref` to the INSERT column list, VALUES placeholders, `ON CONFLICT` SET (`source_ref = excluded.source_ref`), and params. Add:

```python
    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE name = ? LIMIT 1", [name]
        ).fetchone()
        return self._row_to_dict(row)
```

  `glossary.py`: same pattern (`source_ref` through create/upsert; `find_by_term` with `WHERE term = ? LIMIT 1`). Mind the `refresh_fts` kwarg — don't disturb it.
- [ ] **Step 4: PG implementations.** Mirror both changes in `metrics_pg.py` / `glossary_pg.py` using each file's existing SQLAlchemy style (ORM upsert or `insert(...).on_conflict_do_update` — follow what `create` already does there; add `source_ref` to the update-set map). `find_by_name`/`find_by_term` are simple `select(...).where(...).limit(1)` returning the same dict shape the file's other readers produce.
- [ ] **Step 5: Run** — the Step 1 selection PLUS `.venv/bin/pytest tests/db_pg/test_repo_method_parity.py -q` → PASS.
- [ ] **Step 6: Commit** — `git commit -m "feat(repos): source_ref round-trip + exact-name lookups on metrics/glossary, both backends"`

---

### Task 3: Master-token vault slot — API (`kind=master`), validation, cleanup

**Files:**
- Modify: `app/api/admin_source_connections.py`
- Test: `tests/test_admin_source_connections.py`

**Interfaces:**
- Produces:
  - `SecretBody` gains `kind: str = "storage"` (`"storage" | "master"`).
  - `PUT /api/admin/source-connections/{id}/secret` with `kind="master"`: keboola-only (400 `master_token_only_for_keboola`), `_validate_stack_url(config, required=True)` first, then `run_in_threadpool(client.verify_token)`; non-master → 400 (message from `require_master_token`); Storage API failure → 502 with `_redact()`-ed text. Stores under vault key `f"{connection_id}:master"`.
  - `DELETE /api/admin/source-connections/{id}/secret?kind=master` clears that key (kind is a query param on DELETE).
  - `_with_secret_status` adds `row["has_master_secret"] = connection_secrets_repo().has(f"{row['id']}:master")` (guarded like `has_secret`).
  - `delete_connection` also best-effort-deletes `f"{connection_id}:master"`.
  - Module-level helper `def master_secret_key(connection_id: str) -> str: return f"{connection_id}:master"` — Tasks 5/7 import it.
- Consumes: nothing from Tasks 1–2 (independent).

- [ ] **Step 1: Failing tests** in `tests/test_admin_source_connections.py` (follow the file's existing client/fixture pattern; mock `KeboolaStorageClient.verify_token`):

```python
def test_master_secret_rejected_for_non_keboola(...):   # kind=master on bigquery conn → 400
def test_master_secret_rejects_non_master_token(...):   # verify_token → {"isMasterToken": False} → 400
def test_master_secret_stores_and_reports(...):
    # verify_token → {"isMasterToken": True} → 204; GET shows has_master_secret True;
    # DELETE ...?kind=master → GET shows False; has_secret unaffected throughout
def test_master_secret_storage_api_outage(...):         # verify_token raises StorageApiError → 502, token not in body
def test_connection_delete_clears_master_secret(...):   # delete conn → vault .has("{id}:master") False
```

- [ ] **Step 2: Run** — `.venv/bin/pytest tests/test_admin_source_connections.py -k master -q` → FAIL.
- [ ] **Step 3: Implement** in `app/api/admin_source_connections.py`:

```python
def master_secret_key(connection_id: str) -> str:
    return f"{connection_id}:master"

class SecretBody(BaseModel):
    value: str
    kind: str = "storage"
```

  In `set_connection_secret`: after the existing 404/empty-value checks, `if body.kind not in ("storage", "master"): raise HTTPException(400, "invalid kind")`. For `kind == "master"`: 400 unless `row["source_type"] == "keboola"`; `_validate_stack_url(row.get("config"), required=True)`; build `KeboolaStorageClient(url=stack_url, token=body.value)`; `info = await run_in_threadpool(client.verify_token)` wrapped in `try/except (StorageApiError, Exception)` → `raise HTTPException(502, f"storage_api_error: {client._redact(exc)}")`; `if not info.get("isMasterToken"): raise HTTPException(400, <require_master_token message>)`. Store via `connection_secrets_repo().upsert(master_secret_key(connection_id), body.value)` (same `VaultKeyNotConfiguredError` → 409 handling). `kind == "storage"` path unchanged.
  In `delete_connection_secret`: add `kind: str = "storage"` query param, validate, delete the appropriate key.
  In `_with_secret_status` and `delete_connection`: as in Interfaces above.
- [ ] **Step 4: Run** — same selection → PASS. Also `.venv/bin/pytest tests/test_admin_source_connections.py -q` (no regression).
- [ ] **Step 5: Commit** — `git commit -m "feat(api): per-connection Keboola master-token vault slot (kind=master) with at-save validation"`

---

### Task 4: CLI — `agnes admin connection secret` with `--kind`

**Files:**
- Modify: `cli/commands/admin_connection.py`
- Test: `tests/test_data_semantics_cli.py` is unrelated — put tests where the other `admin_connection` CLI tests live (grep `admin_connection` under `tests/`; if none exist, create `tests/test_cli_admin_connection_secret.py` using the CliRunner + mocked `cli.client.api_put/api_delete` pattern from sibling CLI tests).

**Interfaces:**
- Consumes: Task 3 API (`kind` in PUT body, `?kind=` on DELETE).
- Produces: `agnes admin connection secret <connection_id> [--kind storage|master] [--remove]`; token read via hidden interactive prompt (`typer.prompt("Token", hide_input=True)`) — NEVER an argv option.

- [ ] **Step 1: Failing test** — CliRunner invokes `secret CONN --kind master` with `input="tok\n"`, asserts `api_put` called with `/api/admin/source-connections/CONN/secret` and `json={"value": "tok", "kind": "master"}`; `--remove --kind master` asserts `api_delete` with `params={"kind": "master"}`.
- [ ] **Step 2: Run** → FAIL (no such command).
- [ ] **Step 3: Implement:**

```python
@admin_connection_app.command("secret")
def set_secret(
    connection_id: str = typer.Argument(..., help="Connection id"),
    kind: str = typer.Option("storage", "--kind", help="storage | master (master = semantic-layer owner token)"),
    remove: bool = typer.Option(False, "--remove", help="Clear the secret instead of setting it"),
):
    """Set or clear a connection's vault secret. The token is read from a
    hidden prompt — never pass secrets on the command line."""
    if kind not in ("storage", "master"):
        typer.echo("Error: --kind must be storage or master", err=True)
        raise typer.Exit(1)
    if remove:
        resp = api_delete(f"/api/admin/source-connections/{connection_id}/secret", params={"kind": kind})
        if resp.status_code not in (200, 204):
            _fail(resp)
        typer.echo(f"Cleared {kind} secret for {connection_id}")
        return
    token = typer.prompt("Token", hide_input=True)
    resp = api_put(f"/api/admin/source-connections/{connection_id}/secret", json={"value": token, "kind": kind})
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Stored {kind} secret for {connection_id}")
```

  (Check `cli/client.py::api_delete` accepts `params=`; if not, extend it the same way `api_get` does.)
- [ ] **Step 4: Run** → PASS. Also update the module docstring's endpoint map.
- [ ] **Step 5: Commit** — `git commit -m "feat(cli): agnes admin connection secret --kind master"`

---

### Task 5: Multi-source sync loop in `connectors/keboola/semantic_layer.py`

**Files:**
- Modify: `connectors/keboola/semantic_layer.py`
- Test: `tests/test_keboola_semantic_layer_sync.py`, `tests/test_keboola_semantic_layer_credential_resolution.py`

**Interfaces:**
- Consumes: Task 2 repo methods (`source_ref` kwarg, `find_by_name`, `find_by_term`), Task 3 `master_secret_key` (import from `app.api.admin_source_connections`).
- Produces:
  - `_enumerate_master_sources() -> list[dict]` — `[{"connection_id", "name", "stack_url", "token"}]` for every keboola connection with a `{id}:master` vault secret; default connection first, rest ordered by connection `id`.
  - `_sync_one_source(url: str, token: str, source_ref: Optional[str], *, adopt_null: bool) -> dict` — the extracted current single-project body (verify preflight → Metastore fetch → build/create with `source_ref=source_ref` → scoped prune). Returns the current per-source counter dict + `"source_ref"`.
  - `sync_semantic_layer(keboola_url=None, keboola_token=None) -> dict` — orchestrator; keeps signature. Returns top-level aggregates (same keys as today, summed) + `"sources": [per-source dicts with "connection_id"/"name"/"status"/counters/"error"?]`.
  - `MasterTokenRequiredError` unchanged; in multi-source mode it is caught per source into that source's `"error"`; in fallback mode it propagates exactly as today (endpoint's 400 contract).

- [ ] **Step 1: Failing tests.** Extend `tests/test_keboola_semantic_layer_sync.py` (it already fakes `MetastoreClient`/`KeboolaStorageClient` — follow its monkeypatch pattern). Cases, each its own test:

```python
def test_multi_source_syncs_all_master_connections(...):
    # two keboola connections with master secrets, different fake projects
    # → rows from both exist, each stamped with its connection_id in source_ref
def test_prune_is_scoped_per_source(...):
    # source A's second run returns fewer metrics → only A's rows pruned; B untouched
def test_null_ref_adoption_by_default_connection(...):
    # pre-seed row with source_ref=None → default conn's sync stamps it, doesn't duplicate,
    # and its prune scope includes NULL rows
def test_safety_valve_scoped_per_source(...):
    # source A returns zero usable metrics while A has rows → A prune skipped;
    # B's normal prune still runs the same pass
def test_name_conflict_skipped_sticky(...):
    # same metric name from two projects → first (default) wins, second counted
    # in skipped_conflict, rerun in opposite discovery order doesn't flip ownership
def test_duplicate_project_deduped(...):
    # two connections resolving to the same verify_token owner id + host
    # → second reports skipped_duplicate_project, no flip-flop
def test_fallback_no_master_tokens_preserves_foreign_rows(...):
    # no master secrets; legacy env pair set; rows exist with source_ref="other-conn"
    # → fallback sync prunes only NULL/default-conn rows; "other-conn" rows intact
```

- [ ] **Step 2: Run** — `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py -q` → new tests FAIL.
- [ ] **Step 3: Implement.** Refactor mechanics:
  1. Move everything in `sync_semantic_layer` from `storage_client = KeboolaStorageClient(...)` down to the final return into `_sync_one_source(url, token, source_ref, *, adopt_null)`. Inside it:
     - `repo.create(**row, source_ref=source_ref)` (and glossary create likewise).
     - **Conflict gate before each create:** `existing = repo.find_by_name(row["name"])`; owned = `existing is None or existing["id"] == row["id"] or existing.get("source_ref") == source_ref or (existing.get("source_ref") is None and adopt_null and existing.get("source") == "keboola_semantic_layer")`; if not owned → `skipped_conflict += 1; continue`. Mirror with `find_by_term` for glossary.
     - **Scope helper** used by BOTH the prune loop and the safety-valve `existing` computation:

```python
def _in_scope(row: dict, source_ref: Optional[str], adopt_null: bool) -> bool:
    if row.get("source") != "keboola_semantic_layer":
        return False
    ref = row.get("source_ref")
    return ref == source_ref or (ref is None and adopt_null)
```

     - `require_master_token` preflight stays, and capture `verify_token()` info once — return `("project_key", (host, owner_id))` to the orchestrator for dedupe (extend the return dict with `"project_key"`).
  2. New `_enumerate_master_sources()` using `source_connections_repo()` + `connection_secrets_repo().has/get(master_secret_key(id))` (`.get` is correct here — connector-side resolution).
  3. New orchestrating `sync_semantic_layer`: explicit args → single `_sync_one_source(url, token, None, adopt_null=True)` wrapped as one source (today's behavior). Else if `_enumerate_master_sources()` non-empty → loop (default first, `adopt_null=True` only for the default connection; dedupe on `project_key`, later duplicates → `{"status": "skipped", "skipped_duplicate_project": 1}`; catch `MasterTokenRequiredError`/returned error dicts per source). Else → fallback via `_resolve_keboola_credentials` with `source_ref` = default connection id (or None) and `adopt_null=True`. Aggregate counters by summing; top-level `status` = `"ok"` if ≥1 source ok, else `"error"` with the first error message (preserves the endpoint's 502 contract and the "credentials not configured" case).
  4. `skipped_conflict` and `skipped_duplicate_project` join the counter dict (also in `empty_result`).
- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py tests/test_keboola_semantic_layer_credential_resolution.py tests/test_keboola_semantic_layer_refresh_endpoint.py tests/test_keboola_semantic_layer_mapping.py -q` → PASS (fix regressions the refactor causes; the endpoint tests pin the 400-on-MasterTokenRequiredError fallback contract).
- [ ] **Step 5: Commit** — `git commit -m "feat(semantic-layer): multi-project sync — per-connection master tokens, source_ref provenance, scoped prune"`

---

### Task 6: Refresh endpoint — per-source breakdown passthrough

**Files:**
- Modify: `app/api/keboola_semantic_layer_refresh.py` (log line only — the result dict already flows through `_record_completion`/response untouched)
- Test: `tests/test_keboola_semantic_layer_refresh_endpoint.py`

**Interfaces:**
- Consumes: Task 5 result shape (`sources` list).
- Produces: endpoint response + `get_last_refresh_summary()["last_result"]` carry `sources`; no route/auth change.

- [ ] **Step 1: Failing test** — successful refresh (mock `sync_semantic_layer` returning a two-source result) → response JSON contains `sources` with both entries; `get_last_refresh_summary()["last_result"]["sources"]` matches.
- [ ] **Step 2: Run** → likely PASSES already (dict passthrough) — if so, keep the test as a regression pin and just extend the `logger.info` call with `len(result.get("sources") or [])`. If it fails, fix the passthrough.
- [ ] **Step 3: Run file** → PASS. **Commit** — `git commit -m "test(api): pin per-source breakdown in semantic-layer refresh response"`

---

### Task 7: `/admin/semantic-layer` page + data-sources summary slimming

**Files:**
- Modify: `app/web/router.py` (new route + slim the block in `admin_data_sources_page`)
- Create: `app/web/templates/admin_semantic_layer.html`
- Modify: `app/web/templates/admin_data_sources.html` (summary block → one-line status + link)
- Modify: `app/web/templates/_app_header.html` (Admin menu entry next to "Data sources")
- Test: create `tests/test_admin_semantic_layer_page.py` (clone the auth/render pattern from `tests/test_catalog_semantics_page.py`)

**Interfaces:**
- Consumes: Task 3 `has_master_secret`/`master_secret_key`, Task 5 `sources` breakdown, `metric_repo()`/`glossary_repo()` lists with `source_ref`.
- Produces: `GET /admin/semantic-layer` (admin-only, HTML).

- [ ] **Step 1: Failing tests:**

```python
def test_semantic_layer_page_requires_admin(...)      # anonymous/non-admin → redirect or 403 (match sibling pages)
def test_semantic_layer_page_renders_sources(...)     # conn with master secret → its name on page; counts rendered
def test_semantic_layer_page_empty_state(...)         # no master tokens → callout text + /admin/data-sources link
def test_semantic_layer_page_orphaned_rows(...)       # row with source_ref not matching any enumerated source → "orphaned" section
```

- [ ] **Step 2: Run** → FAIL (404).
- [ ] **Step 3: Route** in `app/web/router.py` (mirror `admin_data_sources_page`): build ctx via `_build_context(request, user=user)`; view-model:

```python
from connectors.keboola.semantic_layer import _enumerate_master_sources  # names/ids only — never tokens into ctx
metrics = metric_repo().list()
terms = glossary_repo().list(limit=100000)
def _counts(ref):  # count semantic rows per source_ref (None-safe)
    m = sum(1 for x in metrics if x.get("source") == "keboola_semantic_layer" and x.get("source_ref") == ref)
    g = sum(1 for x in terms if x.get("source") == "keboola_semantic_layer" and x.get("source_ref") == ref)
    return m, g
```

  Sources rows: `{type: "keboola", label: conn name, detail: stack host, counts, last: <matching entry from get_last_refresh_summary()["last_result"]["sources"]>}` — NULL-ref counts fold into the default connection's row. Orphaned: distinct non-null `source_ref` values present in rows but absent from enumerated ids. Strip the token field before templating.
  Slim `admin_data_sources_page`: drop the count computations; keep `semantic_refresh_summary` for a one-line "Semantic layer: <status> — manage at /admin/semantic-layer".
- [ ] **Step 4: Template** `admin_semantic_layer.html`: `{% extends "base_page.html" %}`, hero title "Semantic layer", toolbar hosts the Refresh button (`fetch POST /api/admin/run-keboola-semantic-layer-refresh` then reload; on 409 show the already-running hint via the page's toast helper), `{% block page %}` renders the sources table with `ds.*` components, the empty-state callout, and the orphaned section. CSS only in `{% block head_extra %}` with `var(--ds-*)` tokens. Slim the block in `admin_data_sources.html`; add the `_app_header.html` menu item (`/admin/semantic-layer`, label "Semantic layer", `is-active` on path prefix — copy the "Data sources" line).
- [ ] **Step 5: Run** — new file + `.venv/bin/pytest tests/test_design_system_contract.py -q` → PASS.
- [ ] **Step 6: Screenshot** the page against a dev server (per repo convention — `AGNES_E2E`-style local run or docker) and eyeball hero/toolbar/table before claiming done.
- [ ] **Step 7: Commit** — `git commit -m "feat(web): /admin/semantic-layer sources page; slim data-sources summary"`

---

### Task 8: Master-token UI on the connection card

**Files:**
- Modify: `app/web/templates/admin_data_sources.html`
- Test: `tests/test_admin_source_connections.py` already covers the API; template-side add one render assertion to `tests/test_admin_semantic_layer_page.py`'s sibling if a data-sources page test exists (grep `admin_data_sources` in tests; if only JS, rely on API tests + screenshot).

**Interfaces:**
- Consumes: Task 3 (`has_master_secret` in rows, PUT body `kind`, DELETE `?kind=master`).

- [ ] **Step 1: Implement.** In the connection-card render function (near `_secretBadgeHtml`/`toggleRotate`): add a "Master token (semantic layer)" row — badge `row.has_master_secret ? "set" : "not set"`, a toggleable input row cloned from the rotate-token row (`ds-rotate-row` pattern), Save → `fetch(`${API_CONNECTIONS}/${id}/secret`, {method:"PUT", body: JSON.stringify({value, kind:"master"})})`, Remove → `fetch(..., {method:"DELETE"}) ` with `?kind=master`. Surface the 400 detail verbatim (it carries the "not a master token" explanation). Title attribute: "Optional. Project owner token — required only for semantic-layer sync (Metastore)."
- [ ] **Step 2: Verify** — screenshot the card (set + unset states); run any data-sources page tests + `tests/test_design_system_contract.py`.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): master-token controls on the Keboola connection card"`

---

### Task 9: Docs, CHANGELOG, full-suite gate

**Files:**
- Modify: `CHANGELOG.md`, `docs/architecture.md` (or the metrics/admin doc that documents the semantic-layer sync — grep `semantic layer` under `docs/` and extend the canonical spot)
- Modify: `CLAUDE.md` only if it references the old single-source behavior (grep first; don't add new sections).

- [ ] **Step 1: CHANGELOG** under `## [Unreleased]`:

```markdown
### Added
- Per-connection Keboola master token (semantic layer): set/rotate in /admin/data-sources, `agnes admin connection secret --kind master`, validated at save time.
- Multi-project semantic-layer sync: every Keboola connection with a master token syncs, with per-connection provenance (`source_ref`) and prune isolation.
- /admin/semantic-layer admin page: per-source status, counts, orphaned-rows visibility.

### Changed
- /admin/data-sources semantic-layer summary reduced to a status line linking to /admin/semantic-layer.
```

- [ ] **Step 2: Docs** — short subsection: master-token requirement, where to set it (UI/CLI), multi-project behavior, orphaned-rows semantics.
- [ ] **Step 3: Full suite** — `.venv/bin/pytest tests/ --tb=short -n auto -q` → green (triage per repo policy: unrelated pre-existing failures verified via `git stash` on a clean tree, noted in PR body).
- [ ] **Step 4: Commit** — `git commit -m "docs: semantic-layer sources — changelog + admin docs"`
- [ ] **Step 5:** Run `scripts/verify_syncmap.py`, then `/agnes-review` on the branch diff (mandatory pre-merge loop). At PR-open time apply the release-cut decision tree from `docs/RELEASING.md`.
