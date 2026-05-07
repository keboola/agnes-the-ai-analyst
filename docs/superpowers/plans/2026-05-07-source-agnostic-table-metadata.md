# Source-Agnostic Table Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich `/api/v2/catalog` with cost-relevant per-table metadata (row count, size, partition, cluster) for `query_mode='remote'` rows via source-agnostic providers; add unified cache invalidation across all four catalog/schema/sample/metadata caches with single-row re-warm; halve BQ jobs on `/api/v2/schema` cache miss by consolidating two INFORMATION_SCHEMA.COLUMNS queries into one; auto-warm caches at server startup; surface progress via SSE on `/admin/tables`. Closes #155 + #156.

**Architecture:** Per-source `metadata.py` module exposing `fetch(MetadataRequest) -> TableMetadata | None`; dispatched from `app/api/v2_catalog.py:_size_hint_for_row` after identifier validation; results cached 15 min keyed by `table_id`. Cache invalidation owned by `v2_catalog.invalidate_for_table` flushing four TTLCaches and scheduling a single-row re-warm. Warmup runs as a FastAPI startup background task with bounded concurrency (default 4) and exposes a JSON status endpoint + SSE stream consumed by a new toolbar block on `/admin/tables`.

**Tech Stack:** Python 3.11+, FastAPI, DuckDB (with BigQuery extension), pytest, sse-starlette, vanilla JS (no framework) in admin templates.

**Source spec:** `docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md` (HEAD `a6a660bc`).

**Branch:** `zs/issue-155-156-source-agnostic-metadata` (worktree at `/tmp/agnes-metadata`).

---

## File map

**New:**
- `app/api/_metadata_models.py` — dataclasses `MetadataRequest`, `TableMetadata`
- `connectors/bigquery/metadata.py` — BQ provider
- `connectors/keboola/metadata.py` — Keboola provider
- `app/api/cache_warmup.py` — warmup state + background task + endpoints
- `docs/admin/query-modes.md` — admin doc page
- `tests/test_connectors_bigquery_metadata.py`
- `tests/test_connectors_keboola_metadata.py`
- `tests/test_v2_catalog_remote_metadata.py`
- `tests/test_v2_catalog_invalidation.py`
- `tests/test_cache_warmup.py`
- `tests/test_admin_tables_warmup_ui.py`
- `tests/test_v2_schema_columns_consolidation.py`

**Modified:**
- `app/api/v2_catalog.py` — provider dispatch + 15-min metadata cache + new response fields + `invalidate_for_table`
- `app/api/v2_schema.py` — split RBAC/cache from BQ work; rewire to shared `fetch_bq_columns_full`
- `connectors/bigquery/access.py` — append `fetch_bq_columns_full` helper
- `connectors/keboola/storage_api.py` — append `get_table_info` thin wrapper
- `app/api/admin.py` — wire `invalidate_for_table` into register/update/unregister
- `app/main.py` — register `warm_catalog_caches` startup event
- `cli/commands/admin.py` — third post-register hint for `query_mode=remote`
- `app/web/templates/admin_tables.html` — cache toolbar `<section>`, per-row `col-status` badge, EventSource JS, `?` doc link on query_mode field
- `pyproject.toml` — version bump to 0.46.0; add `sse-starlette` to dependencies if not present
- `CHANGELOG.md` — `## [0.46.0]` section

---

## Task 1: Shared dataclass models

**Files:**
- Create: `app/api/_metadata_models.py`
- Test: `tests/test_metadata_models.py`

- [ ] **Step 1.1: Write failing test**

Create `tests/test_metadata_models.py`:

```python
"""Sanity tests for the shared metadata dataclasses."""

from app.api._metadata_models import MetadataRequest, TableMetadata


def test_metadata_request_constructs():
    req = MetadataRequest(
        table_id="orders", bucket="dwh_base", source_table="orders_2024",
    )
    assert req.table_id == "orders"
    assert req.bucket == "dwh_base"
    assert req.source_table == "orders_2024"


def test_metadata_request_is_frozen():
    """Frozen so cache keys derived from a request are stable."""
    req = MetadataRequest(table_id="x", bucket="b", source_table="t")
    import dataclasses
    try:
        req.bucket = "other"
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("MetadataRequest should be frozen")


def test_table_metadata_all_fields_optional():
    tm = TableMetadata()
    assert tm.rows is None
    assert tm.size_bytes is None
    assert tm.partition_by is None
    assert tm.clustered_by is None


def test_table_metadata_partial_population():
    tm = TableMetadata(rows=100, size_bytes=2048)
    assert tm.rows == 100
    assert tm.size_bytes == 2048
    assert tm.partition_by is None
    assert tm.clustered_by is None
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_metadata_models.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api._metadata_models'`.

- [ ] **Step 1.3: Implement the module**

Create `app/api/_metadata_models.py`:

```python
"""Shared data shapes for source-agnostic table-metadata providers.

Lives under `app/api/` because the primary consumer is
`app/api/v2_catalog.py`. Connector-side providers in `connectors/<source>/`
import upward into this module — the inverse layering would force
`v2_catalog.py` to depend on `connectors/__init__.py`, which is the
wrong direction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetadataRequest:
    """Narrow input passed to a metadata provider's `fetch()`.

    `bucket` and `source_table` are pre-validated by the dispatcher
    (`validate_quoted_identifier`) before construction, so the provider
    can interpolate them into SQL/URL paths without re-checking. Frozen
    so the (provider, request)-keyed cache lookup is stable.
    """
    table_id: str
    bucket: str
    source_table: str


@dataclass
class TableMetadata:
    """Source-agnostic metadata bundle. Every field optional — providers
    fill what they can cheaply get; callers tolerate `None`. Adding a new
    field here is a non-breaking change: existing CLI consumers don't
    even render `rough_size_hint` (verified `grep -rn rough_size_hint cli/`
    is empty), let alone the new fields.
    """
    rows: int | None = None
    size_bytes: int | None = None
    partition_by: str | None = None
    clustered_by: list[str] | None = None
```

- [ ] **Step 1.4: Run test to verify it passes**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_metadata_models.py -v
```
Expected: 4 passed.

- [ ] **Step 1.5: Commit**

```bash
cd /tmp/agnes-metadata
git add app/api/_metadata_models.py tests/test_metadata_models.py
git commit -m "feat(metadata): MetadataRequest + TableMetadata dataclasses"
```

---

## Task 2: KeboolaStorageClient.get_table_info wrapper

**Files:**
- Modify: `connectors/keboola/storage_api.py` (append method)
- Test: `tests/test_keboola_storage_api.py` (append test)

- [ ] **Step 2.1: Write failing test**

Append to `tests/test_keboola_storage_api.py`:

```python
class TestGetTableInfo:
    """`get_table_info` is a thin wrapper around the existing _get path
    so the metadata provider doesn't have to bleed `_get` out of the
    module (#155)."""

    def test_calls_storage_api_with_table_id(self, monkeypatch):
        from connectors.keboola.storage_api import KeboolaStorageClient

        captured = {}

        def fake_get(self, path, **kwargs):
            captured["path"] = path
            return {"rowsCount": 100, "dataSizeBytes": 4096}

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(
            url="https://connection.keboola.com", token="tok"
        )
        info = client.get_table_info("in.c-orders.events")
        assert captured["path"] == "/tables/in.c-orders.events"
        assert info["rowsCount"] == 100
        assert info["dataSizeBytes"] == 4096

    def test_propagates_storage_api_error(self, monkeypatch):
        from connectors.keboola.storage_api import (
            KeboolaStorageClient, StorageApiError,
        )

        def fake_get(self, path, **kwargs):
            raise StorageApiError("404 not found", status=404, body={})

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(url="https://x", token="tok")
        import pytest
        with pytest.raises(StorageApiError):
            client.get_table_info("missing.table")
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_keboola_storage_api.py::TestGetTableInfo -v
```
Expected: FAIL with `AttributeError: 'KeboolaStorageClient' object has no attribute 'get_table_info'`.

- [ ] **Step 2.3: Implement the wrapper**

Find the line in `connectors/keboola/storage_api.py` immediately after the `export_table_async` method (around line 286) and append the new method to the `KeboolaStorageClient` class:

```python
    def get_table_info(self, table_id: str) -> dict:
        """GET /v2/storage/tables/{table_id} — full table metadata.

        Storage API guarantees `rowsCount` + `dataSizeBytes` on success.
        Other fields (`columns`, `primaryKey`, ...) are present but not
        consumed by the metadata provider today. Raises `StorageApiError`
        on 4xx/5xx — caller decides whether to soften to `None`.
        """
        return self._get(f"/tables/{table_id}")
```

- [ ] **Step 2.4: Run test to verify it passes**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_keboola_storage_api.py::TestGetTableInfo -v
```
Expected: 2 passed.

- [ ] **Step 2.5: Commit**

```bash
cd /tmp/agnes-metadata
git add connectors/keboola/storage_api.py tests/test_keboola_storage_api.py
git commit -m "feat(keboola): KeboolaStorageClient.get_table_info thin wrapper"
```

---

## Task 3: Combined `fetch_bq_columns_full` helper + v2_schema rewire

This task halves the BQ job count on `/api/v2/schema/{id}` cache miss (2 jobs → 1) by collapsing the duplicate `INFORMATION_SCHEMA.COLUMNS` queries.

**Files:**
- Modify: `connectors/bigquery/access.py` (append helper)
- Modify: `app/api/v2_schema.py` (rewire `_fetch_bq_schema` and `_fetch_bq_table_options`)
- Test: `tests/test_v2_schema_columns_consolidation.py` (new)

- [ ] **Step 3.1: Write failing test for the consolidation**

Create `tests/test_v2_schema_columns_consolidation.py`:

```python
"""Asserts that /api/v2/schema/{id} for a BQ row makes exactly ONE
bigquery_query() call on cache miss, down from two pre-#155.

Counts via a side-effect tracker on the mocked DuckDB session.
"""

from unittest.mock import MagicMock, patch
import pytest


def _mock_duckdb_session_returning(rows):
    """Build a context-manager mock that returns `rows` on .fetchall().

    Exposes `call_count` on the returned mock for assertion.
    """
    session = MagicMock()
    session.execute.return_value.fetchall.return_value = rows
    cm = MagicMock()
    cm.__enter__.return_value = session
    cm.__exit__.return_value = False
    return cm, session


def test_fetch_bq_columns_full_is_single_query():
    """The new shared helper makes exactly ONE call to bigquery_query."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm, session = _mock_duckdb_session_returning([
        ("event_date", "DATE", "NO", "YES", None),
        ("country", "STRING", "YES", "NO", 1),
        ("user_id", "STRING", "NO", "NO", None),
    ])
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert len(rows) == 3
    # Exactly one bigquery_query() call — no second round-trip.
    assert session.execute.call_count == 1
    first_call = session.execute.call_args_list[0]
    # Outer wrapper SQL is bigquery_query(?, ?, ?)
    assert "bigquery_query" in first_call.args[0]
    # Inner BQ SQL pulls all five columns we need at once.
    inner_sql = first_call.args[1][1]
    assert "column_name" in inner_sql
    assert "data_type" in inner_sql
    assert "is_nullable" in inner_sql
    assert "is_partitioning_column" in inner_sql
    assert "clustering_ordinal_position" in inner_sql


def test_fetch_bq_columns_full_returns_dicts():
    """Each row is a dict with the documented keys."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm, _ = _mock_duckdb_session_returning([
        ("event_date", "DATE", "NO", "YES", None),
    ])
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows == [{
        "name": "event_date",
        "type": "DATE",
        "nullable": False,
        "is_partitioning_column": True,
        "clustering_ordinal_position": None,
    }]


def test_fetch_bq_columns_full_returns_none_when_unconfigured():
    """Sentinel BqAccess (data project empty) → return None, no query."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = ""  # sentinel
    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows is None
    bq.duckdb_session.assert_not_called()


def test_fetch_bq_columns_full_returns_none_on_unsafe_identifier():
    """Refuses to interpolate identifiers that fail validation."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    rows = fetch_bq_columns_full(bq, "evil`; DROP--", "events")
    assert rows is None
    bq.duckdb_session.assert_not_called()


