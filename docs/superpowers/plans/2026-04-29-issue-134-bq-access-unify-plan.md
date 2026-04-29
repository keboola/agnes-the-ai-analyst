# Issue #134 — Unify BigQuery access behind `BqAccess` (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix issue #134 (v2 BigQuery endpoints returning bare HTTP 500) by (a) translating Google API errors into structured 502/400 responses and (b) extracting the duplicated BQ-access pattern into a single `BqAccess` facade in `connectors/bigquery/access.py`.

**Architecture:** Two-phase rollout. Phase 1 = inline `try/except` in three v2 endpoint files emitting the final structured body shape — fixes Pavel's bug atomically. Phase 2 = extract `BqAccess` facade + migrate all four call sites (`v2_scan`, `v2_sample`, `v2_schema` two blocks, `RemoteQueryEngine`) to it; delete `_bq_client_factory` test injection point. Final PR has two reviewable commits.

**Tech Stack:** Python 3.11, FastAPI, DuckDB, google-cloud-bigquery, pytest. See `docs/superpowers/specs/2026-04-29-issue-134-bq-access-unify-design.md` for the full design.

---

## File structure

**Created (Phase 2):**
- `connectors/bigquery/access.py` — `BqProjects`, `BqAccessError`, `BqAccess`, `get_bq_access`, `translate_bq_error`, default factories.
- `tests/test_bq_access.py` — unit tests for the new module.

**Modified:**
- `app/api/v2_sample.py` — Phase 1: billing_project + try/except; Phase 2: replace with `BqAccess`.
- `app/api/v2_scan.py` — Phase 1: try/except around BQ calls; Phase 2: replace with `BqAccess`.
- `app/api/v2_schema.py` — Phase 1: try/except around `_fetch_bq_schema` only; Phase 2: replace with `BqAccess` (both blocks, preserve `_fetch_bq_table_options` swallow-all).
- `src/remote_query.py` — Phase 2: lazy `bq_access` kwarg; drop `_bq_client_factory` + fallback chain + stale docstring.
- `tests/test_v2_scan.py`, `tests/test_v2_scan_estimate.py`, `tests/test_v2_sample.py`, `tests/test_v2_schema.py` — add structured-error tests; in Phase 2 update the `_fetch_*` lambda signatures.
- `tests/test_remote_query.py` — Phase 2: migrate `_bq_client_factory=` injection to `bq_access=BqAccess(..., client_factory=...)`.
- `tests/conftest.py` — Phase 2: add `bq_access` fixture.
- `CHANGELOG.md` — Phase 1 (Fixed); Phase 2 (Changed BREAKING + Internal).

---

# Phase 1 — Commit 1: Atomic bug fix (no `BqAccess` yet)

Goal: structured-body responses for cross-project Forbidden / BadRequest from BQ on all three v2 endpoints. Same body shape that Phase 2 will produce, so client-side parsers (CLI, UI) see one consistent contract throughout the rollout.

**Body shape (used in all Phase 1 tasks):**
```python
# 502 case (Forbidden / upstream BadRequest)
{"error": "<kind>", "message": "<bq error text>", "details": {<contextual>}}
# 400 case (user-derived BadRequest in v2_scan only)
{"error": "bq_bad_request", "message": "<bq error text>", "details": {}}
```

`<kind>` for Forbidden = `"cross_project_forbidden"` if `"serviceusage" in str(e).lower()` else `"bq_forbidden"`.

---

### Task 1.1: v2_sample — add billing_project fallback + wrap `_fetch_bq_sample`

**Files:**
- Modify: `app/api/v2_sample.py:21-48` (`_fetch_bq_sample`), `:97-110` (`sample` endpoint).
- Test: `tests/test_v2_sample.py` (add new tests).

- [ ] **Step 1: Read current state**

Run: `head -120 app/api/v2_sample.py` to confirm baseline (today: reads only `data_source.bigquery.project`; `_fetch_bq_sample` has no try/except).

- [ ] **Step 2: Write failing test for cross-project Forbidden → 502 + structured body**

Add to `tests/test_v2_sample.py`:

```python
def test_sample_returns_502_on_bq_forbidden_serviceusage(reload_db, monkeypatch):
    from app.api import v2_sample
    from google.api_core.exceptions import Forbidden

    def _raise_forbidden(*args, **kwargs):
        raise Forbidden("Permission denied: serviceusage.services.use on project foo")

    monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _raise_forbidden)

    # ... seed registry with a bigquery table, then call the endpoint
    # via TestClient (match the pattern used by other tests in this file)
    # Assert response.status_code == 502
    # Assert response.json()["detail"]["error"] == "cross_project_forbidden"
    # Assert "billing_project" in response.json()["detail"]["details"]["hint"].lower()
```

NOTE: match the test-client construction style of existing tests in this file. If they use a `TestClient` fixture, reuse it; if they call helpers directly, follow that.

- [ ] **Step 3: Write failing test for billing_project fallback being read**

```python
def test_sample_reads_billing_project_from_instance_yaml(reload_db, monkeypatch):
    """Regression guard for the original bug: the project passed to _fetch_bq_sample
    must come from billing_project when set, not from project."""
    captured = {}

    def _capture(project, dataset, table, n):
        captured["project"] = project
        return []

    monkeypatch.setattr("app.api.v2_sample._fetch_bq_sample", _capture)
    monkeypatch.setattr("app.instance_config.get_value", lambda *keys, **kw: {
        ("data_source", "bigquery", "project"): "data-proj",
        ("data_source", "bigquery", "billing_project"): "billing-proj",
    }.get(keys, kw.get("default", "")))

    # ... seed bigquery table, call /api/v2/sample/<id>, assert 200
    assert captured["project"] == "billing-proj"
```

- [ ] **Step 4: Run tests — expect FAIL**

Run: `pytest tests/test_v2_sample.py::test_sample_returns_502_on_bq_forbidden_serviceusage tests/test_v2_sample.py::test_sample_reads_billing_project_from_instance_yaml -v`
Expected: both FAIL — sample endpoint today returns 500 on Forbidden and reads only `project`.

- [ ] **Step 5: Implement billing_project fallback**

Edit `app/api/v2_sample.py:104` (in the `sample` endpoint):

```python
project_id = (
    get_value("data_source", "bigquery", "billing_project", default="")
    or get_value("data_source", "bigquery", "project", default="")
    or ""
)
```

(Replace today's `project_id = get_value("data_source", "bigquery", "project", default="") or ""`.)

- [ ] **Step 6: Wrap `_fetch_bq_sample` with structured-error translation**

Edit `app/api/v2_sample.py:21-48`. Replace the function body's interior with:

```python
def _fetch_bq_sample(project: str, dataset: str, table: str, n: int) -> list[dict]:
    import duckdb
    from google.api_core import exceptions as gax
    from connectors.bigquery.auth import get_metadata_token
    from src.identifier_validation import validate_quoted_identifier
    from fastapi import HTTPException

    if not (validate_quoted_identifier(project, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        bq_sql = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT {int(n)}"
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [project, bq_sql],
            ).fetchdf()
        except gax.Forbidden as e:
            kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
            raise HTTPException(
                status_code=502,
                detail={
                    "error": kind,
                    "message": str(e),
                    "details": {
                        "billing_project": project,
                        "hint": (
                            "Set data_source.bigquery.billing_project in instance.yaml to a project "
                            "where the SA has serviceusage.services.use, or grant the SA that role "
                            "on the data project."
                        ) if kind == "cross_project_forbidden" else "",
                    },
                },
            )
        except gax.BadRequest as e:
            # /sample SQL is server-constructed (validated identifiers + LIMIT n);
            # a BadRequest here means registry corruption → upstream error, not user fault.
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
        except gax.GoogleAPICallError as e:
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
        return df.to_dict(orient="records")
    finally:
        conn.close()
```

- [ ] **Step 7: Run the two new tests — expect PASS**

Run: `pytest tests/test_v2_sample.py::test_sample_returns_502_on_bq_forbidden_serviceusage tests/test_v2_sample.py::test_sample_reads_billing_project_from_instance_yaml -v`
Expected: both PASS.

- [ ] **Step 8: Run full v2_sample test file — expect no regressions**

Run: `pytest tests/test_v2_sample.py -v`
Expected: all green (existing tests unaffected — the change is additive).

- [ ] **Step 9: Commit (intermediate, will be squashed before PR)**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss-134-bq-access"
git add app/api/v2_sample.py tests/test_v2_sample.py
git commit -m "fix(v2_sample): #134 add billing_project fallback + structured 502 on BQ Forbidden"
```

---

### Task 1.2: v2_scan — wrap `_bq_dry_run_bytes`

**Files:**
- Modify: `app/api/v2_scan.py:43-55` (`_bq_dry_run_bytes`).
- Test: `tests/test_v2_scan_estimate.py`.

- [ ] **Step 1: Read current state**

Run: `sed -n '43,55p' app/api/v2_scan.py` to confirm: today no try/except.

- [ ] **Step 2: Write failing test for /scan/estimate 502 on Forbidden**

Add to `tests/test_v2_scan_estimate.py` (match the file's existing test style):

```python
def test_scan_estimate_returns_502_on_bq_forbidden_serviceusage(reload_db, monkeypatch):
    from app.api import v2_scan
    from google.api_core.exceptions import Forbidden

    def _raise_forbidden(project, sql):
        raise Forbidden("Permission denied: serviceusage.services.use on project foo")

    monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _raise_forbidden)

    # ... seed bigquery table, call POST /api/v2/scan/estimate
    # Assert response.status_code == 502
    # Assert response.json()["detail"]["error"] == "cross_project_forbidden"
    # Assert "hint" in response.json()["detail"]["details"]
