# Issue #134 — Unify BigQuery access behind `BqAccess`, fix v2_sample + 502 contract

**Date:** 2026-04-29 (revision 4 — incorporates three rounds of code review)
**Issue:** [keboola/agnes-the-ai-analyst#134](https://github.com/keboola/agnes-the-ai-analyst/issues/134)
**Branch:** `fix/134-bq-access-unify`

## Problem

Issue #134 reports that v2 BigQuery endpoints on `<your-dev-instance>` (0.18.0) return HTTP 500 with no body:

- `POST /api/v2/scan/estimate`
- `POST /api/v2/scan`
- `GET /api/v2/sample/{table_id}`

`POST /api/query/hybrid` against the same tables works correctly.

### Root cause analysis

Two distinct bugs cause the symptom:

#### Bug A — `v2_scan.py` has correct project resolution but no error translation

Commit `33a9964` added the `billing_project` parameter to `app/api/v2_scan.py`. Today both endpoints (`scan_endpoint` line 385, `scan_estimate_endpoint` line 221) read `data_source.bigquery.billing_project` from `instance.yaml`, fall back to `project`, and pass it to the BQ client constructor.

**However, `_bq_dry_run_bytes` (lines 43-55) and `_run_bq_scan` (lines 266-282) have no `try/except` at all.** The endpoint-level handlers in `scan_endpoint` and `scan_estimate_endpoint` catch `WhereValidationError`, `QuotaExceededError`, `FileNotFoundError`, `PermissionError`, `ValueError` — but `google.api_core.exceptions.Forbidden` and `BadRequest` propagate as bare HTTP 500 with no body.

**This is the headline cause of the v2_scan/estimate 500s.** When the SA on `<your-dev-instance>` lacks `serviceusage.services.use` on whatever project resolves to billing, BQ raises `Forbidden`, which propagates uncaught. The config fix landed in `33a9964`; the error translation didn't.

#### Bug B — `v2_sample.py` is missing the billing_project split entirely

`app/api/v2_sample.py:104` reads only `data_source.bigquery.project`, never `billing_project`. It then passes that project to `bigquery_query()` as the billing target. This is the same bug `33a9964` fixed in `v2_scan.py`, just in a sibling file that didn't get the same patch.

`v2_sample.py` also has no structured error handling — it catches only `FileNotFoundError` (404) and `PermissionError` (403). Anything else (Google API errors, identifier `ValueError`, `ImportError`) bubbles up as bare HTTP 500.

#### Bug C — `v2_schema.py` has the same shape (one strict block, one best-effort block)

`app/api/v2_schema.py` contains **two separate blocks** of the INSTALL/LOAD/SECRET/`bigquery_query()` dance with **different error semantics**:

- `_fetch_bq_schema` (lines 48-73): hard-required for the endpoint to return a schema. Today swallows nothing; would benefit from structured translation.
- `_fetch_bq_table_options` (lines 90-129): best-effort partition/cluster info. Wraps everything in `try/except Exception → return {}` and a `logger.warning`. Endpoint returns successfully even if partition info fails.

Schema reportedly works for Pavel today because both queries hit `INFORMATION_SCHEMA`, which doesn't trip the `serviceusage` permission check on the billing project. But the same code path is one query change away from the same 500.

#### Operator-config dimension (out of scope for code fix)

If `instance.yaml` on `<your-dev-instance>` does not set `data_source.bigquery.billing_project`, the fallback `or project_id` puts the call right back in the broken state. The fix surfaces this to the operator via a structured error body containing a `hint` pointing at the missing config key.

### Five+ duplicate code paths today

The BQ-access pattern is duplicated across:

| File | Function | Shape | Status in this PR |
|---|---|---|---|
| `app/api/v2_scan.py` | `_bq_dry_run_bytes`, `_run_bq_scan` | `bigquery.Client` (Python SDK) | **In scope** |
| `app/api/v2_sample.py` | `_fetch_bq_sample` | DuckDB `bigquery_query()` | **In scope** |
| `app/api/v2_schema.py` | `_fetch_bq_schema` | DuckDB `bigquery_query()` | **In scope (strict translation)** |
| `app/api/v2_schema.py` | `_fetch_bq_table_options` | DuckDB `bigquery_query()` | **In scope (preserve swallow-all `except Exception → {}`)** |
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
- Hot-reload of `instance.yaml`. `get_bq_access()` is `@functools.cache`'d at process level. Changing config requires a container restart. (Today's behavior is identical — `instance.yaml` is loaded once at boot.)