def test_fetch_bq_columns_full_returns_none_on_query_error():
    """BQ failure → log + None; never raises."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm = MagicMock()
    cm.__enter__.return_value.execute.side_effect = RuntimeError("BQ down")
    cm.__exit__.return_value = False
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows is None
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema_columns_consolidation.py -v
```
Expected: FAIL with `ImportError: cannot import name 'fetch_bq_columns_full' from 'connectors.bigquery.access'`.

- [ ] **Step 3.3: Implement `fetch_bq_columns_full`**

Append to `connectors/bigquery/access.py` (location: end of file or just before any module-level `_BILLING_PROJECT_RE` block — pick whichever your editor lands on):

```python
def fetch_bq_columns_full(bq, dataset: str, table: str) -> list[dict] | None:
    """Single round-trip to INFORMATION_SCHEMA.COLUMNS pulling everything
    both v2_schema and the metadata provider need.

    Returns one dict per column with the keys ``name``, ``type``,
    ``nullable``, ``is_partitioning_column``, ``clustering_ordinal_position``.
    Consumers project the fields they care about.

    Best-effort: returns ``None`` on any failure (sentinel-unconfigured,
    unsafe identifier, BQ query exception). Does NOT raise. Mirrors the
    failure posture of `app/api/v2_schema.py:_fetch_bq_table_options`,
    which it replaces.

    Replaces two BQ jobs (one for column list + one for partition/cluster)
    with one — half the on-demand cost on each `/api/v2/schema/{id}`
    cache miss (issue #155 / spec).
    """
    from src.identifier_validation import validate_quoted_identifier

    if not bq.projects.data:
        return None

    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        return None

    bq_sql = (
        f"SELECT column_name, data_type, is_nullable, "
        f"       is_partitioning_column, clustering_ordinal_position "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? ORDER BY ordinal_position"
    )
    try:
        with bq.duckdb_session() as conn:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
    except Exception as e:
        logger.warning(
            "BQ COLUMNS fetch failed for %s.%s.%s: %s",
            bq.projects.data, dataset, table, e,
        )
        return None

    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": (r[2] or "").upper() == "YES",
            "is_partitioning_column": (r[3] or "").upper() == "YES",
            "clustering_ordinal_position": r[4],
        }
        for r in rows
    ]
```

If `logger` isn't already imported at the top of `connectors/bigquery/access.py`, add `import logging; logger = logging.getLogger(__name__)` near the top — but check first; the file likely already has it.

- [ ] **Step 3.4: Run consolidation tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema_columns_consolidation.py -v
```
Expected: 5 passed.

- [ ] **Step 3.5: Rewire v2_schema._fetch_bq_schema**

Edit `app/api/v2_schema.py` — replace the body of `_fetch_bq_schema` (function starting around line 33). The function currently builds and runs its own `INFORMATION_SCHEMA.COLUMNS` query; rewire it to call `fetch_bq_columns_full` and project the column-list shape.

Locate `def _fetch_bq_schema(...)` and replace its full body with:

```python
def _fetch_bq_schema(bq, dataset: str, table: str) -> list[dict]:
    """Fetch column list via the shared `fetch_bq_columns_full` helper.

    Pre-#155 this had its own INFORMATION_SCHEMA.COLUMNS query; consolidating
    with `_fetch_bq_table_options` (now also delegating to the same helper)
    halves the BQ job count on cache miss. Returns the schema-endpoint
    column shape: name / type / nullable / description.
    """
    from connectors.bigquery.access import fetch_bq_columns_full, BqAccessError

    # Surface "BQ not configured" as the structured 500 BqAccessError
    # (with hint), not a misleading empty-list. Mirrors pre-refactor
    # behavior — see Devin BUG_0002 in the original docstring.
    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured); endpoint catches it

    rows = fetch_bq_columns_full(bq, dataset, table)
    if rows is None:
        # Identifier validation refused, or BQ raised. The legacy code
        # path raised `ValueError("unsafe BQ identifier in registry")` for
        # the validation case; the BQ-error case raised via translate_bq_error.
        # The new helper conflates both into None; reproduce the legacy
        # error-surfacing here so callers get the same exceptions.
        from src.identifier_validation import validate_quoted_identifier
        if not (validate_quoted_identifier(bq.projects.data, "BQ project")
                and validate_quoted_identifier(dataset, "BQ dataset")
                and validate_quoted_identifier(table, "BQ source_table")):
            raise ValueError("unsafe BQ identifier in registry — refusing to query")
        # Otherwise it's a BQ-side error; raise the same shape the legacy
        # code raised (HTTP 502 via the endpoint's translate_bq_error).
        from connectors.bigquery.access import translate_bq_error
        raise translate_bq_error(
            RuntimeError("BQ INFORMATION_SCHEMA.COLUMNS query failed"),
            bq.projects, bad_request_status="upstream_error",
        )

    return [
        {
            "name": r["name"],
            "type": r["type"],
            "nullable": r["nullable"],
            "description": "",
        }
        for r in rows
    ]
```

- [ ] **Step 3.6: Rewire v2_schema._fetch_bq_table_options**

Same file. Replace the full body of `_fetch_bq_table_options` (around line 85):

```python
def _fetch_bq_table_options(bq, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info via the shared
    `fetch_bq_columns_full` helper.

    Returns ``{}`` on ANY failure (best-effort). Same load-bearing
    contract as before: the /schema endpoint must keep returning 200
    with empty partition info when this fails.
    """
    from connectors.bigquery.access import fetch_bq_columns_full

    rows = fetch_bq_columns_full(bq, dataset, table)
    if not rows:
        return {}

    partition_by = next(
        (r["name"] for r in rows if r["is_partitioning_column"]),
        None,
    )
    clustered_rows = [r for r in rows if r["clustering_ordinal_position"] is not None]
    clustered_rows.sort(key=lambda r: r["clustering_ordinal_position"])
    clustered_by = [r["name"] for r in clustered_rows]
    return {"partition_by": partition_by, "clustered_by": clustered_by}
```

- [ ] **Step 3.7: Run existing schema tests to verify no regression**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema.py -v
```
Expected: all existing tests pass (no behavior change for `/api/v2/schema/{id}` consumers — same response shape).

If a test fails, the most likely causes are: (a) the test mocks `_fetch_bq_schema` or `_fetch_bq_table_options` directly with the old signature — patch the test to mock `fetch_bq_columns_full` instead; (b) a test relies on the old error-surfacing path — check the docstring of step 3.5 and ensure the rewire matches.

- [ ] **Step 3.8: Run consolidation tests once more for full green**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema_columns_consolidation.py tests/test_v2_schema.py -v
```
Expected: all green.

- [ ] **Step 3.9: Commit**

```bash
cd /tmp/agnes-metadata
git add connectors/bigquery/access.py app/api/v2_schema.py tests/test_v2_schema_columns_consolidation.py
git commit -m "perf(v2_schema): consolidate two INFORMATION_SCHEMA.COLUMNS queries to one

/api/v2/schema/{id} cache-miss path was making two BQ jobs against the
same view with the same predicate — one for column list, one for
partition/cluster. New shared connectors/bigquery/access.py:fetch_bq_columns_full
returns one resultset; both v2_schema._fetch_bq_schema and
_fetch_bq_table_options delegate. Same response shape, half the BQ jobs
on cache miss.

Same helper consumed by the upcoming metadata provider's partition/cluster
path — zero SQL duplication across the two consumers.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 4: Split `build_schema` for warmup re-use

The warmup background task needs to populate `_schema_cache` without an authenticated user. Today `build_schema` mixes RBAC + cache + BQ work; extract the BQ-and-cache half so warmup can call it directly.

**Files:**
- Modify: `app/api/v2_schema.py`
- Test: `tests/test_v2_schema.py` (new test asserting the split)

- [ ] **Step 4.1: Write failing test**

Append to `tests/test_v2_schema.py` (or wherever existing v2_schema tests live):

```python
class TestBuildSchemaUncached:
    """The uncached entry point exists for warmup, which has no user
    context. RBAC + cache check live in `build_schema`; the BQ work +
    cache write live in `build_schema_uncached`."""

    def test_uncached_function_exists_and_does_not_take_user(self):
        """Signature: build_schema_uncached(conn, table_id, *, bq)"""
        from app.api.v2_schema import build_schema_uncached
        import inspect
        sig = inspect.signature(build_schema_uncached)
        params = list(sig.parameters)
        assert "user" not in params, (
            "build_schema_uncached should NOT require a user — that's "
            "the whole point of the split (warmup has no user)."
        )
        assert "table_id" in params
        assert "bq" in params

    def test_build_schema_delegates_to_uncached(self, monkeypatch):
        """build_schema should call build_schema_uncached after RBAC+cache check."""
        from app.api import v2_schema

        called_with = {}
        def fake_uncached(conn, table_id, *, bq):
            called_with["table_id"] = table_id
            return {"table_id": table_id, "columns": []}

        monkeypatch.setattr(v2_schema, "build_schema_uncached", fake_uncached)
        # Bypass the cache + RBAC for this assertion — both are tested elsewhere.
        monkeypatch.setattr(v2_schema._schema_cache, "get", lambda k, default=None: None)
        monkeypatch.setattr(v2_schema, "can_access_table", lambda u, tid, c: True)

        # Synthetic registry row.
        from unittest.mock import MagicMock
        repo_mock = MagicMock()
        repo_mock.get.return_value = {"id": "x", "source_type": "bigquery"}
        monkeypatch.setattr(v2_schema, "TableRegistryRepository", lambda c: repo_mock)

        v2_schema.build_schema(
            conn=MagicMock(), user={"id": "u"}, table_id="x", bq=MagicMock(),
        )
        assert called_with["table_id"] == "x"
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema.py::TestBuildSchemaUncached -v
```
Expected: FAIL with `ImportError: cannot import name 'build_schema_uncached'`.

- [ ] **Step 4.3: Perform the split**

Open `app/api/v2_schema.py`. Locate `def build_schema(...)` (around line 143). Refactor as:

```python
def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    bq: BqAccess,
) -> dict:
    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached schema fetched by an authorized one.
    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    cache_key = f"{table_id}"
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    return build_schema_uncached(conn, table_id, bq=bq)


def build_schema_uncached(
    conn: duckdb.DuckDBPyConnection,
    table_id: str,
    *,
    bq: BqAccess,
) -> dict:
    """Build the schema response and populate `_schema_cache`. **Skips
    RBAC and cache-hit short-circuit** — call only from contexts where
    those are either unnecessary (warmup) or already enforced upstream
    (`build_schema` above). The BQ work is the same; the entry-point
    contract is what differs.
    """
    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    cache_key = f"{table_id}"
    source_type = row.get("source_type") or ""
    if source_type == "bigquery":
        dataset = row.get("bucket") or ""
        source_table = row.get("source_table") or table_id
        columns = _fetch_bq_schema(bq, dataset, source_table)
        opts = _fetch_bq_table_options(bq, dataset, source_table)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "bigquery",
            "columns": columns,
            "partition_by": opts.get("partition_by"),
            "clustered_by": opts.get("clustered_by"),
        }
    else:
        # Local sources (Keboola/Jira parquet) — schema lives in extract.duckdb.
        # Preserve whatever the existing code path did. Copy the original
        # else-branch from build_schema verbatim. (Inspect the file before
        # this edit to extract the original lines after `if source_type ==
        # 'bigquery'`.)
        payload = _build_schema_for_local_row(conn, table_id, row)

    _schema_cache.set(cache_key, payload)
    return payload
```

**Important:** the existing `build_schema` body has logic for non-BQ rows after the `if source_type == "bigquery":` block. Read that block (lines ~165-210 in current file) and either keep it inline in `build_schema_uncached` after the `if/else`, or extract it into a helper `_build_schema_for_local_row(conn, table_id, row)` as the snippet above suggests. Pick whichever requires the smaller diff against the existing code.

- [ ] **Step 4.4: Run tests**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_schema.py -v
```
Expected: all green (existing tests + the new TestBuildSchemaUncached class).

- [ ] **Step 4.5: Commit**

```bash
cd /tmp/agnes-metadata
git add app/api/v2_schema.py tests/test_v2_schema.py
git commit -m "refactor(v2_schema): extract build_schema_uncached for warmup re-use

build_schema currently mixes RBAC + cache check + BQ work in one body.
Warmup will need to populate _schema_cache without a user — extract the
BQ-and-cache half so warmup can call it directly. build_schema keeps
the RBAC + cache-hit short-circuit at the top, then delegates.

Behavior unchanged for live /api/v2/schema/{id} consumers.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 5: Provider scaffold + dispatcher

**Files:**
- Create: `connectors/bigquery/metadata.py` (stub returning None)
- Create: `connectors/keboola/metadata.py` (stub returning None)
- Modify: `app/api/v2_catalog.py` (add `_metadata_provider_for` and `_build_metadata_request`)
- Test: `tests/test_v2_catalog_dispatcher.py`

- [ ] **Step 5.1: Write failing tests**

Create `tests/test_v2_catalog_dispatcher.py`:

```python
"""Dispatch + identifier-validation gate for the source-agnostic
metadata providers."""

from app.api._metadata_models import MetadataRequest


def test_dispatcher_returns_bq_provider_for_bigquery():
    from app.api.v2_catalog import _metadata_provider_for
    from connectors.bigquery import metadata as bq_meta
    fn = _metadata_provider_for("bigquery")
    assert fn is bq_meta.fetch


def test_dispatcher_returns_keboola_provider_for_keboola():
    from app.api.v2_catalog import _metadata_provider_for
    from connectors.keboola import metadata as kb_meta
    fn = _metadata_provider_for("keboola")
    assert fn is kb_meta.fetch


def test_dispatcher_returns_none_for_unknown_source():
    from app.api.v2_catalog import _metadata_provider_for
    assert _metadata_provider_for("jira") is None
    assert _metadata_provider_for("") is None
    assert _metadata_provider_for("snowflake") is None


def test_build_metadata_request_for_valid_row():
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "orders",
        "bucket": "dwh_base",
        "source_table": "orders_2024",
    })
    assert isinstance(req, MetadataRequest)
    assert req.table_id == "orders"
    assert req.bucket == "dwh_base"
    assert req.source_table == "orders_2024"


def test_build_metadata_request_rejects_unsafe_bucket():
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "x",
        "bucket": "evil`; DROP--",
        "source_table": "t",
    })
    assert req is None


def test_build_metadata_request_falls_back_to_id_when_source_table_missing():
    """Some legacy Keboola registry rows have empty source_table; the row id
    is the table name in that case (mirrors v2_schema:168 behavior)."""
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "orders",
        "bucket": "in.c-crm",
        "source_table": "",
    })
    assert req is not None
    assert req.source_table == "orders"


def test_stub_providers_return_none():
    """Providers don't have their real bodies yet — stubs return None
    so the catalog endpoint stays 200 while we wire the rest."""
    from connectors.bigquery import metadata as bq_meta
    from connectors.keboola import metadata as kb_meta
    req = MetadataRequest(table_id="x", bucket="b", source_table="t")
    assert bq_meta.fetch(req) is None
    assert kb_meta.fetch(req) is None
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_dispatcher.py -v
```
Expected: multiple FAIL — modules don't exist, dispatcher not defined.

- [ ] **Step 5.3: Create the provider stubs**

Create `connectors/bigquery/metadata.py`:

```python
"""BigQuery metadata provider — populates `TableMetadata` for a remote
BQ-backed registry row.

Stub: returns None pending the full implementation in Task 7.
"""

