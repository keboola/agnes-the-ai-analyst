# Issue #134 — Unify BigQuery access behind `BqAccess`, fix v2_sample + 502 contract

**Date:** 2026-04-29 (revised after first review)
**Issue:** [keboola/agnes-the-ai-analyst#134](https://github.com/keboola/agnes-the-ai-analyst/issues/134)
**Branch:** `fix/134-bq-access-unify`

## Problem

Issue #134 reports that v2 BigQuery endpoints on `agnes-development` (0.18.0) return HTTP 500 with no body:

- `POST /api/v2/scan/estimate`
- `POST /api/v2/scan`
- `GET /api/v2/sample/{table_id}`

`POST /api/query/hybrid` against the same tables works correctly.

### Root cause analysis

Two distinct bugs cause the symptom:

#### Bug A — `v2_scan.py` has correct project resolution but no error translation

Commit `33a9964` (the spec under review previously misattributed this to `RemoteQueryEngine`) added the `billing_project` parameter to `app/api/v2_scan.py`. Today both endpoints (`scan_endpoint` line 385, `scan_estimate_endpoint` line 221) read `data_source.bigquery.billing_project` from `instance.yaml`, fall back to `project`, and pass it to the BQ client constructor.

**However, `_bq_dry_run_bytes` (lines 43-55) and `_run_bq_scan` (lines 266-282) have no `try/except` at all.** The endpoint-level handlers in `scan_endpoint` and `scan_estimate_endpoint` catch `WhereValidationError`, `QuotaExceededError`, `FileNotFoundError`, `PermissionError`, `ValueError` — but `google.api_core.exceptions.Forbidden` and `BadRequest` propagate as bare HTTP 500 with no body.

**This is the headline cause of the v2_scan/estimate 500s.** When the SA on `agnes-development` lacks `serviceusage.services.use` on whatever project resolves to billing, BQ raises `Forbidden`, which propagates uncaught. The config fix landed in `33a9964`; the error translation didn't.

#### Bug B — `v2_sample.py` is missing the billing_project split entirely

`app/api/v2_sample.py:104` reads only `data_source.bigquery.project`, never `billing_project`. It then passes that project to `bigquery_query()` as the billing target. This is the same bug `33a9964` fixed in `v2_scan.py`, just in a sibling file that didn't get the same patch.

`v2_sample.py` also has no structured error handling — it catches only `FileNotFoundError` (404) and `PermissionError` (403). Anything else (Google API errors, identifier `ValueError`, `ImportError`) bubbles up as bare HTTP 500.

#### Bug C — `v2_schema.py` has the same shape as `v2_sample`

`app/api/v2_schema.py` contains **two separate copies** of the INSTALL/LOAD/SECRET/`bigquery_query()` dance (lines 51-60 and 104-117). Schema reportedly works for Pavel today because it queries `INFORMATION_SCHEMA`, which doesn't trip the `serviceusage` permission check. But the same code path is one query change away from the same 500. It uses `data_source.bigquery.project` directly, no billing fallback.

#### Operator-config dimension (out of scope for code fix)

If `instance.yaml` on `agnes-development` does not set `data_source.bigquery.billing_project`, the fallback `or project_id` puts the call right back in the broken state. The fix surfaces this to the operator via a structured error body containing a `hint` pointing at the missing config key.

### Five+ duplicate code paths today

The BQ-access pattern is duplicated across:

| File | Function | Shape | Status in this PR |
|---|---|---|---|
| `app/api/v2_scan.py` | `_bq_dry_run_bytes`, `_run_bq_scan` | `bigquery.Client` (Python SDK) | **In scope** |
| `app/api/v2_sample.py` | `_fetch_bq_sample` | DuckDB `bigquery_query()` | **In scope** |
| `app/api/v2_schema.py` | two anonymous blocks | DuckDB `bigquery_query()` (×2) | **In scope** |
| `src/remote_query.py` | `RemoteQueryEngine._get_bq_client` | `bigquery.Client` (Python SDK) | **In scope** |
| `connectors/bigquery/extractor.py` | sync-time extractor | mixed (`ATTACH 'project=...'` + `bigquery_query()`) | **Deferred** (see below) |
| `scripts/duckdb_manager.py` | `register_bq_table` | `bigquery.Client` (Python SDK) | **Deferred** (see below) |

**Deferred sites — rationale:**

- **`extractor.py`** runs at sync time, async, behind the scheduler. Errors surface in logs / `sync_history`, not as HTTP responses. Different lifecycle, different control flow (uses `ATTACH` not `bigquery.Client.query`). Migrating it doubles the PR size for benefit not in #134's scope. Track as follow-up issue.
- **`register_bq_table`** is admin-only, runs once at table registration time (M1 from #108). Its project resolution is `bq_project or BIGQUERY_PROJECT env` — no `instance.yaml` fallback, different semantics from the runtime path. Different concern. Track as follow-up issue.

The stale docstring at `src/remote_query.py:204` claims `_bq_client_factory` defaults to `scripts.duckdb_manager._create_bq_client`. It doesn't — `_get_bq_client` constructs `_bq_module.Client(project=project)` inline at line 450. Will self-correct when `_bq_client_factory` is removed.

## Goals

1. **Fix the v2_sample billing_project bug** so cross-project BQ reads work when the operator sets `billing_project`.
2. **Fix the v2_scan/estimate error translation** so cross-project Forbidden surfaces as a structured 502 instead of bare 500.
3. **Translate Google API errors into structured responses** with actionable bodies across all three v2 endpoints (the user / CLI gets an error shape they can reason about, not bare 500).
4. **Eliminate four duplicate BQ-access call sites** behind a single facade so the fix lives in one place. (Two deferred — see above.)
5. **Preserve test invasiveness** — existing tests that mock the BQ client must remain straightforward to write, ideally cleaner than today's `_bq_client_factory` injection point.

## Non-goals

- Changing the operator-facing config schema. `data_source.bigquery.billing_project` already exists; we route everything through it.
- Auto-detecting cross-project misconfiguration at startup (rejected for scope; would require a real BQ call at boot).
- Touching the `/api/query/hybrid` endpoint behavior — `RemoteQueryEngine` internals change, but the HTTP contract does not.
- Migrating `extractor.py` or `register_bq_table` to `BqAccess` (deferred — see above).
- Adding per-table multi-project support. Today's `table_registry` schema has no `source_project` column; every BQ table uses `instance.yaml`'s `project` as the data project. Future multi-project is a separate feature; spec notes the constraint so it can be lifted cleanly later.
- Schema migration / data migration of any kind.

## Design

### Architecture — new module `connectors/bigquery/access.py`

```
connectors/bigquery/
├── auth.py          (existing — get_metadata_token, unchanged)
├── extractor.py     (existing — unchanged in this PR)
└── access.py        (NEW)
    ├── BqProjects                 (frozen dataclass)
    ├── BqAccessError              (typed exception with HTTP_STATUS class mapping)
    ├── BqAccess                   (facade with injectable factories)
    ├── translate_bq_error(e, projects, *, sql_origin) -> BqAccessError
    ├── _default_client_factory    (real bigquery.Client construction)
    └── _default_duckdb_session_factory (real INSTALL/LOAD/SECRET dance)
```

### `BqAccess` public API (revised — accepts factories at construction, no `from_config` monkey-patching needed)

```python
@dataclass(frozen=True)
class BqProjects:
    billing: str   # billing/quota target — used as `project=` and `quota_project_id=`
    data: str      # data project for FROM clauses (today: instance.yaml `project`).
                   # Note: locked to a single project per instance until table_registry
                   # grows a per-table source_project column. See "Non-goals".


class BqAccessError(Exception):
    HTTP_STATUS = {
        "not_configured":          500,  # admin/config bug — page on-call
        "bq_lib_missing":          500,  # deployment bug
        "auth_failed":             502,  # GCP metadata server unreachable
        "cross_project_forbidden": 502,  # SA lacks serviceusage.services.use on billing project
        "bq_forbidden":            502,  # other Forbidden from BQ
        "bq_bad_request_user":     400,  # 400 from BQ on user-derived SQL
        "bq_bad_request_server":   502,  # 400 from BQ on server-constructed SQL (registry corruption)
        "bq_upstream_error":       502,  # other GoogleAPICallError catch-all
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)


class BqAccess:
    """Single entry point for BigQuery access — config resolution, client construction,
    DuckDB-extension session, and error translation. Stateless after construction.

    Factories are injectable for tests:
        bq = BqAccess(
            BqProjects(billing="test-billing", data="test-data"),
            client_factory=lambda projects: mock_client,
        )
    """

    def __init__(
        self,
        projects: BqProjects,
        *,
        client_factory: Callable[[BqProjects], "bigquery.Client"] | None = None,
        duckdb_session_factory: Callable[[BqProjects], "AbstractContextManager"] | None = None,
    ):
        self._projects = projects
        self._client_factory = client_factory or _default_client_factory
        self._duckdb_session_factory = duckdb_session_factory or _default_duckdb_session_factory

    @classmethod
    @functools.cache
    def from_config(cls) -> "BqAccess":
        """Resolve projects from BIGQUERY_PROJECT env → instance.yaml billing_project → project.
        Raises BqAccessError(kind='not_configured') if data project unresolvable.

        Cached at process level — config is read at boot and doesn't change at runtime.
        Tests should use `BqAccess(...)` directly with FastAPI dependency_overrides
        rather than mutating this cache."""

    @property
    def projects(self) -> BqProjects: ...

    def client(self) -> "bigquery.Client":
        """Construct (or retrieve from injected factory) a BigQuery client.
        Raises BqAccessError(kind='bq_lib_missing') if google-cloud-bigquery missing."""

    @contextmanager
    def duckdb_session(self) -> Iterator["duckdb.DuckDBPyConnection"]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set
        from get_metadata_token(). Auto-cleanup. Translates INSTALL/LOAD/SECRET failures
        to BqAccessError(kind='auth_failed' or 'bq_lib_missing')."""


def translate_bq_error(
    e: Exception,
    projects: BqProjects,
    *,
    sql_origin: Literal["user_derived", "server_constructed"],
) -> BqAccessError:
    """Pass-through for BqAccessError. Maps google.api_core exceptions:
      - Forbidden + 'serviceusage' in msg -> cross_project_forbidden
      - Forbidden                         -> bq_forbidden
      - BadRequest, sql_origin=user       -> bq_bad_request_user (400)
      - BadRequest, sql_origin=server     -> bq_bad_request_server (502)
      - GoogleAPICallError (catch-all)    -> bq_upstream_error
    Unknown exceptions are re-raised — never silently swallowed.

    `sql_origin` MUST be supplied by the caller. It distinguishes:
      - user_derived: SQL contains user input (select/where/order_by/limit/etc.).
        BQ rejecting it is plausibly the user's fault → 400.
      - server_constructed: SQL is fully built server-side from validated identifiers.
        BQ rejecting it is server-side corruption → 502."""
```

### Project resolution rules (single source of truth)

`from_config` resolves projects in this order (matching today's `RemoteQueryEngine._get_bq_client` behavior):

1. `BIGQUERY_PROJECT` env var → if set, used as **both** billing and data (legacy override).
2. `data_source.bigquery.billing_project` from `instance.yaml` → billing.
3. `data_source.bigquery.project` from `instance.yaml` → data, and billing if (2) is unset.

If neither (1) nor (3) yields a value: `BqAccessError(kind='not_configured', details={"hint": "set data_source.bigquery.project in instance.yaml"})`.

### Cross-project Forbidden detection — heuristic narrowed

Per first-round review: `billing != data` is the **normal** cross-project setup, not a signal of failure. Using it to classify `cross_project_forbidden` over-triggers and gives operators a misleading hint when the actual cause is unrelated (revoked SA, table-level ACL, etc.).

**Heuristic:** `'serviceusage' in str(e).lower()` is the only reliable signal. Drop the `billing != data` clause from the kind-classification logic.

`billing != data` may still enrich `details.hint` ("Note: billing and data projects differ — verify SA has both BQ Read on data project AND serviceusage.services.use on billing project"), but does not alter `kind`.

### Status code mapping rationale

- **400** for `bq_bad_request_user` — BQ rejecting a SQL string built from user input is plausibly the user's fault. Returns it back to the user as a 4xx with the BQ message.
- **502** for `bq_bad_request_server`, `bq_forbidden`, `cross_project_forbidden`, `bq_upstream_error`, `auth_failed` — upstream BQ refused or was unreachable. Operationally distinguishable from 500 in dashboards: "integration with BQ broken" vs "Agnes itself broken".
- **500** for `not_configured`, `bq_lib_missing` — deployment/admin-config bugs that should page on-call, not transient upstream errors.

### Migration of four call sites

#### A. `app/api/v2_scan.py`

`_bq_dry_run_bytes` and `_run_bq_scan` change signature from `(project: str, sql: str)` to `(bq: BqAccess, sql: str)`. Body:

```python
def _bq_dry_run_bytes(bq: BqAccess, sql: str) -> int:
    from google.cloud import bigquery
    try:
        job = bq.client().query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects, sql_origin="user_derived")
```

`_run_bq_scan` mirrors the same shape with `sql_origin="user_derived"` (SQL contains user's `select`/`where`/`order_by`).

`scan_endpoint`, `scan_estimate_endpoint`, `estimate`, and `run_scan` lose the `project_id` and `billing_project` parameters in favor of `bq: BqAccess`. Endpoints inject via FastAPI:

```python
@router.post("/scan")
async def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(BqAccess.from_config),
):
    try:
        ipc = run_scan(conn, user, raw, bq=bq, quota=...)
        return Response(...)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail=...)
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=...)
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
```

`_build_bq_sql(table_row, project_id, req)` keeps `project_id` as a parameter (it's the data project for the FROM clause). Call sites pass `bq.projects.data`. **Forward-compat note** in code comments: when `table_registry` grows a per-table `source_project` column, callers should prefer `table_row.get('source_project') or bq.projects.data`.

#### B. `app/api/v2_sample.py`

`_fetch_bq_sample` changes signature to `(bq: BqAccess, dataset: str, table: str, n: int)`. Body:

```python
def _fetch_bq_sample(bq: BqAccess, dataset: str, table: str, n: int) -> list[dict]:
    from src.identifier_validation import validate_quoted_identifier
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    bq_sql = f"SELECT * FROM `{bq.projects.data}.{dataset}.{table}` LIMIT {int(n)}"
    with bq.duckdb_session() as conn:
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, bq_sql],
            ).fetchdf()
            return df.to_dict(orient="records")
        except Exception as e:
            raise translate_bq_error(e, bq.projects, sql_origin="server_constructed")
```

`build_sample` and `sample` endpoint signatures lose `project_id` for `bq: BqAccess = Depends(BqAccess.from_config)`. Endpoint catch chain adds `BqAccessError` and `ValueError → 400 (kind='unsafe_identifier')`.

#### C. `app/api/v2_schema.py` (new in scope, was missed in v1 of this spec)

Two anonymous INSTALL/LOAD/SECRET blocks (lines 51-60 and 104-117) replaced with `bq.duckdb_session()` context manager. Both call sites use `sql_origin="server_constructed"` (queries are against `INFORMATION_SCHEMA` with validated identifiers, no user-derived fragments). Endpoint signature gains `bq: BqAccess = Depends(BqAccess.from_config)`.

#### D. `src/remote_query.py`

`RemoteQueryEngine.__init__` signature changes:

```python
def __init__(
    self,
    ...,  # existing args
    bq_access: BqAccess | None = None,
):
    ...
    self._bq = bq_access or BqAccess.from_config()
```

`_bq_client_factory` parameter, the docstring at line 204 (which referenced the stale `scripts.duckdb_manager._create_bq_client` default), and lines 407-450 of `_get_bq_client` all delete. Replacement:

```python
def _get_bq_client(self):
    return self._bq.client()
```

The fallback chain logic moves to `BqAccess.from_config`. `_get_bq_client` becomes one line.

### Test rewrite

The existing `_bq_client_factory` injection point in `RemoteQueryEngine` and tests like `tests/test_remote_query.py` currently look like:

```python
engine = RemoteQueryEngine(_bq_client_factory=lambda project: mock_client)
```

Migrates to direct `BqAccess` injection — no monkey-patching, no classmethod gymnastics:

```python
def test_remote_query_x():
    bq = BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=lambda projects: mock_client,
    )
    engine = RemoteQueryEngine(..., bq_access=bq)
    engine.execute(...)
```

For FastAPI endpoint tests (`tests/test_v2_*.py`), use FastAPI's `dependency_overrides`:

```python
def test_v2_scan_x(client):
    bq = BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=lambda projects: mock_client,
    )
    app.dependency_overrides[BqAccess.from_config] = lambda: bq
    try:
        response = client.post("/api/v2/scan", json={...})
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
```

Optional shared fixture in `tests/conftest.py`:

```python
@pytest.fixture
def bq_access():
    """Build a BqAccess with pluggable factories. Returns a callable that overrides
    the FastAPI Depends and yields the BqAccess instance."""
    created = []

    def _build(*, client=None, duckdb_conn=None,
               billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client else None,
            duckdb_session_factory=(
                lambda projects: contextlib.nullcontext(duckdb_conn)
            ) if duckdb_conn else None,
        )
        app.dependency_overrides[BqAccess.from_config] = lambda: bq
        created.append(bq)
        return bq

    yield _build
    app.dependency_overrides.clear()
```

All three endpoint test files plus `test_remote_query.py` use this uniformly. No more per-call-site mocking glue.

### Tests

#### Unit tests — `tests/test_bq_access.py` (new)

| Test | Asserts |
|---|---|
| `test_resolve_env_var_wins` | `BIGQUERY_PROJECT=foo` overrides `instance.yaml`; both billing+data = foo |
| `test_resolve_billing_falls_back_to_project` | unset `billing_project` → both billing and data = `project` |
| `test_resolve_billing_distinct_from_project` | both set → billing and data differ |
| `test_resolve_raises_when_neither_set` | `BqAccessError(kind='not_configured')` with hint |
| `test_from_config_is_cached` | two calls return the same instance |
| `test_translate_forbidden_serviceusage` | `gax.Forbidden('serviceusage.services.use')` → `kind='cross_project_forbidden'` + hint |
| `test_translate_forbidden_no_serviceusage_diff_projects` | `gax.Forbidden('table-level perm denied')` + billing≠data → `kind='bq_forbidden'` (NOT cross_project — heuristic narrowed) |
| `test_translate_forbidden_same_project` | `gax.Forbidden` + billing==data → `kind='bq_forbidden'` |
| `test_translate_bad_request_user` | `gax.BadRequest`, `sql_origin='user_derived'` → `kind='bq_bad_request_user'`, status 400 |
| `test_translate_bad_request_server` | `gax.BadRequest`, `sql_origin='server_constructed'` → `kind='bq_bad_request_server'`, status 502 |
| `test_translate_passes_through_typed` | `BqAccessError` in → identical out |
| `test_translate_unknown_reraises` | `RuntimeError("oops")` is re-raised, NOT silently wrapped |
| `test_client_uses_billing_as_quota_project` | `_default_client_factory` constructs with `quota_project_id=projects.billing` |
| `test_duckdb_session_closes_on_exit` | mock token + duckdb conn, assert `conn.close()` called |
| `test_duckdb_session_closes_on_exception` | exception inside `with` block still triggers `conn.close()` |
| `test_injected_client_factory_overrides_default` | `BqAccess(..., client_factory=...)` skips `_default_client_factory` |

#### Integration tests — extending `tests/test_v2_*.py`

| Test | Asserts |
|---|---|
| `test_v2_scan_returns_502_on_bq_forbidden_serviceusage` | mock client raises `gax.Forbidden('...serviceusage...')`; response 502 + body `error=cross_project_forbidden` + hint mentions `billing_project` |
| `test_v2_scan_returns_400_on_bq_bad_request` | mock client raises `gax.BadRequest('invalid syntax')`; response 400 + body `error=bq_bad_request_user` |
| `test_v2_scan_estimate_returns_502_on_bq_forbidden` | same pattern for `/scan/estimate` |
| `test_v2_sample_returns_502_on_bq_forbidden` | mock duckdb_session raises via `bigquery_query`; response 502 + structured body |
| `test_v2_sample_returns_400_on_unsafe_identifier` | registry row with backtick in `source_table` → 400 + body `error=unsafe_identifier` |
| `test_v2_sample_returns_404_on_unknown_table` | unchanged behavior (regression guard) |
| `test_v2_sample_returns_403_on_unauthorized` | unchanged behavior (regression guard) |
| `test_v2_schema_returns_502_on_bq_forbidden` | NEW (v2_schema in scope) |
| `test_v2_schema_returns_200_on_success` | regression guard (the existing happy path) |

#### E2E manual verification (post-deploy on `agnes-development`)

For each of `/sample`, `/scan/estimate`, `/scan`:

```bash
PAT=...

# Pre-deploy reference (current production behavior — bare 500 with no body)
curl -k -i -H "Authorization: Bearer $PAT" \
  https://agnes-development.groupondev.com/api/v2/sample/s1_session_landings?n=2

curl -k -i -X POST -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  https://agnes-development.groupondev.com/api/v2/scan/estimate \
  -d '{"table_id":"s1_session_landings","select":["event_date"],"where":"event_date = DATE \"2026-04-21\""}'

curl -k -i -X POST -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  https://agnes-development.groupondev.com/api/v2/scan \
  -d '{"table_id":"s1_session_landings","select":["event_date"],"where":"event_date = DATE \"2026-04-21\"","limit":50}'

# Post-deploy, BEFORE fixing instance.yaml — expect 502 + structured JSON body
# with error=cross_project_forbidden and a hint pointing at billing_project on
# all three endpoints.

# Operator action: set data_source.bigquery.billing_project in instance.yaml,
# restart the container.

# Post-config-fix — expect 200 on all three endpoints.
```

This three-endpoint × three-state matrix is the success criterion for closing #134. Without it, "fixed" is unverifiable.

## Implementation strategy — staged commits

Per first-round review: stage as **two commits** so the user-visible bug fix is independently reviewable / revertable from the refactor.

**Commit 1 — Minimal bug fix (revertable):**
- `app/api/v2_sample.py`: read `billing_project` with same fallback as `v2_scan.py:385`; pass to `bigquery_query()`.
- `app/api/v2_scan.py`: wrap `_bq_dry_run_bytes` and `_run_bq_scan` in `try/except` translating Google API errors to `HTTPException(502, detail=...)` inline (no `BqAccess` yet — direct translation).
- Tests: regression tests for the 502 shape on `/scan` and `/scan/estimate`.

This commit alone closes the user-visible part of #134. If commit 2 needs another review round, commit 1 still ships.

**Commit 2 — `BqAccess` extraction + migration:**
- Create `connectors/bigquery/access.py` with the design above.
- Migrate `v2_scan`, `v2_sample`, `v2_schema`, `RemoteQueryEngine` to `BqAccess`.
- Remove `_bq_client_factory` from `RemoteQueryEngine.__init__`.
- Migrate tests to the new `BqAccess(client_factory=...)` + `dependency_overrides` pattern.
- Delete inline `try/except` blocks added in commit 1 (replaced by `BqAccess`-aware translation).

## Risks

1. **Test rewrite breaks something subtle.** `tests/test_remote_query.py` and possibly `tests/test_duckdb_manager.py` have many `_bq_client_factory` call sites. The new fixture pattern must cover every shape they exercise. Mitigation: convert tests one-by-one in commit 2, run pytest after each, before deleting the old injection point.
2. **Cross-project Forbidden detection heuristic is narrow but principled.** Relies on Google's error message containing `'serviceusage'` (case-insensitive). False positives are unlikely (the substring is specific). False **negatives** (real cross-project errors that don't say `'serviceusage'`) are possible — those degrade to `bq_forbidden` with a generic message, still a 502 with structured body, just less specific in the hint. Acceptable.
3. **`BqAccess.from_config()` is `@functools.cache`'d.** Cheap and process-lifetime-safe (config is loaded at boot and immutable). Tests use `dependency_overrides` and direct `BqAccess(...)` construction, never the cached path — no cache invalidation needed in tests.
4. **`bq_bad_request_user → 400` could leak BQ error messages.** BQ's `BadRequest` text typically describes the SQL problem. We surface it in `details.message`. Operators who don't want this can filter at a reverse-proxy layer; this matches behavior of any 4xx-with-detail elsewhere in the app.
5. **Two duplicate sites left behind (`extractor.py`, `register_bq_table`).** Explicit follow-up issue should be filed at PR-merge time.

## CHANGELOG entry (for the implementation PR)

Under `## [Unreleased]`:

**`### Fixed`**
- v2 `/sample` endpoint: BigQuery cross-project queries now respect `data_source.bigquery.billing_project` from `instance.yaml` (mirrors v2 `/scan` fix from `33a9964`). Closes #134 for `/sample`.
- v2 `/scan`, `/scan/estimate`: BigQuery upstream errors no longer return bare HTTP 500 with empty body. `Forbidden` from BQ now returns HTTP 502 with structured JSON body (`{"error": "cross_project_forbidden", "message": "...", "details": {"hint": "..."}}`); `BadRequest` on user-derived SQL returns HTTP 400 with `kind=bq_bad_request_user`. Closes #134 for `/scan*`.
- v2 `/schema`: same error translation applied (was previously also bare 500 on Google API errors, though triggered less often because INFORMATION_SCHEMA queries don't usually fail).

**`### Internal`**
- New shared module `connectors/bigquery/access.py` — `BqAccess` facade unifies BQ project resolution, client construction, DuckDB-extension session, and Google-API error translation across `v2_scan`, `v2_sample`, `v2_schema`, and `RemoteQueryEngine`.
- **BREAKING (internal):** `RemoteQueryEngine.__init__` no longer accepts `_bq_client_factory`. Tests that injected it migrate to `RemoteQueryEngine(..., bq_access=BqAccess(projects, client_factory=...))`. No external callers were affected (parameter prefix `_` indicated test-only).
- Stale docstring at `src/remote_query.py:204` (referencing `scripts.duckdb_manager._create_bq_client` as the default factory) removed.
- Two known-duplicate BQ-access sites (`connectors/bigquery/extractor.py`, `scripts/duckdb_manager.register_bq_table`) explicitly out of scope; tracked as follow-up.
