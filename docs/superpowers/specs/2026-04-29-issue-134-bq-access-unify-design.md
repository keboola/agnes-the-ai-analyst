# Issue #134 — Unify BigQuery access behind `BqAccess`, fix v2_sample + 502 contract

**Date:** 2026-04-29
**Issue:** [keboola/agnes-the-ai-analyst#134](https://github.com/keboola/agnes-the-ai-analyst/issues/134)
**Branch:** `fix/134-bq-access-unify`

## Problem

Issue #134 reports that v2 BigQuery endpoints on `agnes-development` (0.18.0) return HTTP 500 with no body:

- `POST /api/v2/scan/estimate`
- `POST /api/v2/scan`
- `GET /api/v2/sample/{table_id}`

`POST /api/query/hybrid` against the same tables works correctly.

### Root cause analysis (corrects the issue's hypothesis)

The issue header attributes commit `33a9964` to a fix in `RemoteQueryEngine`. That is wrong. `33a9964` actually fixed `app/api/v2_scan.py` (added `billing_project` parameter, used as `quota_project_id` for the BQ client). The `RemoteQueryEngine` fallback chain (`BIGQUERY_PROJECT` env → `instance.yaml billing_project` → `instance.yaml project`) lives at `src/remote_query.py:407-449` and was added separately.

What is actually broken in code today:

1. **`app/api/v2_sample.py:104`** reads only `data_source.bigquery.project`, never `billing_project`. It then passes that project to `bigquery_query()` as the billing target. This is the same bug `33a9964` fixed in `v2_scan.py`, just in a sibling file that didn't get the same patch.

2. **`app/api/v2_sample.py`** has no structured error handling — it catches only `FileNotFoundError` (404) and `PermissionError` (403). Anything else (Google API `Forbidden`, `BadRequest`, identifier validation `ValueError`, `ImportError`) bubbles up as bare HTTP 500 with no body.

3. **`app/api/v2_scan.py`** catches `WhereValidationError`, `QuotaExceededError`, `FileNotFoundError`, `PermissionError`, `ValueError`. It does **not** catch `google.api_core.exceptions.Forbidden` / `BadRequest`. When the SA lacks `serviceusage.services.use` on the data project (Pavel's case), the BQ client raises `Forbidden` which propagates as bare 500. This is plausibly the symptom Pavel reports on `/scan/estimate`.

4. **Operator-config dimension (out of scope for code fix):** if `instance.yaml` on `agnes-development` does not set `data_source.bigquery.billing_project`, the fallback `or project_id` in `v2_scan.py` puts the call right back in the broken state. Fix surfaces this to the operator via a structured error body with `hint`.

### Three duplicate code paths

The BQ-access pattern is duplicated three times today:

- `app/api/v2_scan.py:_bq_dry_run_bytes`, `_run_bq_scan` — `bigquery.Client(...)` construction
- `app/api/v2_sample.py:_fetch_bq_sample` — DuckDB BQ extension via `bigquery_query()`, uses `get_metadata_token()`
- `src/remote_query.py:RemoteQueryEngine._get_bq_client` — `bigquery.Client(...)` construction with full fallback chain

Each call site re-implements project resolution from `instance.yaml` and BQ client construction, with subtle drift.

## Goals

1. **Fix the v2_sample billing_project bug** so cross-project BQ reads work when the operator sets `billing_project`.
2. **Translate Google API errors into structured 502 responses** with actionable bodies (the user / CLI gets an error shape they can reason about, not bare 500).
3. **Eliminate the three duplicate BQ-access call sites** behind a single facade so the fix lives in one place.
4. **Preserve test invasiveness** — existing tests that mock the BQ client must remain straightforward to write.

## Non-goals

- Changing the operator-facing config schema. `data_source.bigquery.billing_project` already exists; we just route everything through it.
- Auto-detecting cross-project misconfiguration at startup (a guardrail discussed but rejected — keeping scope tight).
- Touching the `/api/query/hybrid` endpoint behavior — `RemoteQueryEngine` internals change, but the endpoint contract does not.
- Migrating extant `extract.duckdb` files or `_remote_attach` rows.

## Design

### Architecture — new module `connectors/bigquery/access.py`

```
connectors/bigquery/
├── auth.py          (existing — get_metadata_token, unchanged)
├── extractor.py     (existing — unchanged)
└── access.py        (NEW)
    ├── BqProjects        (frozen dataclass)
    ├── BqAccessError     (typed exception, with HTTP_STATUS class mapping)
    ├── BqAccess          (facade)
    └── translate_bq_error(e, projects) -> BqAccessError
```

### `BqAccess` public API

```python
@dataclass(frozen=True)
class BqProjects:
    billing: str   # billing/quota target — used as `project=` and `quota_project_id=`
    data: str      # default data project for FROM clauses

class BqAccessError(Exception):
    HTTP_STATUS = {
        "not_configured":          500,  # admin/config bug — page on-call
        "bq_lib_missing":          500,  # deployment bug
        "auth_failed":             502,  # GCP metadata server unreachable
        "cross_project_forbidden": 502,  # SA lacks serviceusage.services.use on billing project
        "bq_forbidden":            502,  # other Forbidden from BQ
        "bq_bad_request":          502,  # 400 from BQ (malformed query etc.)
        "bq_upstream_error":       502,  # other GoogleAPICallError catch-all
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)


class BqAccess:
    """Single entry point for BigQuery access — config resolution, client construction,
    DuckDB-extension session, and error translation. Stateless after construction."""

    def __init__(self, projects: BqProjects):
        self._projects = projects

    @classmethod
    def from_config(cls) -> "BqAccess":
        """Resolve projects from BIGQUERY_PROJECT env → instance.yaml billing_project → project.
        Raises BqAccessError(kind='not_configured') if data project is unresolvable."""

    @property
    def projects(self) -> BqProjects: ...

    def client(self) -> "bigquery.Client":
        """bigquery.Client(project=billing, client_options=ClientOptions(quota_project_id=billing)).
        Raises BqAccessError(kind='bq_lib_missing') if google-cloud-bigquery is not installed."""

    @contextmanager
    def duckdb_session(self) -> Iterator["duckdb.DuckDBPyConnection"]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set
        from get_metadata_token(). Auto-cleanup. Translates INSTALL/LOAD/SECRET failures
        to BqAccessError(kind='auth_failed' or 'bq_lib_missing')."""


def translate_bq_error(e: Exception, projects: BqProjects) -> BqAccessError:
    """Pass-through for BqAccessError. Maps google.api_core exceptions:
      - Forbidden + ('serviceusage' in msg or billing != data) -> cross_project_forbidden
      - Forbidden                                              -> bq_forbidden
      - BadRequest                                             -> bq_bad_request
      - GoogleAPICallError (catch-all)                         -> bq_upstream_error
    Unknown exceptions are re-raised — never silently swallowed."""
```

### Project resolution rules (single source of truth)

`from_config` resolves projects in this order (matching today's `RemoteQueryEngine._get_bq_client` behavior):

1. `BIGQUERY_PROJECT` env var → if set, used as **both** billing and data (legacy override).
2. `data_source.bigquery.billing_project` from `instance.yaml` → billing.
3. `data_source.bigquery.project` from `instance.yaml` → data, and billing if (2) is unset.

If neither (1) nor (3) yields a value: `BqAccessError(kind='not_configured')` with a `details.hint` pointing to the missing config key.

### Error translation contract

`translate_bq_error` is the only place in the codebase that knows how to interpret raw Google API exceptions. Call sites wrap their BQ calls in `try/except Exception → raise translate_bq_error(e, bq.projects)` so the rest of the system speaks `BqAccessError`.

Endpoints translate `BqAccessError` to HTTP via:

```python
except BqAccessError as e:
    raise HTTPException(
        status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
        detail={"error": e.kind, "message": e.message, "details": e.details},
    )
```

Status code mapping rationale:
- **502** for upstream BQ refusal (Pavel's bug + the kind-of bugs that are server-side misconfig from the user's POV but caused by an upstream service). Operationally distinguishable from 500 in dashboards — "integration with BQ broken" vs "Agnes itself broken".
- **500** for `not_configured` and `bq_lib_missing` — these are deployment / admin-config bugs that should page on-call, not a transient upstream error.

### Migration of three call sites

#### `app/api/v2_scan.py`

`_bq_dry_run_bytes` and `_run_bq_scan` change signature from `(project: str, sql: str)` to `(bq: BqAccess, sql: str)`. Body becomes:

```python
def _bq_dry_run_bytes(bq: BqAccess, sql: str) -> int:
    from google.cloud import bigquery
    try:
        job = bq.client().query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects)
```

`scan_endpoint`, `scan_estimate_endpoint`, and the underlying `estimate` / `run_scan` functions lose the `project_id` and `billing_project` parameters — they take a `BqAccess` instead. Endpoints construct it with `bq = BqAccess.from_config()` and add `except BqAccessError` after the existing exception clauses.

The existing `_build_bq_sql(table_row, project_id, req)` keeps its `project_id` parameter (it's the data project for the FROM clause), passed as `bq.projects.data`.

#### `app/api/v2_sample.py`

`_fetch_bq_sample` changes signature from `(project: str, dataset: str, table: str, n: int)` to `(bq: BqAccess, dataset: str, table: str, n: int)`. Body becomes:

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
            raise translate_bq_error(e, bq.projects)
```

`build_sample` and `sample` endpoint signatures lose `project_id` for `BqAccess`. Endpoint adds `except BqAccessError` and `except ValueError` (→ 400 for unsafe identifiers).

#### `src/remote_query.py`

`RemoteQueryEngine.__init__` loses the `_bq_client_factory` parameter. `_get_bq_client` collapses to:

```python
def _get_bq_client(self):
    from connectors.bigquery.access import BqAccess
    return BqAccess.from_config().client()
```

Lines 418-449 (lazy import + project resolution + ImportError handling) move into `BqAccess.from_config` and `BqAccess.client`. The class becomes thinner; its public API loses the test-only injection point.

### Test rewrite (eliminating `_bq_client_factory`)

Existing tests that injected `_bq_client_factory` into `RemoteQueryEngine` migrate to monkey-patching `BqAccess.from_config`. New shared fixture in `tests/conftest.py`:

```python
@pytest.fixture
def bq_access_factory(monkeypatch):
    """Returns a callable that patches BqAccess.from_config to yield a controllable BqAccess.

    Usage:
        def test_x(bq_access_factory):
            mock_client = MagicMock()
            bq_access_factory(client=mock_client)
            engine = RemoteQueryEngine(...)
            engine.execute(...)
    """
    def _factory(*, client=None, projects=None):
        proj = projects or BqProjects(billing="test-billing", data="test-data")
        bq = BqAccess(proj)
        if client is not None:
            bq.client = lambda: client
        monkeypatch.setattr(
            "connectors.bigquery.access.BqAccess.from_config", classmethod(lambda cls: bq)
        )
        return bq
    return _factory
```

All three callers (`tests/test_remote_query.py`, `tests/test_v2_scan.py`, `tests/test_v2_sample.py`) use this fixture uniformly. No more per-call-site mocking glue.

### Tests

#### Unit tests — `tests/test_bq_access.py` (new)

| Test | Asserts |
|---|---|
| `test_resolve_env_var_wins` | `BIGQUERY_PROJECT=foo` overrides `instance.yaml` |
| `test_resolve_billing_falls_back_to_project` | unset `billing_project` → both billing and data = `project` |
| `test_resolve_raises_when_neither_set` | `BqAccessError(kind='not_configured')` |
| `test_translate_forbidden_cross_project` | `gax.Forbidden` with `'serviceusage'` in msg → `kind='cross_project_forbidden'` + `details.hint` mentioning `data_source.bigquery.billing_project` |
| `test_translate_forbidden_diff_projects` | `gax.Forbidden` + billing != data (regardless of msg) → `kind='cross_project_forbidden'` |
| `test_translate_forbidden_same_project` | `gax.Forbidden` + billing == data + no `'serviceusage'` substring → `kind='bq_forbidden'` |
| `test_translate_bad_request` | `gax.BadRequest` → `kind='bq_bad_request'` |
| `test_translate_passes_through_typed` | `BqAccessError` in → identical out |
| `test_translate_unknown_reraises` | `RuntimeError("oops")` is re-raised, NOT silently wrapped |
| `test_client_uses_billing_as_quota_project` | mock `bigquery.Client`, assert `quota_project_id=projects.billing` |
| `test_duckdb_session_closes_on_exit` | mock token + duckdb conn, assert `conn.close()` called |
| `test_duckdb_session_closes_on_exception` | exception inside `with` block still triggers `conn.close()` |

#### Integration tests — extending `tests/test_v2_scan.py`, `tests/test_v2_sample.py`

| Test | Asserts |
|---|---|
| `test_v2_scan_returns_502_on_bq_forbidden` | patch `BqAccess.client` to raise `gax.Forbidden`; response is 502, body has `error=cross_project_forbidden` + hint |
| `test_v2_scan_estimate_returns_502_on_bq_forbidden` | same for `/scan/estimate` |
| `test_v2_sample_returns_502_on_bq_forbidden` | patch `BqAccess.duckdb_session` to raise via `bigquery_query`; response is 502, body has structured shape |
| `test_v2_sample_returns_400_on_unsafe_identifier` | registry row with backtick in `source_table` → 400 with structured body |
| `test_v2_sample_returns_404_on_unknown_table` | unchanged behavior (regression guard) |
| `test_v2_sample_returns_403_on_unauthorized` | unchanged behavior (regression guard) |

#### E2E manual verification (post-deploy on `agnes-development`)

```bash
PAT=...

# Pre-fix verification (current state) — expect 500 with empty body on all three:
curl -k -i -H "Authorization: Bearer $PAT" \
  https://agnes-development.groupondev.com/api/v2/sample/s1_session_landings?n=2

# Post-deploy, BEFORE fixing instance.yaml — expect 502 + structured JSON body
# with error=cross_project_forbidden and a hint pointing at billing_project:
curl -k -i -H "Authorization: Bearer $PAT" \
  https://agnes-development.groupondev.com/api/v2/sample/s1_session_landings?n=2

# Operator action: set data_source.bigquery.billing_project in instance.yaml,
# restart the container.

# Post-config-fix — expect 200 with sample rows:
curl -k -i -H "Authorization: Bearer $PAT" \
  https://agnes-development.groupondev.com/api/v2/sample/s1_session_landings?n=2
```

This three-step E2E is the success criterion for closing #134. Without it, "fixed" is unverifiable.

## Out of scope (explicitly)

- **Startup-time BQ misconfig warning.** Discussed and rejected for scope. The structured 502 with hint is the user-visible signal; adding a startup probe means a real BQ call at boot, which is a separate concern.
- **`/api/query/hybrid` endpoint behavior change.** Internal `RemoteQueryEngine.__init__` signature changes (drops `_bq_client_factory`), but the HTTP contract of `/api/query/hybrid` does not.
- **Schema migration / data migration.** None required.

## Risks

1. **Test rewrite breaks something subtle.** Existing tests use `_bq_client_factory` for various RemoteQueryEngine flows. The new fixture must cover every shape they exercised. Mitigation: convert tests one-by-one, run pytest after each, before deleting the old injection point.
2. **Cross-project Forbidden detection heuristic is fuzzy.** Distinguishing `cross_project_forbidden` from generic `bq_forbidden` relies on message-substring + project-comparison. False negatives are mostly cosmetic (user gets less-specific hint). False positives are unlikely (only when billing != data, which is itself a strong signal).
3. **`BqAccess.from_config()` is called per-request in endpoints.** Cheap (just env + yaml lookup) but worth noting — instance is cheap to construct.

## CHANGELOG entry (for the implementation PR)

Under `## [Unreleased]`:

**`### Fixed`**
- v2 `/sample` endpoint: BigQuery cross-project queries now respect `data_source.bigquery.billing_project` from `instance.yaml` (mirrors v2 `/scan` fix from `33a9964`). Closes #134.
- v2 `/scan`, `/scan/estimate`, `/sample`: BigQuery upstream errors now return HTTP 502 with structured JSON body (`{"error": "<kind>", "message": "...", "details": {...}}`) instead of bare HTTP 500. Cross-project Forbidden gets `kind=cross_project_forbidden` with a hint pointing at the missing config key.

**`### Internal`**
- New shared module `connectors/bigquery/access.py` — `BqAccess` facade unifies BQ project resolution, client construction, DuckDB-extension session, and Google-API error translation across `v2_scan`, `v2_sample`, and `RemoteQueryEngine`. Removes `_bq_client_factory` injection point from `RemoteQueryEngine` (test fixture `bq_access_factory` in `tests/conftest.py` replaces it).