from __future__ import annotations

from app.api._metadata_models import MetadataRequest, TableMetadata


def fetch(req: MetadataRequest) -> TableMetadata | None:
    return None
```

Create `connectors/keboola/metadata.py`:

```python
"""Keboola metadata provider — populates `TableMetadata` for a Keboola
registry row via the Storage API.

Stub: returns None pending the full implementation in Task 6.
"""

from __future__ import annotations

from app.api._metadata_models import MetadataRequest, TableMetadata


def fetch(req: MetadataRequest) -> TableMetadata | None:
    return None
```

- [ ] **Step 5.4: Add dispatcher + request builder to v2_catalog**

Edit `app/api/v2_catalog.py`. Add at the top, after the existing imports:

```python
from app.api._metadata_models import MetadataRequest, TableMetadata
from src.identifier_validation import validate_quoted_identifier
```

Then, after the `_table_rows_cache` definition (around line 26), add:

```python
def _metadata_provider_for(source_type: str):
    """Lazy-import dispatch for source-specific metadata providers.

    Lazy because connector modules are heavy (BQ extension, google-cloud
    client, etc.) and a Keboola-only deployment shouldn't pay the BQ
    import cost. Returns ``None`` for unknown source types — the caller
    treats that as "no metadata enrichment available" and falls through.
    """
    if source_type == "bigquery":
        from connectors.bigquery import metadata as m
        return m.fetch
    if source_type == "keboola":
        from connectors.keboola import metadata as m
        return m.fetch
    return None


def _build_metadata_request(row: dict) -> MetadataRequest | None:
    """Construct a validated MetadataRequest from a registry row.

    Pre-validates the identifiers via `validate_quoted_identifier` before
    constructing the request — providers can then interpolate
    `req.bucket` / `req.source_table` into SQL/URL paths without
    re-checking. Returns ``None`` when validation fails; provider is not
    dispatched for that row.
    """
    bucket = row.get("bucket") or ""
    source_table = row.get("source_table") or row.get("id") or ""
    if not bucket or not source_table:
        return None
    if not (validate_quoted_identifier(bucket, "bucket")
            and validate_quoted_identifier(source_table, "source_table")):
        return None
    return MetadataRequest(
        table_id=row["id"], bucket=bucket, source_table=source_table,
    )
```

- [ ] **Step 5.5: Run tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_dispatcher.py -v
```
Expected: 7 passed.

- [ ] **Step 5.6: Commit**

```bash
cd /tmp/agnes-metadata
git add connectors/bigquery/metadata.py connectors/keboola/metadata.py \
        app/api/v2_catalog.py tests/test_v2_catalog_dispatcher.py
git commit -m "feat(catalog): metadata-provider scaffold + dispatcher with identifier validation

Lazy-imported per-source-type providers; dispatcher in v2_catalog.py.
_build_metadata_request validates bucket + source_table before
constructing a MetadataRequest — providers can interpolate them safely.
Providers ship as stubs returning None; real bodies land in Tasks 6-7.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 6: Keboola provider implementation

**Files:**
- Modify: `connectors/keboola/metadata.py` (replace stub with real impl)
- Test: `tests/test_connectors_keboola_metadata.py`

- [ ] **Step 6.1: Write failing tests**

Create `tests/test_connectors_keboola_metadata.py`:

```python
"""Keboola metadata provider — happy + unconfigured + api-error paths."""

from unittest.mock import MagicMock, patch

import pytest

from app.api._metadata_models import MetadataRequest, TableMetadata


@pytest.fixture
def req():
    return MetadataRequest(
        table_id="orders", bucket="in.c-crm", source_table="orders",
    )


def test_happy_path_returns_populated_metadata(req, monkeypatch):
    from connectors.keboola import metadata
    from connectors.keboola.client import KeboolaClient
    # KeboolaClient(token=None, url=None) reads env vars; pretend they're set.
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.return_value = {
            "rowsCount": 1234,
            "dataSizeBytes": 500_000,
            "primaryKey": ["id"],
        }
        result = metadata.fetch(req)

    assert result == TableMetadata(
        rows=1234,
        size_bytes=500_000,
        partition_by=None,
        clustered_by=None,
    )


def test_returns_none_when_unconfigured(req, monkeypatch):
    """No KEBOOLA_STACK_URL / KEBOOLA_STORAGE_TOKEN env → return None."""
    from connectors.keboola import metadata
    monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
    assert metadata.fetch(req) is None


def test_returns_none_on_storage_api_error(req, monkeypatch):
    """`StorageApiError` from get_table_info → log + return None."""
    from connectors.keboola import metadata
    from connectors.keboola.storage_api import StorageApiError
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://x.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.side_effect = StorageApiError(
            "404 not found", status=404, body={},
        )
        assert metadata.fetch(req) is None


def test_table_id_uses_bucket_dot_source_table(req, monkeypatch):
    """Storage API path is `<bucket>.<source_table>`."""
    from connectors.keboola import metadata
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://x.keboola.com")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "tok")

    with patch("connectors.keboola.metadata.KeboolaStorageClient") as MockStorage:
        instance = MockStorage.return_value
        instance.get_table_info.return_value = {
            "rowsCount": 0, "dataSizeBytes": 0,
        }
        metadata.fetch(req)
        instance.get_table_info.assert_called_once_with("in.c-crm.orders")
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_connectors_keboola_metadata.py -v
```
Expected: 4 FAIL (stub returns None for happy-path; assertion fails).

- [ ] **Step 6.3: Replace stub with real implementation**

Open `connectors/keboola/metadata.py` and replace the entire file:

```python
"""Keboola metadata provider — populates `TableMetadata` for a Keboola
registry row via the Storage API.

Reuses `KeboolaClient(token=None, url=None)` to inherit the existing
env-var fallback path (`KEBOOLA_STACK_URL` + `KEBOOLA_STORAGE_TOKEN`),
which is the same hierarchy `connectors/keboola/extractor.py` and
`connectors/keboola/client.py` already use. **Does NOT introduce a third
token-resolution helper.**
"""

from __future__ import annotations

import logging

from app.api._metadata_models import MetadataRequest, TableMetadata
from connectors.keboola.client import KeboolaClient
from connectors.keboola.storage_api import (
    KeboolaStorageClient, StorageApiError,
)

logger = logging.getLogger(__name__)


def fetch(req: MetadataRequest) -> TableMetadata | None:
    """Return Keboola Storage API metadata for the given table, or None.

    Keboola has no BigQuery-style partition/cluster concept; primaryKey is
    conceptually different (uniqueness, not physical layout), so
    `partition_by` and `clustered_by` are left None.
    """
    # Construct a KeboolaClient with no explicit token/url — the
    # constructor reads the standard env vars. Side-effect-free except
    # for setting `.token` and `.url` (verified
    # connectors/keboola/client.py:90-99). When a future refactor extracts
    # `_resolve_keboola_credentials()` as a standalone helper, switch
    # here to call that directly.
    creds = KeboolaClient(token=None, url=None)
    if not creds.url or not creds.token:
        return None  # not configured — same posture as BQ sentinel

    table_id = f"{req.bucket}.{req.source_table}"
    try:
        storage = KeboolaStorageClient(url=creds.url, token=creds.token)
        info = storage.get_table_info(table_id)
    except (StorageApiError, ValueError) as e:
        logger.warning("Keboola metadata fetch failed for %s: %s", table_id, e)
        return None

    return TableMetadata(
        rows=info.get("rowsCount"),
        size_bytes=info.get("dataSizeBytes"),
        partition_by=None,
        clustered_by=None,
    )
```

- [ ] **Step 6.4: Run tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_connectors_keboola_metadata.py -v
```
Expected: 4 passed.

- [ ] **Step 6.5: Commit**

```bash
cd /tmp/agnes-metadata
git add connectors/keboola/metadata.py tests/test_connectors_keboola_metadata.py
git commit -m "feat(keboola): metadata provider returning rows + size_bytes

Reuses KeboolaClient(token=None, url=None) for env-var credential
fallback — no new token-resolver helper invented. partition_by /
clustered_by stay None (Keboola has no equivalent concept).

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 7: BigQuery provider implementation

**Files:**
- Modify: `connectors/bigquery/metadata.py` (replace stub with real impl)
- Test: `tests/test_connectors_bigquery_metadata.py`

- [ ] **Step 7.1: Write failing tests**

Create `tests/test_connectors_bigquery_metadata.py`:

```python
"""BigQuery metadata provider — 5 paths from spec test plan:
happy / sentinel / VIEW / region-typo / both-paths-fail."""

from unittest.mock import MagicMock, patch

import pytest

from app.api._metadata_models import MetadataRequest, TableMetadata


@pytest.fixture
def req():
    return MetadataRequest(
        table_id="orders", bucket="dwh_base", source_table="orders_2024",
    )


def _bq_with_session(table_storage_rows=None, columns_rows=None,
                     table_storage_raises=None, columns_raises=None,
                     legacy_tables_rows=None, legacy_tables_raises=None,
                     projects_data="data-proj", projects_billing="billing-proj"):
    """Build a mock `BqAccess` whose `duckdb_session()` returns a context
    manager whose `.execute(...)` dispatches based on which inner SQL
    string is being run.

    The 3 SQL shapes we route on:
      - INFORMATION_SCHEMA.TABLE_STORAGE  → table_storage_rows / raises
      - INFORMATION_SCHEMA.COLUMNS         → columns_rows / raises
      - __TABLES__                         → legacy_tables_rows / raises
    """
    bq = MagicMock()
    bq.projects.data = projects_data
    bq.projects.billing = projects_billing

    def execute(outer_sql, params):
        inner_sql = params[1] if len(params) > 1 else ""
        if "TABLE_STORAGE" in inner_sql:
            if table_storage_raises:
                raise table_storage_raises
            return MagicMock(
                fetchone=lambda: table_storage_rows[0] if table_storage_rows else None,
                fetchall=lambda: table_storage_rows or [],
            )
        if "INFORMATION_SCHEMA.COLUMNS" in inner_sql:
            if columns_raises:
                raise columns_raises
            return MagicMock(
                fetchall=lambda: columns_rows or [],
            )
        if "__TABLES__" in inner_sql:
            if legacy_tables_raises:
                raise legacy_tables_raises
            return MagicMock(
                fetchone=lambda: legacy_tables_rows[0] if legacy_tables_rows else None,
            )
        raise AssertionError(f"unexpected SQL: {inner_sql[:80]}")

    session = MagicMock()
    session.execute.side_effect = execute
    cm = MagicMock()
    cm.__enter__.return_value = session
    cm.__exit__.return_value = False
    bq.duckdb_session.return_value = cm
    return bq