## Design

### Architecture — new module `connectors/bigquery/access.py`

```
connectors/bigquery/
├── auth.py          (existing — get_metadata_token, unchanged)
├── extractor.py     (existing — unchanged in this PR)
└── access.py        (NEW)
    ├── BqProjects                      (frozen dataclass)
    ├── BqAccessError                   (typed exception with HTTP_STATUS class mapping)
    ├── BqAccess                        (facade with injectable factories)
    ├── get_bq_access() -> BqAccess     (module-level, @functools.cache; FastAPI Depends target)
    ├── translate_bq_error(e, projects, *, bad_request_status) -> BqAccessError
    ├── _default_client_factory         (real bigquery.Client construction)
    └── _default_duckdb_session_factory (real INSTALL/LOAD/SECRET dance)
```

### `BqAccess` public API

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
        "bq_bad_request":          400,  # 400 from BQ when caller flagged it as client-derived
        "bq_upstream_error":       502,  # all other upstream BQ failures (incl. server-derived BadRequest)
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

    @property
    def projects(self) -> BqProjects: ...

    def client(self) -> "bigquery.Client":
        """Construct (or retrieve from injected factory) a BigQuery client billed to
        projects.billing. Raises BqAccessError(kind='bq_lib_missing') if
        google-cloud-bigquery is not installed; raises BqAccessError(kind='auth_failed')
        on credential resolution failure."""

    @contextmanager
    def duckdb_session(self) -> Iterator["duckdb.DuckDBPyConnection"]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set
        from get_metadata_token(). Auto-cleanup. Translates INSTALL/LOAD/SECRET failures
        and metadata-token failures to BqAccessError(kind='auth_failed' or 'bq_lib_missing')."""


@functools.cache
def get_bq_access() -> "BqAccess":
    """Module-level factory used as the FastAPI Depends target.

    Resolves projects from BIGQUERY_PROJECT env → instance.yaml billing_project →
    instance.yaml project. Returns a BqAccess instance with default factories.

    Process-cached: config is loaded at boot and doesn't change at runtime. Hot-reload
    of instance.yaml is explicitly out of scope. Note that functools.cache does NOT
    cache exceptions, so a failed call (BqAccessError(kind='not_configured')) is retried
    on the next invocation — useful when the operator fixes config and restarts a
    request without restarting the container.

    Tests inject via FastAPI's dependency_overrides[get_bq_access] = lambda: bq, or
    construct BqAccess(...) directly for non-endpoint code (e.g. RemoteQueryEngine).
    Tests do NOT mutate this cache.

    Module-level (not BqAccess.from_config classmethod) to avoid the
    @classmethod + @functools.cache stacking footgun and to give FastAPI's
    dependency introspection a clean function signature."""


def translate_bq_error(
    e: Exception,
    projects: BqProjects,
    *,
    bad_request_status: Literal["client_error", "upstream_error"],
) -> BqAccessError:
    """Convert Google API exceptions into a typed BqAccessError.

    Mapping (FIRST match wins):
      1. BqAccessError                    -> pass through unchanged (this is critical:
                                             bq.client() and bq.duckdb_session() can raise
                                             BqAccessError directly for bq_lib_missing /
                                             auth_failed, and those must round-trip through
                                             translate_bq_error without being reclassified
                                             as 'unknown' and re-raised).
      2. Forbidden + 'serviceusage' in str(e).lower()
                                          -> cross_project_forbidden (with hint)
      3. Forbidden                        -> bq_forbidden
      4. BadRequest, bad_request_status='client_error'
                                          -> bq_bad_request (HTTP 400)
      5. BadRequest, bad_request_status='upstream_error'
                                          -> bq_upstream_error (HTTP 502)
      6. GoogleAPICallError (other)       -> bq_upstream_error
      7. Anything else                    -> RE-RAISED unchanged (don't swallow programmer errors)

    `bad_request_status` MUST be supplied by the caller. It distinguishes:
      - 'client_error': SQL contains user input (select/where/order_by/limit). BQ rejecting
        it is plausibly the user's fault — return 400 with the BQ message.
      - 'upstream_error': SQL is fully built server-side from validated identifiers.
        BQ rejecting it is server-side corruption — return 502."""
```

### Project resolution rules (single source of truth)

`get_bq_access` resolves projects in this order (matching today's `RemoteQueryEngine._get_bq_client` behavior):

1. `BIGQUERY_PROJECT` env var → if set, used as **both** billing and data (legacy override).
2. `data_source.bigquery.billing_project` from `instance.yaml` → billing.
3. `data_source.bigquery.project` from `instance.yaml` → data, and billing if (2) is unset.

If neither (1) nor (3) yields a value: `BqAccessError(kind='not_configured', details={"hint": "set data_source.bigquery.project in instance.yaml"})`.

> **BREAKING for env-only deployments.** Today `RemoteQueryEngine._get_bq_client` uses `BIGQUERY_PROJECT` *only* as the billing project; data project for FROM clauses comes from elsewhere (e.g. SQL strings the user wrote). After this change, `BIGQUERY_PROJECT` sets both billing and data, **overriding** `data_source.bigquery.project` for FROM-clause construction in `v2_scan` / `v2_sample` / `v2_schema`. Deployments that combine env-var-for-billing + yaml-for-data must migrate by setting `data_source.bigquery.billing_project` in `instance.yaml` and clearing the env var. Flagged in CHANGELOG.

### Cross-project Forbidden detection

`'serviceusage' in str(e).lower()` is the only reliable signal of the cross-project quota issue. Other cross-project failures (revoked SA, table-level ACL, dataset-level deny) degrade to generic `bq_forbidden` — still 502, still structured body, just less specific in the hint. `billing != data` is the **normal** cross-project setup, not a signal of failure; using it to classify would over-trigger and misdirect operators.

`billing != data` MAY enrich `details.hint` ("Note: billing and data projects differ — verify SA has both BQ Read on data project AND serviceusage.services.use on billing project") but does not alter `kind`.

### Status code mapping rationale

- **400** for `bq_bad_request` — BQ rejecting a SQL string built from user input is plausibly the user's fault. Returns it back to the user as a 4xx with the BQ message.
- **502** for `bq_forbidden`, `cross_project_forbidden`, `bq_upstream_error`, `auth_failed` — upstream BQ refused or was unreachable. Operationally distinguishable from 500 in dashboards: "integration with BQ broken" vs "Agnes itself broken".
- **500** for `not_configured`, `bq_lib_missing` — deployment/admin-config bugs that should page on-call, not transient upstream errors.

### Migration of four call sites

#### A. `app/api/v2_scan.py`

`_bq_dry_run_bytes` and `_run_bq_scan` change signature from `(project: str, sql: str)` to `(bq: BqAccess, sql: str)`. Body (note: `bq.client()` is called *outside* the `try/except` so a `BqAccessError` from it still propagates correctly through the endpoint's `except BqAccessError` clause, even before the translator's pass-through would catch it):

```python
def _bq_dry_run_bytes(bq: BqAccess, sql: str) -> int:
    from google.cloud import bigquery
    client = bq.client()  # may raise BqAccessError(bq_lib_missing/auth_failed); propagates as-is
    try:
        job = client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects, bad_request_status="client_error")
```

`_run_bq_scan` mirrors the same shape with `bad_request_status="client_error"` (SQL contains user's `select`/`where`/`order_by`).

`scan_endpoint`, `scan_estimate_endpoint`, `estimate`, and `run_scan` lose the `project_id` and `billing_project` parameters in favor of `bq: BqAccess`. Endpoints inject via FastAPI:

```python
@router.post("/scan")
async def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
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

`_build_bq_sql(table_row, project_id, req)` keeps `project_id` as a parameter (it's the data project for the FROM clause). Call sites pass `bq.projects.data`. **Forward-compat note** in code comments: when `table_registry` grows a per-table `source_project` column, callers should prefer `table_row.get('source_project') or bq.projects.data`. `tests/test_v2_scan.py:206-207` imports `_build_bq_sql` directly — the signature is unchanged, no test update needed.

#### B. `app/api/v2_sample.py`

`_fetch_bq_sample` changes signature to `(bq: BqAccess, dataset: str, table: str, n: int)`:

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
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")
```

`build_sample` and `sample` endpoint signatures lose `project_id` for `bq: BqAccess = Depends(get_bq_access)`. Endpoint catch chain adds `BqAccessError` and `ValueError → 400 (kind='unsafe_identifier')`.

#### C. `app/api/v2_schema.py` — two blocks, two semantics

The two blocks have **different error contracts** — preserve them.

**`_fetch_bq_schema` (lines 48-73)** — strict; the endpoint can't return without it. Migrate to `bq.duckdb_session()` + `translate_bq_error(..., bad_request_status="upstream_error")`. Endpoint wraps in `try/except BqAccessError` and returns the structured 502.

**`_fetch_bq_table_options` (lines 90-129)** — best-effort; current code has `except Exception as e: logger.warning(...); return {}`. **Preserve this contract.** Migrate to use `bq.duckdb_session()` for the connection but keep the outer `try/except Exception → return {}`. Do NOT translate via `translate_bq_error`; the `/schema` endpoint must continue to return successfully with empty partition info when BQ is unreachable, not 502.

```python
def _fetch_bq_table_options(bq: BqAccess, dataset: str, table: str) -> dict:
    from src.identifier_validation import validate_quoted_identifier
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        return {}

    try:
        with bq.duckdb_session() as conn:
            bq_sql = (
                f"SELECT column_name, is_partitioning_column, clustering_ordinal_position "
                f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                f"WHERE table_name = ? "
                f"ORDER BY clustering_ordinal_position NULLS LAST"
            )
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
        if not rows:
            return {}
        partition_by = next(
            (r[0] for r in rows if (r[1] or "").upper() == "YES"),
            None,
        )
        clustered_by = [r[0] for r in rows if r[2] is not None]
        return {"partition_by": partition_by, "clustered_by": clustered_by}
    except Exception as e:
        logger.warning("BQ table options fetch failed for %s.%s.%s: %s",
                       bq.projects.data, dataset, table, e)
        return {}
```

Endpoint signature gains `bq: BqAccess = Depends(get_bq_access)`.

#### D. `src/remote_query.py` — lazy `BqAccess` construction

`RemoteQueryEngine.__init__` signature changes:

```python
def __init__(
    self,
    ...,  # existing args
    bq_access: BqAccess | None = None,
):
    ...
    self._bq = bq_access  # may stay None — resolved lazily
```

**Lazy resolution is critical.** Many existing tests in `tests/test_remote_query.py` (lines 106, 148, 196, 417, 520, 529, 538) construct `RemoteQueryEngine(analytics_conn)` for DuckDB-only paths that never touch BQ. After this change, eager `get_bq_access()` at construction would fail those tests with `not_configured` in any environment without `instance.yaml` configured for BQ. Resolve only when actually needed:

```python
def _get_bq_client(self):
    if self._bq is None:
        from connectors.bigquery.access import get_bq_access
        self._bq = get_bq_access()  # may raise BqAccessError; that's fine
    return self._bq.client()
```

`_bq_client_factory` parameter, the docstring at line 204 (which referenced the stale `scripts.duckdb_manager._create_bq_client` default), and lines 407-450 of `_get_bq_client` all delete. The fallback chain logic moves to `get_bq_access`.

**External caller note:** `cli/commands/query.py:120` constructs `RemoteQueryEngine(conn, **engine_kwargs)`. The new `bq_access` kwarg has default `None`, so the CLI continues to work via the lazy `get_bq_access()` path. No CLI change needed.

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

DuckDB-only `RemoteQueryEngine` tests (the ~7 sites listed above) need NO change — `bq_access=None` defaults preserve today's behavior; `get_bq_access()` is never called.

For FastAPI endpoint tests (`tests/test_v2_*.py`), use FastAPI's `dependency_overrides`:

```python
def test_v2_scan_x(client):
    bq = BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=lambda projects: mock_client,
    )
    app.dependency_overrides[get_bq_access] = lambda: bq
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
    def _build(*, client=None, duckdb_conn=None,
               billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client else None,
            duckdb_session_factory=(
                lambda projects: contextlib.nullcontext(duckdb_conn)
            ) if duckdb_conn else None,
        )
        app.dependency_overrides[get_bq_access] = lambda: bq
        return bq

    yield _build
    app.dependency_overrides.clear()
```

> **Fixture caveat for nested sessions.** `contextlib.nullcontext(duckdb_conn)` does NOT close the conn on `__exit__`. The production path closes it via `_default_duckdb_session_factory`. Tests that exercise multiple sequential `bq.duckdb_session()` calls within a single test function will see the same conn object both times — fine for assertion purposes, but won't catch close-and-reopen regressions. The unit test `test_duckdb_session_closes_on_exit` covers the close behavior on the production factory; the fixture is for endpoint integration tests that don't care.

### Tests

#### Unit tests — `tests/test_bq_access.py` (new)

| Test | Asserts |
|---|---|
| `test_resolve_env_var_wins` | `BIGQUERY_PROJECT=foo` overrides `instance.yaml`; both billing+data = foo |
| `test_resolve_billing_falls_back_to_project` | unset `billing_project` → both billing and data = `project` |
| `test_resolve_billing_distinct_from_project` | both set → billing and data differ |
| `test_resolve_raises_when_neither_set` | `BqAccessError(kind='not_configured')` with hint |
| `test_resolve_succeeds_after_config_set` | call once → raises; set config; call again → succeeds (functools.cache doesn't cache exceptions) |
| `test_get_bq_access_is_cached` | two successful calls return the same instance |
| `test_translate_forbidden_serviceusage` | `gax.Forbidden('serviceusage.services.use')` → `kind='cross_project_forbidden'` + hint |
| `test_translate_forbidden_no_serviceusage_diff_projects` | `gax.Forbidden('table-level perm denied')` + billing≠data → `kind='bq_forbidden'` (NOT cross_project) |
| `test_translate_forbidden_same_project` | `gax.Forbidden` + billing==data → `kind='bq_forbidden'` |
| `test_translate_bad_request_client_error` | `gax.BadRequest`, `bad_request_status='client_error'` → `kind='bq_bad_request'`, status 400 |
| `test_translate_bad_request_upstream_error` | `gax.BadRequest`, `bad_request_status='upstream_error'` → `kind='bq_upstream_error'`, status 502 |
| `test_translate_passes_through_BqAccessError` | `BqAccessError('bq_lib_missing', ...)` in → identical out (CRITICAL — guards against bq.client() raising it inside try/except) |
| `test_translate_unknown_reraises` | `RuntimeError("oops")` is re-raised, NOT silently wrapped |
| `test_client_uses_billing_as_quota_project` | `_default_client_factory` constructs with `quota_project_id=projects.billing` |
| `test_default_client_factory_raises_bq_lib_missing_on_importerror` | mock missing google-cloud-bigquery → `BqAccessError(bq_lib_missing)` |
| `test_default_duckdb_session_raises_auth_failed_on_metadata_error` | mock `get_metadata_token` raising `BQMetadataAuthError` → `BqAccessError(auth_failed)` |
| `test_duckdb_session_closes_on_exit` | mock token + duckdb conn, assert `conn.close()` called |
| `test_duckdb_session_closes_on_exception` | exception inside `with` block still triggers `conn.close()` |
| `test_injected_client_factory_overrides_default` | `BqAccess(..., client_factory=...)` skips `_default_client_factory` |

#### Integration tests — extending `tests/test_v2_*.py`

| Test | Asserts |
|---|---|
| `test_v2_scan_returns_502_on_bq_forbidden_serviceusage` | mock client raises `gax.Forbidden('...serviceusage...')`; response 502 + body `error=cross_project_forbidden` + hint mentions `billing_project` |
| `test_v2_scan_returns_400_on_bq_bad_request` | mock client raises `gax.BadRequest('invalid syntax')`; response 400 + body `error=bq_bad_request` |
| `test_v2_scan_estimate_returns_502_on_bq_forbidden` | same pattern for `/scan/estimate` |
| `test_v2_scan_returns_500_on_bq_lib_missing` | mock client raises `BqAccessError(bq_lib_missing)`; response 500 + structured body |
| `test_v2_sample_returns_502_on_bq_forbidden` | mock duckdb_session raises via `bigquery_query`; response 502 + structured body |
| `test_v2_sample_returns_400_on_unsafe_identifier` | registry row with backtick in `source_table` → 400 + body `error=unsafe_identifier` |
| `test_v2_sample_returns_404_on_unknown_table` | unchanged behavior (regression guard) |
| `test_v2_sample_returns_403_on_unauthorized` | unchanged behavior (regression guard) |
| `test_v2_schema_returns_502_on_bq_forbidden` | strict block (`_fetch_bq_schema`) failure → 502 |
| `test_v2_schema_returns_200_with_empty_partition_on_bq_failure` | best-effort block (`_fetch_bq_table_options`) failure → 200, schema returned, partition_by/clustered_by absent. Regression guard for the swallow-all preservation. |
| `test_v2_schema_returns_200_on_success` | regression guard (the existing happy path) |

#### E2E manual verification (post-deploy on `<your-dev-instance>`)

For each of `/sample`, `/scan/estimate`, `/scan`, `/schema`:

```bash
PAT=...

# Pre-deploy reference (current production behavior — bare 500 with no body for /sample/scan*)
curl -k -i -H "Authorization: Bearer $PAT" \
  https://<your-agnes-host>/api/v2/sample/<bq_table_id>?n=2

curl -k -i -X POST -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  https://<your-agnes-host>/api/v2/scan/estimate \
  -d '{"table_id":"<bq_table_id>","select":["event_date"],"where":"event_date = DATE \"2026-04-21\""}'

curl -k -i -X POST -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  https://<your-agnes-host>/api/v2/scan \
  -d '{"table_id":"<bq_table_id>","select":["event_date"],"where":"event_date = DATE \"2026-04-21\"","limit":50}'

curl -k -i -H "Authorization: Bearer $PAT" \
  https://<your-agnes-host>/api/v2/schema/<bq_table_id>

# Post-deploy, BEFORE fixing instance.yaml — expect:
#   /sample, /scan, /scan/estimate: 502 + structured JSON body with
#                                   error=cross_project_forbidden and a hint.
#   /schema: 200 (because INFORMATION_SCHEMA queries don't fail today; if
#            something else fails on the strict block, expect 502).

# Operator action: set data_source.bigquery.billing_project in instance.yaml,
# restart the container.

# Post-config-fix — expect 200 on all four endpoints.
```

This four-endpoint × three-state matrix is the success criterion for closing #134. Without it, "fixed" is unverifiable.

## Implementation strategy — staged commits

Per first-round review: stage as **two commits** so the user-visible bug fix is independently reviewable / revertable from the refactor. Per second-round review: **both commits emit the same structured response shape** so client-side parsers (CLI, UI) don't see contract churn between commits.

**Commit 1 — Minimal bug fix (revertable, atomic across all three v2 endpoints):**
- `app/api/v2_sample.py`:
  - Read `billing_project` with same fallback as `v2_scan.py:385`; pass to `bigquery_query()`.
  - Wrap `_fetch_bq_sample` in `try/except google.api_core.exceptions.*` translating to `HTTPException` with the structured body shape (see below).
- `app/api/v2_scan.py`: wrap `_bq_dry_run_bytes` and `_run_bq_scan` in the same `try/except` shape.
- `app/api/v2_schema.py`: wrap `_fetch_bq_schema` (strict block) in the same `try/except`. **Do NOT touch `_fetch_bq_table_options`** — its `except Exception → return {}` swallow-all is preserved unchanged in commit 1, then migrated to use `bq.duckdb_session()` in commit 2 (still preserved).

All three endpoints emit the **same structured body shape** that commit 2 will produce, so client-side parsers (CLI, UI) see one consistent contract throughout the rollout:

```python
except gax.Forbidden as e:
    kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
    raise HTTPException(
        status_code=502,
        detail={"error": kind, "message": str(e), "details": {...}},
    )
except gax.BadRequest as e:
    # v2_scan: bad_request_status='client_error' → kind='bq_bad_request', 400
    # v2_sample, v2_schema: bad_request_status='upstream_error' → kind='bq_upstream_error', 502
    raise HTTPException(
        status_code=400 if <user_derived> else 502,
        detail={"error": "bq_bad_request" if <user_derived> else "bq_upstream_error",
                "message": str(e), "details": {}},
    )
```

- Tests: regression tests for the structured body shape on all three endpoints.

This commit alone closes the user-visible part of #134 atomically — no half-rolled-out window where `/sample` returns bare 500 while `/scan` returns structured 502. If commit 2 needs another review round, commit 1 still ships and the response contract is forward-compatible.

**Commit 2 — `BqAccess` extraction + migration:**
- Create `connectors/bigquery/access.py` with the design above.
- Migrate `v2_scan`, `v2_sample`, `v2_schema` (both blocks, with separate semantics), `RemoteQueryEngine` (lazy `bq_access`) to `BqAccess`.
- Remove `_bq_client_factory` from `RemoteQueryEngine.__init__` and the stale docstring at `src/remote_query.py:204`.
- Migrate tests to the new `BqAccess(client_factory=...)` + `dependency_overrides` pattern.
- Delete inline `try/except gax.*` blocks added in commit 1; route through `translate_bq_error` instead.

## Risks

1. **Test rewrite breaks something subtle.** `tests/test_remote_query.py` and possibly `tests/test_duckdb_manager.py` have many `_bq_client_factory` call sites. The new fixture pattern must cover every shape they exercise. Mitigation: convert tests one-by-one in commit 2, run pytest after each, before deleting the old injection point. DuckDB-only tests that don't touch BQ are protected by lazy `bq_access` resolution in `RemoteQueryEngine`.
2. **Cross-project Forbidden detection heuristic is narrow but principled.** Relies on Google's error message containing `'serviceusage'` (case-insensitive). False positives are unlikely (the substring is specific). False **negatives** are possible — those degrade to `bq_forbidden` with a generic message, still a 502 with structured body. Acceptable.
3. **`get_bq_access()` is `@functools.cache`'d.** Cheap and process-lifetime-safe (config is loaded at boot and immutable). Tests use `dependency_overrides` and direct `BqAccess(...)` construction, never the cached path — no cache invalidation needed in tests. Hot-reload of `instance.yaml` is explicitly out of scope.
4. **`bq_bad_request → 400` could leak BQ error messages.** BQ's `BadRequest` text typically describes the SQL problem. We surface it in `details.message`. Operators who don't want this can filter at a reverse-proxy layer; this matches behavior of any 4xx-with-detail elsewhere in the app.
5. **`BIGQUERY_PROJECT` env-var precedence is BREAKING for env-only deployments.** Deployments that combine env-var-for-billing + yaml-for-data must migrate. See the project-resolution rules section. Flag in CHANGELOG and release notes.
6. **Two duplicate sites left behind (`extractor.py`, `register_bq_table`).** Explicit follow-up issue should be filed at PR-merge time.
7. **`translate_bq_error` pass-through ordering is load-bearing.** `bq.client()` and `bq.duckdb_session()` raise `BqAccessError` directly for `bq_lib_missing` / `auth_failed`. The translator's first clause MUST be `if isinstance(e, BqAccessError): return e`. Otherwise those typed errors fall through to "unknown" and get re-raised as bare 500. Unit-tested via `test_translate_passes_through_BqAccessError`.
8. **In-memory DB reload cost on each `duckdb_session()` (pre-existing).** Every BQ-via-DuckDB call runs `INSTALL bigquery` fresh. If the extension binary isn't already cached on disk, this is an HTTPS download. Today's code has the same cost; this PR doesn't fix it. Future optimization: long-lived in-memory conn with extension pre-loaded, behind a thread-safe pool.

## CHANGELOG entry (for the implementation PR)

Under `## [Unreleased]`:

**`### Fixed`**
- v2 `/sample` endpoint: BigQuery cross-project queries now respect `data_source.bigquery.billing_project` from `instance.yaml` (mirrors v2 `/scan` fix from `33a9964`). Closes #134 for `/sample`.
- v2 `/scan`, `/scan/estimate`: BigQuery upstream errors no longer return bare HTTP 500 with empty body. `Forbidden` from BQ now returns HTTP 502 with structured JSON body (`{"error": "cross_project_forbidden", "message": "...", "details": {"hint": "..."}}`); `BadRequest` on user-derived SQL returns HTTP 400 with `kind=bq_bad_request`. Closes #134 for `/scan*`.
- v2 `/schema`: same error translation applied to the strict path (`_fetch_bq_schema`); the best-effort partition-info path (`_fetch_bq_table_options`) preserves its swallow-all-and-return-empty behavior, so `/schema` still returns 200 with empty partition info if BQ partition-info queries fail.

**`### Changed`**
- **BREAKING for deployments using `BIGQUERY_PROJECT` env var alongside `data_source.bigquery.project` in `instance.yaml`.** The env var now sets BOTH billing and data project, overriding `data_source.bigquery.project` for FROM-clause construction in `v2_scan` / `v2_sample` / `v2_schema`. Migrate by clearing `BIGQUERY_PROJECT` and using `data_source.bigquery.billing_project` + `data_source.bigquery.project` in `instance.yaml`. (Previously `BIGQUERY_PROJECT` only affected `RemoteQueryEngine` billing.)

**`### Internal`**
- New shared module `connectors/bigquery/access.py` — `BqAccess` facade unifies BQ project resolution, client construction, DuckDB-extension session, and Google-API error translation across `v2_scan`, `v2_sample`, `v2_schema`, and `RemoteQueryEngine`.
- **Internal API change:** `RemoteQueryEngine.__init__` no longer accepts `_bq_client_factory`. Callers that injected it migrate to `RemoteQueryEngine(..., bq_access=BqAccess(projects, client_factory=...))`. The CLI (`cli/commands/query.py`) is unaffected — it never injected the factory and the new `bq_access` kwarg defaults to `None` (lazy `get_bq_access()` on first BQ call).
- Stale docstring at `src/remote_query.py:204` (referencing `scripts.duckdb_manager._create_bq_client` as the default factory) removed.
- Two known-duplicate BQ-access sites (`connectors/bigquery/extractor.py`, `scripts/duckdb_manager.register_bq_table`) explicitly out of scope; tracked as follow-up.