```

```python
def test_scan_estimate_returns_400_on_bq_bad_request(reload_db, monkeypatch):
    from app.api import v2_scan
    from google.api_core.exceptions import BadRequest

    def _raise_bad_request(project, sql):
        raise BadRequest("Syntax error: unexpected token at line 1, column 5")

    monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _raise_bad_request)

    # ... call POST /api/v2/scan/estimate
    # Assert response.status_code == 400
    # Assert response.json()["detail"]["error"] == "bq_bad_request"
    # Assert "Syntax error" in response.json()["detail"]["message"]
```

- [ ] **Step 3: Run tests — expect FAIL**

Run: `pytest tests/test_v2_scan_estimate.py::test_scan_estimate_returns_502_on_bq_forbidden_serviceusage tests/test_v2_scan_estimate.py::test_scan_estimate_returns_400_on_bq_bad_request -v`
Expected: both FAIL with bare 500.

- [ ] **Step 4: Wrap `_bq_dry_run_bytes`**

Replace `app/api/v2_scan.py:43-55` with:

```python
def _bq_dry_run_bytes(project: str, sql: str) -> int:
    """Run a BQ dry-run via the google-cloud-bigquery client and return totalBytesProcessed.

    Errors translated to HTTPException with the structured-body shape used across
    all v2 endpoints. SQL here is user-derived (built from req.select/where/order_by),
    so BadRequest → 400.
    """
    from google.cloud import bigquery
    from google.api_core import exceptions as gax
    from google.api_core.client_options import ClientOptions
    from fastapi import HTTPException

    client = bigquery.Client(
        project=project,
        client_options=ClientOptions(quota_project_id=project),
    )
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)
    except gax.Forbidden as e:
        kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
        raise HTTPException(
            status_code=502,
            detail={
                "error": kind,
                "message": str(e),
                "details": {
                    "billing_project": project,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ) if kind == "cross_project_forbidden" else "",
                },
            },
        )
    except gax.BadRequest as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "bq_bad_request", "message": str(e), "details": {}},
        )
    except gax.GoogleAPICallError as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
        )
```

- [ ] **Step 5: Run tests — expect PASS**

Run: `pytest tests/test_v2_scan_estimate.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/api/v2_scan.py tests/test_v2_scan_estimate.py
git commit -m "fix(v2_scan): #134 structured 502/400 on BQ errors in dry-run path"
```

---

### Task 1.3: v2_scan — wrap `_run_bq_scan`

**Files:**
- Modify: `app/api/v2_scan.py:266-282` (`_run_bq_scan`).
- Test: `tests/test_v2_scan.py`.

- [ ] **Step 1: Write failing tests (mirror Task 1.2)**

Add to `tests/test_v2_scan.py`:

```python
def test_scan_returns_502_on_bq_forbidden_serviceusage(reload_db, monkeypatch):
    from app.api import v2_scan
    from google.api_core.exceptions import Forbidden

    def _raise(project, sql):
        raise Forbidden("Permission denied: serviceusage.services.use on project foo")

    monkeypatch.setattr(v2_scan, "_run_bq_scan", _raise)

    # ... seed bigquery table, call POST /api/v2/scan
    # Assert response.status_code == 502
    # Assert response.json()["detail"]["error"] == "cross_project_forbidden"


def test_scan_returns_400_on_bq_bad_request(reload_db, monkeypatch):
    from app.api import v2_scan
    from google.api_core.exceptions import BadRequest

    def _raise(project, sql):
        raise BadRequest("Syntax error")

    monkeypatch.setattr(v2_scan, "_run_bq_scan", _raise)

    # ... call POST /api/v2/scan
    # Assert response.status_code == 400
    # Assert response.json()["detail"]["error"] == "bq_bad_request"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_v2_scan.py::test_scan_returns_502_on_bq_forbidden_serviceusage tests/test_v2_scan.py::test_scan_returns_400_on_bq_bad_request -v`
Expected: FAIL with bare 500.

- [ ] **Step 3: Wrap `_run_bq_scan`**

Replace `app/api/v2_scan.py:266-282` with:

```python
def _run_bq_scan(project: str, sql: str):
    """Run a BQ query via DuckDB BQ extension. Returns Arrow table.

    Errors translated to HTTPException with the structured-body shape used across
    all v2 endpoints. SQL here is user-derived → BadRequest → 400.
    """
    import duckdb
    from google.api_core import exceptions as gax
    from connectors.bigquery.auth import get_metadata_token
    from fastapi import HTTPException

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        try:
            return conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [project, sql],
            ).arrow()
        except gax.Forbidden as e:
            kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
            raise HTTPException(
                status_code=502,
                detail={
                    "error": kind,
                    "message": str(e),
                    "details": {
                        "billing_project": project,
                        "hint": (
                            "Set data_source.bigquery.billing_project in instance.yaml to a project "
                            "where the SA has serviceusage.services.use, or grant the SA that role "
                            "on the data project."
                        ) if kind == "cross_project_forbidden" else "",
                    },
                },
            )
        except gax.BadRequest as e:
            raise HTTPException(
                status_code=400,
                detail={"error": "bq_bad_request", "message": str(e), "details": {}},
            )
        except gax.GoogleAPICallError as e:
            raise HTTPException(
                status_code=502,
                detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
            )
    finally:
        conn.close()
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_v2_scan.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add app/api/v2_scan.py tests/test_v2_scan.py
git commit -m "fix(v2_scan): #134 structured 502/400 on BQ errors in scan path"
```

---

### Task 1.4: v2_schema — wrap `_fetch_bq_schema` (strict block only)

**Files:**
- Modify: `app/api/v2_schema.py:36-73` (`_fetch_bq_schema` function — exact line range may vary; the strict block is the FIRST one in the file, NOT `_fetch_bq_table_options`).
- Test: `tests/test_v2_schema.py`.

**IMPORTANT — preserve `_fetch_bq_table_options` exactly as-is in this task.** That function (lines 90-129) wraps everything in `try/except Exception → return {}` and is the best-effort partition-info path. Phase 2 will migrate it to use `BqAccess` while preserving the swallow-all. **Do not touch it in Phase 1.**

- [ ] **Step 1: Write failing test for /schema 502 on _fetch_bq_schema Forbidden**

Add to `tests/test_v2_schema.py`:

```python
def test_schema_returns_502_on_bq_forbidden_serviceusage(reload_db, monkeypatch):
    from app.api import v2_schema
    from google.api_core.exceptions import Forbidden

    def _raise(project, dataset, table):
        raise Forbidden("Permission denied: serviceusage.services.use on project foo")

    monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _raise)

    # ... seed bigquery table, call GET /api/v2/schema/<id>
    # Assert response.status_code == 502
    # Assert response.json()["detail"]["error"] == "cross_project_forbidden"
```

- [ ] **Step 2: Write regression-guard test for /schema 200 with empty partition info on `_fetch_bq_table_options` failure**

```python
def test_schema_returns_200_with_empty_partition_on_table_options_failure(reload_db, monkeypatch):
    """Regression guard: _fetch_bq_table_options is best-effort. /schema must keep
    returning successfully even if partition-info fetch fails."""
    from app.api import v2_schema
    from google.api_core.exceptions import Forbidden

    def _ok_schema(project, dataset, table):
        return [{"name": "event_date", "type": "DATE"}]

    def _fail_options(project, dataset, table):
        raise Forbidden("denied")

    monkeypatch.setattr(v2_schema, "_fetch_bq_schema", _ok_schema)
    monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", _fail_options)

    # ... seed bigquery table, call GET /api/v2/schema/<id>
    # Assert response.status_code == 200
    # Assert response.json()["columns"] == [{"name": "event_date", "type": "DATE"}]
    # Assert "partition_by" not in response.json() OR response.json().get("partition_by") is None
```