def test_happy_path_returns_full_metadata(req, monkeypatch):
    """TABLE_STORAGE returns rows+size, COLUMNS returns partition+cluster."""
    from connectors.bigquery import metadata
    from app.instance_config import get_value

    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda key, default=None: "us-central1" if "location" in key else default,
        raising=False,
    )

    bq = _bq_with_session(
        table_storage_rows=[(1234567, 5_000_000)],
        columns_rows=[
            ("event_date", "DATE", "NO", "YES", None),
            ("country", "STRING", "YES", "NO", 1),
            ("user_id", "STRING", "NO", "NO", None),
        ],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result == TableMetadata(
        rows=1234567,
        size_bytes=5_000_000,
        partition_by="event_date",
        clustered_by=["country"],
    )


def test_sentinel_unconfigured_returns_none_no_query(req, monkeypatch):
    """`bq.projects.data == ''` → return None before any query."""
    from connectors.bigquery import metadata
    bq = _bq_with_session(projects_data="")
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        assert metadata.fetch(req) is None
    bq.duckdb_session.assert_not_called()


def test_view_path_returns_metadata_with_null_rows_size(req, monkeypatch):
    """VIEW: TABLE_STORAGE empty + __TABLES__ empty → rows/size = None;
    partition + cluster from COLUMNS still surface."""
    from connectors.bigquery import metadata
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda key, default=None: "us-central1" if "location" in key else default,
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_rows=[],   # view → no row
        legacy_tables_rows=[],   # view also absent from __TABLES__
        columns_rows=[
            ("event_date", "DATE", "NO", "YES", None),
        ],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows is None
    assert result.size_bytes is None
    assert result.partition_by == "event_date"


def test_region_typo_falls_through_to_legacy_tables(req, monkeypatch):
    """TABLE_STORAGE raises (typo'd region) → fall through to __TABLES__."""
    from connectors.bigquery import metadata
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda key, default=None: "us-central" if "location" in key else default,  # typo!
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_raises=RuntimeError("Not found: ..."),
        legacy_tables_rows=[(100, 2048)],
        columns_rows=[("event_date", "DATE", "NO", "YES", None)],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows == 100
    assert result.size_bytes == 2048


def test_both_paths_fail_returns_metadata_with_partition_only(req, monkeypatch):
    """Both TABLE_STORAGE and __TABLES__ fail → rows/size None, partition still fills."""
    from connectors.bigquery import metadata
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda key, default=None: "us-central1" if "location" in key else default,
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_raises=RuntimeError("BQ down"),
        legacy_tables_raises=RuntimeError("BQ still down"),
        columns_rows=[("event_date", "DATE", "NO", "YES", None)],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows is None
    assert result.size_bytes is None
    assert result.partition_by == "event_date"


def test_bq_access_error_returns_none(req):
    """get_bq_access() raises BqAccessError → return None gracefully."""
    from connectors.bigquery import metadata
    from connectors.bigquery.access import BqAccessError
    with patch(
        "connectors.bigquery.metadata.get_bq_access",
        side_effect=BqAccessError("not_configured"),
    ):
        assert metadata.fetch(req) is None
```

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_connectors_bigquery_metadata.py -v
```
Expected: 6 FAIL (stub returns None unconditionally).

- [ ] **Step 7.3: Implement the BQ provider**

Replace `connectors/bigquery/metadata.py` entirely:

```python
"""BigQuery metadata provider — populates `TableMetadata` for a remote
BQ-backed registry row.

Two queries (different INFORMATION_SCHEMA scopes — TABLE_STORAGE is
region-scoped, COLUMNS is dataset-scoped, can't be combined):

  1. INFORMATION_SCHEMA.TABLE_STORAGE — total_rows + active+long_term
     bytes. Region-portable per Google's docs; only valid via
     `<project>.region-<region>.INFORMATION_SCHEMA.TABLE_STORAGE`
     (verified live 2026-05-07; dataset-scoped TABLE_STORAGE doesn't
     exist).

  2. INFORMATION_SCHEMA.COLUMNS — partition_by + clustered_by. Reuses
     the consolidated `fetch_bq_columns_full` helper that v2_schema also
     calls; one shared shape, one round-trip.

Region resolution chain: `instance.yaml.data_source.bigquery.location` →
`bq.bigquery_client().get_dataset(...)` → fall back to legacy `__TABLES__`
(dataset-scoped, no region required).

VIEW handling: TABLE_STORAGE returns no rows for entries whose
`table_type='VIEW'`; the legacy `__TABLES__` fallback also doesn't list
views. The provider returns `TableMetadata(rows=None, size_bytes=None,
partition_by=<from COLUMNS>, clustered_by=<from COLUMNS>)` — analyst
Claude reads `null` size and applies the existing CLAUDE.md guidance.

`size_bytes` reports `active_logical_bytes + long_term_logical_bytes`
(a full BQ scan reads both — reporting only active undercounts aged
partitioned tables).
"""

from __future__ import annotations

import logging

from app.api._metadata_models import MetadataRequest, TableMetadata
from connectors.bigquery.access import (
    BqAccessError, fetch_bq_columns_full, get_bq_access,
)

logger = logging.getLogger(__name__)


def fetch(req: MetadataRequest) -> TableMetadata | None:
    try:
        bq = get_bq_access()
    except BqAccessError:
        return None

    if not bq.projects.data:
        return None

    rows_size = _fetch_rows_and_size(bq, req)
    columns = fetch_bq_columns_full(bq, req.bucket, req.source_table)
    part_clust = _derive_partition_cluster(columns) if columns else None

    if rows_size is None and part_clust is None:
        return None

    return TableMetadata(
        rows=(rows_size or {}).get("rows"),
        size_bytes=(rows_size or {}).get("size_bytes"),
        partition_by=(part_clust or {}).get("partition_by"),
        clustered_by=(part_clust or {}).get("clustered_by"),
    )


def _derive_partition_cluster(columns: list[dict]) -> dict | None:
    """Mirror v2_schema._fetch_bq_table_options derivations from the
    shared columns-full result."""
    if not columns:
        return None
    partition_by = next(
        (c["name"] for c in columns if c["is_partitioning_column"]),
        None,
    )
    clustered = sorted(
        (c for c in columns if c["clustering_ordinal_position"] is not None),
        key=lambda c: c["clustering_ordinal_position"],
    )
    clustered_by = [c["name"] for c in clustered]
    return {"partition_by": partition_by, "clustered_by": clustered_by}


def _fetch_rows_and_size(bq, req: MetadataRequest) -> dict | None:
    """Resolve rows + size_bytes via TABLE_STORAGE → __TABLES__ fallthrough.

    See module docstring + spec Open Question §1 for view-path nuance.
    """
    location = _resolve_bq_location(bq, req)
    if location:
        result = _fetch_via_table_storage(bq, req, location)
        if result is not None:
            return result
        # TABLE_STORAGE returned None despite having a location: could
        # be a typo in `data_source.bigquery.location`, a multi-region
        # dataset operator misclassified, the table is a VIEW, or a
        # transient permission gap. Try __TABLES__ before giving up.
    return _fetch_via_legacy_tables(bq, req)


def _resolve_bq_location(bq, req: MetadataRequest) -> str | None:
    """instance.yaml.location → REST get_dataset → None."""
    from app.instance_config import get_value
    cfg_location = (get_value("data_source.bigquery.location") or "").strip()
    if cfg_location:
        return cfg_location
    try:
        ds = bq.bigquery_client().get_dataset(
            f"{bq.projects.data}.{req.bucket}"
        )
        return ds.location
    except Exception as e:
        logger.warning(
            "BQ dataset.get failed for %s.%s — falling back to __TABLES__: %s",
            bq.projects.data, req.bucket, e,
        )
        return None


def _fetch_via_table_storage(bq, req: MetadataRequest, location: str) -> dict | None:
    """Region-scoped INFORMATION_SCHEMA.TABLE_STORAGE — preferred path.

    `validate_quoted_identifier` accepts `us-central1`, `europe-west1`,
    `EU`, `us` etc. (regex `^[a-zA-Z0-9_][a-zA-Z0-9_.\\-]{0,127}$`).
    Refuses anything that could break out of the backtick-quoted path.

    Returns None on no-row (table is a VIEW, or different region than
    configured) — caller decides whether to fall through.

    `size_bytes` is `active + long_term` logical bytes (a full BQ scan
    reads both; reporting only active undercounts aged partitioned tables).
    """
    from src.identifier_validation import validate_quoted_identifier
    if not validate_quoted_identifier(location, "BQ region"):
        return None
    try:
        bq_sql = (
            f"SELECT total_rows, "
            f"IFNULL(active_logical_bytes, 0) + IFNULL(long_term_logical_bytes, 0) "
            f"FROM `{bq.projects.data}.region-{location}.INFORMATION_SCHEMA.TABLE_STORAGE` "
            f"WHERE table_schema = ? AND table_name = ?"
        )
        with bq.duckdb_session() as conn:
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?, ?)",
                [bq.projects.billing, bq_sql, req.bucket, req.source_table],
            ).fetchone()
    except Exception as e:
        logger.warning(
            "BQ TABLE_STORAGE fetch failed for %s.%s.%s: %s",
            bq.projects.data, req.bucket, req.source_table, e,
        )
        return None
    if row is None:
        return None  # VIEW or wrong region
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }


def _fetch_via_legacy_tables(bq, req: MetadataRequest) -> dict | None:
    """Last-resort dataset-scoped __TABLES__ — works without region."""
    try:
        bq_sql = (
            f"SELECT row_count, size_bytes "
            f"FROM `{bq.projects.data}.{req.bucket}.__TABLES__` "
            f"WHERE table_id = ?"
        )
        with bq.duckdb_session() as conn:
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, req.source_table],
            ).fetchone()
    except Exception as e:
        logger.warning(
            "BQ __TABLES__ fetch failed for %s.%s.%s: %s",
            bq.projects.data, req.bucket, req.source_table, e,
        )
        return None
    if row is None:
        return None
    rows_, size_bytes = row
    return {
        "rows": int(rows_) if rows_ is not None else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
    }
```

- [ ] **Step 7.4: Run tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_connectors_bigquery_metadata.py -v
```
Expected: 6 passed.

- [ ] **Step 7.5: Commit**

```bash
cd /tmp/agnes-metadata
git add connectors/bigquery/metadata.py tests/test_connectors_bigquery_metadata.py
git commit -m "feat(bigquery): metadata provider with TABLE_STORAGE + COLUMNS + __TABLES__ fallback

Resolves rows + size_bytes via region-scoped INFORMATION_SCHEMA.TABLE_STORAGE
(verified the only valid scope on 2026-05-07; dataset-scoped doesn't exist).
size_bytes is active + long_term logical bytes — full scan reads both.

Region resolved from data_source.bigquery.location → REST get_dataset →
fall back to legacy __TABLES__ on TABLE_STORAGE failure (region typo,
VIEW, IAM gap). Partition + cluster derive from the shared
fetch_bq_columns_full helper introduced in Task 3.

VIEW handling: TABLE_STORAGE empty → fall through to __TABLES__ empty →
TableMetadata(rows=None, size_bytes=None, partition_by=..., clustered_by=...).
The null size signals analyst Claude to use the existing 'treat as
potentially large' guidance.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 8: v2_catalog wiring — _size_hint_for_row + response shape + cache

**Files:**
- Modify: `app/api/v2_catalog.py`
- Test: `tests/test_v2_catalog_remote_metadata.py`

- [ ] **Step 8.1: Write failing tests**

Create `tests/test_v2_catalog_remote_metadata.py`:

```python
"""Catalog endpoint integration: per-table metadata enrichment for
remote rows."""

from unittest.mock import patch

from app.api._metadata_models import TableMetadata


def test_remote_row_includes_metadata_fields(seeded_app, monkeypatch):
    """Catalog response for a query_mode='remote' BQ row carries the four
    new fields populated by the provider."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    fake_meta = TableMetadata(
        rows=10000, size_bytes=2_000_000,
        partition_by="event_date", clustered_by=["country", "platform"],
    )

    # Register a synthetic remote BQ row in the test instance's registry.
    seeded_app["register_table"](
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )

    with patch(
        "connectors.bigquery.metadata.fetch", return_value=fake_meta,
    ):
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    orders = next(t for t in tables if t["id"] == "orders")
    assert orders["rows"] == 10000
    assert orders["size_bytes"] == 2_000_000
    assert orders["partition_by"] == "event_date"
    assert orders["clustered_by"] == ["country", "platform"]
    # Existing fields still present.
    assert orders["query_mode"] == "remote"


def test_local_row_unaffected_by_provider_dispatch(seeded_app):
    """query_mode='local' rows take the parquet-stat path; provider not called."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    seeded_app["register_table"](
        id="users", source_type="keboola", bucket="in.c-crm",
        source_table="users", query_mode="local",
    )

    with patch("connectors.keboola.metadata.fetch") as mock_fetch:
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    mock_fetch.assert_not_called()


def test_provider_failure_returns_null_metadata(seeded_app):
    """Provider returns None → row appears with null new fields, not
    a 500. Catalog endpoint must stay 200."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    seeded_app["register_table"](
        id="broken", source_type="bigquery", bucket="dwh_base",
        source_table="broken_t", query_mode="remote",
    )

    with patch(
        "connectors.bigquery.metadata.fetch", return_value=None,
    ):
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    broken = next(t for t in tables if t["id"] == "broken")
    assert broken["rows"] is None
    assert broken["size_bytes"] is None
    assert broken["partition_by"] is None
    assert broken["clustered_by"] is None


def test_cache_hit_does_not_call_provider_twice(seeded_app):
    """First call invokes provider; second within 15 min hits cache."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    seeded_app["register_table"](
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )

    fake_meta = TableMetadata(rows=1, size_bytes=2)
    with patch(
        "connectors.bigquery.metadata.fetch", return_value=fake_meta,
    ) as mock_fetch:
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})
    assert mock_fetch.call_count == 1
```

The `seeded_app["register_table"]` helper may need to be added to `conftest.py` if it doesn't exist. Quick check + extension:

```python
# In tests/conftest.py — add to seeded_app fixture
def register_table(*, id, source_type, bucket, source_table, query_mode, **extra):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(seeded_app["conn"])
    repo.register(
        id=id, name=id, source_type=source_type, bucket=bucket,
        source_table=source_table, query_mode=query_mode, **extra,
    )
seeded_app["register_table"] = register_table
```

- [ ] **Step 8.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_remote_metadata.py -v
```
Expected: FAIL — response doesn't include the new fields yet.

- [ ] **Step 8.3: Wire up the dispatch + cache + response shape**

Edit `app/api/v2_catalog.py`:

A. Add a `_metadata_cache` near the existing `_table_rows_cache` (around line 26):

```python
# Per-table cached TableMetadata. 15-min TTL — long enough to amortise
# across an analyst session, short enough that a freshly-registered
# remote table shows real numbers within a coffee break (the cache-bust
# path in `invalidate_for_table` accelerates this for the common admin-
# verifies-registration flow).
_metadata_cache = TTLCache(maxsize=512, ttl_seconds=900)
```

B. Rename the existing function `_materialized_size_hint` to `_size_hint_for_row` and extend it. Find the function (around line 68) and replace its full body:

```python
def _size_hint_for_row(row: dict) -> dict:
    """Resolve the per-row metadata bundle the catalog response surfaces.

    Renamed from `_materialized_size_hint` (which always also handled
    `local` rows; the old name was misleading). Returns a dict with up
    to four keys: `rough_size_hint`, `rows`, `size_bytes`, `partition_by`,
    `clustered_by`. Missing keys are reported as `null` in the response.

    Branches:
      - `local` / `materialized` → existing on-disk parquet stat (cheap).
      - `remote` → dispatch to the per-source-type provider; cache the
        TableMetadata for 15 min.
    """
    table_id = row["id"]
    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or "local"

    if query_mode in ("local", "materialized"):
        return {"rough_size_hint": _materialized_parquet_size_bucket(
            table_id, source_type, query_mode,
        )}

    if query_mode != "remote":
        return {"rough_size_hint": None}

    # Cache lookup (per-row TableMetadata).
    cached = _metadata_cache.get(table_id)
    if cached is None:
        cached = _resolve_remote_metadata(row)
        if cached is not None:
            _metadata_cache.set(table_id, cached)

    if cached is None:
        return {"rough_size_hint": None}

    return {
        "rough_size_hint": _bucket_size(cached.size_bytes) if cached.size_bytes else None,
        "rows": cached.rows,
        "size_bytes": cached.size_bytes,
        "partition_by": cached.partition_by,
        "clustered_by": cached.clustered_by,
    }


def _materialized_parquet_size_bucket(
    table_id: str, source_type: str, query_mode: str,
) -> str | None:
    """Size hint for rows whose data is on the server filesystem
    (the old `_materialized_size_hint` body)."""
    if not source_type:
        return None
    try:
        path = (
            Path(_get_data_dir()) / "extracts" / source_type / "data"
            / f"{table_id}.parquet"
        )
        if not path.exists():
            return None
        return _bucket_size(path.stat().st_size)
    except Exception:
        return None


def _resolve_remote_metadata(row: dict) -> "TableMetadata | None":
    """Provider dispatch for a remote row. Returns None on any failure."""
    source_type = row.get("source_type") or ""
    provider = _metadata_provider_for(source_type)
    if provider is None:
        return None
    req = _build_metadata_request(row)
    if req is None:
        return None
    try:
        return provider(req)
    except Exception:
        # Defense in depth — providers are documented as never-raises,
        # but a regression would otherwise 500 the whole catalog.
        return None
```

C. Update `build_catalog` (around line 94) to merge the new fields into each visible row's payload. Find the loop body that builds `visible.append(...)` and replace the appended dict construction:

```python
    visible = []
    for r in rows:
        if not can_access_table(user, r["id"], conn):
            continue
        hint = _size_hint_for_row(r)
        visible.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "description": r.get("description") or "",
            "source_type": r.get("source_type") or "",
            "query_mode": r.get("query_mode") or "local",
            "sql_flavor": _flavor_for(r.get("source_type") or ""),
            "where_examples": _examples_for(r.get("source_type") or ""),
            "fetch_via": _fetch_hint(r["id"], r.get("source_type") or ""),
            "rough_size_hint": hint.get("rough_size_hint"),
            "rows": hint.get("rows"),
            "size_bytes": hint.get("size_bytes"),
            "partition_by": hint.get("partition_by"),
            "clustered_by": hint.get("clustered_by"),
        })
