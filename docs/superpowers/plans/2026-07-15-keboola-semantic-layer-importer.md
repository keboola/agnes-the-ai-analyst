# Keboola Semantic Layer Importer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a Keboola project's semantic layer (Metastore API: datasets, metrics, constraints) into Agnes's `metric_definitions` table on a scheduled cadence, so Keboola-authored business metrics become available to Agnes's business-metric rails without hand-transcription.

**Architecture:** A new `connectors/keboola/metastore_client.py` (GET-only HTTP client) and `connectors/keboola/semantic_layer.py` (mapping + orchestration) feed a new scheduled endpoint (`app/api/keboola_semantic_layer_refresh.py`, mirroring `app/api/bq_metadata_refresh.py`) that upserts+prunes rows through the existing `metric_repo()` factory — no new DB migration, no new DuckDB/PG parity code.

**Tech Stack:** Python, `requests` (HTTP), FastAPI, DuckDB/Postgres via the existing repository factory, `pytest` + `unittest.mock`.

## Global Constraints

- Requires a **master (owner) Storage API token** for the Keboola project — verified live: a non-master token, even with full bucket permissions, gets `401 {"exception": "Failed to create project scope"}` from every Metastore endpoint. Fail fast with a clear error, never silently degrade.
- No DB migration in this plan — `metric_definitions` schema is unchanged; this only adds a new `source` value (`"keboola_semantic_layer"`).
- Out of scope (per the approved spec, `docs/superpowers/specs/2026-07-15-keboola-semantic-layer-importer-design.md`): `semantic-relationship`, `semantic-glossary`, multi-model support (v1 uses the first model, logs a warning if more than one exists), admin web UI (tracked separately at [keboola/agnes-the-ai-analyst#853](https://github.com/keboola/agnes-the-ai-analyst/issues/853)).
- CHANGELOG bullet required in the final task per repo convention.
- Run `.venv/bin/pytest tests/ --tb=short -n auto -q` before considering the plan done.

---

## File Structure

| File | Responsibility |
|---|---|
| `connectors/keboola/metastore_client.py` (new) | GET-only HTTP client for the Metastore API — host derivation, auth, list/error parsing. No business logic. |
| `connectors/keboola/storage_api.py` (modify) | Add `verify_token()` — one new method on the existing `KeboolaStorageClient`. |
| `connectors/keboola/semantic_layer.py` (new) | All mapping logic (table/dataset resolution, SQL composition, foreign-alias detection, constraint merge, metric-row builder) plus the top-level `sync_semantic_layer()` orchestrator. |
| `app/api/keboola_semantic_layer_refresh.py` (new) | FastAPI endpoint, mirrors `app/api/bq_metadata_refresh.py`'s single-flight-lock + `require_admin` pattern. |
| `app/main.py` (modify) | Register the new router. |
| `services/scheduler/__main__.py` (modify) | New env var + job tuple. |
| `CHANGELOG.md` (modify) | `[Unreleased]` bullet. |
| `tests/test_keboola_metastore_client.py` (new) | `MetastoreClient` unit tests (mocked HTTP). |
| `tests/test_keboola_semantic_layer_mapping.py` (new) | Pure-function mapping-logic unit tests. |
| `tests/test_keboola_semantic_layer_sync.py` (new) | `sync_semantic_layer()` orchestrator tests (mocked Metastore, real test DuckDB). |
| `tests/test_keboola_semantic_layer_refresh_endpoint.py` (new) | Endpoint tests, mirrors `tests/test_bq_metadata_refresh_endpoint.py`. |
| `tests/test_scheduler_sidecar.py` (modify) | Extend with the new job's env-override tests. |

---

### Task 1: `MetastoreClient` — GET-only Metastore API client

**Files:**
- Create: `connectors/keboola/metastore_client.py`
- Test: `tests/test_keboola_metastore_client.py`

**Interfaces:**
- Produces: `MetastoreApiError(RuntimeError)` (`.status`, `.body`); `derive_metastore_url(storage_api_url: str) -> str`; `MetastoreClient(url: str, token: str, session: Optional[requests.Session] = None)` with method `list_items(item_type: str, model_uuid: Optional[str] = None) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_metastore_client.py
"""MetastoreClient — GET-only Keboola semantic-layer (Metastore) API client.

Tests mock requests.Session directly (same pattern as
tests/test_keboola_storage_api.py) so we exercise real HTTP shapes without
touching the network.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from connectors.keboola.metastore_client import (
    MetastoreApiError,
    MetastoreClient,
    derive_metastore_url,
)


def _mock_response(status, body):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestDeriveMetastoreUrl:
    def test_replaces_connection_with_metastore(self):
        assert derive_metastore_url(
            "https://connection.us-east4.gcp.keboola.com"
        ) == "https://metastore.us-east4.gcp.keboola.com"

    def test_strips_trailing_slash(self):
        assert derive_metastore_url(
            "https://connection.keboola.com/"
        ) == "https://metastore.keboola.com"


class TestMetastoreClientInit:
    def test_rejects_missing_url_or_token(self):
        with pytest.raises(ValueError):
            MetastoreClient(url="", token="t")
        with pytest.raises(ValueError):
            MetastoreClient(url="https://connection.keboola.com", token="")

    def test_base_url_includes_api_v1(self):
        c = MetastoreClient(url="https://connection.keboola.com", token="t")
        assert c.base == "https://metastore.keboola.com/api/v1"


class TestListItems:
    def test_list_items_sends_token_header_and_returns_data(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {"data": [
                {"type": "semantic-model", "id": "m1", "attributes": {"name": "core"}},
            ]},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-model")

        assert items == [{"type": "semantic-model", "id": "m1", "attributes": {"name": "core"}}]
        url = sess.get.call_args.args[0]
        assert url == "https://metastore.keboola.com/api/v1/repository/semantic-model"
        headers = sess.get.call_args.kwargs["headers"]
        assert headers["X-StorageApi-Token"] == "tok"

    def test_list_items_filters_by_model_uuid(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {"data": [
                {"type": "semantic-metric", "id": "a", "attributes": {"modelUUID": "u1", "name": "a"}},
                {"type": "semantic-metric", "id": "b", "attributes": {"modelUUID": "u2", "name": "b"}},
            ]},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-metric", model_uuid="u1")

        assert [i["id"] for i in items] == ["a"]

    def test_list_items_no_model_uuid_returns_all(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            200,
            {"data": [
                {"type": "semantic-metric", "id": "a", "attributes": {"modelUUID": "u1"}},
                {"type": "semantic-metric", "id": "b", "attributes": {"modelUUID": "u2"}},
            ]},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        items = c.list_items("semantic-metric")

        assert len(items) == 2

    def test_401_raises_metastore_api_error_with_status(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            401,
            {"error": 401, "code": "401", "exception": "Failed to create project scope",
             "status": "error", "context": {"path": "/api/v1/repository/semantic-model"}},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="tok", session=sess)

        with pytest.raises(MetastoreApiError) as exc_info:
            c.list_items("semantic-model")

        assert exc_info.value.status == 401
        assert "Failed to create project scope" in str(exc_info.value)

    def test_token_redacted_in_error_message(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(
            403, {"detail": "rejected token=secrettoken123"},
        )
        c = MetastoreClient(url="https://connection.keboola.com", token="secrettoken123", session=sess)

        with pytest.raises(MetastoreApiError) as exc_info:
            c.list_items("semantic-model")

        assert "secrettoken123" not in str(exc_info.value)
        assert "<redacted-storage-token>" in str(exc_info.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_metastore_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'connectors.keboola.metastore_client'`

- [ ] **Step 3: Write the implementation**

```python
# connectors/keboola/metastore_client.py
"""Keboola Metastore API client — semantic layer (datasets, metrics,
relationships, constraints, glossary).

Separate service at ``metastore.<stack>``, derived from the project's
Storage API URL (``connection.<stack>``) by string substitution. Same
``X-StorageApi-Token`` auth as Storage API.

**Requires a master (owner) Storage API token.** Verified live
(2026-07-15): a non-master token — even one with full bucket read/manage
permissions — gets ``401 {"exception": "Failed to create project scope"}``
on every repository endpoint; the project's master token succeeds
immediately. See ``connectors/keboola/semantic_layer.py``'s
``_require_master_token`` for the preflight check that turns this opaque
error into an actionable one.

GET-only for v1 — this client only reads the semantic layer, it never
writes to it.
"""

from __future__ import annotations

from typing import Any, Optional

import requests


class MetastoreApiError(RuntimeError):
    """Wraps a non-2xx Metastore API response with the parsed body for context."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def derive_metastore_url(storage_api_url: str) -> str:
    """Derive the Metastore base URL from a Storage API stack URL.

    e.g. ``https://connection.us-east4.gcp.keboola.com`` ->
    ``https://metastore.us-east4.gcp.keboola.com``. Cloud/region-agnostic —
    only the ``connection.`` -> ``metastore.`` substitution matters.
    """
    return storage_api_url.rstrip("/").replace("connection.", "metastore.", 1)


class MetastoreClient:
    """GET-only client for the Keboola Metastore (semantic layer) API."""

    def __init__(self, *, url: str, token: str, session: Optional[requests.Session] = None):
        if not url or not token:
            raise ValueError("MetastoreClient requires url and token")
        self.base = derive_metastore_url(url) + "/api/v1"
        self.token = token
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        return {"X-StorageApi-Token": self.token, "Accept": "application/json"}

    def _get(self, path: str) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        return self._parse(resp, "GET", url)

    def _parse(self, resp: requests.Response, method: str, url: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            redacted = self._redact(body)
            raise MetastoreApiError(
                f"{method} {url} -> HTTP {resp.status_code}: {redacted}",
                status=resp.status_code,
                body=body,
            )
        if not isinstance(body, dict):
            raise MetastoreApiError(
                f"{method} {url} -> unexpected non-JSON response: {str(body)[:200]}",
                status=resp.status_code,
                body=body,
            )
        return body

    def _redact(self, body: Any) -> str:
        s = str(body)
        if self.token and self.token in s:
            s = s.replace(self.token, "<redacted-storage-token>")
        return s[:500]

    def list_items(self, item_type: str, model_uuid: Optional[str] = None) -> list[dict]:
        """List all items of ``item_type`` (e.g. ``'semantic-model'``,
        ``'semantic-dataset'``, ``'semantic-metric'``, ``'semantic-constraint'``),
        optionally filtered to one model.

        Returns the raw item shape: ``{"type", "id", "attributes", "meta"}``.
        Filtering is client-side on ``attributes.modelUUID`` — the server's
        ``?modelId=`` query param is documented upstream (kbagent CLI) as
        historically unreliable, so the defensive client-side filter wins.
        """
        body = self._get(f"/repository/{item_type}")
        items = body.get("data", []) if isinstance(body, dict) else []
        if model_uuid is None:
            return items
        return [i for i in items if (i.get("attributes") or {}).get("modelUUID") == model_uuid]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_metastore_client.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/metastore_client.py tests/test_keboola_metastore_client.py
git commit -m "Add MetastoreClient for Keboola semantic layer API"
```

---

### Task 2: Master-token preflight check

**Files:**
- Modify: `connectors/keboola/storage_api.py` (add `verify_token()` to `KeboolaStorageClient`)
- Create: (function added to) `connectors/keboola/semantic_layer.py` — this task creates the file with just this one function; later tasks append to it.
- Test: `tests/test_keboola_semantic_layer_mapping.py` (created here, extended in later tasks)

**Interfaces:**
- Consumes: `KeboolaStorageClient` (existing, from Task setup).
- Produces: `KeboolaStorageClient.verify_token() -> dict`; `MasterTokenRequiredError(RuntimeError)`; `require_master_token(storage_client) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_semantic_layer_mapping.py
"""Pure-function mapping/validation logic for the Keboola semantic-layer
importer (connectors/keboola/semantic_layer.py). No live API calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    require_master_token,
)


class TestRequireMasterToken:
    def test_passes_silently_for_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": True}

        require_master_token(storage_client)  # must not raise

    def test_raises_for_non_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": False}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)

    def test_raises_for_missing_field(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)
```

Also add a test for the new `KeboolaStorageClient` method to the existing storage-api test file:

```python
# Append to tests/test_keboola_storage_api.py, inside class TestStorageClient:
    def test_verify_token_calls_tokens_verify(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"id": "123", "isMasterToken": True})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        info = c.verify_token()

        assert info["isMasterToken"] is True
        url = sess.get.call_args.args[0]
        assert url == "https://kbc/v2/storage/tokens/verify"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py tests/test_keboola_storage_api.py::TestStorageClient::test_verify_token_calls_tokens_verify -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'connectors.keboola.semantic_layer'` and `AttributeError: 'KeboolaStorageClient' object has no attribute 'verify_token'`

- [ ] **Step 3: Implement**

Add to `connectors/keboola/storage_api.py`, directly below `get_table_info` (around line 377):

```python
    def verify_token(self) -> dict:
        """GET /v2/storage/tokens/verify — token metadata including
        `isMasterToken`, `bucketPermissions`, `owner`. Used by the
        semantic-layer importer's master-token preflight check (the
        Metastore API requires a master token; see
        connectors/keboola/semantic_layer.py:require_master_token)."""
        return self._get("/tokens/verify")
```

Create `connectors/keboola/semantic_layer.py`:

```python
"""Keboola semantic layer (Metastore) -> Agnes metric_definitions importer.

Design: docs/superpowers/specs/2026-07-15-keboola-semantic-layer-importer-design.md

Maps a Keboola project's semantic-layer metrics (bound to Storage tables via
`semantic-dataset`, annotated with `semantic-constraint` rules) into Agnes's
`metric_definitions` table. Runs on a schedule (see
app/api/keboola_semantic_layer_refresh.py); this module has no HTTP-layer
concerns of its own.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MasterTokenRequiredError(RuntimeError):
    """The configured Keboola token is not a master (owner) Storage API token.

    Verified live (2026-07-15): the Metastore API rejects non-master tokens
    with an opaque ``401 {"exception": "Failed to create project scope"}``
    regardless of the token's bucket permissions. This check turns that into
    an actionable error before any Metastore call is made.
    """


def require_master_token(storage_client) -> None:
    """Raise MasterTokenRequiredError unless the client's token is a master token.

    `storage_client` is a `connectors.keboola.storage_api.KeboolaStorageClient`
    (or any object exposing a compatible `verify_token() -> dict` method).
    """
    info = storage_client.verify_token()
    if not info.get("isMasterToken"):
        raise MasterTokenRequiredError(
            "Keboola semantic layer sync requires a master (owner) Storage "
            "API token; the configured token is not a master token. The "
            "Metastore API rejects non-master tokens with an opaque "
            "'Failed to create project scope' error regardless of bucket "
            "permissions — use the project's owner token instead."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py tests/test_keboola_storage_api.py -v`
Expected: PASS (all tests, including the pre-existing storage_api tests still green)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/storage_api.py connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py tests/test_keboola_storage_api.py
git commit -m "Add master-token preflight check for Keboola semantic layer sync"
```

---

### Task 3: Table and dataset resolution

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions over plain dicts/lists).
- Produces: `table_lookup_from_registry(rows: list[dict]) -> dict[tuple[str, str], str]`; `resolve_table_name(table_id: str, lookup: dict[tuple[str, str], str]) -> Optional[str]`; `dataset_lookup_by_table_id(dataset_items: list[dict]) -> dict[str, dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import (
    dataset_lookup_by_table_id,
    resolve_table_name,
    table_lookup_from_registry,
)


class TestTableLookupFromRegistry:
    def test_builds_bucket_source_table_to_name_map(self):
        rows = [
            {"bucket": "in.c-example_source", "source_table": "orders", "name": "crm_orders"},
            {"bucket": "in.c-example_source", "source_table": "contacts", "name": "crm_contacts"},
        ]
        lookup = table_lookup_from_registry(rows)
        assert lookup == {
            ("in.c-example_source", "orders"): "crm_orders",
            ("in.c-example_source", "contacts"): "crm_contacts",
        }

    def test_skips_rows_missing_bucket_or_source_table(self):
        rows = [
            {"bucket": None, "source_table": "orders", "name": "x"},
            {"bucket": "in.c-example_source", "source_table": None, "name": "y"},
            {"bucket": "in.c-example_source", "source_table": "contacts", "name": None},
        ]
        assert table_lookup_from_registry(rows) == {}


class TestResolveTableName:
    def test_splits_on_last_dot_bucket_may_contain_dots(self):
        # Bucket ids look like `in.c-example_source` (contain dots themselves) —
        # must split the tableId on the LAST dot, not the first.
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.orders", lookup) == "crm_orders"

    def test_returns_none_for_unregistered_table(self):
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.unknown_table", lookup) is None

    def test_returns_none_for_malformed_table_id(self):
        assert resolve_table_name("no_dot_here", {}) is None


class TestDatasetLookupByTableId:
    def test_builds_table_id_to_attributes_map(self):
        items = [
            {"type": "semantic-dataset", "id": "d1",
             "attributes": {"tableId": "in.c-example_source.orders", "grain": "One row per order"}},
        ]
        lookup = dataset_lookup_by_table_id(items)
        assert lookup == {"in.c-example_source.orders": {"tableId": "in.c-example_source.orders", "grain": "One row per order"}}

    def test_skips_items_missing_table_id(self):
        items = [{"type": "semantic-dataset", "id": "d1", "attributes": {"name": "no tableId"}}]
        assert dataset_lookup_by_table_id(items) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'table_lookup_from_registry'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
def table_lookup_from_registry(rows: list[dict]) -> dict[tuple[str, str], str]:
    """Build {(bucket, source_table): agnes_view_name} from table_registry
    rows (from `table_registry_repo().list_by_source("keboola")`)."""
    lookup: dict[tuple[str, str], str] = {}
    for row in rows:
        bucket = row.get("bucket")
        source_table = row.get("source_table")
        name = row.get("name")
        if bucket and source_table and name:
            lookup[(bucket, source_table)] = name
    return lookup


def resolve_table_name(table_id: str, lookup: dict[tuple[str, str], str]) -> Optional[str]:
    """Resolve a Keboola tableId ('bucket.table') to its Agnes
    table_registry view name, or None if that table isn't registered.

    Bucket ids themselves contain dots (e.g. `in.c-example_source`), so the
    tableId must be split on the LAST dot to isolate the table name —
    splitting on the first dot would misparse the bucket.
    """
    if "." not in table_id:
        return None
    bucket, _, source_table = table_id.rpartition(".")
    return lookup.get((bucket, source_table))


def dataset_lookup_by_table_id(dataset_items: list[dict]) -> dict[str, dict]:
    """Build {tableId: attributes} from semantic-dataset items, for
    enriching a metric row with grain/dimensions/synonyms/notes."""
    result: dict[str, dict] = {}
    for d in dataset_items:
        attrs = d.get("attributes") or {}
        table_id = attrs.get("tableId")
        if table_id:
            result[table_id] = attrs
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add table/dataset resolution for Keboola semantic layer importer"
```

---

### Task 4: SQL composition and foreign-alias detection

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `references_foreign_alias(expression: str) -> bool`; `compose_sql(expression: str, table_name: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import compose_sql, references_foreign_alias


class TestReferencesForeignAlias:
    def test_bare_column_reference_is_not_foreign(self):
        assert references_foreign_alias('SUM("cost_value")') is False

    def test_case_expression_without_alias_is_not_foreign(self):
        assert references_foreign_alias(
            'COUNT(CASE WHEN "status" = \'error\' THEN 1 END)'
        ) is False

    def test_alias_qualified_column_is_foreign(self):
        assert references_foreign_alias(
            'ROUND(SUM(TRY_CAST(o."amount" AS DECIMAL(18,2))), 2)'
        ) is True

    def test_multiple_foreign_aliases_detected(self):
        assert references_foreign_alias(
            'CASE WHEN um.metric_id = \'x\' THEN SUM(kumv.value) ELSE 0 END'
        ) is True


class TestComposeSql:
    def test_composes_select_with_alias_t(self):
        assert compose_sql('SUM("amount")', "orders") == 'SELECT SUM("amount") FROM "orders" AS t'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'references_foreign_alias'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
# Matches `<alias>.` followed by a quoted or unquoted column reference
# (`o."amount"` or `um.metric_id`) — both shapes appear in live multi-dataset
# expressions. Verified live (2026-07-15): single-dataset expressions are
# always bare column references (`SUM("amount")`); an alias-qualified
# reference only appears when the expression crosses into a JOINed dataset
# via semantic-relationship data this importer does not have (relationship
# support is out of scope for v1) — so any match here means "skip, cannot
# safely compose."
_ALIAS_QUALIFIER_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_"])')


def references_foreign_alias(expression: str) -> bool:
    """True if `expression` qualifies any column with an `<alias>.` prefix.

    See _ALIAS_QUALIFIER_RE docstring for why this indicates a multi-dataset
    JOIN this importer cannot safely compose in v1.
    """
    return bool(_ALIAS_QUALIFIER_RE.search(expression))


def compose_sql(expression: str, table_name: str) -> str:
    """Compose a full, runnable metric_definitions.sql from a Keboola
    semantic-metric.sql fragment (a bare aggregation expression, verified
    live to never be a full query) and the resolved Agnes table_registry
    view name.

    Callers MUST check `references_foreign_alias(expression)` first and
    skip the metric if True — this function does not itself guard against
    that case.
    """
    return f'SELECT {expression} FROM "{table_name}" AS t'
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add SQL composition and foreign-alias detection for Keboola metrics"
```

---

### Task 5: Constraint merge into `validation`

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `merge_constraints(metric_name: str, constraints: list[dict]) -> Optional[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import merge_constraints


class TestMergeConstraints:
    def test_returns_none_when_no_constraint_references_metric(self):
        constraints = [
            {"type": "semantic-constraint", "id": "c1",
             "attributes": {"name": "positive", "constraintType": "inequality",
                             "rule": "value >= 0", "metrics": ["other_metric"],
                             "severity": "warning"}},
        ]
        assert merge_constraints("revenue", constraints) is None

    def test_merges_single_matching_constraint(self):
        constraints = [
            {"type": "semantic-constraint", "id": "c1",
             "attributes": {"name": "revenue_non_negative", "constraintType": "inequality",
                             "rule": "value >= 0", "metrics": ["revenue"],
                             "severity": "warning"}},
        ]
        result = merge_constraints("revenue", constraints)
        assert result == {
            "rules": [
                {"name": "revenue_non_negative", "constraint_type": "inequality",
                 "rule": "value >= 0", "severity": "warning"},
            ]
        }

    def test_merges_multiple_matching_constraints(self):
        constraints = [
            {"type": "semantic-constraint", "id": "c1",
             "attributes": {"name": "revenue_non_negative", "constraintType": "inequality",
                             "rule": "value >= 0", "metrics": ["revenue"], "severity": "warning"}},
            {"type": "semantic-constraint", "id": "c2",
             "attributes": {"name": "revenue_not_null", "constraintType": "equality",
                             "rule": "value IS NOT NULL", "metrics": ["revenue", "other"],
                             "severity": "critical"}},
        ]
        result = merge_constraints("revenue", constraints)
        assert len(result["rules"]) == 2
        assert result["rules"][1]["name"] == "revenue_not_null"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_constraints'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
def merge_constraints(metric_name: str, constraints: list[dict]) -> Optional[dict]:
    """Build the `validation` JSON for one metric from semantic-constraint
    items whose `metrics[]` list includes it, or None if none match.

    Constraint attribute shape (`name`, `constraintType`, `rule` — a single
    SQL-ish string like `'value >= 0'`, `metrics: [...]`, `severity`) per
    `keboola/cli`'s documented live-verified contract.
    """
    matching = [
        c for c in constraints
        if metric_name in ((c.get("attributes") or {}).get("metrics") or [])
    ]
    if not matching:
        return None
    return {
        "rules": [
            {
                "name": (c.get("attributes") or {}).get("name"),
                "constraint_type": (c.get("attributes") or {}).get("constraintType"),
                "rule": (c.get("attributes") or {}).get("rule"),
                "severity": (c.get("attributes") or {}).get("severity"),
            }
            for c in matching
        ]
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add constraint merging into metric validation JSON"
```

---

### Task 6: Metric row builder

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_mapping.py` (append)

**Interfaces:**
- Consumes: `resolve_table_name`, `dataset_lookup_by_table_id` (Task 3); `references_foreign_alias`, `compose_sql` (Task 4); `merge_constraints` (Task 5).
- Produces: `build_metric_row(metric_item: dict, table_lookup: dict[tuple[str, str], str], dataset_lookup: dict[str, dict], constraints: list[dict], model_uuid: str) -> tuple[Optional[dict], Optional[str]]` — returns `(row, None)` on success or `(None, skip_reason)` where `skip_reason` is `"unresolved_table"` or `"foreign_alias_reference"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_keboola_semantic_layer_mapping.py`:

```python
from connectors.keboola.semantic_layer import build_metric_row


def _metric_item(name, sql, dataset, description="", model_uuid="model-1"):
    return {
        "type": "semantic-metric", "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset,
                       "description": description, "modelUUID": model_uuid},
    }


class TestBuildMetricRow:
    def test_builds_row_for_simple_metric(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        dataset_lookup = {}
        metric = _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders",
                               description="Total revenue")

        row, skip_reason = build_metric_row(metric, table_lookup, dataset_lookup, [], "model-1")

        assert skip_reason is None
        assert row["id"] == "keboola/model-1/total_revenue"
        assert row["name"] == "total_revenue"
        assert row["table_name"] == "crm_orders"
        assert row["expression"] == 'SUM("amount")'
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert row["description"] == "Total revenue"
        assert row["source"] == "keboola_semantic_layer"
        assert "validation" not in row

    def test_skips_unresolved_table(self):
        metric = _metric_item("m", 'SUM("x")', "in.c-unknown.table")

        row, skip_reason = build_metric_row(metric, {}, {}, [], "model-1")

        assert row is None
        assert skip_reason == "unresolved_table"

    def test_skips_foreign_alias_expression(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        metric = _metric_item("m", 'SUM(o."amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "foreign_alias_reference"

    def test_enriches_from_dataset_grain_and_ai_block(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        dataset_lookup = {
            "in.c-example_source.orders": {
                "tableId": "in.c-example_source.orders",
                "grain": "One row per order",
                "primaryKey": ["order_id"],
                "ai": {"synonyms": ["sales"], "hints": ["Join via customer_id"], "warnings": ["Excludes refunds"]},
            }
        }
        metric = _metric_item("m", 'SUM("amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, dataset_lookup, [], "model-1")

        assert skip_reason is None
        assert row["grain"] == "One row per order"
        assert row["dimensions"] == ["order_id"]
        assert row["synonyms"] == ["sales"]
        assert row["notes"] == ["Join via customer_id", "Excludes refunds"]

    def test_includes_validation_when_constraint_matches(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        constraints = [
            {"type": "semantic-constraint", "id": "c1",
             "attributes": {"name": "m_non_negative", "constraintType": "inequality",
                             "rule": "value >= 0", "metrics": ["m"], "severity": "warning"}},
        ]
        metric = _metric_item("m", 'SUM("amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, constraints, "model-1")

        assert skip_reason is None
        assert row["validation"]["rules"][0]["name"] == "m_non_negative"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_metric_row'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py`:

```python
def build_metric_row(
    metric_item: dict,
    table_lookup: dict[tuple[str, str], str],
    dataset_lookup: dict[str, dict],
    constraints: list[dict],
    model_uuid: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Map one semantic-metric item to a metric_definitions row dict.

    Returns (row, None) on success, or (None, skip_reason) where
    skip_reason is "unresolved_table" (the metric's dataset isn't
    registered in Agnes's table_registry) or "foreign_alias_reference"
    (the expression needs a JOIN this importer can't safely compose — see
    references_foreign_alias).
    """
    attrs = metric_item.get("attributes") or {}
    name = attrs.get("name")
    expression = attrs.get("sql") or ""
    dataset_table_id = attrs.get("dataset") or ""

    if references_foreign_alias(expression):
        return None, "foreign_alias_reference"

    table_name = resolve_table_name(dataset_table_id, table_lookup)
    if table_name is None:
        return None, "unresolved_table"

    row: dict[str, Any] = {
        "id": f"keboola/{model_uuid}/{name}",
        "name": name,
        "display_name": name,
        "category": "keboola",
        "description": attrs.get("description") or "",
        "expression": expression,
        "table_name": table_name,
        "sql": compose_sql(expression, table_name),
        "source": "keboola_semantic_layer",
    }

    dataset_attrs = dataset_lookup.get(dataset_table_id) or {}
    grain = dataset_attrs.get("grain")
    if grain:
        row["grain"] = grain
    primary_key = dataset_attrs.get("primaryKey") or []
    if primary_key:
        row["dimensions"] = list(primary_key)
    ai_block = dataset_attrs.get("ai") or {}
    synonyms = ai_block.get("synonyms") or []
    if synonyms:
        row["synonyms"] = list(synonyms)
    notes = list(ai_block.get("hints") or []) + list(ai_block.get("warnings") or [])
    if notes:
        row["notes"] = notes

    validation = merge_constraints(name, constraints)
    if validation is not None:
        row["validation"] = validation

    return row, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_mapping.py -v`
Expected: PASS (all tests in this file — should be ~20 by now)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_mapping.py
git commit -m "Add metric row builder for Keboola semantic layer importer"
```

---

### Task 7: Top-level orchestrator `sync_semantic_layer()`

**Files:**
- Modify: `connectors/keboola/semantic_layer.py` (append)
- Test: `tests/test_keboola_semantic_layer_sync.py` (new)

**Interfaces:**
- Consumes: `require_master_token` (Task 2); `table_lookup_from_registry`, `dataset_lookup_by_table_id` (Task 3); `build_metric_row` (Task 6); `MetastoreClient` (Task 1); `connectors.keboola.storage_api.KeboolaStorageClient` (existing); `src.repositories.table_registry_repo`, `src.repositories.metric_repo` (existing factories).
- Produces: `sync_semantic_layer(keboola_url: Optional[str] = None, keboola_token: Optional[str] = None) -> dict` — returns `{"status": "ok"|"error", "created_or_updated": int, "pruned": int, "skipped_unresolved_table": int, "skipped_foreign_alias": int}` on success, `{"status": "error", "error": str}` on failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_semantic_layer_sync.py
"""sync_semantic_layer() orchestrator — mocked MetastoreClient/StorageClient,
real test DuckDB via the e2e_env fixture (same pattern as
tests/test_bq_metadata_refresh_endpoint.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _register_keboola_table(bucket: str, source_table: str, name: str):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name, name=name, source_type="keboola",
            bucket=bucket, source_table=source_table, query_mode="local",
        )
    finally:
        conn.close()


def _model_item(uuid="model-1", name="core"):
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _metric_item(name, sql, dataset, model_uuid="model-1"):
    return {
        "type": "semantic-metric", "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


class TestSyncSemanticLayer:
    def test_creates_metrics_from_metastore(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
        }[item_type]

        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage), \
             patch("connectors.keboola.semantic_layer.MetastoreClient", return_value=fake_metastore):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "ok"
        assert result["created_or_updated"] == 1
        assert result["skipped_unresolved_table"] == 0
        assert result["skipped_foreign_alias"] == 0

        row = metric_repo().get("keboola/model-1/total_revenue")
        assert row is not None
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert row["source"] == "keboola_semantic_layer"

    def test_prunes_metrics_removed_upstream(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()

        # First run: two metrics.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [
                _metric_item("a", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("b", 'COUNT(*)', "in.c-example_source.orders"),
            ],
            "semantic-constraint": [],
        }[item_type]
        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage), \
             patch("connectors.keboola.semantic_layer.MetastoreClient", return_value=fake_metastore):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert metric_repo().get("keboola/model-1/a") is not None
        assert metric_repo().get("keboola/model-1/b") is not None

        # Second run: metric "b" removed upstream.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
        }[item_type]
        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage), \
             patch("connectors.keboola.semantic_layer.MetastoreClient", return_value=fake_metastore):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 1
        assert metric_repo().get("keboola/model-1/a") is not None
        assert metric_repo().get("keboola/model-1/b") is None

    def test_never_prunes_other_sources(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        metric_repo().create(
            id="manual/hand_authored", name="hand_authored", display_name="Hand Authored",
            category="manual", sql="SELECT 1", source="manual",
        )

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()], "semantic-dataset": [],
            "semantic-metric": [], "semantic-constraint": [],
        }[item_type]

        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage), \
             patch("connectors.keboola.semantic_layer.MetastoreClient", return_value=fake_metastore):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 0
        assert metric_repo().get("manual/hand_authored") is not None

    def test_skips_metric_with_unresolved_table(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()], "semantic-dataset": [],
            "semantic-metric": [_metric_item("orphan", 'SUM("x")', "in.c-unregistered.table")],
            "semantic-constraint": [],
        }[item_type]

        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage), \
             patch("connectors.keboola.semantic_layer.MetastoreClient", return_value=fake_metastore):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_unresolved_table"] == 1
        assert metric_repo().get("keboola/model-1/orphan") is None

    def test_raises_master_token_required(self, e2e_env):
        from connectors.keboola.semantic_layer import MasterTokenRequiredError, sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": False}

        with patch("connectors.keboola.semantic_layer.KeboolaStorageClient", return_value=fake_storage):
            with pytest.raises(MasterTokenRequiredError):
                sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="regular-tok")

    def test_missing_credentials_returns_error_status(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

        result = sync_semantic_layer()

        assert result["status"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'sync_semantic_layer'`

- [ ] **Step 3: Implement**

Append to `connectors/keboola/semantic_layer.py` (add these imports to the top of the file alongside the existing `import logging` / `import re`):

```python
import os
```

Then append at the end of the file:

```python
def sync_semantic_layer(
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
) -> dict:
    """Fetch a Keboola project's semantic layer (Metastore) and upsert it
    into Agnes's metric_definitions table, pruning stale
    'keboola_semantic_layer'-sourced rows that no longer exist upstream.

    Credentials default to the standard Keboola env-var/vault resolution
    (KEBOOLA_STACK_URL + KEBOOLA_STORAGE_TOKEN via datasource_secret) — same
    hierarchy connectors/keboola/metadata.py uses.

    Raises MasterTokenRequiredError if the configured token is not a master
    token (see require_master_token) — this is a configuration error the
    caller should surface loudly, not swallow into the returned dict.
    """
    from app.datasource_secrets import datasource_secret
    from connectors.keboola.storage_api import KeboolaStorageClient
    from connectors.keboola.metastore_client import MetastoreClient
    from src.repositories import table_registry_repo, metric_repo

    url = keboola_url or os.environ.get("KEBOOLA_STACK_URL", "")
    token = keboola_token or datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
    if not url or not token:
        return {"status": "error", "error": "Keboola credentials not configured"}

    storage_client = KeboolaStorageClient(url=url, token=token)
    require_master_token(storage_client)

    metastore = MetastoreClient(url=url, token=token)

    models = metastore.list_items("semantic-model")
    empty_result = {
        "status": "ok", "created_or_updated": 0, "pruned": 0,
        "skipped_unresolved_table": 0, "skipped_foreign_alias": 0,
    }
    if not models:
        return empty_result
    if len(models) > 1:
        logger.warning(
            "Keboola project has %d semantic models; using the first (%s)",
            len(models), (models[0].get("attributes") or {}).get("name"),
        )
    model_uuid = models[0]["id"]

    datasets = metastore.list_items("semantic-dataset", model_uuid)
    metrics = metastore.list_items("semantic-metric", model_uuid)
    constraints = metastore.list_items("semantic-constraint", model_uuid)

    table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    dataset_lookup = dataset_lookup_by_table_id(datasets)

    repo = metric_repo()
    seen_ids: set[str] = set()
    skipped_unresolved_table = 0
    skipped_foreign_alias = 0

    for item in metrics:
        row, skip_reason = build_metric_row(item, table_lookup, dataset_lookup, constraints, model_uuid)
        if row is None:
            if skip_reason == "unresolved_table":
                skipped_unresolved_table += 1
            else:
                skipped_foreign_alias += 1
            continue
        repo.create(**row)
        seen_ids.add(row["id"])

    existing = [m for m in repo.list() if m.get("source") == "keboola_semantic_layer"]
    pruned = 0
    for m in existing:
        if m["id"] not in seen_ids:
            repo.delete(m["id"])
            pruned += 1

    return {
        "status": "ok",
        "created_or_updated": len(seen_ids),
        "pruned": pruned,
        "skipped_unresolved_table": skipped_unresolved_table,
        "skipped_foreign_alias": skipped_foreign_alias,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_sync.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/semantic_layer.py tests/test_keboola_semantic_layer_sync.py
git commit -m "Add sync_semantic_layer orchestrator with upsert+prune"
```

---

### Task 8: FastAPI endpoint

**Files:**
- Create: `app/api/keboola_semantic_layer_refresh.py`
- Modify: `app/main.py` (register router — mirror lines 297 and 1604)
- Test: `tests/test_keboola_semantic_layer_refresh_endpoint.py` (new)

**Interfaces:**
- Consumes: `connectors.keboola.semantic_layer.sync_semantic_layer` (Task 7); `app.auth.access.require_admin` (existing).
- Produces: `POST /api/admin/run-keboola-semantic-layer-refresh` endpoint, `router` (FastAPI `APIRouter`), module-level `_refresh_lock` (`asyncio.Lock`) and `_refresh_state` (dict) — single-flight guard mirroring `app/api/bq_metadata_refresh.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_semantic_layer_refresh_endpoint.py
"""End-to-end tests for POST /api/admin/run-keboola-semantic-layer-refresh."""

import asyncio
from unittest.mock import patch


def test_run_refresh_returns_sync_result(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake_result = {
        "status": "ok", "created_or_updated": 3, "pruned": 0,
        "skipped_unresolved_table": 1, "skipped_foreign_alias": 0,
    }
    with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["created_or_updated"] == 3
    assert body["pruned"] == 0
    assert body["skipped_unresolved_table"] == 1
    assert body["skipped_foreign_alias"] == 0
    assert body["run_id"]
    assert body["started_at"]


def test_run_refresh_maps_master_token_error_to_400(seeded_app):
    from connectors.keboola.semantic_layer import MasterTokenRequiredError

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with patch(
        "app.api.keboola_semantic_layer_refresh.sync_semantic_layer",
        side_effect=MasterTokenRequiredError("needs a master token"),
    ):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400
    assert "master token" in r.json()["detail"]


def test_run_refresh_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/run-keboola-semantic-layer-refresh")
    assert r.status_code == 401


def test_run_refresh_returns_409_when_already_running(seeded_app):
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    async def _acquire():
        await endpoint_module._refresh_lock.acquire()

    asyncio.run(_acquire())
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_running"
    finally:
        endpoint_module._refresh_lock.release()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_refresh_endpoint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.keboola_semantic_layer_refresh'`

- [ ] **Step 3: Implement**

Create `app/api/keboola_semantic_layer_refresh.py`:

```python
"""Keboola semantic layer refresh — owner of the sync_semantic_layer() call path.

POST /api/admin/run-keboola-semantic-layer-refresh — called by the
scheduler container (auth: shared scheduler token resolves to a synthetic
admin user, same mechanism as app/api/bq_metadata_refresh.py) on the
SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL cadence. Also callable by
a real admin on demand.

Single-flight guarded (mirrors app/api/bq_metadata_refresh.py): a second
concurrent call while a sync is in flight gets 409 already_running instead
of racing a second Metastore fetch + upsert/prune pass against the same
metric_definitions rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    sync_semantic_layer,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
_refresh_state: dict[str, Any] = {"run_id": None, "started_at": None}


@router.post("/api/admin/run-keboola-semantic-layer-refresh")
async def run_keboola_semantic_layer_refresh(
    user: dict = Depends(require_admin),
):
    """Sync the configured Keboola project's semantic layer into
    metric_definitions. See connectors/keboola/semantic_layer.py for the
    mapping/prune logic.
    """
    if _refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "already_running",
                "run_id": _refresh_state.get("run_id"),
                "started_at": _refresh_state.get("started_at"),
                "hint": "A refresh is already in flight; this caller is a no-op.",
            },
        )

    async with _refresh_lock:
        run_id = uuid.uuid4().hex[:8]
        started_at = datetime.now(timezone.utc).isoformat()
        _refresh_state["run_id"] = run_id
        _refresh_state["started_at"] = started_at
        try:
            result = await asyncio.to_thread(sync_semantic_layer)
        except MasterTokenRequiredError as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "keboola semantic layer refresh: run_id=%s status=%s created_or_updated=%s "
        "pruned=%s skipped_unresolved_table=%s skipped_foreign_alias=%s",
        run_id, result.get("status"), result.get("created_or_updated"), result.get("pruned"),
        result.get("skipped_unresolved_table"), result.get("skipped_foreign_alias"),
    )
    return {**result, "run_id": run_id, "started_at": started_at}
```

Modify `app/main.py`: add the import next to line 297 (`from app.api.bq_metadata_refresh import router as bq_metadata_refresh_router`):

```python
from app.api.keboola_semantic_layer_refresh import router as keboola_semantic_layer_refresh_router
```

And the registration next to line 1604 (`app.include_router(bq_metadata_refresh_router)`):

```python
app.include_router(keboola_semantic_layer_refresh_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_keboola_semantic_layer_refresh_endpoint.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/api/keboola_semantic_layer_refresh.py app/main.py tests/test_keboola_semantic_layer_refresh_endpoint.py
git commit -m "Add Keboola semantic layer refresh endpoint"
```

---

### Task 9: Scheduler wiring

**Files:**
- Modify: `services/scheduler/__main__.py`
- Modify: `tests/test_scheduler_sidecar.py` (append)

**Interfaces:**
- Consumes: `POST /api/admin/run-keboola-semantic-layer-refresh` (Task 8).
- Produces: new job tuple `("keboola-semantic-layer-refresh", schedule, "/api/admin/run-keboola-semantic-layer-refresh", "POST", 900)` in `build_jobs()`; new env var `SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL` (default 6h — metrics change less often than BQ metadata's 4h default).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler_sidecar.py`:

```python
def test_build_jobs_includes_keboola_semantic_layer_refresh_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL", raising=False)
    from services.scheduler.__main__ import build_jobs

    target = next(j for j in build_jobs() if j[0] == "keboola-semantic-layer-refresh")
    _, schedule, endpoint, method, timeout = target
    assert schedule == "every 6h"
    assert endpoint == "/api/admin/run-keboola-semantic-layer-refresh"
    assert method == "POST"
    assert timeout == 900


def test_build_jobs_honors_keboola_semantic_layer_refresh_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL", "3600")  # 1h
    from services.scheduler.__main__ import build_jobs

    jobs = {name: schedule for name, schedule, *_ in build_jobs()}
    assert jobs["keboola-semantic-layer-refresh"] == "every 1h"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scheduler_sidecar.py -k keboola_semantic_layer -v`
Expected: FAIL — `StopIteration` (no job named `keboola-semantic-layer-refresh` yet)

- [ ] **Step 3: Implement**

In `services/scheduler/__main__.py`, add to the `_DEFAULTS` dict (right after the `SCHEDULER_BQ_METADATA_REFRESH_INTERVAL` entry, around line 99):

```python
    # Keboola semantic layer (Metastore) refresh: walks a Keboola project's
    # datasets/metrics/constraints and upserts+prunes metric_definitions
    # rows tagged source='keboola_semantic_layer'. Default 6 h — metrics
    # change less often than BQ metadata cache entries (4 h default), and
    # each run does far fewer HTTP calls (a handful of Metastore list
    # requests vs one BQ metadata fetch per registered table).
    "SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL": 6 * 60 * 60,
```

In `build_jobs()`, add the env read next to `bqmeta` (around line 315):

```python
    kbsl = _read_positive_int("SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL")
```

Add `kbsl` to the `smallest = min(...)` call (around line 322-336):

```python
    smallest = min(
        refresh,
        health,
        scripts,
        sess,
        verify,
        usage,
        corpmem,
        bqmeta,
        kbsl,
        usageprune,
        jirasla,
        jiraconsis,
        kpkg,
        kdig,
    )
```

Add the job tuple to the `jobs` list, right after the `bq-metadata-refresh` entry (around line 391):

```python
        # Keboola semantic layer refresh — keeps metric_definitions rows
        # tagged source='keboola_semantic_layer' in sync with the project's
        # Metastore. Short-circuits (returns an error result, doesn't crash)
        # when Keboola credentials aren't configured or the token isn't a
        # master token — see connectors/keboola/semantic_layer.py.
        (
            "keboola-semantic-layer-refresh",
            _seconds_to_schedule(kbsl),
            "/api/admin/run-keboola-semantic-layer-refresh",
            "POST",
            900,
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_scheduler_sidecar.py -v`
Expected: PASS (all tests in this file, including pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add services/scheduler/__main__.py tests/test_scheduler_sidecar.py
git commit -m "Wire Keboola semantic layer refresh into the scheduler"
```

---

### Task 10: CHANGELOG and full suite verification

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing (documentation-only task).
- Produces: nothing (end of plan).

- [ ] **Step 1: Add the CHANGELOG bullet**

Add under the `## [Unreleased]` heading in `CHANGELOG.md` (create the heading with an `### Added` subsection if `[Unreleased]` doesn't already have one open):

```markdown
### Added

- Keboola semantic layer importer: `connectors/keboola/semantic_layer.py` syncs a Keboola project's Metastore (datasets, metrics, constraints) into `metric_definitions` on a schedule (`SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL`, default 6h), tagged `source='keboola_semantic_layer'` and upsert+pruned each run. Requires a master (owner) Storage API token. Metrics referencing a JOINed dataset (via an aliased column not in scope for v1) are skipped and counted rather than guessed.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Add CHANGELOG entry for Keboola semantic layer importer"
```

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: All tests pass (no regressions in any pre-existing test file this plan touched: `tests/test_keboola_storage_api.py`, `tests/test_scheduler_sidecar.py`).

- [ ] **Step 4: Verify no leftover sensitive data**

Run: `git log --oneline -12` and confirm every commit message and every file in this plan's diff contains no real Keboola project data (table names, SQL formulas, project IDs) — this plan's example values are all fabricated placeholders (`in.c-example_source.orders`, `SUM("amount")`, etc.), consistent with the redacted design spec.