(NOTE: today `_fetch_bq_table_options` already has the swallow-all `try/except → return {}`, so this test should pass even without changes — it's a regression guard for Phase 2.)

- [ ] **Step 3: Run — expect first test FAIL, second PASS**

Run: `pytest tests/test_v2_schema.py::test_schema_returns_502_on_bq_forbidden_serviceusage tests/test_v2_schema.py::test_schema_returns_200_with_empty_partition_on_table_options_failure -v`
Expected: 502 test FAILS (today returns 500); regression-guard PASSES.

- [ ] **Step 4: Wrap `_fetch_bq_schema` with the same try/except shape**

Edit `app/api/v2_schema.py:36-73` (the FIRST INSTALL/LOAD/SECRET block, in `_fetch_bq_schema`). Wrap the `conn.execute("SELECT * FROM bigquery_query(?, ?, ?)", ...)` line in:

```python
from google.api_core import exceptions as gax
from fastapi import HTTPException

try:
    rows = conn.execute(
        "SELECT * FROM bigquery_query(?, ?, ?)",
        [project, bq_sql, table],
    ).fetchall()
except gax.Forbidden as e:
    kind = "cross_project_forbidden" if "serviceusage" in str(e).lower() else "bq_forbidden"
    raise HTTPException(
        status_code=502,
        detail={
            "error": kind,
            "message": str(e),
            "details": {
                "billing_project": project,
                "hint": (
                    "Set data_source.bigquery.billing_project in instance.yaml to a project "
                    "where the SA has serviceusage.services.use, or grant the SA that role "
                    "on the data project."
                ) if kind == "cross_project_forbidden" else "",
            },
        },
    )
except gax.GoogleAPICallError as e:
    raise HTTPException(
        status_code=502,
        detail={"error": "bq_upstream_error", "message": str(e), "details": {}},
    )
```

(`/schema` SQL hits INFORMATION_SCHEMA, fully server-constructed → BadRequest folded into upstream_error 502; no separate 400 case.)

**Do not modify `_fetch_bq_table_options`.**

- [ ] **Step 5: Run — expect PASS**

Run: `pytest tests/test_v2_schema.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/api/v2_schema.py tests/test_v2_schema.py
git commit -m "fix(v2_schema): #134 structured 502 on BQ errors in strict schema path"
```

---

### Task 1.5: CHANGELOG entry for Phase 1; finalize commit 1

**Files:** `CHANGELOG.md`.

- [ ] **Step 1: Read current state**

Run: `head -20 CHANGELOG.md` to confirm there is an `## [Unreleased]` section. If not, create one above the topmost release version.

- [ ] **Step 2: Add Fixed entries under `## [Unreleased] / ### Fixed`**

```markdown
### Fixed
- v2 `/sample` endpoint: BigQuery cross-project queries now respect `data_source.bigquery.billing_project` from `instance.yaml` (mirrors v2 `/scan` fix from `33a9964`). Closes #134 for `/sample`.
- v2 `/scan`, `/scan/estimate`, `/sample`, `/schema`: BigQuery upstream errors no longer return bare HTTP 500 with empty body. `Forbidden` from BQ now returns HTTP 502 with structured JSON body (`{"error": "cross_project_forbidden", "message": "...", "details": {"hint": "..."}}`); user-derived `BadRequest` on `/scan*` returns HTTP 400 with `kind=bq_bad_request`. Closes #134.
- v2 `/schema`: best-effort partition-info path (`_fetch_bq_table_options`) preserves its swallow-all behavior unchanged; `/schema` still returns 200 with empty partition info when BQ partition queries fail.
```

- [ ] **Step 3: Stage CHANGELOG and verify Phase 1 working tree is clean**

Run: `git add CHANGELOG.md && git status`
Expected: only `CHANGELOG.md` staged; nothing else dirty.

- [ ] **Step 4: Run full test suite to confirm no regressions before sealing Phase 1**

Run: `pytest tests/ -v -x --ignore=tests/test_telegram --ignore=tests/test_ws_gateway 2>&1 | tail -60`
Expected: all green (or only pre-existing failures unrelated to BQ/v2).

If any new failures appear, fix them before committing — Phase 1 must ship green.

- [ ] **Step 5: Squash Phase 1 commits (intermediate WIP) into a single Phase-1 commit**

```bash
# Identify the first commit in this phase (it has "fix(v2_sample): #134 add billing_project")
git log --oneline | grep "#134" | tail -1
# Note the commit BEFORE that one — call it $BASE
# Soft-reset to $BASE, then make one clean commit
git reset --soft $BASE
git commit -m "fix(v2): #134 structured 502/400 on BQ errors across /scan, /scan/estimate, /sample, /schema

Wraps the BigQuery call sites in v2_scan, v2_sample, and v2_schema (strict
block only) with try/except for google.api_core exceptions, translating to
HTTPException with a structured body shape: {error, message, details}.

Fixes Pavel's report (#134) where these endpoints returned bare HTTP 500
with no body when the SA on <your-dev-instance> hit cross-project Forbidden
on serviceusage.services.use.

Also fixes /sample's missing billing_project fallback (the bug 33a9964
fixed for /scan never landed here).

Body shape matches what the upcoming BqAccess refactor (next commit) will
produce, so client-side parsers see one consistent contract throughout
the staged rollout. _fetch_bq_table_options preserved exactly as-is —
its swallow-all-and-return-empty contract is intentional and survives
into the refactor."
```

This is the final Phase 1 commit (the one that goes into the PR).

---

# Phase 2 — Commit 2: `BqAccess` facade extraction + migration

Goal: extract the `BqAccess` facade and migrate all four call sites to use it. Delete the inline `try/except` blocks from Phase 1; route through `translate_bq_error` instead. Drop `_bq_client_factory` from `RemoteQueryEngine`.

---

### Task 2.1: Create `connectors/bigquery/access.py` skeleton with `BqProjects` + `BqAccessError`

**Files:**
- Create: `connectors/bigquery/access.py`.
- Create: `tests/test_bq_access.py`.

- [ ] **Step 1: Write failing tests for `BqProjects` and `BqAccessError`**

Create `tests/test_bq_access.py`:

```python
"""Tests for connectors/bigquery/access.py — the BqAccess facade."""
import pytest


class TestBqProjects:
    def test_bq_projects_is_frozen_dataclass(self):
        from connectors.bigquery.access import BqProjects
        p = BqProjects(billing="b", data="d")
        assert p.billing == "b"
        assert p.data == "d"
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            p.billing = "other"


class TestBqAccessError:
    def test_carries_kind_message_details(self):
        from connectors.bigquery.access import BqAccessError
        e = BqAccessError("my_kind", "boom", {"foo": "bar"})
        assert e.kind == "my_kind"
        assert e.message == "boom"
        assert e.details == {"foo": "bar"}
        assert str(e) == "boom"

    def test_default_details_is_empty_dict(self):
        from connectors.bigquery.access import BqAccessError
        e = BqAccessError("k", "m")
        assert e.details == {}

    def test_http_status_map_covers_all_kinds(self):
        from connectors.bigquery.access import BqAccessError
        expected = {
            "not_configured": 500,
            "bq_lib_missing": 500,
            "auth_failed": 502,
            "cross_project_forbidden": 502,
            "bq_forbidden": 502,
            "bq_bad_request": 400,
            "bq_upstream_error": 502,
        }
        assert BqAccessError.HTTP_STATUS == expected
```

- [ ] **Step 2: Run — expect FAIL (module doesn't exist)**

Run: `pytest tests/test_bq_access.py -v`
Expected: collection error / ModuleNotFoundError on `connectors.bigquery.access`.

- [ ] **Step 3: Create the module skeleton**

Create `connectors/bigquery/access.py`:

```python
"""Single entry point for BigQuery access — config resolution, client construction,
DuckDB-extension session, and Google-API error translation.

See docs/superpowers/specs/2026-04-29-issue-134-bq-access-unify-design.md for the
full design rationale.
"""
from __future__ import annotations

import functools
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BqProjects:
    """Pair of GCP project IDs used by Agnes.

    `billing` is the project the BQ client bills jobs to (also used as quota_project_id).
    `data` is the default data project for FROM-clause construction. Today equal to
    instance.yaml `data_source.bigquery.project`; locked to a single project per instance
    until table_registry grows a per-table source_project column. See spec "Non-goals".
    """
    billing: str
    data: str


class BqAccessError(Exception):
    """Typed error for BQ access failures.

    `kind` is one of HTTP_STATUS keys; endpoint translation maps it to status codes.
    """

    HTTP_STATUS = {
        "not_configured":          500,  # admin/config bug — page on-call
        "bq_lib_missing":          500,  # deployment bug
        "auth_failed":             502,  # GCP metadata server unreachable
        "cross_project_forbidden": 502,  # SA lacks serviceusage.services.use on billing project
        "bq_forbidden":            502,  # other Forbidden from BQ
        "bq_bad_request":          400,  # 400 from BQ when caller flagged it as client-derived
        "bq_upstream_error":       502,  # all other upstream BQ failures
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_bq_access.py::TestBqProjects tests/test_bq_access.py::TestBqAccessError -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/access.py tests/test_bq_access.py
git commit -m "feat(bq_access): skeleton — BqProjects + BqAccessError"
```

---

### Task 2.2: Add `translate_bq_error` to `connectors/bigquery/access.py`

**Files:**
- Modify: `connectors/bigquery/access.py`.
- Modify: `tests/test_bq_access.py`.

- [ ] **Step 1: Add failing tests for `translate_bq_error`**

Append to `tests/test_bq_access.py`:

```python
class TestTranslateBqError:
    def setup_method(self):
        from connectors.bigquery.access import BqProjects
        self.projects = BqProjects(billing="bill", data="data")

    def test_passes_through_BqAccessError(self):
        """CRITICAL: bq.client() / bq.duckdb_session() raise BqAccessError directly
        for bq_lib_missing / auth_failed. translate_bq_error must pass them through,
        not reclassify as 'unknown' and re-raise."""
        from connectors.bigquery.access import BqAccessError, translate_bq_error
        original = BqAccessError("bq_lib_missing", "no google lib")
        result = translate_bq_error(original, self.projects, bad_request_status="client_error")
        assert result is original

    def test_forbidden_serviceusage_to_cross_project(self):
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error
        e = Forbidden("Permission denied: serviceusage.services.use on project foo")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "cross_project_forbidden"
        assert "billing_project" in result.details
        assert "hint" in result.details

    def test_forbidden_no_serviceusage_to_bq_forbidden(self):
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error
        e = Forbidden("Permission denied on table-level ACL")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_forbidden"

    def test_forbidden_diff_projects_no_serviceusage_still_bq_forbidden(self):
        """billing != data is the NORMAL cross-project setup, not a signal of failure.
        Heuristic must rely on 'serviceusage' substring only."""
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error, BqProjects
        e = Forbidden("Permission denied on table-level ACL")
        result = translate_bq_error(e, BqProjects(billing="b", data="d"),
                                     bad_request_status="client_error")
        assert result.kind == "bq_forbidden"  # NOT cross_project_forbidden

    def test_bad_request_client_error_to_bq_bad_request_400(self):
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error, BqAccessError
        e = BadRequest("Syntax error at line 1")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_bad_request"
        assert BqAccessError.HTTP_STATUS[result.kind] == 400

    def test_bad_request_upstream_error_to_bq_upstream_error_502(self):
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error, BqAccessError
        e = BadRequest("malformed identifier")
        result = translate_bq_error(e, self.projects, bad_request_status="upstream_error")
        assert result.kind == "bq_upstream_error"
        assert BqAccessError.HTTP_STATUS[result.kind] == 502

    def test_other_google_api_error_to_bq_upstream_error(self):
        from google.api_core.exceptions import InternalServerError
        from connectors.bigquery.access import translate_bq_error
        e = InternalServerError("BQ borked")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_upstream_error"

    def test_unknown_exception_reraises(self):
        from connectors.bigquery.access import translate_bq_error
        with pytest.raises(RuntimeError, match="oops"):
            translate_bq_error(RuntimeError("oops"), self.projects,
                               bad_request_status="client_error")
```

- [ ] **Step 2: Run — expect FAIL (function doesn't exist)**

Run: `pytest tests/test_bq_access.py::TestTranslateBqError -v`
Expected: ImportError on `translate_bq_error`.

- [ ] **Step 3: Implement `translate_bq_error`**

Append to `connectors/bigquery/access.py`:

```python
def translate_bq_error(
    e: Exception,
    projects: BqProjects,
    *,
    bad_request_status: Literal["client_error", "upstream_error"],
) -> BqAccessError:
    """Convert Google API exceptions into a typed BqAccessError.

    Mapping (FIRST match wins):
      1. BqAccessError                    -> pass through unchanged (CRITICAL: bq.client()
                                             and bq.duckdb_session() can raise BqAccessError
                                             directly for bq_lib_missing / auth_failed; those
                                             must round-trip without reclassification)
      2. Forbidden + 'serviceusage' in str(e).lower()
                                          -> cross_project_forbidden (with hint)
      3. Forbidden                        -> bq_forbidden
      4. BadRequest, bad_request_status='client_error'
                                          -> bq_bad_request (HTTP 400)
      5. BadRequest, bad_request_status='upstream_error'
                                          -> bq_upstream_error (HTTP 502)
      6. GoogleAPICallError (other)       -> bq_upstream_error
      7. Anything else                    -> RE-RAISED unchanged (don't swallow programmer errors)
    """
    if isinstance(e, BqAccessError):
        return e

    try:
        from google.api_core import exceptions as gax  # type: ignore
    except ImportError:
        # No google lib installed → can't classify Google errors. Re-raise.
        raise e

    msg = str(e)

    if isinstance(e, gax.Forbidden):
        if "serviceusage" in msg.lower():
            return BqAccessError(
                "cross_project_forbidden",
                msg,
                details={
                    "billing_project": projects.billing,
                    "data_project": projects.data,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ),
                },
            )
        return BqAccessError(
            "bq_forbidden",
            msg,
            details={"billing_project": projects.billing, "data_project": projects.data},
        )

    if isinstance(e, gax.BadRequest):
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    if isinstance(e, gax.GoogleAPICallError):
        return BqAccessError("bq_upstream_error", msg)

    # Don't swallow programmer errors / unknown exceptions
    raise e
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_bq_access.py::TestTranslateBqError -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/access.py tests/test_bq_access.py
git commit -m "feat(bq_access): translate_bq_error — typed BqAccessError mapping"
```

---

### Task 2.3: Add `_default_client_factory` and `_default_duckdb_session_factory`

**Files:**
- Modify: `connectors/bigquery/access.py`.
- Modify: `tests/test_bq_access.py`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bq_access.py`:

```python
class TestDefaultClientFactory:
    def test_constructs_client_with_billing_project_as_quota(self, monkeypatch):
        """quota_project_id must be projects.billing, NOT projects.data."""
        from connectors.bigquery.access import _default_client_factory, BqProjects

        captured = {}

        class FakeClientOptions:
            def __init__(self, **kwargs):
                captured["client_options_kwargs"] = kwargs

        class FakeClient:
            def __init__(self, project, client_options):
                captured["project"] = project
                captured["client_options"] = client_options

        import google.cloud.bigquery as bq_mod
        import google.api_core.client_options as co_mod
        monkeypatch.setattr(bq_mod, "Client", FakeClient)
        monkeypatch.setattr(co_mod, "ClientOptions", FakeClientOptions)

        _default_client_factory(BqProjects(billing="bill", data="data"))

        assert captured["project"] == "bill"
        assert captured["client_options_kwargs"]["quota_project_id"] == "bill"

    def test_raises_bq_lib_missing_on_importerror(self, monkeypatch):
        """If google-cloud-bigquery is not installed, raise BqAccessError, not ImportError."""
        from connectors.bigquery.access import _default_client_factory, BqProjects, BqAccessError
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "google.cloud" or name.startswith("google.cloud.bigquery"):
                raise ImportError("no google-cloud-bigquery")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(BqAccessError) as exc_info:
            _default_client_factory(BqProjects(billing="b", data="d"))
        assert exc_info.value.kind == "bq_lib_missing"


class TestDefaultDuckdbSessionFactory:
    def test_yields_duckdb_conn_with_secret_then_closes(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        executed_sql = []

        class FakeConn:
            def __init__(self):
                self.closed = False
            def execute(self, sql, params=None):
                executed_sql.append((sql, params))
                return self
            def close(self):
                self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr("duckdb.connect", lambda _: fake_conn)
        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", lambda: "tok123")

        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn:
            assert conn is fake_conn
        assert fake_conn.closed is True

        # Verify INSTALL/LOAD/SECRET sequence ran
        assert any("INSTALL bigquery" in sql for sql, _ in executed_sql)
        assert any("LOAD bigquery" in sql for sql, _ in executed_sql)
        assert any("CREATE OR REPLACE SECRET" in sql and "tok123" in sql for sql, _ in executed_sql)

    def test_closes_on_exception_inside_with_block(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        class FakeConn:
            closed = False
            def execute(self, *a, **kw): return self
            def close(self): self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr("duckdb.connect", lambda _: fake_conn)
        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", lambda: "t")

        with pytest.raises(RuntimeError, match="boom"):
            with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn:
                raise RuntimeError("boom")
        assert fake_conn.closed is True

    def test_translates_metadata_auth_error_to_auth_failed(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects, BqAccessError
        from connectors.bigquery.auth import BQMetadataAuthError

        def fail():
            raise BQMetadataAuthError("metadata server unreachable")

        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", fail)

        with pytest.raises(BqAccessError) as exc_info:
            with _default_duckdb_session_factory(BqProjects(billing="b", data="d")):
                pass
        assert exc_info.value.kind == "auth_failed"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_bq_access.py::TestDefaultClientFactory tests/test_bq_access.py::TestDefaultDuckdbSessionFactory -v`
Expected: FAIL — factories don't exist.

- [ ] **Step 3: Implement the two default factories**

Append to `connectors/bigquery/access.py`:

```python
def _default_client_factory(projects: BqProjects):
    """Real BigQuery client construction. Raises BqAccessError on import / config issues."""
    try:
        from google.cloud import bigquery  # type: ignore
        from google.api_core.client_options import ClientOptions  # type: ignore
    except ImportError as e:
        raise BqAccessError(
            "bq_lib_missing",
            "google-cloud-bigquery is not installed",
            details={"original": str(e)},
        )

    return bigquery.Client(
        project=projects.billing,
        client_options=ClientOptions(quota_project_id=projects.billing),
    )


@contextmanager
def _default_duckdb_session_factory(projects: BqProjects):
    """Yield an in-memory DuckDB conn with bigquery extension loaded + SECRET set
    from get_metadata_token(). Auto-cleanup. Translates auth/install failures
    to BqAccessError(kind='auth_failed' or 'bq_lib_missing').

    Note: `projects.billing` is not used by this factory directly — bigquery_query()
    callers pass it themselves as the first positional arg to identify the billing
    project. The factory keeps the parameter for symmetry with _default_client_factory.
    """
    import duckdb  # type: ignore
    from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError

    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        raise BqAccessError(
            "auth_failed",
            f"could not fetch GCP metadata token: {e}",
            details={"original": str(e)},
        )

    conn = duckdb.connect(":memory:")
    try:
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            escaped = token.replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')"
            )
        except Exception as e:
            raise BqAccessError(
                "bq_lib_missing",
                f"failed to install/load BigQuery DuckDB extension: {e}",
                details={"original": str(e)},
            )
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_bq_access.py::TestDefaultClientFactory tests/test_bq_access.py::TestDefaultDuckdbSessionFactory -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/access.py tests/test_bq_access.py
git commit -m "feat(bq_access): default factories for client + duckdb_session"
```

---

### Task 2.4: Add `BqAccess` class

**Files:**
- Modify: `connectors/bigquery/access.py`.
- Modify: `tests/test_bq_access.py`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bq_access.py`:

```python
class TestBqAccess:
    def test_uses_default_factories_when_none_passed(self, monkeypatch):
        from connectors.bigquery.access import BqAccess, BqProjects

        captured = []
        monkeypatch.setattr(
            "connectors.bigquery.access._default_client_factory",
            lambda projects: captured.append(("client", projects)) or "FAKE_CLIENT",
        )
        bq = BqAccess(BqProjects(billing="b", data="d"))
        assert bq.client() == "FAKE_CLIENT"
        assert captured == [("client", BqProjects(billing="b", data="d"))]

    def test_injected_client_factory_overrides_default(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        bq = BqAccess(
            BqProjects(billing="b", data="d"),
            client_factory=lambda projects: "MOCK_CLIENT",
        )
        assert bq.client() == "MOCK_CLIENT"

    def test_injected_duckdb_session_factory_overrides_default(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        from contextlib import contextmanager

        @contextmanager
        def fake_session(projects):
            yield "FAKE_CONN"

        bq = BqAccess(
            BqProjects(billing="b", data="d"),
            duckdb_session_factory=fake_session,
        )
        with bq.duckdb_session() as conn:
            assert conn == "FAKE_CONN"

    def test_projects_property(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        p = BqProjects(billing="b", data="d")
        bq = BqAccess(p)
        assert bq.projects is p
```

- [ ] **Step 2: Run — expect FAIL (BqAccess class doesn't exist)**

Run: `pytest tests/test_bq_access.py::TestBqAccess -v`
Expected: AttributeError on `BqAccess`.

- [ ] **Step 3: Implement `BqAccess`**

Append to `connectors/bigquery/access.py`:

```python
class BqAccess:
    """Single entry point for BigQuery access. Stateless after construction.

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
        client_factory: Callable[[BqProjects], object] | None = None,
        duckdb_session_factory: Callable[[BqProjects], object] | None = None,
    ):
        self._projects = projects
        self._client_factory = client_factory or _default_client_factory
        self._duckdb_session_factory = duckdb_session_factory or _default_duckdb_session_factory

    @property
    def projects(self) -> BqProjects:
        return self._projects

    def client(self):
        """Construct (or retrieve from injected factory) a BigQuery client."""
        return self._client_factory(self._projects)

    @contextmanager
    def duckdb_session(self) -> Iterator[object]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set."""
        with self._duckdb_session_factory(self._projects) as conn:
            yield conn
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_bq_access.py::TestBqAccess -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/access.py tests/test_bq_access.py
git commit -m "feat(bq_access): BqAccess facade with injectable factories"
```

---

### Task 2.5: Add `get_bq_access` (module-level cached entry point)

**Files:**
- Modify: `connectors/bigquery/access.py`.
- Modify: `tests/test_bq_access.py`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bq_access.py`:

```python
class TestGetBqAccess:
    def setup_method(self):
        # Clear the cache between tests
        from connectors.bigquery.access import get_bq_access
        get_bq_access.cache_clear()

    def test_env_var_wins(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.setenv("BIGQUERY_PROJECT", "env-proj")
        bq = get_bq_access()
        assert bq.projects.billing == "env-proj"
        assert bq.projects.data == "env-proj"

    def test_billing_project_from_yaml_when_no_env(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "billing_project"): "yaml-bill",
                ("data_source", "bigquery", "project"): "yaml-data",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        bq = get_bq_access()
        assert bq.projects.billing == "yaml-bill"
        assert bq.projects.data == "yaml-data"

    def test_billing_falls_back_to_project_when_no_billing(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "project"): "yaml-data",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        bq = get_bq_access()
        assert bq.projects.billing == "yaml-data"
        assert bq.projects.data == "yaml-data"

    def test_raises_not_configured_when_neither_set(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access, BqAccessError
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default="": default)

        with pytest.raises(BqAccessError) as exc_info:
            get_bq_access()
        assert exc_info.value.kind == "not_configured"
        assert "billing_project" in exc_info.value.details["hint"].lower() or \
               "project" in exc_info.value.details["hint"].lower()

    def test_is_cached(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.setenv("BIGQUERY_PROJECT", "p")
        a = get_bq_access()
        b = get_bq_access()
        assert a is b

    def test_does_not_cache_exceptions(self, monkeypatch):
        """functools.cache does not cache exceptions — config can be fixed and retried."""
        from connectors.bigquery.access import get_bq_access, BqAccessError
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default="": default)

        with pytest.raises(BqAccessError):
            get_bq_access()

        # Now "fix" the config
        monkeypatch.setenv("BIGQUERY_PROJECT", "p")
        bq = get_bq_access()
        assert bq.projects.billing == "p"
```

- [ ] **Step 2: Run — expect FAIL (function doesn't exist)**

Run: `pytest tests/test_bq_access.py::TestGetBqAccess -v`
Expected: ImportError on `get_bq_access`.

- [ ] **Step 3: Implement `get_bq_access`**

Append to `connectors/bigquery/access.py`:

```python
@functools.cache
def get_bq_access() -> BqAccess:
    """Module-level FastAPI Depends target. Resolves projects from config and returns
    a BqAccess instance with default factories.

    Resolution order:
      1. BIGQUERY_PROJECT env var → both billing + data (legacy override)
      2. instance.yaml data_source.bigquery.billing_project → billing
      3. instance.yaml data_source.bigquery.project → data, and billing if (2) is unset

    Process-cached. Hot-reload of instance.yaml is out of scope; restart the container
    on config change. functools.cache does NOT cache exceptions, so a failed call is
    retried on the next invocation.

    Tests inject via `app.dependency_overrides[get_bq_access] = lambda: bq` for
    endpoints, or construct `BqAccess(...)` directly for non-endpoint code.

    Module-level (not a classmethod) to avoid the @classmethod + @functools.cache
    stacking footgun and to give FastAPI's dependency introspection a clean signature.
    """
    import os

    env_project = os.environ.get("BIGQUERY_PROJECT", "").strip()
    if env_project:
        return BqAccess(BqProjects(billing=env_project, data=env_project))

    from app.instance_config import get_value
    billing = (get_value("data_source", "bigquery", "billing_project", default="") or "").strip()
    data = (get_value("data_source", "bigquery", "project", default="") or "").strip()

    if not data:
        raise BqAccessError(
            "not_configured",
            "BigQuery project not configured",
            details={
                "hint": (
                    "Set data_source.bigquery.project in instance.yaml "
                    "(and optionally data_source.bigquery.billing_project for cross-project "
                    "deployments). BIGQUERY_PROJECT env var also accepted as legacy override."
                ),
            },
        )

    if not billing:
        billing = data

    return BqAccess(BqProjects(billing=billing, data=data))
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_bq_access.py::TestGetBqAccess -v`
Expected: all green.

- [ ] **Step 5: Run the WHOLE bq_access test file as a sanity check**

Run: `pytest tests/test_bq_access.py -v`
Expected: every test green.

- [ ] **Step 6: Commit**

```bash
git add connectors/bigquery/access.py tests/test_bq_access.py
git commit -m "feat(bq_access): get_bq_access — cached module-level entry point"
```

---

### Task 2.6: Add `bq_access` fixture to `tests/conftest.py`

**Files:**
- Modify: `tests/conftest.py`.

- [ ] **Step 1: Add fixture without test (it's infrastructure for later tasks)**

Append to `tests/conftest.py`:

```python
import contextlib as _contextlib


@pytest.fixture
def bq_access():
    """Build a BqAccess with pluggable factories and override the FastAPI Depends.

    Usage:
        def test_x(bq_access):
            mock_client = MagicMock()
            bq = bq_access(client=mock_client)
            # endpoint test code

    Override is auto-cleared on fixture teardown.

    NOTE: `contextlib.nullcontext(duckdb_conn)` does NOT close the conn on exit.
    The production path closes via _default_duckdb_session_factory. Tests that
    care about close behavior should use that factory directly (see
    tests/test_bq_access.py::TestDefaultDuckdbSessionFactory).
    """
    from connectors.bigquery.access import BqAccess, BqProjects, get_bq_access
    from app.main import app

    def _build(*, client=None, duckdb_conn=None,
               billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client is not None else None,
            duckdb_session_factory=(
                lambda projects: _contextlib.nullcontext(duckdb_conn)
            ) if duckdb_conn is not None else None,
        )
        app.dependency_overrides[get_bq_access] = lambda: bq
        return bq

    yield _build
    from app.main import app as _app
    _app.dependency_overrides.pop(get_bq_access, None)
```

(Note: import path for `app` may need adjustment depending on how tests import it; match the existing pattern.)

- [ ] **Step 2: Verify fixture loads (no test, just collection)**

Run: `pytest --collect-only tests/conftest.py 2>&1 | tail -5`
Expected: no collection errors.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add bq_access fixture for FastAPI dep override"
```

---

### Task 2.7: Migrate `app/api/v2_scan.py` to `BqAccess`

**Files:**
- Modify: `app/api/v2_scan.py`.
- Modify: `tests/test_v2_scan.py`, `tests/test_v2_scan_estimate.py` (update lambda signatures + use new fixture).

- [ ] **Step 1: Read current state to confirm Phase-1 try/except blocks are still in place**

Run: `sed -n '43,90p' app/api/v2_scan.py`
Expected: `_bq_dry_run_bytes` has the inline try/except added in Task 1.2.

- [ ] **Step 2: Update `_bq_dry_run_bytes` signature and body**

Replace `app/api/v2_scan.py:43-95` (the entire `_bq_dry_run_bytes` from Task 1.2):

```python
def _bq_dry_run_bytes(bq, sql: str) -> int:
    """Run a BQ dry-run via the google-cloud-bigquery client and return totalBytesProcessed."""
    from google.cloud import bigquery
    from connectors.bigquery.access import translate_bq_error

    client = bq.client()  # raises BqAccessError(bq_lib_missing/auth_failed) — propagates as-is
    try:
        job = client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects, bad_request_status="client_error")
```

- [ ] **Step 3: Update `_run_bq_scan` similarly**

Replace `app/api/v2_scan.py:266-...` (the entire `_run_bq_scan` from Task 1.3):

```python
def _run_bq_scan(bq, sql: str):
    """Run a BQ query via DuckDB BQ extension. Returns Arrow table."""
    from connectors.bigquery.access import translate_bq_error

    with bq.duckdb_session() as conn:
        try:
            return conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, sql],
            ).arrow()
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="client_error")
```

- [ ] **Step 4: Update `estimate` function signature and call site**

In `app/api/v2_scan.py:135-...` (`def estimate(...)`):

- Remove `project_id: str, billing_project: str | None = None` parameters.
- Add `bq` parameter.
- Inside: replace `_bq_dry_run_bytes(billing_project or project_id, bq_sql)` with `_bq_dry_run_bytes(bq, bq_sql)`.
- Replace `_build_bq_sql(row, project_id, req)` with `_build_bq_sql(row, bq.projects.data, req)`.
- Replace `_resolve_schema(conn, user, req.table_id, project_id)` with `_resolve_schema(conn, user, req.table_id, bq.projects.data)`.

- [ ] **Step 5: Update `run_scan` function similarly**

Same pattern: drop `project_id`, `billing_project` params; add `bq`. Replace internal references.

- [ ] **Step 6: Update `scan_endpoint` and `scan_estimate_endpoint`**

Replace lines `app/api/v2_scan.py:217-...` (`scan_estimate_endpoint`) and `:378-...` (`scan_endpoint`):

```python
@router.post("/scan/estimate")
async def scan_estimate_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    try:
        return estimate(conn, user, raw, bq=bq)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}})
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"table {e!s} not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )


@router.post("/scan")
async def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    quota = _build_quota_tracker()
    try:
        ipc = run_scan(conn, user, raw, bq=bq, quota=quota)
        return Response(content=ipc, media_type=CONTENT_TYPE)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}})
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail={"error": "quota_exceeded", **(e.detail or {})})
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"table {e!s} not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
```

Add at top of `app/api/v2_scan.py`:

```python
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access
```

- [ ] **Step 7: Update existing tests in `tests/test_v2_scan.py` and `tests/test_v2_scan_estimate.py` for the new signature**

Tests that previously did `monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", lambda project, sql: ...)` now need `lambda bq, sql: ...`. Same for `_run_bq_scan`. Trace through all such monkeypatches and update.

The Forbidden / BadRequest tests from Task 1.2 / 1.3 also need updating: instead of patching `_bq_dry_run_bytes` to raise, you can either (a) keep patching that function (now with new signature), or (b) use the new `bq_access` fixture to inject a client that raises. Pattern (b) is cleaner long-term:

```python
def test_scan_estimate_returns_502_on_bq_forbidden_serviceusage(reload_db, bq_access):
    from google.api_core.exceptions import Forbidden
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_client.query.side_effect = Forbidden("Permission denied: serviceusage.services.use on project foo")
    bq_access(client=mock_client)

    # ... call POST /api/v2/scan/estimate
    # Assert response.status_code == 502
    # Assert response.json()["detail"]["error"] == "cross_project_forbidden"
```

- [ ] **Step 8: Run v2_scan tests — expect PASS**

Run: `pytest tests/test_v2_scan.py tests/test_v2_scan_estimate.py -v`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add app/api/v2_scan.py tests/test_v2_scan.py tests/test_v2_scan_estimate.py
git commit -m "refactor(v2_scan): #134 migrate to BqAccess facade"
```

---

### Task 2.8: Migrate `app/api/v2_sample.py` to `BqAccess`

**Files:**
- Modify: `app/api/v2_sample.py`.
- Modify: `tests/test_v2_sample.py`.

- [ ] **Step 1: Update `_fetch_bq_sample` signature and body**

Replace the entire function (added in Task 1.1):

```python
def _fetch_bq_sample(bq, dataset: str, table: str, n: int) -> list[dict]:
    from connectors.bigquery.access import translate_bq_error
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

- [ ] **Step 2: Update `build_sample` signature**

Replace `def build_sample(conn, user, table_id, *, n, project_id):` with:

```python
def build_sample(conn, user, table_id, *, n, bq):
```

Inside, replace `_fetch_bq_sample(project_id, ...)` with `_fetch_bq_sample(bq, ...)`. Local-source (parquet) path unchanged.

- [ ] **Step 3: Update `sample` endpoint**

Replace `app/api/v2_sample.py:97-110`:

```python
@router.get("/sample/{table_id}")
async def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    try:
        return build_sample(conn, user, table_id, n=n, bq=bq)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsafe_identifier", "message": str(e), "details": {}},
        )
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
```

Add at top:

```python
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access
```

- [ ] **Step 4: Update tests in `tests/test_v2_sample.py`**

The Phase-1 tests patched `_fetch_bq_sample` with a 4-arg lambda `(project, dataset, table, n)`. Update all such patches to `(bq, dataset, table, n)`. Convert the Forbidden test to use `bq_access` fixture (matches pattern from Task 2.7 step 7).

- [ ] **Step 5: Run — expect PASS**

Run: `pytest tests/test_v2_sample.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/api/v2_sample.py tests/test_v2_sample.py
git commit -m "refactor(v2_sample): #134 migrate to BqAccess facade"
```

---

### Task 2.9: Migrate `app/api/v2_schema.py` (both blocks, preserve best-effort semantics)

**Files:**
- Modify: `app/api/v2_schema.py`.
- Modify: `tests/test_v2_schema.py`.

- [ ] **Step 1: Update `_fetch_bq_schema` (strict block) — replace Phase-1 inline try/except with translate_bq_error**

Replace `app/api/v2_schema.py:36-...` (the `_fetch_bq_schema` function). Body becomes:

```python
def _fetch_bq_schema(bq, dataset: str, table: str) -> list[dict]:
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry")

    bq_sql = (
        f"SELECT column_name, data_type FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? "
        f"ORDER BY ordinal_position"
    )
    with bq.duckdb_session() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")
    return [{"name": r[0], "type": r[1]} for r in rows]
```

(Adjust the SELECT shape to match what the function returns today; this sketch preserves the {name, type} shape of the existing function.)

- [ ] **Step 2: Update `_fetch_bq_table_options` (best-effort block) — preserve swallow-all**

Replace `app/api/v2_schema.py:90-129`:

```python
def _fetch_bq_table_options(bq, dataset: str, table: str) -> dict:
    """Best-effort partition/cluster info. Returns {} on ANY failure (preserved
    from pre-refactor; /schema endpoint must keep returning successfully even
    when partition queries fail)."""
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
        logger.warning(
            "BQ table options fetch failed for %s.%s.%s: %s",
            bq.projects.data, dataset, table, e,
        )
        return {}
```

**Note**: the outer `try/except Exception → return {}` is the load-bearing contract preserved from today.

- [ ] **Step 3: Update `build_schema` and `schema` endpoint signatures**

`build_schema(conn, user, table_id, *, project_id)` → `build_schema(conn, user, table_id, *, bq)`.
Inside: `_fetch_bq_schema(project_id, dataset, table)` → `_fetch_bq_schema(bq, dataset, table)`. Same for `_fetch_bq_table_options`.

`schema` endpoint (`app/api/v2_schema.py:...`):

```python
@router.get("/schema/{table_id}")
async def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    try:
        return build_schema(conn, user, table_id, bq=bq)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsafe_identifier", "message": str(e), "details": {}},
        )
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
```

Add at top:

```python
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access
```

- [ ] **Step 4: Update tests in `tests/test_v2_schema.py`**

Update `_fetch_bq_schema` and `_fetch_bq_table_options` monkeypatches to new signatures `(bq, dataset, table)`. The Phase-1 regression-guard test for partition-info failure should still pass (the contract is preserved).

- [ ] **Step 5: Run — expect PASS**

Run: `pytest tests/test_v2_schema.py -v`
Expected: all green, including the regression guard.

- [ ] **Step 6: Commit**

```bash
git add app/api/v2_schema.py tests/test_v2_schema.py
git commit -m "refactor(v2_schema): #134 migrate to BqAccess (strict + best-effort blocks)"
```

---

### Task 2.10: Migrate `src/remote_query.py` — lazy `bq_access`, drop `_bq_client_factory`

**Files:**
- Modify: `src/remote_query.py`.

- [ ] **Step 1: Read current state of `__init__` and `_get_bq_client`**

Run: `sed -n '195,230p' src/remote_query.py && echo '---' && sed -n '407,455p' src/remote_query.py`
Expected: see today's `_bq_client_factory` parameter and the fallback chain in `_get_bq_client`.

- [ ] **Step 2: Update `__init__` signature**

In `src/remote_query.py` `RemoteQueryEngine.__init__`:

- Remove `_bq_client_factory=None` parameter.
- Add `bq_access: "BqAccess | None" = None` parameter.
- Add `self._bq = bq_access` line in body.
- Remove `self._bq_client_factory = _bq_client_factory` line.
- Update the docstring to remove the old factory reference (the stale line 204 about `scripts.duckdb_manager._create_bq_client`).

- [ ] **Step 3: Replace `_get_bq_client`**

Delete the existing `_get_bq_client` (lines ~407-450) and replace with:

```python
def _get_bq_client(self):
    """Lazy-resolve BqAccess on first use. Many tests construct RemoteQueryEngine
    for DuckDB-only paths and never touch BQ — those must not fail with not_configured."""
    if self._bq is None:
        from connectors.bigquery.access import get_bq_access
        self._bq = get_bq_access()  # may raise BqAccessError; that's fine
    return self._bq.client()
```

- [ ] **Step 4: Run existing remote_query tests — expect MOSTLY PASS, with failures in tests that injected `_bq_client_factory`**

Run: `pytest tests/test_remote_query.py -v 2>&1 | tail -40`
Expected: ~10 failures from tests that pass `_bq_client_factory=...` (now an unknown kwarg) or that mocked the old factory factory chain.

- [ ] **Step 5: DO NOT commit yet** — Task 2.11 fixes the tests. Both must land together.

---

### Task 2.11: Migrate `tests/test_remote_query.py` from `_bq_client_factory` to `bq_access` injection

**Files:**
- Modify: `tests/test_remote_query.py`.

- [ ] **Step 1: Find all `_bq_client_factory` usages**

Run: `grep -n "_bq_client_factory" tests/test_remote_query.py`
Note: the spec mentions ~12+ call sites; expect roughly that count.

- [ ] **Step 2: For each occurrence, migrate to `bq_access=BqAccess(..., client_factory=...)`**

Pattern:

```python
# Before
engine = RemoteQueryEngine(conn, _bq_client_factory=lambda project: mock_client)

# After
from connectors.bigquery.access import BqAccess, BqProjects
bq = BqAccess(
    BqProjects(billing="test-billing", data="test-data"),
    client_factory=lambda projects: mock_client,
)
engine = RemoteQueryEngine(conn, bq_access=bq)
```

For tests where the factory was `lambda project: ...` and used `project` argument: now it's `lambda projects: ...` (a `BqProjects` instance). If the test asserted on the project argument, change to `assert call.projects.billing == "expected-project"`.

- [ ] **Step 3: DuckDB-only tests need NO change**

Tests like `RemoteQueryEngine(analytics_conn)` (no factory) at lines 106, 148, 188, 196, 417, 520, 529, 538: these now have `bq_access=None` by default and don't trigger `get_bq_access()` because they never call `_get_bq_client`. Confirm these pass without modification.

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_remote_query.py -v`
Expected: all green.

- [ ] **Step 5: Commit Tasks 2.10 + 2.11 together**

```bash
git add src/remote_query.py tests/test_remote_query.py
git commit -m "refactor(remote_query): #134 lazy BqAccess injection, drop _bq_client_factory

RemoteQueryEngine.__init__ no longer accepts _bq_client_factory. Tests
migrate to bq_access=BqAccess(projects, client_factory=...) injection.
DuckDB-only tests that never touch BQ continue to work without changes
because bq_access defaults to None and get_bq_access() is only invoked
on first BQ call (lazy resolution).

Drops the stale docstring at the previous src/remote_query.py:204
that referenced scripts.duckdb_manager._create_bq_client as the default
factory — RemoteQueryEngine never actually used that function.

CLI (cli/commands/query.py) is unaffected: it never injected the factory,
and the new bq_access kwarg's None default routes through get_bq_access()
on first BQ call — matching today's eager behavior for the CLI path."
```

---

### Task 2.12: CHANGELOG entries for Phase 2

**Files:** `CHANGELOG.md`.

- [ ] **Step 1: Add `### Changed` BREAKING entry under `## [Unreleased]`**

Add immediately below the `### Fixed` block from Phase 1:

```markdown
### Changed
- **BREAKING for deployments using `BIGQUERY_PROJECT` env var alongside `data_source.bigquery.project` in `instance.yaml`.** The env var now sets BOTH billing and data project, overriding `data_source.bigquery.project` for FROM-clause construction in `v2_scan` / `v2_sample` / `v2_schema`. Migrate by clearing `BIGQUERY_PROJECT` and using `data_source.bigquery.billing_project` + `data_source.bigquery.project` in `instance.yaml`. (Previously `BIGQUERY_PROJECT` only affected `RemoteQueryEngine` billing.)
```

- [ ] **Step 2: Add `### Internal` entries**

```markdown
### Internal
- New shared module `connectors/bigquery/access.py` — `BqAccess` facade unifies BQ project resolution, client construction, DuckDB-extension session, and Google-API error translation across `v2_scan`, `v2_sample`, `v2_schema`, and `RemoteQueryEngine`. Replaces four duplicate code paths with one.
- **Internal API change:** `RemoteQueryEngine.__init__` no longer accepts `_bq_client_factory`. Callers that injected it migrate to `RemoteQueryEngine(..., bq_access=BqAccess(projects, client_factory=...))`. The CLI (`cli/commands/query.py`) is unaffected — it never injected the factory and the new `bq_access` kwarg defaults to `None` (lazy `get_bq_access()` on first BQ call).
- Removed stale docstring in `src/remote_query.py` referencing `scripts.duckdb_manager._create_bq_client` as the default BQ client factory (the engine never actually used that function).
- Two known-duplicate BQ-access sites (`connectors/bigquery/extractor.py`, `scripts/duckdb_manager.register_bq_table`) explicitly out of scope for this PR; tracked as follow-up.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): #134 Phase 2 BqAccess refactor — Changed BREAKING + Internal"
```

---

# Phase 3 — Verification

### Task 3.1: Run full test suite

- [ ] **Step 1: Run all tests**

Run:
```bash
pytest tests/ -v --tb=short 2>&1 | tee /tmp/test-output-issue-134.log
```

Expected: every test passes. If failures appear that aren't in BQ/v2 paths, investigate — they may be flaky tests unrelated to this PR; document any pre-existing failures.

- [ ] **Step 2: Run linters / type checks if the repo enforces them**

Run: `ruff check connectors/bigquery/access.py app/api/v2_*.py src/remote_query.py tests/test_bq_access.py 2>&1 | head -30`
Expected: no new lint errors introduced.

Run: `mypy connectors/bigquery/access.py 2>&1 | head -30`
Expected: no new type errors. (CI may run with `continue-on-error` per repo convention — match that.)

- [ ] **Step 3: If any failures, fix them before proceeding**

Do NOT proceed to E2E with red CI.

---

### Task 3.2: Squash dev commits into final PR shape (two commits)

The intermediate commits during development can be squashed into the two PR-shape commits that the spec calls for.

- [ ] **Step 1: Identify the boundary**

Phase 1 ended with the squash commit `fix(v2): #134 structured 502/400 on BQ errors across /scan, /scan/estimate, /sample, /schema`. Find its SHA:

```bash
git log --oneline | grep "structured 502/400 on BQ errors across"
```

Note this SHA — call it `$PHASE1`.

- [ ] **Step 2: Soft-reset everything after `$PHASE1` to staging**

```bash
git reset --soft $PHASE1
git status   # expect a large pile of staged changes representing Phase 2
```

- [ ] **Step 3: Make the single Phase 2 commit**

```bash
git commit -m "refactor(bq): #134 BqAccess facade — unify v2_scan, v2_sample, v2_schema, RemoteQueryEngine

Extracts the duplicated BigQuery-access pattern (project resolution +
client construction + DuckDB-extension session + Google-API error
translation) into connectors/bigquery/access.py. Migrates four
call sites to use it:

- app/api/v2_scan.py — _bq_dry_run_bytes, _run_bq_scan
- app/api/v2_sample.py — _fetch_bq_sample
- app/api/v2_schema.py — _fetch_bq_schema (strict translation),
  _fetch_bq_table_options (preserves swallow-all best-effort contract)
- src/remote_query.py — RemoteQueryEngine, lazy bq_access kwarg

Removes _bq_client_factory parameter from RemoteQueryEngine.__init__
and the stale docstring referencing scripts.duckdb_manager._create_bq_client.
Tests migrate from _bq_client_factory injection to
bq_access=BqAccess(projects, client_factory=...) injection. DuckDB-only
RemoteQueryEngine tests need no changes (lazy resolution skips
get_bq_access() when bq_access is None and BQ is never touched).

BREAKING for deployments combining BIGQUERY_PROJECT env var with
data_source.bigquery.project in instance.yaml — the env var now
overrides data project too. See CHANGELOG.

Two known-duplicate BQ-access sites (connectors/bigquery/extractor.py,
scripts/duckdb_manager.register_bq_table) explicitly out of scope;
tracked as follow-up."
```

- [ ] **Step 4: Verify final shape**

Run: `git log --oneline | head -10`
Expected: top two commits are exactly the two intended for the PR (one fix(v2), one refactor(bq)).

---

### Task 3.3: E2E verification on `<your-dev-instance>`

This task happens AFTER the PR is merged and deployed to `<your-dev-instance>`. Per the spec, this is the success criterion for closing #134 — without it, "fixed" is unverifiable.

**Prerequisite:** `<your-dev-instance>` is running the PR's image; reproduce the PAT used in Pavel's report.

- [ ] **Step 1: Pre-config baseline (BEFORE setting `billing_project`)**

```bash
PAT=...

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
```

Expected (per spec § E2E manual verification):
- `/sample`, `/scan`, `/scan/estimate`: HTTP 502 with JSON body containing `error=cross_project_forbidden` + `details.hint` mentioning `billing_project`.
- `/schema`: HTTP 200 (INFORMATION_SCHEMA queries don't trip serviceusage; only fails if something else breaks the strict block).

- [ ] **Step 2: Operator action — set `billing_project` and restart**

In `instance.yaml` on the VM:

```yaml
data_source:
  bigquery:
    project: <your-data-project>        # data project, unchanged
    billing_project: <project-where-SA-can-bill>   # NEW
```

Restart the container.

- [ ] **Step 3: Post-config verification**

Repeat the four curls from Step 1.

Expected: HTTP 200 on all four, with valid response bodies.

- [ ] **Step 4: Document and close #134**

Add a comment to issue #134 with:
- The four curl invocations + their pre/post status codes.
- Confirmation that the structured body shape is what was specced.
- Note any unexpected behavior.

Close the issue.

---

## Self-review (against the spec)

| Spec section | Plan task |
|---|---|
| Bug A — `v2_scan` missing try/except | Tasks 1.2, 1.3 + 2.7 |
| Bug B — `v2_sample` missing billing_project + try/except | Tasks 1.1 + 2.8 |
| Bug C — `v2_schema` (two blocks, two semantics) | Tasks 1.4 + 2.9 |
| `BqProjects` + `BqAccessError` | Task 2.1 |
| `translate_bq_error` (incl. BqAccessError pass-through) | Task 2.2 |
| `_default_client_factory`, `_default_duckdb_session_factory` | Task 2.3 |
| `BqAccess` class with injectable factories | Task 2.4 |
| `get_bq_access` module-level cached | Task 2.5 |
| `bq_access` fixture in conftest | Task 2.6 |
| RemoteQueryEngine lazy bq_access; drop `_bq_client_factory` | Tasks 2.10 + 2.11 |
| CHANGELOG (Fixed + Changed BREAKING + Internal) | Tasks 1.5 + 2.12 |
| Two-commit PR shape | Task 3.2 |
| E2E verification protocol | Task 3.3 |
| Cross-project Forbidden heuristic ('serviceusage' only) | Task 2.2 (`test_forbidden_diff_projects_no_serviceusage_still_bq_forbidden`) |
| Status code split (400/502/500) | Task 2.1 (`test_http_status_map_covers_all_kinds`) |
| `_fetch_bq_table_options` swallow-all preserved | Task 2.9 + Task 1.4 regression guard |

All spec sections have at least one task implementing them. No placeholders in the plan body.

## Final notes

- Each task's tests come BEFORE the implementation per TDD. Run the failing test first, then implement, then verify pass.
- Intermediate commits during dev are fine — Task 3.2 squashes them into the two PR-shape commits.
- The plan stays in the worktree (`../tmp_oss-134-bq-access`, branch `fix/134-bq-access-unify`). All commits land there. Push to GitHub when ready for PR.
- If a task's TDD step finds an issue with the spec's design, STOP and update the spec before proceeding. Don't paper over design holes in implementation.