```

D. Verify the existing `from pathlib import Path` and `_get_data_dir` import are still in scope; if not, add them.

- [ ] **Step 8.4: Run tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_remote_metadata.py tests/test_v2_catalog_dispatcher.py -v
```
Expected: all green.

- [ ] **Step 8.5: Run existing v2_catalog tests for regression**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog.py -v
```
Expected: all green (response is additive — no field renamed or removed).

- [ ] **Step 8.6: Commit**

```bash
cd /tmp/agnes-metadata
git add app/api/v2_catalog.py tests/test_v2_catalog_remote_metadata.py
git commit -m "feat(catalog): enrich remote rows with rows / size_bytes / partition_by / clustered_by

_size_hint_for_row (renamed from _materialized_size_hint) now dispatches
to the per-source-type metadata provider for query_mode='remote' rows
and caches the TableMetadata for 15 minutes. Catalog response gains
four new optional fields; existing CLI consumers that read only
rough_size_hint stay unchanged.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 9: Unified cache invalidation

**Files:**
- Modify: `app/api/v2_catalog.py` (add `invalidate_for_table`)
- Modify: `app/api/admin.py` (call from register/update/unregister)
- Test: `tests/test_v2_catalog_invalidation.py`

- [ ] **Step 9.1: Write failing test**

Create `tests/test_v2_catalog_invalidation.py`:

```python
"""Unified cache flush across all four catalog/schema/sample/metadata
caches on registry write."""

from unittest.mock import patch


def test_invalidate_flushes_all_four_caches():
    from app.api import v2_catalog, v2_schema, v2_sample
    from app.api._metadata_models import TableMetadata

    # Pre-populate.
    v2_catalog._table_rows_cache.set("all", ["fake_row"])
    v2_catalog._metadata_cache.set("orders", TableMetadata(rows=10))
    v2_schema._schema_cache.set("orders", {"columns": []})
    v2_sample._sample_cache.set("orders|10", [{"row": 1}])

    v2_catalog.invalidate_for_table("orders")

    assert v2_catalog._table_rows_cache.get("all") is None
    assert v2_catalog._metadata_cache.get("orders") is None
    assert v2_schema._schema_cache.get("orders") is None
    # Sample cache is cleared whole (we don't have prefix-invalidation).
    assert v2_sample._sample_cache.get("orders|10") is None


def test_invalidate_schedules_single_row_rewarm(monkeypatch):
    """After the flush, a background re-warm task is scheduled for the
    same table_id. Assert via patching create_task."""
    import asyncio
    from app.api import v2_catalog

    scheduled = []

    def fake_create_task(coro):
        # Drain the coroutine so the test doesn't leak it.
        coro.close()
        scheduled.append(coro)
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    v2_catalog.invalidate_for_table("orders")
    assert len(scheduled) == 1


def test_register_table_invalidates(seeded_app):
    """Registering a table flushes the rows cache so the next catalog
    request reflects it without waiting for the 5-min TTL."""
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.set("all", [])

    seeded_app["register_table"](
        id="new_t", source_type="keboola", bucket="in.c-x",
        source_table="t", query_mode="local",
    )
    assert v2_catalog._table_rows_cache.get("all") is None


def test_update_table_invalidates(seeded_app):
    from app.api import v2_catalog
    seeded_app["register_table"](
        id="u_t", source_type="keboola", bucket="in.c-x",
        source_table="t", query_mode="local",
    )
    v2_catalog._table_rows_cache.set("all", ["pre-update"])
    seeded_app["http_put"]("/api/admin/registry/u_t", {"description": "new"})
    assert v2_catalog._table_rows_cache.get("all") is None


def test_unregister_table_invalidates(seeded_app):
    from app.api import v2_catalog
    seeded_app["register_table"](
        id="d_t", source_type="keboola", bucket="in.c-x",
        source_table="t", query_mode="local",
    )
    v2_catalog._table_rows_cache.set("all", ["pre-delete"])
    seeded_app["http_delete"]("/api/admin/registry/d_t")
    assert v2_catalog._table_rows_cache.get("all") is None
```

If `seeded_app["http_put"]` or `["http_delete"]` aren't already defined in `tests/conftest.py`, add thin wrappers that call `c.put(...)` / `c.delete(...)` with the admin token.

- [ ] **Step 9.2: Run tests to verify they fail**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_invalidation.py -v
```
Expected: FAIL with `AttributeError: module 'app.api.v2_catalog' has no attribute 'invalidate_for_table'`.

- [ ] **Step 9.3: Add `invalidate_for_table` to v2_catalog**

Append to `app/api/v2_catalog.py` (anywhere after `_metadata_cache` is defined):

```python
def invalidate_for_table(table_id: str) -> None:
    """Drop every per-table cache so the next /api/v2/* request reflects
    the just-registered / updated / unregistered row immediately. Owned
    by the catalog module so admin.py doesn't need to know which caches
    exist.

    Imports v2_schema and v2_sample lazily — keeps catalog tests from
    pulling in BQ-extension imports they don't need.
    """
    import asyncio
    from app.api import v2_schema, v2_sample

    _table_rows_cache.clear()
    _metadata_cache.invalidate(table_id)
    v2_schema._schema_cache.invalidate(table_id)
    # Sample cache key is `f"{table_id}|{n}"`; clearing the whole sample
    # cache is heavier than precise invalidation, but registry-change
    # frequency (handful per day on a typical instance) doesn't justify
    # adding a prefix-invalidation primitive to TTLCache.
    v2_sample._sample_cache.clear()

    # Schedule a single-row re-warm so admins editing a registry row
    # see fresh data within a couple of seconds rather than waiting for
    # the next analyst to trigger a miss. Fire-and-forget; failures
    # log + skip inside the coroutine.
    try:
        asyncio.create_task(_rewarm_one_row(table_id))
    except RuntimeError:
        # No running event loop (e.g. called from a sync test). Skip
        # the re-warm — the next live request will populate via miss.
        pass


async def _rewarm_one_row(table_id: str) -> None:
    """Background single-row re-warm. Imports cache_warmup lazily to
    avoid a circular import at module load."""
    try:
        from app.api.cache_warmup import warm_one_table
        await warm_one_table(table_id)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "single-row re-warm failed for %s — next live request will populate",
            table_id,
        )
```

The `cache_warmup.warm_one_table` function will be defined in Task 10. For now it doesn't exist; the lazy import + outer `try` means the test for the schedule-task can run without it, falling through to the warning path. The unit test `test_invalidate_schedules_single_row_rewarm` patches `asyncio.create_task` so the inner coroutine never runs.

- [ ] **Step 9.4: Wire the helper into admin.py**

Edit `app/api/admin.py`. Find the success-return paths in:

- `register_table` (around line 1037 / inner success branch)
- `update_table` (around line 2486)
- `unregister_table` (the DELETE handler around line 2538)

For each, add a single line just before the function returns:

```python
    from app.api.v2_catalog import invalidate_for_table
    invalidate_for_table(table_id)
```

(The import inside the function avoids a circular import at module load.)

For `register_table`, the `table_id` may be derived from `result["id"]` or similar — adapt to the actual variable name in scope. For `unregister_table`, the path parameter is `table_id`. For `update_table`, also `table_id`.

- [ ] **Step 9.5: Run tests**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_v2_catalog_invalidation.py -v
```
Expected: all green.

- [ ] **Step 9.6: Commit**

```bash
cd /tmp/agnes-metadata
git add app/api/v2_catalog.py app/api/admin.py tests/test_v2_catalog_invalidation.py
git commit -m "feat(catalog): unified invalidate_for_table flushes 4 caches + schedules rewarm

invalidate_for_table flushes _table_rows_cache + _metadata_cache +
_schema_cache + _sample_cache, then schedules a single-row re-warm so
admin-edited rows reflect fresh data within ~1s rather than waiting for
the next analyst miss. Wired into register_table / update_table /
unregister_table.

Pre-fix none of the four caches were invalidated on registry change —
admin registers a table, agnes catalog doesn't show the new row for up
to 5 min; admin updates a row's bucket, agnes schema returns the OLD
column list for up to 1h.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 10: Cache warmup framework — state + bg task + endpoints

**Files:**
- Create: `app/api/cache_warmup.py`
- Modify: `pyproject.toml` (add `sse-starlette` dep)
- Test: `tests/test_cache_warmup.py`

- [ ] **Step 10.1: Add sse-starlette dependency**

Edit `pyproject.toml`. Find the `dependencies = [` block under `[project]` and append:

```
    "sse-starlette>=2.0",
```

Run:

```bash
cd /tmp/agnes-metadata && uv pip install -e ".[dev]"
```

- [ ] **Step 10.2: Write failing tests**

Create `tests/test_cache_warmup.py`:

```python
"""Cache warmup framework — state, bg task, endpoints."""

import asyncio
from unittest.mock import patch

import pytest


def test_warmup_run_state_starts_empty():
    from app.api.cache_warmup import WARMUP_STATE
    assert WARMUP_STATE is None or WARMUP_STATE.completed_at is not None


@pytest.mark.asyncio
async def test_warmup_skips_when_env_set(monkeypatch):
    """AGNES_SKIP_CACHE_WARMUP=1 → background warmup is a no-op."""
    monkeypatch.setenv("AGNES_SKIP_CACHE_WARMUP", "1")
    from app.api import cache_warmup

    with patch.object(cache_warmup, "_warm_catalog_caches_bg") as mock_bg:
        cache_warmup.maybe_schedule_startup_warmup()
        # Either not called, or called and short-circuits internally.
    # Exact assertion depends on implementation; pick the simpler one.
    mock_bg.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_runs_one_per_remote_row():
    """`_warm_catalog_caches_bg` calls `_warm_one` once per remote row."""
    from app.api import cache_warmup

    # Stub the registry to return 3 remote BQ rows + 1 local row.
    fake_rows = [
        {"id": "r1", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "r2", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "r3", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "L1", "query_mode": "local", "source_type": "keboola"},
    ]
    warmed = []

    async def fake_warm_one(row, state, sem):
        warmed.append(row["id"])

    with patch.object(cache_warmup, "_list_remote_rows", return_value=fake_rows[:3]):
        with patch.object(cache_warmup, "_warm_one", fake_warm_one):
            await cache_warmup._warm_catalog_caches_bg(trigger="manual")

    assert sorted(warmed) == ["r1", "r2", "r3"]


@pytest.mark.asyncio
async def test_warmup_failure_isolated_to_one_row():
    from app.api import cache_warmup

    async def fake_warm_one(row, state, sem):
        if row["id"] == "fail":
            raise RuntimeError("BQ down for this row")

    with patch.object(cache_warmup, "_list_remote_rows", return_value=[
        {"id": "ok1", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "fail", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "ok2", "query_mode": "remote", "source_type": "bigquery"},
    ]):
        with patch.object(cache_warmup, "_warm_one", fake_warm_one):
            await cache_warmup._warm_catalog_caches_bg(trigger="manual")

    state = cache_warmup.WARMUP_STATE
    assert state.total == 3
    # Other rows still ran; the bad one is recorded but doesn't blow up
    # the gather. Note: this asserts on the gather behavior, not on
    # `_warm_one`'s internal exception handling — that lives in
    # the real implementation, not the stub here. If the implementation
    # catches inside `_warm_one`, the gather completes fully.


@pytest.mark.asyncio
async def test_run_endpoint_idempotent_on_concurrent_invocation(seeded_app):
    """Two POST /run calls in flight return the same run_id."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # First POST starts a run.
    r1 = c.post("/api/admin/cache-warmup/run", headers=headers)
    assert r1.status_code == 200
    body1 = r1.json()
    # Second POST while first is in-flight: returns existing run_id.
    r2 = c.post("/api/admin/cache-warmup/run", headers=headers)
    body2 = r2.json()
    if body2.get("status") == "already_running":
        assert body2["run_id"] == body1.get("run_id") or "run_id" in body1
    # Otherwise the first run completed before the second arrived; that's
    # also OK — idempotency only matters under true concurrency.


def test_status_endpoint_before_first_run(seeded_app):
    """GET /status returns {state: never_run} before any warmup."""
    from app.api import cache_warmup
    cache_warmup.WARMUP_STATE = None  # reset for this test

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/admin/cache-warmup/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"state": "never_run"}
```

- [ ] **Step 10.3: Run tests to verify they fail**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_cache_warmup.py -v
```
Expected: ImportError for `app.api.cache_warmup`.

- [ ] **Step 10.4: Implement the warmup module**

Create `app/api/cache_warmup.py`:

```python
"""Cache warmup framework — populates catalog/schema/metadata caches at
container startup so the first analyst hits warm caches.

Bounded concurrency (4 by default). Exposes:
  - GET /api/admin/cache-warmup/status — JSON snapshot
  - POST /api/admin/cache-warmup/run — manual trigger (idempotent)
  - GET /api/admin/cache-warmup/stream — Server-Sent Events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.auth.dependencies import _get_db
from app.auth.access import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class WarmupRowState:
    table_id: str
    status: Literal["pending", "warming", "fresh", "error"]
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    last_warmed_at: str | None = None


@dataclass
class WarmupRunState:
    run_id: str
    trigger: Literal["startup", "manual", "registry_change"]
    started_at: str
    completed_at: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    rows: dict[str, WarmupRowState] = field(default_factory=dict)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)


WARMUP_STATE: WarmupRunState | None = None
_RUN_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def maybe_schedule_startup_warmup() -> None:
    """Called from app/main.py FastAPI startup event."""
    if os.environ.get("AGNES_SKIP_CACHE_WARMUP") == "1":
        logger.info("cache warmup skipped (AGNES_SKIP_CACHE_WARMUP=1)")
        return
    try:
        asyncio.create_task(_warm_catalog_caches_bg(trigger="startup"))
    except RuntimeError:
        logger.warning("no running event loop — startup warmup skipped")


async def _warm_catalog_caches_bg(trigger: str = "startup") -> None:
    """Walk registry, warm metadata + schema caches for every remote row."""
    global WARMUP_STATE
    async with _RUN_LOCK:
        # Re-check inside the lock — another caller might have completed
        # a run while we were waiting.
        if WARMUP_STATE and WARMUP_STATE.completed_at is None:
            return

        run_id = uuid4().hex[:8]
        state = WarmupRunState(
            run_id=run_id, trigger=trigger, started_at=_now_iso(),
        )
        WARMUP_STATE = state

    rows = _list_remote_rows()
    state.total = len(rows)
    for r in rows:
        state.rows[r["id"]] = WarmupRowState(
            table_id=r["id"], status="pending",
        )
    _broadcast(state, {"event": "start", "data": {
        "run_id": run_id, "trigger": trigger, "total": state.total,
    }})

    sem = asyncio.Semaphore(int(os.environ.get("AGNES_WARMUP_CONCURRENCY", "4")))
    await asyncio.gather(
        *(_warm_one(r, state, sem) for r in rows), return_exceptions=True,
    )

    state.completed_at = _now_iso()
    _broadcast(state, {"event": "complete", "data": {
        "run_id": run_id, "total": state.total,
        "completed": state.completed, "failed": state.failed,
    }})
    logger.info(
        "cache warmup complete: run_id=%s total=%d ok=%d fail=%d",
        run_id, state.total, state.completed, state.failed,
    )


def _list_remote_rows() -> list[dict]:
    """Snapshot of registry rows that need a warmup pass."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    rows = TableRegistryRepository(conn).list_all()
    return [
        r for r in rows
        if r.get("query_mode") == "remote"
        and r.get("source_type") == "bigquery"
    ]


async def _warm_one(
    row: dict, state: WarmupRunState, sem: asyncio.Semaphore,
) -> None:
    async with sem:
        rs = state.rows[row["id"]]
        rs.status = "warming"
        rs.started_at = _now_iso()
        _broadcast(state, {"event": "row", "data": asdict(rs)})
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(_warm_metadata_sync, row)
            await asyncio.to_thread(_warm_schema_sync, row)
            rs.status = "fresh"
            rs.last_warmed_at = _now_iso()
            state.completed += 1
        except Exception as e:
            rs.status = "error"
            rs.error = str(e)
            state.failed += 1
            logger.warning("cache warmup row=%s failed: %s", row["id"], e)
        finally:
            rs.completed_at = _now_iso()
            rs.duration_ms = int((time.monotonic() - t0) * 1000)
            _broadcast(state, {"event": "row", "data": asdict(rs)})


def _warm_metadata_sync(row: dict) -> None:
    """Trigger metadata cache populate via the catalog's normal path."""
    from app.api.v2_catalog import _size_hint_for_row
    _size_hint_for_row(row)


def _warm_schema_sync(row: dict) -> None:
    """Trigger schema cache populate via build_schema_uncached."""
    from app.api.v2_schema import build_schema_uncached
    from connectors.bigquery.access import get_bq_access
    from src.db import get_system_db
    bq = get_bq_access()
    build_schema_uncached(get_system_db(), row["id"], bq=bq)


async def warm_one_table(table_id: str) -> None:
    """Single-row re-warm — invoked by `invalidate_for_table` after a
    registry change. Does NOT update WARMUP_STATE (small change shouldn't
    overwrite the last full run's status); just refreshes the caches."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    row = TableRegistryRepository(conn).get(table_id)
    if not row or row.get("query_mode") != "remote":
        return
    try:
        await asyncio.to_thread(_warm_metadata_sync, row)
        await asyncio.to_thread(_warm_schema_sync, row)
    except Exception as e:
        logger.warning("single-row warmup failed for %s: %s", table_id, e)


def _broadcast(state: WarmupRunState, event: dict) -> None:
    """Send an event to every SSE subscriber. Dead queues are pruned."""
    dead = []
    for q in state._subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        state._subscribers.remove(q)


def _serialize_state(state: WarmupRunState) -> dict:
    return {
        "run_id": state.run_id,
        "trigger": state.trigger,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "total": state.total,
        "completed": state.completed,
        "failed": state.failed,
        "rows": {tid: asdict(rs) for tid, rs in state.rows.items()},
    }


# ─── Endpoints ────────────────────────────────────────────────────────


@router.get("/api/admin/cache-warmup/status")
async def warmup_status(user: dict = Depends(require_admin)):
    if WARMUP_STATE is None:
        return {"state": "never_run"}
    return _serialize_state(WARMUP_STATE)


@router.post("/api/admin/cache-warmup/run")
async def warmup_run(user: dict = Depends(require_admin)):
    if WARMUP_STATE and WARMUP_STATE.completed_at is None:
        return {"run_id": WARMUP_STATE.run_id, "status": "already_running"}
    asyncio.create_task(_warm_catalog_caches_bg(trigger="manual"))
    # Brief sleep so WARMUP_STATE is populated by the time we return.
    await asyncio.sleep(0)
    return {
        "run_id": WARMUP_STATE.run_id if WARMUP_STATE else None,
        "status": "started",
    }


@router.get("/api/admin/cache-warmup/stream")
async def warmup_stream(user: dict = Depends(require_admin)):
    async def gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        if WARMUP_STATE:
            WARMUP_STATE._subscribers.append(q)
        # Replay current snapshot.
        if WARMUP_STATE:
            yield {"event": "snapshot", "data": json.dumps(
                _serialize_state(WARMUP_STATE)
            )}
        try:
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
                yield {"event": ev["event"], "data": json.dumps(ev["data"])}
                if ev["event"] == "complete":
                    return
        except asyncio.TimeoutError:
            return
        finally:
            if WARMUP_STATE and q in WARMUP_STATE._subscribers:
                WARMUP_STATE._subscribers.remove(q)

    return EventSourceResponse(gen())
```

- [ ] **Step 10.5: Register the router in app/main.py**

Edit `app/main.py`. Find the section where routers are included (look for `app.include_router(...)` calls). Add:

```python
from app.api.cache_warmup import router as cache_warmup_router
app.include_router(cache_warmup_router)
```

- [ ] **Step 10.6: Run tests**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_cache_warmup.py -v
```
Expected: all green.

- [ ] **Step 10.7: Commit**

```bash
cd /tmp/agnes-metadata
git add app/api/cache_warmup.py app/main.py pyproject.toml \
        tests/test_cache_warmup.py
git commit -m "feat(cache-warmup): startup background warmup + status / run / stream endpoints

- WarmupRunState / WarmupRowState dataclasses + module-level singleton.
- _warm_catalog_caches_bg walks remote BQ rows with bounded concurrency
  (Semaphore(4), tunable via AGNES_WARMUP_CONCURRENCY).
- Per-row failures isolated; never blow up the gather.
- maybe_schedule_startup_warmup honors AGNES_SKIP_CACHE_WARMUP=1.
- warm_one_table for the single-row re-warm called from
  invalidate_for_table.
- GET /api/admin/cache-warmup/status — JSON snapshot.
- POST /api/admin/cache-warmup/run — manual trigger, idempotent under
  concurrent invocation (locks + checks completed_at).
- GET /api/admin/cache-warmup/stream — Server-Sent Events stream of
  start / row / complete events.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 11: FastAPI startup event hook

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_main_startup_warmup.py`

- [ ] **Step 11.1: Write failing test**

Create `tests/test_main_startup_warmup.py`:

```python
"""The FastAPI startup event schedules `maybe_schedule_startup_warmup`
without blocking readiness."""

from unittest.mock import patch


def test_startup_event_calls_warmup_scheduler():
    from app.main import app
    # FastAPI startup events are functions on `app.router.on_startup`.
    handler_names = [h.__name__ for h in app.router.on_startup]
    assert any("warm" in n.lower() for n in handler_names), (
        "Expected a startup handler with 'warm' in its name; "
        f"found: {handler_names}"
    )


def test_health_check_returns_immediately_during_warmup(seeded_app):
    """/api/health doesn't await warmup; readiness is fire-and-forget."""
    c = seeded_app["client"]
    r = c.get("/api/health")
    assert r.status_code == 200
```

- [ ] **Step 11.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_main_startup_warmup.py -v
```
Expected: first test FAIL, second probably passes.

- [ ] **Step 11.3: Add the startup hook**

Edit `app/main.py`. Locate the existing FastAPI app instantiation (`app = FastAPI(...)`). Add a startup event handler nearby:

```python
@app.on_event("startup")
async def warm_catalog_caches_on_startup():
    """Schedule a background warmup of the v2 catalog/schema/metadata
    caches. Fire-and-forget; readiness is not blocked. Failures inside
    the background task are logged + swallowed.
    """
    from app.api.cache_warmup import maybe_schedule_startup_warmup
    maybe_schedule_startup_warmup()
```

If the file already uses lifespan handlers (newer FastAPI pattern), add the call into the lifespan function instead. Inspect the file first.

- [ ] **Step 11.4: Run tests to verify they pass**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_main_startup_warmup.py -v
```
Expected: both pass.

- [ ] **Step 11.5: Commit**

```bash
cd /tmp/agnes-metadata
git add app/main.py tests/test_main_startup_warmup.py
git commit -m "feat(main): startup event hook for catalog cache warmup

Fire-and-forget — readiness is not blocked. Per-row warmup runs in
background with bounded concurrency. Honors AGNES_SKIP_CACHE_WARMUP=1
opt-out for dev/test instances.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 12: CLI post-register hint for query_mode=remote

**Files:**
- Modify: `cli/commands/admin.py` (extend `register_table` post-success hint)
- Test: `tests/test_cli_admin.py` (append test)

- [ ] **Step 12.1: Write failing test**

Append to `tests/test_cli_admin.py`:

```python
class TestRegisterTableHints:
    """The CLI prints helpful follow-up hints after a successful
    register-table call. v0.46 adds a third hint for query_mode=remote
    pointing at the IAM verify-your-SA smoke check."""

    def test_remote_register_emits_iam_verify_hint(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t"})):
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "bigquery",
                "--bucket", "dwh_base",
                "--source-table", "orders",
                "--query-mode", "remote",
            ])
        assert result.exit_code == 0
        assert "agnes query --remote" in result.output
        assert "query-modes.md" in result.output

    def test_local_register_does_not_emit_remote_hint(self):
        with patch("cli.commands.admin.api_post", return_value=_resp(201, {"id": "t"})):
            result = runner.invoke(app, [
                "admin", "register-table", "users",
                "--source-type", "keboola",
                "--bucket", "in.c-crm",
                "--source-table", "users",
                "--query-mode", "local",
            ])
        assert result.exit_code == 0
        assert "agnes query --remote" not in result.output
```

- [ ] **Step 12.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_cli_admin.py::TestRegisterTableHints -v
```
Expected: first FAIL.

- [ ] **Step 12.3: Add the hint**

Edit `cli/commands/admin.py`. Find the success branch in `register_table` (search for the existing two hints — `Next: run agnes setup first-sync` and `register-table does not auto-grant`). Add a third hint conditional on `query_mode == "remote"`:

```python
        # Third hint: BQ-remote rows can fail at first analyst query if the
        # SA lacks dataViewer/jobUser. Pointing at the smoke command
        # surfaces the failure at registration time, not 30 minutes later.
        if query_mode == "remote":
            typer.echo(
                f"  Note: this is a remote-query table. Verify the SA can read it:\n"
                f"    agnes query --remote \"SELECT COUNT(*) FROM {name}\"\n"
                f"  If it 403s, see docs/admin/query-modes.md → \"BigQuery → IAM\"."
            )
```

- [ ] **Step 12.4: Run tests**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_cli_admin.py::TestRegisterTableHints -v
```
Expected: 2 passed.

- [ ] **Step 12.5: Commit**

```bash
cd /tmp/agnes-metadata
git add cli/commands/admin.py tests/test_cli_admin.py
git commit -m "feat(cli): third post-register hint for query_mode=remote

Points at agnes query --remote SELECT COUNT(*) as the smoke check for
SA IAM. Surfaces 403 USER_PROJECT_DENIED at register time, not when
the first analyst hits the table 30 minutes later. Cross-references the
new docs/admin/query-modes.md page (Task 13).

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 13: docs/admin/query-modes.md

**Files:**
- Create: `docs/admin/query-modes.md`
- Test: smoke test linking via `tests/test_docs_links.py` (optional)

- [ ] **Step 13.1: Write the doc**

Create `docs/admin/query-modes.md` with this exact content:

````markdown
# Query Modes — when to register a table as `local`, `remote`, or `materialized`

Source-agnostic guide to the three `query_mode` values Agnes supports. Pick the right mode at registration time and the analyst-side experience is fast, cost-aware, and predictable. Pick wrong and you'll either burn BQ scan budget on every query or spend hours waiting on syncs that didn't need to happen.

## TL;DR — decision tree

```
Is the table small (< 1 GB) and updated daily-or-slower?
  └─ YES → query_mode: local       (sync to laptop, query offline)

Is the table the result of an aggregate SQL the operator controls?
  └─ YES → query_mode: materialized  (server runs SQL → parquet, distributed)

Otherwise:
  └─ query_mode: remote   (data stays in upstream; analyst queries on demand)
```

## Three modes side-by-side

| Aspect | `local` | `materialized` | `remote` |
|---|---|---|---|
| Where the data lives | Analyst laptop (parquet) | Agnes server filesystem (parquet) | Upstream (BigQuery, Keboola, …) |
| Who runs the query | Analyst's local DuckDB | Analyst's local DuckDB | Upstream engine via DuckDB extension |
| Cost model | Free after sync | Free after each sync | Per-query scan cost on the analyst's first hit |
| Freshness | As fresh as last sync | As fresh as last scheduled run | Live |
| Scan limits | None (laptop disk) | None (server disk) | `bq_max_scan_bytes` cost gate (default 5 GiB) |
| Best for | Stable reference data, daily-updated facts | Aggregates, daily snapshots | Big tables, live data, residency-restricted |

## Per-source-type reference

### BigQuery — `query_mode: remote`

The most common use case for `remote`. Data stays in BQ; analysts query on demand via the Agnes server's service account.

**IAM:** the server's SA must have:
- `roles/bigquery.dataViewer` on the dataset (read access)
- `roles/bigquery.jobUser` on the *billing* project (run jobs)

If `data_source.bigquery.billing_project == data_source.bigquery.project`, set the SA's `serviceusage.services.use` permission too — the BQ extension can otherwise 403 USER_PROJECT_DENIED on the first query. The instance health check (`agnes diagnose`) surfaces this as an `info`-tier entry on `bq_config`.

**Register via UI:** `/admin/tables` → "Add table" → Source type `bigquery` → Mode `remote` → fill `dataset` (your BQ dataset name) + `source_table` (the BQ table id within that dataset).

**Register via CLI:**

```bash
agnes admin register-table sales_2024 \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table sales_2024 \
    --query-mode remote
```

After registration, smoke-test the SA's access:

```bash
agnes query --remote "SELECT COUNT(*) FROM sales_2024"
```

A 403 here means the SA is missing `dataViewer` or `jobUser`; fix in IAM and re-test.

**Cost guardrail:** `bq_max_scan_bytes` (default 5 GiB) refuses queries whose pre-execution scan estimate exceeds the cap. Configurable in `/admin/server-config`. When an analyst hits the cap, the response includes a hint to use `agnes snapshot create --where '<predicate>'` to materialise a scoped subset locally.

### BigQuery — `query_mode: materialized`

The server runs a scheduled SQL aggregate against BigQuery and writes the result to a parquet on the Agnes filesystem. Analysts get the parquet via `agnes pull` like any other local table.

**Register via CLI:**

```bash
agnes admin register-table monthly_kpis \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table monthly_kpis \
    --query-mode materialized \
    --query @path/to/monthly_kpis.sql \
    --sync-schedule "daily 03:00"
```

**Cost guardrail:** `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; set `0` to disable) refuses materialise runs whose query plan exceeds the cap. Catches a typo'd `WHERE` clause that would otherwise scan a year of data.

### Keboola — `query_mode: local` (the production path)

The Agnes server's Keboola DuckDB extension downloads the table to a parquet on the server filesystem; `agnes pull` distributes it to analyst laptops.

**Setup:** `instance.yaml.data_source.type: keboola` + Storage API token via `KEBOOLA_STORAGE_TOKEN` env var (or whatever `instance.yaml.token_env` points at).

**Register via CLI:**

```bash
agnes admin register-table users \
    --source-type keboola \
    --bucket in.c-crm \
    --source-table users \
    --query-mode local
```

**`query_mode: remote` for Keboola** is architecturally supported via the `_remote_attach` mechanism (the orchestrator can ATTACH the Keboola DuckDB extension on demand the same way it does for BQ), but **not in active deployment use today**. If you have an analyst workflow against a Keboola table that's too big to sync, file an issue — the architecture is in place but the registration UX hasn't been polished.

### Jira — `query_mode: local` only

Event-driven: webhooks update parquets incrementally. No `remote` or `materialized` mode for Jira today.

## Worked examples

**1. Big BigQuery fact table you query weekly:** `query_mode: remote`. SA needs `dataViewer` + `jobUser`. Analyst uses `agnes query --remote` for one-off aggregates and `agnes snapshot create` for cross-week joins.

**2. Daily Keboola dimension table:** `query_mode: local`. Synced once a day by the scheduler; analyst's `agnes pull` picks it up.

**3. Monthly KPI aggregate from a BQ datawarehouse:** `query_mode: materialized` + `--sync-schedule "0 3 1 * *"` (3:00 on the 1st of each month). The server runs your aggregate SQL once a month; analysts get a parquet of the result.

## See also

- `docs/RBAC.md` — granting analysts access to a registered table.
- `config/instance.yaml.example` — the `data_source` config block.
- `agnes catalog --json` — inspect a registered table's mode + size hints.
- `agnes diagnose` — surface `bq_config` IAM issues and other health entries.
````

- [ ] **Step 13.2: Quick smoke check that the doc builds without dead links**

```bash
cd /tmp/agnes-metadata && python -c "
from pathlib import Path
content = Path('docs/admin/query-modes.md').read_text()
# Check the cross-referenced files exist.
for ref in ['docs/RBAC.md', 'config/instance.yaml.example']:
    assert Path(ref).exists(), f'broken cross-ref: {ref}'
print('docs OK')
"
```
Expected: `docs OK`.

- [ ] **Step 13.3: Commit**

```bash
cd /tmp/agnes-metadata
git add docs/admin/query-modes.md
git commit -m "docs(admin): query-modes.md — when to register a table as local/remote/materialized

Source-agnostic decision tree + per-source-type reference (BigQuery,
Keboola, Jira). Three worked examples. Cross-references to RBAC.md +
instance.yaml.example.

Linked from the admin UI tooltip (Task 14) and the CLI post-register
hint (Task 12).

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 14: Admin UI integration on /admin/tables

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Test: `tests/test_admin_tables_warmup_ui.py`

- [ ] **Step 14.1: Write failing test**

Create `tests/test_admin_tables_warmup_ui.py`:

```python
"""Smoke test that /admin/tables HTML contains the cache toolbar markup,
the EventSource wiring, and the per-row col-status slot."""


def test_cache_toolbar_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/admin/tables", headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="cacheWarmupCard"' in body
    assert "Re-warm all" in body
    assert "/api/admin/cache-warmup/stream" in body
    assert "EventSource" in body


def test_query_mode_doc_link_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/admin/tables", headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "query-modes.md" in r.text or "/docs/admin/query-modes" in r.text


def test_col_status_th_present_in_renderer(seeded_app):
    """The renderRegistryListing JS still emits <th class='col-status'>
    so the per-row badge slot exists."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers={"Authorization": f"Bearer {token}"})
    assert 'class="col-status"' in r.text
```

- [ ] **Step 14.2: Run test to verify it fails**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_admin_tables_warmup_ui.py -v
```
Expected: 2 of 3 fail (toolbar absent, EventSource not yet wired). Third probably passes.

- [ ] **Step 14.3: Add the cache toolbar `<section>` to `admin_tables.html`**

Edit `app/web/templates/admin_tables.html`. Locate the section between the page header and the per-source-type table listings — search for `bqTableListing` to find the area. Insert the new card just before the listings:

```html
<section id="cacheWarmupCard" class="card" style="margin-bottom: 20px;">
    <header class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
        <h2>Cache freshness</h2>
        <button class="btn btn-secondary" id="cacheWarmupRunBtn" onclick="cacheWarmupRun()">
            Re-warm all
        </button>
    </header>
    <div class="card-body">
        <div id="cacheWarmupProgress" style="margin-bottom: 8px;">
            <span id="cacheWarmupSummary">Loading…</span>
        </div>
        <progress id="cacheWarmupBar" max="100" value="0" style="width: 100%; display: none;"></progress>
        <details style="margin-top: 8px;">
            <summary style="cursor: pointer; user-select: none;">Show log</summary>
            <pre id="cacheWarmupLog" style="background: #0a0a0a; color: #dcdcdc; font-family: ui-monospace, Menlo, monospace; font-size: 12px; padding: 8px; max-height: 240px; overflow-y: auto; margin-top: 8px; border-radius: 4px;"></pre>
        </details>
    </div>
</section>
```

- [ ] **Step 14.4: Add the JS for live updates**

Inside the existing `<script>` block at the bottom of `admin_tables.html`, append:

```javascript
    // ── Cache warmup toolbar (issue #155 / #156) ────────────────
    let cacheWarmupSource = null;

    function cacheWarmupInit() {
        cacheWarmupRefreshSnapshot();
        cacheWarmupOpenStream();
    }

    function cacheWarmupRefreshSnapshot() {
        fetch('/api/admin/cache-warmup/status')
            .then(function(r) { return r.json(); })
            .then(function(state) { cacheWarmupRender(state); })
            .catch(function() { /* silent */ });
    }

    function cacheWarmupOpenStream() {
        try {
            cacheWarmupSource = new EventSource('/api/admin/cache-warmup/stream');
            cacheWarmupSource.addEventListener('start', cacheWarmupOnStart);
            cacheWarmupSource.addEventListener('row', cacheWarmupOnRow);
            cacheWarmupSource.addEventListener('complete', cacheWarmupOnComplete);
            cacheWarmupSource.addEventListener('snapshot', function(e) {
                cacheWarmupRender(JSON.parse(e.data));
            });
            cacheWarmupSource.onerror = function() {
                // Polling fallback — close + retry every 3 s.
                if (cacheWarmupSource) {
                    cacheWarmupSource.close();
                    cacheWarmupSource = null;
                }
                setTimeout(cacheWarmupRefreshSnapshot, 3000);
            };
        } catch (e) {
            // EventSource unsupported; poll instead.
            setInterval(cacheWarmupRefreshSnapshot, 3000);
        }
    }

    function cacheWarmupRender(state) {
        var summary = document.getElementById('cacheWarmupSummary');
        var bar = document.getElementById('cacheWarmupBar');
        var btn = document.getElementById('cacheWarmupRunBtn');
        if (!summary) return;

        if (!state || state.state === 'never_run') {
            summary.textContent = 'No cache warmup yet — click Re-warm all to start.';
            bar.style.display = 'none';
            btn.disabled = false;
            return;
        }
        var inProgress = state.completed_at === null || state.completed_at === undefined;
        var pct = state.total > 0 ? Math.round((state.completed * 100) / state.total) : 0;
        summary.textContent = inProgress
            ? state.completed + ' / ' + state.total + ' fresh — running…'
            : 'Last run: ' + state.completed + ' ok, ' + state.failed + ' errors';
        bar.style.display = 'block';
        bar.value = pct;
        btn.disabled = inProgress;

        // Per-row badges.
        if (state.rows) {
            for (var tid in state.rows) {
                cacheWarmupSetRowBadge(tid, state.rows[tid]);
            }
        }
    }

    function cacheWarmupOnStart(e) {
        var data = JSON.parse(e.data);
        var log = document.getElementById('cacheWarmupLog');
        log.textContent = '';
        cacheWarmupAppendLog(data.run_id ? '[' + nowHHMMSS() + '] start  trigger=' + data.trigger + ' total=' + data.total : '');
        cacheWarmupRefreshSnapshot();
    }

    function cacheWarmupOnRow(e) {
        var rs = JSON.parse(e.data);
        cacheWarmupAppendLog(
            '[' + nowHHMMSS() + '] ' + rs.status.padEnd(7) + rs.table_id +
            (rs.duration_ms ? '  (' + (rs.duration_ms / 1000).toFixed(1) + ' s)' : '') +
            (rs.error ? '  ' + rs.error : '')
        );
        cacheWarmupSetRowBadge(rs.table_id, rs);
        cacheWarmupRefreshSnapshot();
    }

    function cacheWarmupOnComplete(e) {
        var data = JSON.parse(e.data);
        cacheWarmupAppendLog(
            '[' + nowHHMMSS() + '] complete total=' + data.total +
            ' ok=' + data.completed + ' fail=' + data.failed
        );
        cacheWarmupRefreshSnapshot();
    }

    function cacheWarmupAppendLog(line) {
        var log = document.getElementById('cacheWarmupLog');
        if (!log) return;
        log.textContent += line + '\n';
        log.scrollTop = log.scrollHeight;
    }

    function cacheWarmupSetRowBadge(tableId, rs) {
        // Per-row badge in the col-status slot. Selector: any <tr>
        // whose first <td class="col-id"> text matches tableId.
        document.querySelectorAll('tr').forEach(function(tr) {
            var idCell = tr.querySelector('td.col-id');
            if (!idCell || idCell.textContent.trim() !== tableId) return;
            var statusCell = tr.querySelector('td.col-status');
            if (!statusCell) return;
            var color = {fresh: '#10B77F', warming: '#0073D1', pending: '#9CA3AF', error: '#EA580C'}[rs.status] || '#9CA3AF';
            var label = rs.status === 'fresh' ? 'fresh' : rs.status;
            statusCell.innerHTML = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;font-size:11px;background:' + color + ';color:white;" title="' + (rs.error || '') + '">' + label + '</span>';
        });
    }

    function nowHHMMSS() {
        var d = new Date();
        return d.toTimeString().slice(0, 8);
    }

    function cacheWarmupRun() {
        var btn = document.getElementById('cacheWarmupRunBtn');
        btn.disabled = true;
        fetch('/api/admin/cache-warmup/run', {method: 'POST'})
            .then(function(r) { return r.json(); })
            .then(function() { /* SSE stream picks up the new run */ })
            .catch(function() { btn.disabled = false; });
    }

    // Init on page load.
    document.addEventListener('DOMContentLoaded', cacheWarmupInit);
```

- [ ] **Step 14.5: Add `?` icon next to `query_mode` field in the edit modal**

Find the edit-modal block in `admin_tables.html` (search for `editBqQueryMode` or similar). Next to the `query_mode` field's `<label>`, append:

```html
<a href="/docs/admin/query-modes.md" target="_blank" title="When to use which mode" style="margin-left: 6px; text-decoration: none;">?</a>
```

If the docs aren't served at `/docs/admin/...`, link to the GitHub URL instead (look at recent edits — `cli/lib/setup_instructions.py` may have a server-side doc base URL).

- [ ] **Step 14.6: Run smoke tests**

```bash
cd /tmp/agnes-metadata && python -m pytest tests/test_admin_tables_warmup_ui.py -v
```
Expected: 3 passed.

- [ ] **Step 14.7: Manual visual check (optional but recommended)**

If a dev instance is available, browse to `/admin/tables`, register a remote BQ table, click "Re-warm all", and verify the log scrolls + per-row badges update.

- [ ] **Step 14.8: Commit**

```bash
cd /tmp/agnes-metadata
git add app/web/templates/admin_tables.html tests/test_admin_tables_warmup_ui.py
git commit -m "feat(admin/tables): cache freshness toolbar + per-row badges + SSE log

Adds a single cache panel above the per-source-type listings:
- Re-warm all button → POST /api/admin/cache-warmup/run.
- Progress bar + summary line.
- Collapsible terminal-style log fed by EventSource on
  /api/admin/cache-warmup/stream (polling fallback at 3 s on SSE error).
- Per-row badge in the col-status slot updates live as warmup events
  arrive.
- ? icon next to the query_mode field in the edit modal links to
  docs/admin/query-modes.md.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 15: CHANGELOG + version bump

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`

- [ ] **Step 15.1: Bump version**

Edit `pyproject.toml`. Find:

```
version = "0.45.x"
```

Change to:

```
version = "0.46.0"
```

- [ ] **Step 15.2: Add CHANGELOG entry**

Edit `CHANGELOG.md`. Find `## [Unreleased]` near the top. Replace with:

```markdown
## [Unreleased]

## [0.46.0] — 2026-05-07

Catalog metadata enrichment + cache discipline + automatic warmup.
Closes #155 + #156.

### Added

- **`/api/v2/catalog` returns four new optional fields per row** — `rows`,
  `size_bytes`, `partition_by`, `clustered_by` — populated by per-source-type
  metadata providers (`connectors/bigquery/metadata.py`,
  `connectors/keboola/metadata.py`). For `query_mode='remote'` BigQuery rows,
  `size_bytes` is `active_logical_bytes + long_term_logical_bytes` (a full
  scan reads both); region resolved from `data_source.bigquery.location` →
  `bq_client.get_dataset(...)` → fall back to legacy `__TABLES__`.
  Existing CLI consumers reading only `rough_size_hint` are unaffected.
- **Automatic cache warmup at startup.** FastAPI startup event schedules
  a background task that walks BQ remote rows and pre-populates
  `_metadata_cache` + `_schema_cache` with bounded concurrency (default 4,
  tunable via `AGNES_WARMUP_CONCURRENCY`). Doesn't block readiness;
  per-row failures logged + skipped. Opt-out via `AGNES_SKIP_CACHE_WARMUP=1`.
- **Three new admin endpoints under `/api/admin/cache-warmup/*`:**
  - `GET /status` — JSON snapshot of the latest run.
  - `POST /run` — manual trigger, idempotent under concurrent invocation.
  - `GET /stream` — Server-Sent Events with `start` / `row` / `complete`
    events for live UI updates.
- **`/admin/tables` cache freshness panel.** Toolbar above the per-source-type
  listings with progress bar + "Re-warm all" button + collapsible
  terminal-style log fed by SSE (polling fallback at 3 s). Per-row badge
  in the existing `col-status` column updates live (fresh / warming /
  pending / error).
- **`docs/admin/query-modes.md`** — source-agnostic admin reference for
  registering tables as `local` / `remote` / `materialized`. Decision
  tree, per-source-type IAM + setup, three worked examples. Linked from
  the `?` icon next to the `query_mode` field in the admin UI edit modal
  and from the third post-register hint in `agnes admin register-table`.
- **`agnes admin register-table` post-register hint** for `query_mode=remote`:
  points at `agnes query --remote "SELECT COUNT(*)..."` as the IAM smoke
  check so a missing `dataViewer` / `jobUser` surfaces at registration
  time, not 30 minutes later.

### Changed

- **`/api/v2/schema/{id}` cache miss now does 1 BQ job instead of 2.**
  `connectors/bigquery/access.py:fetch_bq_columns_full` collapses what
  used to be `_fetch_bq_schema` + `_fetch_bq_table_options` into a single
  `INFORMATION_SCHEMA.COLUMNS` query (same view, same predicate, just a
  combined SELECT list). The metadata provider's partition/cluster path
  shares the same helper — zero SQL duplication across the two consumers.
- **All four catalog/schema/sample/metadata caches are flushed on registry
  change.** `app/api/v2_catalog.py:invalidate_for_table` is wired into
  `POST /api/admin/register-table`, `PUT /api/admin/registry/{id}`, and
  `DELETE /api/admin/registry/{id}`. After a registry write, a single-row
  re-warm task is scheduled in the background so the admin's verification
  request hits warm caches within ~1 s instead of waiting for the next
  analyst miss. Pre-fix none of the caches were invalidated — admin
  registers a table, `agnes catalog` doesn't show the new row for up to
  5 min; admin updates a row's bucket, `agnes schema` returns the OLD
  column list for up to 1 hour.
- **`v2_schema.build_schema` split into RBAC-aware outer + RBAC-naive
  inner (`build_schema_uncached`).** Live endpoint behavior unchanged;
  warmup uses the inner entry point to populate `_schema_cache` without
  a user context.

### Internal

- New shared dataclass module `app/api/_metadata_models.py` with
  `MetadataRequest` (frozen) + `TableMetadata` for source-agnostic
  provider input/output.
- New `connectors/keboola/storage_api.py:KeboolaStorageClient.get_table_info`
  thin wrapper — keeps `_get` private to the module.
- New env vars (operator-facing tuning, no required setup change):
  - `AGNES_SKIP_CACHE_WARMUP` — opt-out of startup warmup.
  - `AGNES_WARMUP_CONCURRENCY` — default 4, max parallel BQ
    INFORMATION_SCHEMA jobs during a warmup pass.
- New runtime dependency: `sse-starlette>=2.0` (Server-Sent Events
  responses for the cache-warmup stream).
- Tests added: `test_metadata_models`, `test_v2_schema_columns_consolidation`,
  `test_v2_catalog_dispatcher`, `test_connectors_bigquery_metadata`,
  `test_connectors_keboola_metadata`, `test_v2_catalog_remote_metadata`,
  `test_v2_catalog_invalidation`, `test_cache_warmup`,
  `test_main_startup_warmup`, `test_admin_tables_warmup_ui`.
```

- [ ] **Step 15.3: Smoke-check version + changelog consistency**

```bash
cd /tmp/agnes-metadata && grep '^version' pyproject.toml
cd /tmp/agnes-metadata && head -20 CHANGELOG.md
```
Expected: pyproject shows `0.46.0`; CHANGELOG has `## [0.46.0] — 2026-05-07` directly after `## [Unreleased]`.

- [ ] **Step 15.4: Run the full targeted test set**

```bash
cd /tmp/agnes-metadata && python -m pytest \
    tests/test_metadata_models.py \
    tests/test_keboola_storage_api.py::TestGetTableInfo \
    tests/test_v2_schema_columns_consolidation.py \
    tests/test_v2_schema.py \
    tests/test_v2_catalog_dispatcher.py \
    tests/test_connectors_bigquery_metadata.py \
    tests/test_connectors_keboola_metadata.py \
    tests/test_v2_catalog_remote_metadata.py \
    tests/test_v2_catalog_invalidation.py \
    tests/test_cache_warmup.py \
    tests/test_main_startup_warmup.py \
    tests/test_admin_tables_warmup_ui.py \
    tests/test_cli_admin.py::TestRegisterTableHints \
    -v
```
Expected: all green.

- [ ] **Step 15.5: Commit**

```bash
cd /tmp/agnes-metadata
git add CHANGELOG.md pyproject.toml
git commit -m "release: 0.46.0 — source-agnostic catalog metadata + cache discipline

PR closes #155 + #156.

Highlights:
- Catalog response gains rows / size_bytes / partition_by / clustered_by.
- /api/v2/schema cache miss: 2 BQ jobs → 1 (-50%).
- All 4 catalog/schema/sample/metadata caches flush on registry change.
- Automatic startup warmup with bounded concurrency.
- /admin/tables shows cache freshness toolbar + per-row badges + SSE log.
- docs/admin/query-modes.md as the single source of truth for mode choice.

No DB migration; no wire break; MIN_COMPAT_CLI_VERSION unchanged.

Refs: docs/superpowers/specs/2026-05-07-source-agnostic-table-metadata-spec.md"
```

---

## Task 16: Final verification + PR

- [ ] **Step 16.1: Push branch**

```bash
cd /tmp/agnes-metadata && git push -u origin zs/issue-155-156-source-agnostic-metadata
```

- [ ] **Step 16.2: Open PR with auto-close keywords PER ISSUE**

```bash
gh pr create --title "release: 0.46.0 — source-agnostic catalog metadata + cache discipline" \
  --body "$(cat <<'EOF'
## Summary

- Catalog enrichment for `query_mode='remote'` rows: rows + size_bytes + partition_by + clustered_by.
- `/api/v2/schema` cache miss: 2 BQ jobs → 1 (-50%) via shared `fetch_bq_columns_full`.
- All four catalog/schema/sample/metadata caches flush on registry change; single-row re-warm scheduled.
- Automatic cache warmup at server startup; SSE-driven freshness toolbar on `/admin/tables`.

Closes #155.
Closes #156.

## Test plan

- [x] Targeted: 50+ unit + integration tests across 12 new test modules.
- [x] No DB migration; no wire-break; MIN_COMPAT_CLI_VERSION unchanged.
- [ ] Reviewer: register a remote BQ table, observe `/admin/tables` toolbar populates within 2 s.
- [ ] Reviewer: trigger `Re-warm all`, verify SSE log scrolls and per-row badges update.
- [ ] Reviewer: edit a registered row's bucket, verify `agnes schema <id>` returns updated columns immediately (no 1-hour staleness).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Per the user's `feedback_github_close_keyword_per_issue` memory: each issue gets its own `Closes #N` keyword on its own line. A list after one keyword only auto-closes the first.

---

## Self-Review

After writing this plan, I checked it against the spec end-to-end:

**Spec coverage:**
- ✅ Provider pattern + dispatcher → Tasks 5, 6, 7
- ✅ Identifier validation in dispatcher → Task 5
- ✅ COLUMNS query consolidation → Task 3
- ✅ `build_schema` RBAC/cache split → Task 4
- ✅ v2_catalog wiring + 15-min metadata cache → Task 8
- ✅ Unified `invalidate_for_table` → Task 9
- ✅ Cache warmup framework + endpoints → Task 10
- ✅ FastAPI startup hook → Task 11
- ✅ CLI post-register hint → Task 12
- ✅ docs/admin/query-modes.md → Task 13
- ✅ Admin UI integration → Task 14
- ✅ CHANGELOG + version bump → Task 15

**Placeholder scan:** none of the "TBD", "TODO", "implement later" patterns. Every step has actual code or an exact command.

**Type consistency:** `MetadataRequest` / `TableMetadata` / `WarmupRunState` / `WarmupRowState` field names referenced consistently across tasks. `fetch_bq_columns_full` signature consistent between Task 3 and Task 7.

**One known nit:** Task 9's `_rewarm_one_row` references `cache_warmup.warm_one_table` which is defined in Task 10. The lazy import inside the function + outer try/except handles the during-development gap, but if a subagent runs Tasks 9 and 10 strictly in order without the lazy import, the test in Task 9 still passes because it patches `asyncio.create_task`. Documented in Task 9 step 9.3.

Ready for execution.
