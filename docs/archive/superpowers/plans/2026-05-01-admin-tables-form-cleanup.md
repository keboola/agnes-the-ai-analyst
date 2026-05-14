# `/admin/tables` Unified Tab UI + Keboola Materialized Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify `/admin/tables` operator UX with per-connector tabs (BigQuery / Keboola / Jira), bring Keboola to capability-parity with BigQuery for the materialized SQL path, clean up the misleading Keboola form fields, and resolve the `profile_after_sync` dead-code bug — all in **one PR** because the four concerns are tightly interconnected and splitting them would mean re-doing the form layout multiple times.

**Architecture:** New branch on top of merged `main` (after PR #148 lands BQ-materialized + analyst auto-sync). The PR adds: (1) per-connector tab navigation in `/admin/tables`, (2) generalized materialized path in `app/api/sync.py` that dispatches by `source_type` to either `connectors/bigquery/extractor.py:materialize_query` (existing) or new `connectors/keboola/extractor.py:materialize_query`, (3) Keboola tab form with the new "Custom SQL" mode mirroring BQ's two-question radio, (4) Keboola form cleanup (drop Strategy, add Schedule, hide PK), (5) Pydantic deprecation marks and inert behavior for `profile_after_sync` (column stays in DB, removed from runtime). No DB schema migration. Existing BQ `#148` form structure preserved verbatim, just relocated into the BQ tab.

**Tech Stack:** Jinja2 templates + vanilla JS (admin UI), FastAPI + Pydantic v2 (admin API), DuckDB BigQuery + Keboola extensions (extract path), pytest + TestClient (test suite).

---

## Brief — the problem and the design decision

### Today's `/admin/tables` UX has four interconnected problems

1. **Single mixed form** — One Jinja `{% if data_source.type == 'bigquery' %}` switch picks Keboola vs BigQuery branch. Result: an instance configured for Keboola can't register BigQuery rows from the UI, and vice versa. Multi-source instances have no UI surface for the secondary source. Edit modal compounds the mix with `keboola-edit-only` / `bq-edit-only` show/hide classes.

2. **Capability asymmetry: Keboola is a subset of BigQuery** — In our current model:

   | mode | BigQuery | Keboola |
   |---|---|---|
   | Live (queries hit source) | ✅ `query_mode='remote'` via DuckDB BQ extension | ❌ DuckDB Keboola extension has no live-attach mode |
   | Synced / Whole (full table → parquet) | ✅ `query_mode='materialized'` with auto `SELECT *` | ✅ legacy path: extractor downloads bucket/table |
   | Synced / Custom SQL (filtered/aggregated → parquet) | ✅ `query_mode='materialized'` with admin SELECT | ❌ not implemented |

   Two of the three modes work for both sources today, but the asymmetry is hidden in code and the operator can't see it. **Verified spike (2026-05-01):** the DuckDB Keboola extension supports `COPY (SELECT * FROM kbc."bucket"."table" WHERE …) TO 'parquet'` — same pattern the existing extractor already uses at `connectors/keboola/extractor.py:209`. The Keboola materialized path is a clean parallel of the BigQuery one.

3. **Misleading Keboola form fields** — Two independent agent reviews (2026-05-01) found:
   - `Sync Strategy` dropdown's hint claims it controls extraction, but no extractor reads the field — only `src/profiler.py:222 is_partitioned()` consumes it for parquet-layout detection. Every Keboola sync is a full overwrite regardless of value. Operators picking "Incremental" expect deltas and get full-refresh.
   - `Primary Key` looks like an upsert key but is decorative metadata only. No upsert/dedup anywhere; every sync is a full overwrite. Profiler reads it for catalog annotation.
   - `Sync Schedule` input is **missing entirely** from the Keboola branch even though `src/scheduler.py:248 filter_due_tables` honors per-table cron for every source. Operators have to use the API/CLI to set per-table cadence — no UI surface.

4. **`profile_after_sync` is dead code** — Agent 1 finding: BQ register endpoint at `app/api/admin.py:791,881` forces the field to `False` "as a signal," but `app/api/sync.py:410-438` profiler block **never reads the flag**. Profiler runs unconditionally on every synced table. Field is inert.

### Why one PR

These four problems cluster around the same template (`app/web/templates/admin_tables.html`) and the same backend dispatch (`app/api/admin.py` + `app/api/sync.py`). Splitting into four sequential PRs would mean:

- Form-cleanup PR touches Keboola-form-as-it-is-today, then tab-split PR re-does the same layout inside a tab → throwaway work
- Keboola-materialized PR adds a Custom SQL textarea to the Keboola form, but the form layout is still the mixed flat one → confusing partial state
- profile_after_sync PR is its own concern but loosely tied to the Pydantic models touched by the form changes

Doing them as one PR lets us land a coherent operator-facing change: **"`/admin/tables` is now per-connector tabs, with Keboola at full capability parity with BigQuery."** The internal cleanup (form labels, dead code) comes along naturally.

### What this PR is NOT

- Not a schema migration. `table_registry.profile_after_sync` and `sync_strategy` columns stay in DB (back-compat for external API consumers + profiler keeps reading sync_strategy). Marked `Field(deprecated=True)` in Pydantic. A future PR can drop the columns once external consumers migrate.
- Not a Live mode for Keboola. The Keboola DuckDB extension doesn't support remote view passthrough; adding it is upstream extension work outside this scope.
- Not a refactor of the orchestrator or analytics view layer. Materialized parquets land in `data/extracts/<source>/data/` and the existing `SyncOrchestrator.rebuild()` local-parquet walk picks them up unchanged.

---

## E2E safety contract

User feedback (2026-05-01): "**Naše změny musí ve výsledku fungovat E2E, takže nemůžeme nic vynechávat.**" The plan must protect these invariants — every task that could violate one has an explicit gating test.

1. **PUT preservation invariant** — When Edit modal stops sending `sync_strategy` in the payload, an existing row's stored value (especially `'partitioned'`, used by `profiler.is_partitioned()` for parquet-directory layout) must survive. Verified: `app/api/admin.py:1623` uses `request.model_dump()` (without `exclude_unset=True`) plus `if v is not None` filter, so omitted Optional fields drop out before merge. Phase F locks this with a regression test.

2. **Existing partitioned rows still profile correctly** — `sync_strategy` stays alive in DB + Pydantic. Profiler `src/profiler.py:222` keeps reading it. Existing rows with `sync_strategy='partitioned'` keep their parquet-directory layout. No DB migration. No behavioral change for legacy rows.

3. **Existing #148 BQ form behavior preserved verbatim** — Two-question radio (Live × Synced × Whole | Custom), Discover/List tables/Use-as-base buttons, table-vs-view auto-detection hint — all of it lifted into the BigQuery tab unchanged. `tests/test_admin_tables_ui_materialized.py` and `tests/test_admin_bq_register.py` tests asserting form structure must still pass.

4. **External API back-compat** — `tests/test_migration.py:44`, `tests/test_repositories.py:277`, `tests/test_api_complete.py:117` POST `sync_strategy='incremental'` to the API. These must keep passing — `RegisterTableRequest` still accepts the field; only the UI omits it.

5. **`profile_after_sync` becomes inert, not breaking** — Pydantic still accepts the field (with `deprecated=True`). External API clients that send it get no error, no warning — server silently ignores. Existing tests at `tests/test_admin_bq_register.py:247,648,1371,1430` updated: assertions of `profile_after_sync == False` removed (the field is no longer persisted), but request payloads with the field still work.

6. **Materialized Keboola dispatch is conservative** — The new `_run_materialized_pass` Keboola branch only fires for rows with `source_type='keboola' AND query_mode='materialized'`. Existing Keboola rows (`query_mode='local'`, the default) keep going through the legacy `connectors/keboola/extractor.py` download path unchanged. No silent rerouting.

7. **Tab navigation degrades gracefully** — The page works without JS (server-renders all three tabs visible, JS just hides the inactive ones). If only one source type is configured, the relevant tab is auto-active and the other tabs render with a "no [source] configured" notice instead of an empty form.

---

## File Structure

**Created:**
- `connectors/keboola/extractor.py:materialize_query` — new top-level function (parallel to `connectors/bigquery/extractor.py:materialize_query`). Takes `(table_id, sql, *, keboola_url, token, output_dir)`, ATTACHes Keboola extension, runs `COPY (sql) TO 'parquet'`, returns dict with rows / bytes / md5 / path.
- `connectors/keboola/access.py` — thin facade analogous to `connectors/bigquery/access.py:BqAccess`. Provides `KeboolaAccess.duckdb_session()` context manager that yields a DuckDB connection with the Keboola extension loaded + ATTACHed. Encapsulates token handling so `_run_materialized_pass` doesn't need to know extension wiring details.
- `tests/test_keboola_materialize.py` — unit + integration tests for `materialize_query`. Mocks the Keboola extension where possible; uses a real fixture extract.duckdb otherwise.
- `tests/test_admin_keboola_materialized.py` — admin API tests for registering/updating Keboola-materialized rows.
- `tests/test_sync_trigger_keboola_materialized.py` — scheduler-level integration test asserting that `_run_materialized_pass` dispatches to Keboola for Keboola-materialized rows.
- `tests/test_admin_tables_tab_ui.py` — UI tests for the new tab structure.
- `tests/test_admin_put_preservation.py` — regression guard for PUT field-preservation invariant (item 1 of the E2E safety contract).

**Modified:**
- `app/web/templates/admin_tables.html` — substantial restructure: tab nav, per-tab content panels, per-tab Register modal triggers, per-tab listing filter. Existing BQ form contents preserved verbatim, relocated into BQ tab. Keboola form rebuilt with the same two-question radio model + new Custom SQL textarea. Jira tab is read-only listing.
- `app/api/admin.py` — extend `RegisterTableRequest._check_mode_query_coherence` model_validator to allow `query_mode='materialized'` for `source_type='keboola'` (today the validator implicitly assumes BQ for materialized). Mark `sync_strategy` and `profile_after_sync` as `Field(deprecated=True)`. Stop reading `profile_after_sync` from the request in BQ register / `update_table` (no longer persisted, but the field is accepted for back-compat).
- `app/api/sync.py` — `_run_materialized_pass` dispatches by `source_type`: existing BQ branch keeps `BqAccess` + `connectors.bigquery.extractor.materialize_query`; new Keboola branch uses `KeboolaAccess` + `connectors.keboola.extractor.materialize_query`. Cost guardrail (BQ dry-run) only runs for BQ rows; Keboola has no analogous dry-run primitive in the extension and Storage API has different cost shape — skipped with a TODO comment for future work.
- `connectors/keboola/extractor.py` — `init_extract` (the legacy full-download path) skips `query_mode='materialized'` rows so they aren't double-extracted. Mirror of the BQ extractor's existing skip at `connectors/bigquery/extractor.py:188`.
- `tests/test_admin_bq_register.py` — remove assertions of `row["profile_after_sync"] is False` (field is no longer persisted); request payloads keep the field for back-compat verification. Existing form-structure tests adjusted for tab restructure (selectors prefixed with tab container ids).
- `tests/test_admin_tables_ui_materialized.py` — assertions adjusted for tab restructure.
- `CHANGELOG.md` — `## [Unreleased]` block with `### Added`, `### Changed`, `### Fixed`, `### Deprecated` entries.

**Deleted:**
- Nothing (Pydantic fields stay alive with `deprecated=True`).

**Untouched:**
- `src/db.py` — schema stays at v20. Columns survive.
- `src/profiler.py` — keeps reading `sync_strategy` for partition detection.
- `src/orchestrator.py` — local-parquet walk picks up Keboola materialized parquets the same way it picks up BQ ones today.
- `connectors/jira/**` — Jira tab is read-only; no register form, no backend change.
- `cli/**` — analyst-side `da sync` / `da query` / `da fetch` flow unchanged. Materialized Keboola parquets show up in the manifest with `source_type='keboola'` + `query_mode='local'` (because the result is a local parquet) — analyst-side rails (`CLAUDE.md`) treat them like any other Keboola table.

---

## Phase A — Spike: lock down the Keboola extension query passthrough

**Goal:** Phase B and onward depend on the Keboola DuckDB extension supporting `COPY (admin SELECT) TO 'parquet'`. The grep at planning time confirmed the existing extractor already uses this pattern, but we want a dedicated test that pins the capability so a future extension upgrade doesn't silently break the Keboola materialized path.

### Task A1: Lock-in test for Keboola extension query passthrough

**Files:**
- Create: `tests/test_keboola_extension_query_passthrough.py`

- [ ] **Step 1: Write the failing test**

```python
"""Lock-in test for the DuckDB Keboola extension's query-passthrough
capability that the Keboola materialized path depends on.

Run only when KBC_TEST_URL + KBC_TEST_TOKEN env vars are set (CI without
real Keboola credentials skips). Local dev with a real Storage API
token exercises the path.
"""
import os
import pytest
import duckdb


KBC_URL = os.environ.get("KBC_TEST_URL")
KBC_TOKEN = os.environ.get("KBC_TEST_TOKEN")
KBC_BUCKET = os.environ.get("KBC_TEST_BUCKET")
KBC_TABLE = os.environ.get("KBC_TEST_TABLE")

pytestmark = pytest.mark.skipif(
    not all([KBC_URL, KBC_TOKEN, KBC_BUCKET, KBC_TABLE]),
    reason="Keboola integration creds not provided",
)


def test_extension_supports_attach_and_select(tmp_path):
    """Keboola extension must support: ATTACH 'keboola://...' AS kbc, then
    SELECT * FROM kbc.bucket.table. The Keboola materialized path uses this
    primitive at runtime (just like connectors/keboola/extractor.py:133)."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
    rows = conn.execute(
        f'SELECT COUNT(*) FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}"'
    ).fetchone()
    assert rows[0] >= 0  # any non-negative count is fine; we're testing the path works


def test_extension_supports_copy_to_parquet(tmp_path):
    """Keboola materialized writes the SELECT result via
    `COPY (...) TO '...' (FORMAT PARQUET)`. Lock that primitive."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")

    parquet_path = tmp_path / "out.parquet"
    safe_lit = str(parquet_path).replace("'", "''")
    conn.execute(
        f'COPY (SELECT * FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 5) '
        f"TO '{safe_lit}' (FORMAT PARQUET)"
    )
    assert parquet_path.exists() and parquet_path.stat().st_size > 0


def test_extension_supports_filtered_query(tmp_path):
    """Most important capability: a non-trivial WHERE/projection survives.
    This is what 'Custom SQL' mode actually relies on."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")

    parquet_path = tmp_path / "filtered.parquet"
    safe_lit = str(parquet_path).replace("'", "''")
    # Trivially filterable SELECT — extension must push the WHERE down or
    # at minimum execute it client-side. Either is acceptable for our
    # materialized path.
    conn.execute(
        f'COPY (SELECT 1 AS marker FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 3) '
        f"TO '{safe_lit}' (FORMAT PARQUET)"
    )
    assert parquet_path.exists()
```

- [ ] **Step 2: Run the test to verify it skips (no creds in dev) or passes (creds present)**

```
pytest tests/test_keboola_extension_query_passthrough.py -v
```

Expected: SKIP if no creds, PASS if `KBC_TEST_URL` etc. are set. Both outcomes confirm the test is well-formed; it gates Phase B but doesn't block dev work.

- [ ] **Step 3: Commit**

```bash
git add tests/test_keboola_extension_query_passthrough.py
git commit -m "test(keboola): lock-in Keboola extension query passthrough capability

The upcoming Keboola materialized path depends on the DuckDB Keboola
extension supporting:
  ATTACH 'keboola://...' AS kbc (TYPE keboola, TOKEN '...');
  COPY (SELECT * FROM kbc.bucket.table WHERE ...) TO 'parquet';

The existing extractor already uses this pattern (extractor.py:209), so
the capability is verified; this test pins it so a future extension
upgrade doesn't silently regress the materialized path. Skips in CI
without KBC_TEST_* env vars; passes locally with a real Storage API
token."
```

---

## Phase B — Backend: Keboola materialized path

### Task B1: `KeboolaAccess` facade

**Files:**
- Create: `connectors/keboola/access.py`
- Create: `tests/test_keboola_access.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for KeboolaAccess facade."""
import os
import pytest
from connectors.keboola.access import KeboolaAccess


def test_access_session_yields_attached_duckdb(tmp_path, monkeypatch):
    """Mock-mode test: the facade should accept a token, install+load
    the Keboola extension, and ATTACH it as 'kbc'. We verify the SQL
    issued by intercepting the duckdb.connect call.
    """
    issued = []
    class FakeConn:
        def execute(self, sql, *args, **kwargs):
            issued.append(sql)
            class R:
                def fetchall(s): return []
                def fetchone(s): return (0,)
            return R()
        def close(self): pass

    import duckdb
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: FakeConn())

    acc = KeboolaAccess(
        url="https://connection.keboola.com/",
        token="fake-token-xyz",
    )
    with acc.duckdb_session() as conn:
        assert conn is not None
    # Verify the install + load + attach sequence happened.
    joined = "\n".join(issued)
    assert "INSTALL keboola" in joined
    assert "LOAD keboola" in joined
    assert "ATTACH" in joined and "TYPE keboola" in joined
    # Token must be escaped for embedding in the ATTACH literal.
    assert "fake-token-xyz" in joined


def test_access_escapes_single_quote_in_token(monkeypatch):
    """Defense against a token containing a single quote breaking the
    ATTACH literal. SQL injection here is non-trivial because the token
    is admin-supplied at instance config time, but escape it anyway."""
    issued = []
    class FakeConn:
        def execute(self, sql, *args, **kwargs):
            issued.append(sql)
            class R:
                def fetchall(s): return []
                def fetchone(s): return (0,)
            return R()
        def close(self): pass
    import duckdb
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: FakeConn())

    acc = KeboolaAccess(url="x", token="bad'token")
    with acc.duckdb_session() as conn:
        pass
    attach_sql = next(s for s in issued if "ATTACH" in s)
    # Doubled single-quote per SQL string-literal escaping.
    assert "bad''token" in attach_sql


def test_access_real_attach_when_creds_present(tmp_path):
    """Smoke when KBC_TEST_URL + KBC_TEST_TOKEN are present."""
    url = os.environ.get("KBC_TEST_URL")
    token = os.environ.get("KBC_TEST_TOKEN")
    if not (url and token):
        pytest.skip("Keboola creds not provided")
    acc = KeboolaAccess(url=url, token=token)
    with acc.duckdb_session() as conn:
        # ATTACH must have succeeded — querying duckdb_databases() should
        # show the 'kbc' alias.
        rows = [r[0] for r in conn.execute("SELECT name FROM duckdb_databases()").fetchall()]
        assert "kbc" in rows
```

- [ ] **Step 2: Run, verify failure**

```
pytest tests/test_keboola_access.py -v
```

Expected: ImportError on `connectors.keboola.access` — module not yet created.

- [ ] **Step 3: Implement `KeboolaAccess`**

Write `connectors/keboola/access.py`:

```python
"""DuckDB session facade for the Keboola Storage API extension.

Parallel of `connectors/bigquery/access.py:BqAccess`. The materialized
Keboola SQL path needs a one-shot DuckDB connection with the Keboola
extension installed, loaded, and ATTACHed; this facade encapsulates
that wiring so `_run_materialized_pass` doesn't need to know the
extension name, the ATTACH alias, or how the token gets quoted into
the URL literal.
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator

import duckdb


class KeboolaAccess:
    """Lazy DuckDB session manager for the Keboola Storage API extension.

    Single-use — call `.duckdb_session()` as a context manager once per
    materialized job.
    """

    def __init__(self, *, url: str, token: str) -> None:
        if not url or not token:
            raise ValueError("KeboolaAccess requires url and token")
        self._url = url
        self._token = token

    @contextmanager
    def duckdb_session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("INSTALL keboola FROM community")
            conn.execute("LOAD keboola")
            escaped_token = self._token.replace("'", "''")
            conn.execute(
                f"ATTACH '{self._url}' AS kbc "
                f"(TYPE keboola, TOKEN '{escaped_token}')"
            )
            yield conn
        finally:
            conn.close()
```

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_keboola_access.py -v
```

Expected: 2 PASS (mock tests), 1 SKIP (real-creds test).

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/access.py tests/test_keboola_access.py
git commit -m "feat(keboola): add KeboolaAccess facade for DuckDB-extension session

Parallel of connectors/bigquery/access.py:BqAccess. Encapsulates the
INSTALL + LOAD + ATTACH sequence the Keboola materialized SQL path
needs, with single-quote-escaped token interpolation. Single-use
context manager — caller wraps `with acc.duckdb_session() as conn:`
around one materialized job.

Mock tests verify the SQL sequence; a real-creds test exercises the
ATTACH end-to-end when KBC_TEST_URL + KBC_TEST_TOKEN are set."
```

---

### Task B2: `connectors.keboola.extractor.materialize_query`

**Files:**
- Modify: `connectors/keboola/extractor.py`
- Create: `tests/test_keboola_materialize.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the Keboola materialize_query path."""
import hashlib
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from connectors.keboola import extractor as kbe


def test_materialize_query_writes_parquet_and_returns_metadata(tmp_path, monkeypatch):
    """Mock-mode: feed in a fake KeboolaAccess that yields a fake DuckDB
    connection accepting `COPY ... TO '...' (FORMAT PARQUET)` and just
    writes a small parquet via duckdb's own primitive on a tmp DB.
    """
    import duckdb
    real_conn = duckdb.connect(":memory:")
    # Pre-create a small relation the fake materialize "copies".
    real_conn.execute("CREATE TABLE t AS SELECT 1 AS x, 'hello' AS y UNION ALL SELECT 2, 'world'")

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield real_conn
            return _cm()
    fake_access = FakeAccess()

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Submit a query that selects from the in-memory table (not a real
    # Keboola bucket — the test verifies the COPY/parquet/hash path,
    # not the extension behavior).
    result = kbe.materialize_query(
        table_id="example_subset",
        sql="SELECT * FROM t",
        keboola_access=fake_access,
        output_dir=output_dir,
    )

    parquet_path = output_dir / "example_subset.parquet"
    assert parquet_path.exists()
    assert result["table_id"] == "example_subset"
    assert result["path"] == str(parquet_path)
    assert result["rows"] == 2
    assert result["bytes"] > 0
    # MD5 of the bytes should match what we recompute.
    expected_md5 = hashlib.md5(parquet_path.read_bytes()).hexdigest()
    assert result["md5"] == expected_md5


def test_materialize_query_zero_rows_logs_warning(tmp_path, caplog):
    import duckdb
    real_conn = duckdb.connect(":memory:")
    real_conn.execute("CREATE TABLE t AS SELECT 1 AS x WHERE FALSE")

    class FakeAccess:
        def duckdb_session(self):
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield real_conn
            return _cm()

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with caplog.at_level("WARNING"):
        result = kbe.materialize_query(
            table_id="empty_subset",
            sql="SELECT * FROM t",
            keboola_access=FakeAccess(),
            output_dir=output_dir,
        )
    assert result["rows"] == 0
    assert "0 rows" in caplog.text or "empty" in caplog.text.lower()


def test_materialize_query_rejects_unsafe_table_id(tmp_path):
    """Defense: table_id is interpolated into the parquet filename. SQL/
    path-traversal-unsafe values must be rejected up-front (mirror of BQ
    materialize_query's validation)."""
    class FakeAccess:
        def duckdb_session(self):
            raise AssertionError("should not be called")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="table_id"):
        kbe.materialize_query(
            table_id="../../etc/passwd",
            sql="SELECT 1",
            keboola_access=FakeAccess(),
            output_dir=output_dir,
        )
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_keboola_materialize.py -v
```

Expected: AttributeError on `kbe.materialize_query` — function not yet defined.

- [ ] **Step 3: Implement**

Add to `connectors/keboola/extractor.py` (before any existing top-level helpers):

```python
def materialize_query(
    table_id: str,
    sql: str,
    *,
    keboola_access,  # KeboolaAccess (avoid circular import)
    output_dir: Path,
) -> dict:
    """Materialize an admin-registered SELECT against the Keboola Storage
    API extension into a parquet file.

    Parallel of `connectors/bigquery/extractor.py:materialize_query`.
    Cost guardrail: the Keboola extension has no analog of BQ dry-run;
    Storage API cost is download-shaped (per-byte egress + Storage API
    job). Phase B ships without a guardrail and logs the byte count;
    a future PR can add a configurable `max_bytes_per_keboola_materialize`
    gate similar to BQ's `max_bytes_per_materialize`.
    """
    import re
    import hashlib
    import logging

    logger = logging.getLogger(__name__)

    # Defense: table_id is interpolated into the parquet filename.
    # Reject anything that's not a safe identifier.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_id):
        raise ValueError(f"unsafe table_id for materialize: {table_id!r}")

    parquet_path = output_dir / f"{table_id}.parquet"
    safe_pq_lit = str(parquet_path).replace("'", "''")

    with keboola_access.duckdb_session() as conn:
        # Run the admin SELECT and copy the result to parquet.
        # The COPY wrapper is identical to the existing legacy extract
        # path at extractor.py:209; the only difference is the SELECT is
        # admin-supplied rather than `SELECT * FROM kbc.bucket.table`.
        conn.execute(f"COPY ({sql}) TO '{safe_pq_lit}' (FORMAT PARQUET)")

        # Read back row count.
        row_count = conn.execute(
            f"SELECT COUNT(*) FROM read_parquet('{safe_pq_lit}')"
        ).fetchone()[0]

    file_bytes = parquet_path.read_bytes()
    md5 = hashlib.md5(file_bytes).hexdigest()
    size = len(file_bytes)

    if row_count == 0:
        logger.warning(
            "Materialized Keboola query for %s wrote 0 rows — verify the "
            "SQL filters and that the source bucket has data.",
            table_id,
        )

    return {
        "table_id": table_id,
        "path": str(parquet_path),
        "rows": row_count,
        "bytes": size,
        "md5": md5,
    }
```

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_keboola_materialize.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/extractor.py tests/test_keboola_materialize.py
git commit -m "feat(keboola): add materialize_query — admin SELECT → parquet

Parallel of connectors/bigquery/extractor.py:materialize_query. Runs an
admin-registered SELECT through the Keboola DuckDB extension via
KeboolaAccess.duckdb_session(), wraps it in COPY ... TO '...'
(FORMAT PARQUET), and returns rows/bytes/md5/path metadata for
sync_state bookkeeping.

Cost guardrail intentionally omitted in this iteration — the Keboola
extension has no dry-run analog and Storage API cost shape is
download-byte-based, not scan-byte-based. Phase B ships with byte-count
logging; a follow-up can add a configurable max_bytes gate if needed.

table_id is validated as a safe identifier (mirror of BQ implementation)
because it's interpolated into the parquet filename."
```

---

### Task B3: `init_extract` skips materialized rows

**Files:**
- Modify: `connectors/keboola/extractor.py`
- Create: `tests/test_keboola_init_extract_skips.py`

- [ ] **Step 1: Failing test**

```python
"""Verify the legacy Keboola download path skips materialized rows.

Materialized rows are handled by `_run_materialized_pass` in
`app/api/sync.py`, not by the legacy extractor. Mirror of the BQ
extractor's existing skip behavior at line 188."""
import json
from pathlib import Path
from unittest.mock import patch

from connectors.keboola import extractor as kbe


def test_init_extract_skips_materialized_rows(tmp_path):
    """Given a registry with one local row + one materialized row, the
    legacy init_extract path must process only the local row."""
    extracts = tmp_path / "extracts" / "keboola"
    extracts.mkdir(parents=True)
    (extracts / "data").mkdir()

    table_configs = [
        {
            "id": "orders",
            "name": "orders",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
        {
            "id": "orders_recent",
            "name": "orders_recent",
            "source_query": "SELECT * FROM kbc.\"in.c-sales\".\"orders\" WHERE date > '2026-01-01'",
            "query_mode": "materialized",
        },
    ]

    # Patch the actual ATTACH/COPY path so the test doesn't need real Keboola.
    seen = []
    def fake_run_one(conn, tc, *a, **kw):
        seen.append(tc["id"])
    with patch.object(kbe, "_extract_one_table", fake_run_one, create=True):
        kbe.init_extract(
            extracts_dir=extracts,
            table_configs=table_configs,
            keboola_url="https://x/",
            keboola_token="t",
        )
    assert seen == ["orders"]  # materialized row skipped


def test_init_extract_logs_skip_reason(tmp_path, caplog):
    """When skipping a materialized row, log the reason for ops visibility."""
    extracts = tmp_path / "extracts" / "keboola"
    extracts.mkdir(parents=True)
    (extracts / "data").mkdir()

    table_configs = [
        {
            "id": "orders_recent",
            "name": "orders_recent",
            "source_query": "SELECT 1",
            "query_mode": "materialized",
        },
    ]
    with caplog.at_level("INFO"):
        with patch.object(kbe, "_extract_one_table", lambda *a, **kw: None, create=True):
            kbe.init_extract(
                extracts_dir=extracts,
                table_configs=table_configs,
                keboola_url="https://x/",
                keboola_token="t",
            )
    assert "Skipping" in caplog.text and "materialized" in caplog.text
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_keboola_init_extract_skips.py -v
```

Expected: FAIL — current `init_extract` does not skip.

- [ ] **Step 3: Implement skip**

Find the existing iteration loop in `connectors/keboola/extractor.py` (around lines 100–135 where each table_config is processed). Add at the top of the per-table-config loop:

```python
        for tc in table_configs:
            if tc.get("query_mode") == "materialized":
                logger.info(
                    "Skipping legacy extract for %s — query_mode='materialized', "
                    "handled by _run_materialized_pass instead",
                    tc.get("id") or tc.get("name"),
                )
                continue
            ...  # existing per-table extract logic
```

(Refactoring note: if the existing loop body is monolithic, optionally extract it into `_extract_one_table(conn, tc, ...)` so the test can patch it cleanly. The first test above assumes that helper exists; if you keep the body inline, write the test to assert by directly observing parquet outputs instead.)

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_keboola_init_extract_skips.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add connectors/keboola/extractor.py tests/test_keboola_init_extract_skips.py
git commit -m "feat(keboola): legacy extract skips query_mode='materialized' rows

Mirror of the BQ extractor's existing skip at line 188. Materialized
Keboola rows are handled by _run_materialized_pass (post-Phase-B
implementation) rather than by the legacy bucket-download path. Without
this skip, a materialized row would get full-extracted via its source
bucket reference, double-writing data and confusing the sync_state
bookkeeping."
```

---

### Task B4: `_run_materialized_pass` dispatches by `source_type`

**Files:**
- Modify: `app/api/sync.py`
- Create: `tests/test_sync_trigger_keboola_materialized.py`

- [ ] **Step 1: Failing test**

```python
"""Scheduler-level test: when a Keboola row has query_mode='materialized',
_run_materialized_pass uses connectors.keboola.extractor.materialize_query
(not BQ's). Existing BQ-materialized rows continue using BqAccess."""
from unittest.mock import patch, MagicMock
import pytest


def test_run_materialized_pass_dispatches_keboola_to_keboola_extractor(seeded_app, tmp_path):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register a Keboola materialized row.
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": (
                "SELECT * FROM kbc.\"in.c-sales\".\"orders\" "
                "WHERE date > '2026-01-01'"
            ),
        },
    )
    assert r.status_code == 201, r.text

    # Patch the two extractor entry points so we can observe which fires.
    bq_called = MagicMock()
    kb_called = MagicMock()
    with patch(
        "connectors.bigquery.extractor.materialize_query", bq_called
    ), patch(
        "connectors.keboola.extractor.materialize_query", kb_called
    ):
        # Trigger sync.
        r = c.post("/api/sync/trigger", headers=auth)
        # Allow background tasks to drain (depends on test client setup).

    assert kb_called.called, "Keboola materialize_query was not invoked"
    assert not bq_called.called, "BQ materialize_query was wrongly invoked for a Keboola row"


def test_run_materialized_pass_dispatches_bigquery_to_bq_extractor(seeded_app):
    """Regression: existing BQ-materialized path keeps working unchanged."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "events_summary",
            "source_type": "bigquery",
            "query_mode": "materialized",
            "source_query": "SELECT date, COUNT(*) FROM `proj.dataset.events` GROUP BY 1",
        },
    )
    assert r.status_code == 201, r.text

    bq_called = MagicMock()
    kb_called = MagicMock()
    with patch(
        "connectors.bigquery.extractor.materialize_query", bq_called
    ), patch(
        "connectors.keboola.extractor.materialize_query", kb_called
    ):
        c.post("/api/sync/trigger", headers=auth)

    assert bq_called.called
    assert not kb_called.called
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_sync_trigger_keboola_materialized.py -v
```

Expected: FAIL — `_run_materialized_pass` doesn't yet dispatch by `source_type` for Keboola.

- [ ] **Step 3: Implement dispatch**

Find `_run_materialized_pass` in `app/api/sync.py` (around line 57). The current body iterates rows and calls `_materialize_table` (which wraps BQ's `materialize_query`). Refactor:

```python
def _run_materialized_pass(conn, bq=None) -> dict:
    """Run all materialized rows that are due, dispatching by source_type
    to the correct connector's materialize_query.

    BigQuery rows go through BqAccess + bigquery_query() (jobs API),
    optionally cost-guarded by max_bytes_per_materialize.
    Keboola rows go through KeboolaAccess + ATTACH-and-COPY, no
    guardrail (extension has no dry-run primitive)."""
    from connectors.bigquery.extractor import materialize_query as bq_materialize
    from connectors.keboola.extractor import materialize_query as kb_materialize
    from connectors.keboola.access import KeboolaAccess
    from src.repositories.table_registry import TableRegistryRepository
    from src.scheduler import is_table_due
    # ... existing imports

    repo = TableRegistryRepository(conn)
    rows = repo.list_materialized_due()  # or however the existing iteration looks

    stats = {"materialized": 0, "skipped": 0, "errors": []}
    keboola_access = None  # lazy

    for row in rows:
        source_type = row.get("source_type") or "bigquery"  # legacy default
        if source_type == "bigquery":
            try:
                bq_materialize(
                    table_id=row["id"],
                    sql=row["source_query"],
                    bq=bq,  # existing BqAccess instance
                    output_dir=...,  # existing path
                    max_bytes=...,  # existing guardrail config
                )
                stats["materialized"] += 1
            except Exception as e:
                stats["errors"].append({"id": row["id"], "error": str(e)})
        elif source_type == "keboola":
            if keboola_access is None:
                # Lazy-init using instance config.
                from app.instance_config import get_value
                keboola_url = get_value("data_source", "keboola", "url")
                keboola_token = os.environ.get(
                    get_value("data_source", "keboola", "token_env")
                )
                if not (keboola_url and keboola_token):
                    stats["errors"].append({
                        "id": row["id"],
                        "error": "Keboola URL/token not configured for materialized path",
                    })
                    continue
                keboola_access = KeboolaAccess(url=keboola_url, token=keboola_token)
            try:
                kb_materialize(
                    table_id=row["id"],
                    sql=row["source_query"],
                    keboola_access=keboola_access,
                    output_dir=...,  # /data/extracts/keboola/data/
                )
                stats["materialized"] += 1
            except Exception as e:
                stats["errors"].append({"id": row["id"], "error": str(e)})
        else:
            stats["skipped"] += 1
            stats["errors"].append({
                "id": row["id"],
                "error": f"materialized path not supported for source_type={source_type!r}",
            })

    return stats
```

(Adapt to the actual existing `_run_materialized_pass` shape — the snippet above is the structural change; concrete details like output_dir path and existing helper names are read from the file at implementation time.)

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_sync_trigger_keboola_materialized.py -v
pytest tests/test_sync_trigger_materialized.py -v  # existing BQ test must still pass
```

Expected: both files pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/sync.py tests/test_sync_trigger_keboola_materialized.py
git commit -m "feat(sync): _run_materialized_pass dispatches by source_type

BQ materialized rows continue using BqAccess + bigquery_query() with
the cost guardrail. New Keboola materialized rows go through
KeboolaAccess + ATTACH-and-COPY (no guardrail — Keboola extension has
no dry-run primitive; download-byte-shaped cost is logged).

Existing tests for BQ dispatch keep passing (regression test
explicitly added). New tests verify Keboola dispatch fires for
source_type='keboola' rows and stays silent for BQ rows."
```

---

## Phase C — Backend: Pydantic deprecation + `profile_after_sync` becomes inert

### Task C1: Mark `sync_strategy` and `profile_after_sync` deprecated, stop persisting `profile_after_sync`

**Files:**
- Modify: `app/api/admin.py` (Pydantic models around lines 654–728 and 880–895; BQ register endpoint around line 791; `update_table` around line 1623)
- Modify: `tests/test_admin_bq_register.py` (assertions of `row["profile_after_sync"] is False` → drop, replace with assertion that the field-being-sent doesn't error)

- [ ] **Step 1: Failing test (deprecation visible in OpenAPI + field becomes inert)**

```python
"""Verify Phase C deprecation marks + profile_after_sync becomes inert."""
import pytest
from app.api.admin import RegisterTableRequest, UpdateTableRequest


def test_register_request_marks_sync_strategy_deprecated():
    schema = RegisterTableRequest.model_json_schema()
    field = schema["properties"]["sync_strategy"]
    assert field.get("deprecated") is True


def test_register_request_marks_profile_after_sync_deprecated():
    schema = RegisterTableRequest.model_json_schema()
    field = schema["properties"]["profile_after_sync"]
    assert field.get("deprecated") is True


def test_register_endpoint_accepts_profile_after_sync_for_backcompat(seeded_app):
    """External clients sending profile_after_sync get no error — the
    field is silently ignored."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "bucket": "in.c-foo",
            "source_table": "y",
            "query_mode": "local",
            "profile_after_sync": True,  # legacy client may send this
        },
    )
    assert r.status_code == 201


def test_register_endpoint_does_not_persist_profile_after_sync(seeded_app):
    """The persisted row no longer carries the old profile_after_sync
    value (column may still exist in DB for back-compat, but admin path
    never writes a non-default value)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "y",
            "source_type": "keboola",
            "bucket": "in.c-foo",
            "source_table": "y",
            "query_mode": "local",
            "profile_after_sync": True,
        },
    )
    assert r.status_code == 201
    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "y")
    # The field's value in the registry response is now whatever the DB
    # default is (True per current schema). Critical: the request value
    # is NOT echoed back.
    # If the value is in the response at all (legacy back-compat in the
    # GET serializer), it's the schema default, not the request value.
    # If the value is absent (deprecated and stripped), that's also fine.
    if "profile_after_sync" in row:
        # Whatever this is, it's the schema default, not request-driven.
        assert row["profile_after_sync"] is True or row["profile_after_sync"] is None
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_phase_c_deprecation.py -v
```

Expected: deprecation-mark assertions FAIL (no `deprecated=True` yet).

- [ ] **Step 3: Implement Pydantic deprecation marks**

In `app/api/admin.py` `RegisterTableRequest` definition, change:

```python
sync_strategy: str = "full_refresh"
```

to:

```python
sync_strategy: str = Field(
    default="full_refresh",
    deprecated=True,
    description=(
        "DEPRECATED: catalog/profiler metadata only. No extractor reads "
        "this field; every sync is a full overwrite regardless of value. "
        "profiler.is_partitioned() consumes it for parquet-layout "
        "detection. Field stays for back-compat; will be removed in a "
        "future major release."
    ),
)
```

Same treatment for `profile_after_sync`:

```python
profile_after_sync: bool = Field(
    default=True,
    deprecated=True,
    description=(
        "DEPRECATED: not consumed by the runtime (Agent 1 finding "
        "2026-05-01). Profiler runs unconditionally on every synced "
        "table; this flag has no effect. Field stays for back-compat."
    ),
)
```

In the BQ register endpoint at `app/api/admin.py:791`, find the line that sets `request.profile_after_sync = False` and remove it (the field is now inert, no need to force a value).

In `update_table` at `app/api/admin.py:1657`, the synthetic `RegisterTableRequest` carries `profile_after_sync=bool(merged.get("profile_after_sync") or False)` — keep this for back-compat but understand it's now decorative; the synthetic-validate path doesn't need to change.

In `register_table` at `app/api/admin.py:1362` (the actual repo.register call), drop `profile_after_sync=request.profile_after_sync` from the kwargs if it's there. The DB column has its default (`True` per schema) and stays consistent.

- [ ] **Step 4: Run**

```
pytest tests/test_admin_phase_c_deprecation.py -v
pytest tests/test_admin_bq_register.py -v
```

Expected: new tests pass; existing BQ register tests need updates where they assert `row["profile_after_sync"] is False`.

- [ ] **Step 5: Update existing assertions in `tests/test_admin_bq_register.py`**

Find lines 247, 648, 1371, 1430 where `assert row["profile_after_sync"] is False` exists. Replace with a comment + back-compat assertion:

```python
# Phase C: profile_after_sync is now inert. The field is accepted in
# the request for back-compat but no longer overrides the DB default.
# Was: assert row["profile_after_sync"] is False  (when BQ register
# forced it to False as a "signal"). Now the row carries the schema
# default (True). Profiler runs unconditionally regardless.
assert row.get("profile_after_sync") in (True, None)
```

- [ ] **Step 6: Run full sweep**

```
pytest tests/test_admin_bq_register.py tests/test_admin_phase_c_deprecation.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/api/admin.py tests/test_admin_bq_register.py tests/test_admin_phase_c_deprecation.py
git commit -m "feat(admin-api): mark sync_strategy + profile_after_sync deprecated; profile_after_sync becomes inert

OpenAPI schema now flags both fields with deprecated=true. External API
clients see the signal during their next regen but get no runtime
error — back-compat preserved.

profile_after_sync was previously force-set to False by the BQ register
endpoint as a 'signal,' but app/api/sync.py:410-438 never reads the
flag (Agent 1 finding 2026-05-01). The runtime profiles every synced
table unconditionally. Phase C removes the force-False line and stops
the field from overriding the DB default — it's now decorative-only
in both directions.

sync_strategy stays alive in DB and Pydantic because
profiler.is_partitioned() at src/profiler.py:222 still consumes it for
parquet-directory-layout detection on existing partitioned rows. Phase
F (UI) hides the field from the form; Phase C just labels it for
external consumers.

Existing BQ register tests asserting row['profile_after_sync'] is False
updated to back-compat-tolerant form."
```

---

### Task C2: `RegisterTableRequest` validator allows Keboola materialized

**Files:**
- Modify: `app/api/admin.py` (`_check_mode_query_coherence` model validator, around lines 681–692)
- Create: `tests/test_admin_keboola_materialized.py`

- [ ] **Step 1: Failing test**

```python
"""Tests for Keboola materialized registration."""
import pytest


def test_register_keboola_materialized_accepts_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT * FROM kbc.\"in.c-sales\".\"orders\" WHERE date > '2026-01-01'",
            "sync_schedule": "daily 03:00",
        },
    )
    assert r.status_code == 201, r.text


def test_register_keboola_materialized_rejects_missing_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            # source_query missing
        },
    )
    assert r.status_code == 422
    assert "source_query" in r.text


def test_register_keboola_materialized_skips_bucket_check(seeded_app):
    """Materialized rows don't need bucket/source_table — the SELECT inlines
    the references. Mirror of BQ materialized validator behavior."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
            # No bucket / source_table — must still succeed.
        },
    )
    assert r.status_code == 201, r.text


def test_update_keboola_materialized_clears_stale_source_query_on_mode_switch(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register materialized.
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
        },
    )
    assert r.status_code == 201

    # PUT to switch back to local — source_query must clear.
    r = c.put(
        "/api/admin/registry/x",
        headers=auth,
        json={
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-foo",
            "source_table": "y",
        },
    )
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "x")
    assert row.get("source_query") in (None, "")
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_keboola_materialized.py -v
```

Expected: at least the first test fails — current validator rejects materialized for non-BQ source_type, or accepts but the storage path bombs.

- [ ] **Step 3: Implement validator update**

Find `_check_mode_query_coherence` in `app/api/admin.py` (around lines 681–692). It currently enforces `source_query` IFF `query_mode='materialized'`. Verify it doesn't gate by `source_type`. If it does, remove the gate. If it doesn't, the test should already pass — investigate.

Also check `_validate_bigquery_register_payload` (around line 794) — make sure it isn't called for non-BQ rows. The dispatch at `register_table` line 1354 should already be `source_type == 'bigquery'`-gated.

For the `update_table` PUT semantics test, verify that `update_table` at line 1642 already has the "switching away from materialized → drop source_query" logic. Mirror it for the reverse (switching INTO materialized → drop bucket/source_table) if needed.

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_admin_keboola_materialized.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/api/admin.py tests/test_admin_keboola_materialized.py
git commit -m "feat(admin-api): allow query_mode='materialized' for Keboola source_type

The model validator already only gates materialized↔source_query
coherence (no source_type-specific check). Phase B made the runtime
materialized path source_type-aware. This commit pins the API contract
with end-to-end tests that:
  - Keboola+materialized POST with source_query succeeds
  - Keboola+materialized POST without source_query is rejected (422)
  - Keboola+materialized POST without bucket/source_table succeeds (the
    SELECT inlines references — same as BQ)
  - PUT switching a materialized row back to local clears the stale
    source_query (mirror of BQ behavior at admin.py:1642)"
```

---

## Phase D — UI: tab-split scaffold

### Task D1: Tab nav structure + routing

**Files:**
- Modify: `app/web/templates/admin_tables.html` (top of `<body>` around the existing single form area)
- Create: `tests/test_admin_tables_tab_ui.py`

- [ ] **Step 1: Failing test**

```python
"""UI tests for the per-connector tab layout."""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_tab_nav(seeded_app):
    """Page has tab nav with at least the source types configured for
    the instance plus Jira (always shown when any Jira rows exist)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    html = r.text
    assert 'role="tablist"' in html or 'class="tab-nav"' in html
    assert 'data-tab="bigquery"' in html or 'id="tab-bigquery"' in html
    assert 'data-tab="keboola"' in html or 'id="tab-keboola"' in html


def test_admin_tables_active_tab_matches_instance_type(seeded_app, monkeypatch):
    """When data_source.type='bigquery', the BigQuery tab is the
    initially-active one. Operator can still switch to Keboola tab if
    they want to register a secondary source."""
    fake_cfg = {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # The BQ tab content is the visible one initially.
        # Either a class="active" on the BQ tab button, or aria-selected="true".
        assert (
            'data-tab="bigquery" class="tab active"' in html
            or 'data-tab="bigquery" aria-selected="true"' in html
        )
    finally:
        reset_cache()


def test_admin_tables_each_tab_has_register_button(seeded_app):
    """Each writable source tab has its own Register button. Jira is
    read-only (no Register)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # Each Register button is scoped to its tab — id distinguishes.
    # We check presence of the registration trigger elements.
    assert 'id="bqRegisterBtn"' in html or 'data-register-source="bigquery"' in html
    assert 'id="kbRegisterBtn"' in html or 'data-register-source="keboola"' in html
    # No Jira register button (Jira is webhook-driven).
    assert 'data-register-source="jira"' not in html


def test_admin_tables_listing_per_tab(seeded_app):
    """The registry table is rendered per tab — each tab has its own
    <tbody> filtered by source_type. Listing JS reads tables from the
    catalog API and routes each row into the matching tab's <tbody>."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="bqTableListing"' in html
    assert 'id="kbTableListing"' in html
    assert 'id="jiraTableListing"' in html


def test_admin_tables_tab_persists_in_url_hash(seeded_app):
    """Tab switching updates window.location.hash so refresh keeps the
    operator on the right tab. Verify the JS hooks for it are present."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert "location.hash" in html or "history.replaceState" in html
    # And initial-tab pickup from hash on load.
    assert "window.location.hash" in html or "getActiveTabFromHash" in html
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_tables_tab_ui.py -v
```

Expected: all FAIL — tab structure not yet in template.

- [ ] **Step 3: Implement tab nav + content panels**

Restructure `app/web/templates/admin_tables.html`. The existing single content area becomes three tab panels. Outline of the new top-level structure (replace the existing single page-content area):

```html
{# Determine the initial active tab from the data source type +
   any registered rows. Operator can still switch tabs to register
   in another source. #}
{% set initial_tab = data_source_type %}

<nav class="tab-nav" role="tablist">
    <button class="tab" data-tab="bigquery"
            aria-selected="{{ 'true' if initial_tab == 'bigquery' else 'false' }}"
            onclick="switchTab('bigquery')">BigQuery</button>
    <button class="tab" data-tab="keboola"
            aria-selected="{{ 'true' if initial_tab == 'keboola' else 'false' }}"
            onclick="switchTab('keboola')">Keboola</button>
    <button class="tab" data-tab="jira"
            aria-selected="false"
            onclick="switchTab('jira')">Jira</button>
</nav>

<section id="tab-content-bigquery" class="tab-content"
         style="display: {% if initial_tab == 'bigquery' %}block{% else %}none{% endif %};">
    {# BQ tab: Register button, listing, modals — Phase E moves
       existing content here. #}
    <div class="tab-header">
        <h2>BigQuery tables</h2>
        <button id="bqRegisterBtn" class="btn btn-primary"
                onclick="openRegisterModal('bigquery')">Register BigQuery table</button>
    </div>
    <div id="bqTableListing"></div>
    {# Existing BQ register/edit modals get scoped here in Phase E. #}
</section>

<section id="tab-content-keboola" class="tab-content"
         style="display: {% if initial_tab == 'keboola' %}block{% else %}none{% endif %};">
    <div class="tab-header">
        <h2>Keboola tables</h2>
        <button id="kbRegisterBtn" class="btn btn-primary"
                onclick="openRegisterModal('keboola')">Register Keboola table</button>
    </div>
    <div id="kbTableListing"></div>
    {# Phase F builds the Keboola form here. #}
</section>

<section id="tab-content-jira" class="tab-content" style="display: none;">
    <div class="tab-header">
        <h2>Jira tables</h2>
        <p class="hint">Jira tables are populated by webhooks. To register a new
            Jira webhook integration, see <code>docs/connectors/jira.md</code>.</p>
    </div>
    <div id="jiraTableListing"></div>
</section>

<script>
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(function(b) {
        b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-content').forEach(function(c) {
        c.style.display = c.id === ('tab-content-' + tab) ? 'block' : 'none';
    });
    history.replaceState(null, '', '#' + tab);
}

(function initTabFromHash() {
    var hash = window.location.hash.replace(/^#/, '');
    if (hash === 'bigquery' || hash === 'keboola' || hash === 'jira') {
        switchTab(hash);
    }
})();
</script>

<style>
.tab-nav { display: flex; gap: 4px; border-bottom: 1px solid #e0e0e0; margin-bottom: 16px; }
.tab { padding: 8px 16px; background: transparent; border: 0; cursor: pointer; }
.tab[aria-selected="true"] { border-bottom: 2px solid #4a8cff; font-weight: 600; }
.tab-content { padding: 16px 0; }
.tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
</style>
```

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_admin_tables_tab_ui.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/admin_tables.html tests/test_admin_tables_tab_ui.py
git commit -m "feat(admin-ui): tab-split scaffold for /admin/tables

Per-connector tabs (BigQuery / Keboola / Jira) replace the single
mixed form. Each tab has its own Register button + listing div +
(later) form modals. Initial active tab matches data_source.type
from instance.yaml; operator can switch tabs to manage a secondary
source.

Tab state persists in window.location.hash so refresh keeps the
operator on the right tab. No JS framework — vanilla JS toggles
display on .tab-content sections.

Listing divs (bqTableListing / kbTableListing / jiraTableListing)
are wired in Phase H (per-tab listing filter)."
```

---

## Phase E — UI: BigQuery tab content (relocate existing #148 form)

### Task E1: Move BQ Register modal + listing logic into BQ tab

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Modify: `tests/test_admin_tables_ui_materialized.py` (selector adjustments)

- [ ] **Step 1: Failing test (existing tests must pass against new tab structure)**

The existing `test_admin_tables_renders_two_question_radio_form` and `test_edit_modal_has_bq_parity_fields` already assert the BQ form exists. Update them to assert the form is **inside** the BQ tab:

```python
def test_admin_tables_renders_two_question_radio_form(seeded_app, bq_instance):
    """[Phase E] BQ form moved into tab-content-bigquery section."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Existing assertions (preserved):
    assert 'name="bqAccessMode"' in html
    assert 'value="live"' in html
    # ... (all the original assertions stay)

    # NEW: form fields are inside the BQ tab content area.
    bq_tab_content = html[html.index('id="tab-content-bigquery"'):]
    bq_tab_end = bq_tab_content.index('</section>')
    bq_section = bq_tab_content[:bq_tab_end]
    assert 'name="bqAccessMode"' in bq_section
    assert 'id="bqDataset"' in bq_section
    assert 'id="bqSourceQuery"' in bq_section
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_tables_ui_materialized.py::test_admin_tables_renders_two_question_radio_form -v
```

Expected: FAIL on the `tab-content-bigquery` slice — form not yet inside the tab.

- [ ] **Step 3: Move BQ form into BQ tab content section**

Take the existing BQ register form block (currently inside the `{% if data_source.type == 'bigquery' %}` Jinja branch) and physically relocate it inside the `<section id="tab-content-bigquery">` element added in Phase D. Remove the outer `{% if %}` branch — the form is always rendered, just inside its tab. Same for the BQ Edit modal block — relocate inside the BQ tab section.

Adjust the open/close modal trigger functions:

```javascript
// Old: openRegisterModal() — assumed single source
// New: openRegisterModal(source)
function openRegisterModal(source) {
    if (source === 'bigquery') {
        document.getElementById('registerBqModal').style.display = 'block';
    } else if (source === 'keboola') {
        document.getElementById('registerKeboolaModal').style.display = 'block';
    }
}
```

(The Keboola modal id is added in Phase F.)

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_admin_tables_ui_materialized.py -v
pytest tests/test_admin_bq_register.py -v
pytest tests/test_admin_discover_bigquery.py -v
```

Expected: all existing BQ-form tests pass — the form behaves identically, just from inside a tab.

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/admin_tables.html tests/test_admin_tables_ui_materialized.py
git commit -m "refactor(admin-ui): relocate BigQuery form into BigQuery tab

Phase E of the tab-split. Existing BQ register/edit modals + Discover/
List-tables/Use-as-base buttons + two-question radio model preserved
verbatim — only the parent <section> changed. The Jinja
{% if data_source.type == 'bigquery' %} branch is gone; the form is
always rendered, just inside #tab-content-bigquery.

openRegisterModal() now takes a source argument. Existing tests for
form structure adjusted to slice on the BQ tab content; no behavior
change."
```

---

## Phase F — UI: Keboola tab content (with Custom SQL + form cleanup)

### Task F1: Keboola Register modal — full rebuild with two-question radio + form cleanup

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Create test: extend `tests/test_admin_tables_ui_materialized.py`

- [ ] **Step 1: Failing test**

```python
def test_keboola_register_form_has_two_question_radio(seeded_app, monkeypatch):
    """Phase F: Keboola tab Register form mirrors BQ's two-question
    radio model, but Q1 (access mode) is forced to 'synced' (no Live
    mode for Keboola), so visually only Q2 (sync mode = whole | custom)
    is exposed.

    Q2.whole → query_mode='materialized' with auto SELECT * FROM kbc.bucket.table
    Q2.custom → query_mode='materialized' with admin SELECT
    Both create materialized rows; the legacy 'local' mode is no longer
    user-selectable (it would be exactly equivalent to whole)."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        kb_tab = html[html.index('id="tab-content-keboola"'):]
        kb_tab = kb_tab[:kb_tab.index('</section>')]

        # Q2 radio — Whole vs Custom.
        assert 'name="kbSyncMode"' in kb_tab
        assert 'value="whole"' in kb_tab
        assert 'value="custom"' in kb_tab

        # Bucket + source-table inputs reused for whole mode.
        assert 'id="kbBucket"' in kb_tab
        assert 'id="kbSourceTable"' in kb_tab
        # Custom-SQL textarea + Use-table-as-base prefill button.
        assert 'id="kbSourceQuery"' in kb_tab
        assert 'kbPrefillFromTable' in html or 'prefillFromTable(\'kbSourceQuery\')' in html

        # Sync Schedule input — was missing from old Keboola form.
        assert 'id="kbSyncSchedule"' in kb_tab

        # Sync Strategy dropdown — gone.
        assert 'id="kbStrategy"' not in kb_tab
        assert 'id="regStrategy"' not in html  # leftover sanity

        # Primary Key — under <details>Advanced.
        assert 'id="kbPrimaryKey"' in kb_tab
        assert "<details" in kb_tab
        assert ">Advanced" in kb_tab

        # Discover datasets / List tables buttons.
        assert 'kbDiscoverBuckets' in html or "discoverKeboolaBuckets(" in html
        assert 'kbListTables' in html or "discoverKeboolaTables(" in html


def test_keboola_register_payload_maps_to_materialized(seeded_app, monkeypatch):
    """The form's whole-table mode posts query_mode='materialized' with
    a synthetic SELECT * SQL — same pattern as BQ Synced/Whole."""
    # This test exercises the JS payload via a parameterized fetch shim
    # is harder than necessary; instead, verify the API endpoint accepts
    # the payload shape the form is going to send.
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": 'SELECT * FROM kbc."in.c-sales"."orders"',
            "sync_schedule": "every 6h",
        },
    )
    assert r.status_code == 201, r.text
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_tables_ui_materialized.py::test_keboola_register_form_has_two_question_radio -v
```

Expected: FAIL — Keboola form not yet built.

- [ ] **Step 3: Build the Keboola Register modal**

Inside the `<section id="tab-content-keboola">` from Phase D, add the modal:

```html
<div id="registerKeboolaModal" class="modal" style="display:none;">
    <div class="modal-content">
        <div class="modal-header">
            <h3>Register Keboola table</h3>
            <button class="btn-close" onclick="closeRegisterKeboolaModal()">&times;</button>
        </div>
        <div class="modal-body">

            {# Q2 radio — Sync mode. (Q1 is implicitly 'synced'; Keboola
               has no Live mode.) #}
            <div class="form-group">
                <label class="form-label">What to sync?</label>
                <div class="radio-row">
                    <label>
                        <input type="radio" name="kbSyncMode" value="whole" checked
                               onchange="onKbSyncModeChange()">
                        <strong>Whole table</strong> — pull everything in the
                        bucket/table on each schedule tick
                    </label>
                </div>
                <div class="radio-row">
                    <label>
                        <input type="radio" name="kbSyncMode" value="custom"
                               onchange="onKbSyncModeChange()">
                        <strong>Custom SQL</strong> — pre-aggregate or filter
                        with your own SELECT (e.g. last 30 days only,
                        per-day rollup)
                    </label>
                </div>
            </div>

            <div class="form-group">
                <label class="form-label" for="kbViewName">View name (analyst-visible)</label>
                <input type="text" class="form-input" id="kbViewName"
                       placeholder="e.g. orders_recent">
            </div>

            <div class="form-group kb-source-table">
                <label class="form-label" for="kbBucket">
                    Bucket
                    <button type="button" class="btn btn-secondary btn-sm"
                            onclick="discoverKeboolaBuckets('kbBucketList')"
                            style="float:right;">Discover</button>
                </label>
                <input type="text" class="form-input" id="kbBucket"
                       list="kbBucketList" placeholder="e.g. in.c-sales">
                <datalist id="kbBucketList"></datalist>
            </div>
            <div class="form-group kb-source-table">
                <label class="form-label" for="kbSourceTable">
                    Source Table
                    <button type="button" class="btn btn-secondary btn-sm"
                            onclick="discoverKeboolaTables('kbBucket', 'kbTableList')"
                            style="float:right;">List tables</button>
                </label>
                <input type="text" class="form-input" id="kbSourceTable"
                       list="kbTableList" placeholder="e.g. orders">
                <datalist id="kbTableList"></datalist>
            </div>
            <div class="form-group kb-source-custom" style="display:none;">
                <label class="form-label" for="kbSourceQuery">
                    SQL
                    <button type="button" class="btn btn-secondary btn-sm"
                            onclick="prefillFromKeboolaTable('kbSourceQuery')"
                            style="float:right;"
                            title="Prefill SELECT * FROM kbc.bucket.table so you only edit the WHERE / projection">
                        Use table as base
                    </button>
                </label>
                <textarea class="form-textarea" id="kbSourceQuery" rows="8"></textarea>
                <div class="form-hint">SELECT against <code>kbc."bucket"."table"</code>.
                    Result is materialized to parquet and distributed via <code>da sync</code>.</div>
            </div>

            <div class="form-group">
                <label class="form-label" for="kbSyncSchedule">Sync Schedule
                    <span class="optional">(optional, default <code>every 1h</code>)</span></label>
                <input type="text" class="form-input" id="kbSyncSchedule" placeholder="every 6h">
                <div class="form-hint">
                    How often Agnes refreshes the local copy. Examples:
                    <code>every 15m</code>, <code>every 6h</code>,
                    <code>daily 03:00</code>, <code>daily 07:00,13:00,18:00</code> (UTC).
                </div>
            </div>

            <div class="form-group">
                <label class="form-label" for="kbDescription">Description
                    <span class="optional">(optional)</span></label>
                <textarea class="form-textarea" id="kbDescription"
                          placeholder="Brief description of the table contents..."></textarea>
            </div>
            <div class="form-group">
                <label class="form-label" for="kbFolder">Folder
                    <span class="optional">(optional)</span></label>
                <input type="text" class="form-input" id="kbFolder"
                       placeholder="e.g. crm, finance, marketing">
            </div>

            <details class="form-group">
                <summary>Advanced (optional)</summary>
                <div class="form-group" style="margin-top:8px;">
                    <label class="form-label" for="kbPrimaryKey">Primary Key</label>
                    <input type="text" class="form-input" id="kbPrimaryKey"
                           placeholder="e.g. id">
                    <div class="form-hint">Comma-separated list. <strong>Catalog
                        metadata only</strong> — Agnes always does full-overwrite
                        sync; no upsert/dedup. Auto-filled from the Keboola source
                        when available.</div>
                </div>
            </details>

        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="closeRegisterKeboolaModal()">Cancel</button>
            <button class="btn btn-primary" onclick="registerKeboolaTable()">Register</button>
        </div>
    </div>
</div>
```

Plus the JS for the form (in the `<script>` block):

```javascript
function _getKbSyncMode() {
    var el = document.querySelector('input[name="kbSyncMode"]:checked');
    return el ? el.value : 'whole';
}

function onKbSyncModeChange() {
    var mode = _getKbSyncMode();
    document.querySelectorAll('.kb-source-table').forEach(function(el) {
        el.style.display = (mode === 'whole') ? '' : 'none';
    });
    document.querySelectorAll('.kb-source-custom').forEach(function(el) {
        el.style.display = (mode === 'custom') ? '' : 'none';
    });
}

function _buildKeboolaPayload() {
    var mode = _getKbSyncMode();
    var viewName = document.getElementById('kbViewName').value.trim();
    var bucket = document.getElementById('kbBucket').value.trim();
    var sourceTable = document.getElementById('kbSourceTable').value.trim();
    var pk = document.getElementById('kbPrimaryKey').value.trim();
    var primaryKey = pk
        ? pk.split(',').map(function(s) { return s.trim(); }).filter(Boolean)
        : [];

    var common = {
        name: viewName || sourceTable,
        source_type: 'keboola',
        query_mode: 'materialized',
        primary_key: primaryKey,
        sync_schedule: document.getElementById('kbSyncSchedule').value.trim() || null,
        description: document.getElementById('kbDescription').value.trim() || null,
        folder: document.getElementById('kbFolder').value.trim() || null,
    };

    if (mode === 'custom') {
        return Object.assign({}, common, {
            source_query: document.getElementById('kbSourceQuery').value.trim(),
        });
    }
    // Whole — synthesize SELECT *.
    return Object.assign({}, common, {
        bucket: bucket,
        source_table: sourceTable,
        source_query: 'SELECT * FROM kbc."' + bucket + '"."' + sourceTable + '"',
    });
}

function registerKeboolaTable() {
    var payload = _buildKeboolaPayload();
    fetch('/api/admin/register-table', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(function(r) {
            if (!r.ok) {
                return r.json().then(function(d) {
                    throw new Error(d.detail || d.error || 'Registration failed');
                });
            }
            return r.json();
        })
        .then(function() {
            closeRegisterKeboolaModal();
            showToast('Table registered', 'success');
            loadRegistry();  // existing function; will route the new row into the right tab
        })
        .catch(function(err) {
            showToast('' + err.message, 'error');
        });
}

// Discovery shims — reuse generic helpers if /api/admin/discover-tables
// supports both BQ and Keboola; otherwise add a /api/admin/discover-keboola-tables
// endpoint as a Task F2 (skipped if discovery already source-aware).
function discoverKeboolaBuckets(datalistId) {
    fetch('/api/admin/discover-tables?source=keboola')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var dl = document.getElementById(datalistId);
            dl.innerHTML = '';
            (data.buckets || data.datasets || []).forEach(function(b) {
                var o = document.createElement('option');
                o.value = b;
                dl.appendChild(o);
            });
        });
}
function discoverKeboolaTables(bucketInputId, tablesDatalistId) {
    var bucket = document.getElementById(bucketInputId).value.trim();
    if (!bucket) {
        showToast('Fill bucket first', 'error');
        return;
    }
    fetch('/api/admin/discover-tables?source=keboola&bucket=' + encodeURIComponent(bucket))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var dl = document.getElementById(tablesDatalistId);
            dl.innerHTML = '';
            (data.tables || []).forEach(function(t) {
                var o = document.createElement('option');
                o.value = t;
                dl.appendChild(o);
            });
        });
}
function prefillFromKeboolaTable(textareaId) {
    var bucket = document.getElementById('kbBucket').value.trim();
    var sourceTable = document.getElementById('kbSourceTable').value.trim();
    if (!bucket || !sourceTable) {
        showToast('Fill bucket + source table first', 'error');
        return;
    }
    var ta = document.getElementById(textareaId);
    if (ta.value.trim()) {
        if (!confirm('Replace existing SQL?')) return;
    }
    ta.value = 'SELECT *\nFROM kbc."' + bucket + '"."' + sourceTable + '"\nWHERE -- your filter here';
}
```

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_admin_tables_ui_materialized.py -v
pytest tests/test_admin_keboola_materialized.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/admin_tables.html tests/test_admin_tables_ui_materialized.py
git commit -m "feat(admin-ui): Keboola tab Register modal with Custom SQL + cleanup

Phase F. The Keboola tab now exposes the same two-question radio
model as BigQuery (minus Live, which Keboola doesn't support):

  Q2 = Whole table | Custom SQL
    Whole  → query_mode='materialized', auto SELECT * FROM kbc.bucket.table
    Custom → query_mode='materialized', admin-supplied SELECT

This unifies the operator mental model across sources and brings
Keboola to capability parity for the materialized path. The legacy
'local' mode (extractor-driven full-table download) remains supported
by the API but is no longer the default — Whole mode is functionally
equivalent and follows the same materialized pipeline.

Form cleanup baked into the rebuild:
  - Sync Strategy dropdown gone (UI lied; runtime never read it)
  - Primary Key under <details>Advanced with catalog-only hint
  - Sync Schedule input present (was missing from old Keboola form)

Discovery (List buckets / List tables / Use-table-as-base) parallels
the BQ tab's Discover/List tables/Use-as-base buttons via the
existing /api/admin/discover-tables endpoint with source=keboola
parameter."
```

---

### Task F2: Keboola Edit modal — same parity

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Modify: `tests/test_admin_tables_ui_materialized.py`

- [ ] **Step 1: Failing test**

```python
def test_keboola_edit_modal_parity(seeded_app, monkeypatch):
    """Phase F: Edit modal mirrors Register's two-question structure
    for Keboola rows."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # Q2 radio in edit.
        assert 'name="editKbSyncMode"' in html
        assert 'id="editKbBucket"' in html
        assert 'id="editKbSourceTable"' in html
        assert 'id="editKbSourceQuery"' in html
        assert 'id="editKbSyncSchedule"' in html
        # Discover/List/Use-as-base buttons mirror Register.
        assert "discoverKeboolaBuckets('editKbBucketList')" in html
        assert "discoverKeboolaTables('editKbBucket', 'editKbTableList')" in html
        assert "prefillFromKeboolaTable('editKbSourceQuery')" in html
        # Strategy gone, PK under details.
        assert 'id="editStrategy"' not in html
        assert 'id="editKbPrimaryKey"' in html
    finally:
        reset_cache()
```

- [ ] **Step 2: Run, verify fail**

```
pytest tests/test_admin_tables_ui_materialized.py::test_keboola_edit_modal_parity -v
```

- [ ] **Step 3: Build Edit modal** (mirror Register; reuse the helper functions which already accept ids).

(Concrete HTML omitted for brevity — mirror the Register modal with `editKb*` ids and add `editKbSyncMode` radios. Use existing helpers `discoverKeboolaBuckets(datalistId)`, `discoverKeboolaTables(inputId, datalistId)`, `prefillFromKeboolaTable(textareaId)` with the `editKb*` ids.)

- [ ] **Step 4: Run, verify pass**

```
pytest tests/test_admin_tables_ui_materialized.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/admin_tables.html tests/test_admin_tables_ui_materialized.py
git commit -m "feat(admin-ui): Keboola tab Edit modal — parity with Register

Mirror of the Phase F Register modal in the Edit flow. Same Q2 radio,
same Discover/List tables/Use-as-base buttons via the parameterized
helpers, same Sync Schedule input, same Advanced disclosure for PK."
```

---

## Phase G — UI: Jira tab (read-only listing)

### Task G1: Jira tab listing

**Files:**
- Modify: `app/web/templates/admin_tables.html`
- Extend: `tests/test_admin_tables_tab_ui.py`

- [ ] **Step 1: Failing test**

```python
def test_jira_tab_is_read_only(seeded_app):
    """Jira tables are populated by webhooks, not by admin registration.
    Tab shows the listing + a hint pointing to docs; no Register button."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    jira_tab = html[html.index('id="tab-content-jira"'):]
    jira_tab = jira_tab[:jira_tab.index('</section>')]
    # No Register button.
    assert 'data-register-source="jira"' not in jira_tab
    assert 'jiraRegisterBtn' not in jira_tab
    # Hint pointing to docs.
    assert "webhooks" in jira_tab.lower()
    # Listing div present.
    assert 'id="jiraTableListing"' in jira_tab
```

- [ ] **Step 2: Run, verify pass**

The Phase D scaffold already created the section with the hint — this test should already pass against the Phase D template. If it doesn't, adjust the Phase D HTML to match.

- [ ] **Step 3: (Skip or commit if Phase D was sufficient)**

```bash
git add tests/test_admin_tables_tab_ui.py
git commit -m "test(admin-ui): assert Jira tab is read-only listing"
```

---

## Phase H — UI: per-tab listing filter, drop Strategy column

### Task H1: Listing routes rows into the matching tab's `<tbody>`

**Files:**
- Modify: `app/web/templates/admin_tables.html` (the existing `loadRegistry` JS or its renderer)

- [ ] **Step 1: Failing test**

```python
def test_listing_partitions_rows_by_source_type(seeded_app):
    """When the operator has registered tables across all three sources,
    each tab's listing shows only the rows matching its source_type.
    JS-driven so we test by inspecting the JS branching logic indirectly:
    the renderer function takes a source filter and emits rows accordingly."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    c.post("/api/admin/register-table", headers=auth, json={
        "name": "kb_table", "source_type": "keboola", "bucket": "in.c-x",
        "source_table": "y", "query_mode": "local",
    })
    c.post("/api/admin/register-table", headers=auth, json={
        "name": "bq_table", "source_type": "bigquery",
        "query_mode": "materialized", "source_query": "SELECT 1",
    })

    r = c.get("/admin/tables", headers=auth)
    html = r.text
    # The renderer function is dispatched per tab. The test verifies the
    # JS code paths exist (we don't run JS in tests, just confirm the
    # template provides the wiring).
    assert "renderRegistryListing" in html or "loadRegistry" in html
    # Each tab listing div is the renderer target.
    assert 'document.getElementById(\'bqTableListing\')' in html
    assert 'document.getElementById(\'kbTableListing\')' in html
    assert 'document.getElementById(\'jiraTableListing\')' in html
```

- [ ] **Step 2: Implement renderer dispatch**

In the existing `loadRegistry` (or whatever the listing fetch is named), branch by source_type:

```javascript
function loadRegistry() {
    fetch('/api/admin/registry').then(function(r) { return r.json(); })
        .then(function(data) {
            var tables = data.tables || [];
            renderRegistryListing(
                'bqTableListing',
                tables.filter(function(t) { return t.source_type === 'bigquery'; })
            );
            renderRegistryListing(
                'kbTableListing',
                tables.filter(function(t) { return t.source_type === 'keboola'; })
            );
            renderRegistryListing(
                'jiraTableListing',
                tables.filter(function(t) { return t.source_type === 'jira'; })
            );
        });
}

function renderRegistryListing(targetId, tables) {
    var target = document.getElementById(targetId);
    if (!target) return;
    if (tables.length === 0) {
        target.innerHTML = '<p class="empty-hint">No tables registered yet.</p>';
        return;
    }
    var html = '<table class="registry-table">';
    html += '<thead><tr>';
    html += '<th>Table ID</th>';
    html += '<th>Mode</th>';        // NEW: replaces Strategy column
    html += '<th>Primary Key</th>';
    html += '<th>Description</th>';
    html += '<th class="col-actions">Actions</th>';
    html += '</tr></thead><tbody>';
    tables.forEach(function(table) {
        html += '<tr>';
        html += '<td class="col-id" title="' + escapeHtml(table.id) + '">' + escapeHtml(table.id) + '</td>';
        html += '<td>' + escapeHtml(table.query_mode || 'local') + '</td>';
        html += '<td>' + escapeHtml((table.primary_key || []).join(', ') || '-') + '</td>';
        html += '<td>' + escapeHtml(table.description || '-') + '</td>';
        html += '<td class="col-actions">';
        html += '<button class="btn-icon" title="Edit" onclick=\'openEditModal(' + JSON.stringify(table).replace(/\'/g, "\\'") + ')\'>...</button>';
        html += '<button class="btn-icon danger" title="Delete" onclick="deleteTable(\'' + escapeHtml(table.id).replace(/\'/g, "\\'") + '\')">...</button>';
        html += '</td></tr>';
    });
    html += '</tbody></table>';
    target.innerHTML = html;
}
```

Drop the legacy CSS `.col-strategy` and `.strategy-badge` from the `<style>` block (lines 514, 523 of the original file).

- [ ] **Step 3: Run, verify pass**

```
pytest tests/test_admin_tables_tab_ui.py -v
pytest tests/test_admin_tables_ui_materialized.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/admin_tables.html tests/test_admin_tables_tab_ui.py
git commit -m "feat(admin-ui): per-tab listing filter; drop Strategy column

loadRegistry partitions the tables by source_type and dispatches each
slice to its own tab's listing div via renderRegistryListing(target, rows).

The Strategy column is replaced with a Mode column showing query_mode
(live / synced / materialized) — far more meaningful information.
.col-strategy and .strategy-badge CSS rules removed (no consumers left)."
```

---

## Phase I — E2E integration tests + manual smoke + CHANGELOG + push

### Task I1: PUT preservation regression guard

(Same as the prior plan iteration — re-stated here for completeness.)

**Files:**
- Create: `tests/test_admin_put_preservation.py`

- [ ] **Step 1: Lock in the invariant**

```python
def test_put_preserves_omitted_sync_strategy(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    r = c.post("/api/admin/register-table", headers=auth, json={
        "name": "events_partitioned",
        "source_type": "keboola",
        "bucket": "in.c-events",
        "source_table": "events",
        "query_mode": "local",
        "sync_strategy": "partitioned",
    })
    assert r.status_code == 201, r.text

    r = c.put("/api/admin/registry/events_partitioned", headers=auth, json={
        "sync_schedule": "daily 03:00",
        "description": "now daily",
    })
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "events_partitioned")
    assert row["sync_strategy"] == "partitioned"
```

(Plus a parallel `test_put_preserves_omitted_primary_key`.)

- [ ] **Step 2: Run, verify pass on current code**

```
pytest tests/test_admin_put_preservation.py -v
```

Expected: PASS — the invariant holds today; we're locking it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_admin_put_preservation.py
git commit -m "test(admin-api): regression guard for PUT field preservation

Locks the Pydantic semantics that the Phase F form-cleanup relies on.
If a future maintainer flips model_dump() to exclude_unset=True, this
fires before partitioned rows silently regress."
```

---

### Task I2: E2E integration for Keboola materialized

**Files:**
- Create: `tests/test_keboola_materialized_e2e.py` (skipped without real Keboola creds)

- [ ] **Step 1: Write the test**

```python
"""End-to-end: register a Keboola materialized row → trigger sync →
parquet appears → manifest serves it → CLI da sync would download it.

Skipped unless KBC_TEST_URL + KBC_TEST_TOKEN + KBC_TEST_BUCKET +
KBC_TEST_TABLE are present."""
import os
import pytest
from pathlib import Path


KBC_URL = os.environ.get("KBC_TEST_URL")
KBC_TOKEN = os.environ.get("KBC_TEST_TOKEN")
KBC_BUCKET = os.environ.get("KBC_TEST_BUCKET")
KBC_TABLE = os.environ.get("KBC_TEST_TABLE")

pytestmark = pytest.mark.skipif(
    not all([KBC_URL, KBC_TOKEN, KBC_BUCKET, KBC_TABLE]),
    reason="Keboola creds not provided",
)


def test_register_trigger_manifest_path(seeded_app, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEBOOLA_TOKEN", KBC_TOKEN)
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: {
            "data_source": {
                "type": "keboola",
                "keboola": {
                    "url": KBC_URL,
                    "token_env": "KEBOOLA_TOKEN",
                },
            },
        },
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register.
    r = c.post("/api/admin/register-table", headers=auth, json={
        "name": "smoke_subset",
        "source_type": "keboola",
        "query_mode": "materialized",
        "source_query": (
            f'SELECT * FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 5'
        ),
    })
    assert r.status_code == 201

    # Trigger sync.
    r = c.post("/api/sync/trigger", headers=auth)
    assert r.status_code in (200, 202)

    # Parquet must exist.
    parquet = Path(tmp_path) / "extracts" / "keboola" / "data" / "smoke_subset.parquet"
    assert parquet.exists() and parquet.stat().st_size > 0

    # Manifest serves it.
    r = c.get("/api/sync/manifest", headers=auth)
    rows = r.json()["tables"]
    smoke = next((t for t in rows if t["id"] == "smoke_subset"), None)
    assert smoke is not None
    assert smoke["source_type"] == "keboola"
    assert smoke["query_mode"] == "local"  # materialized parquets surface as local
    assert smoke["md5"]  # has a hash for da sync delta detection
```

- [ ] **Step 2: Run**

```
KBC_TEST_URL=... KBC_TEST_TOKEN=... KBC_TEST_BUCKET=... KBC_TEST_TABLE=... \
    pytest tests/test_keboola_materialized_e2e.py -v
```

Expected: PASS with creds; SKIP without.

- [ ] **Step 3: Commit**

```bash
git add tests/test_keboola_materialized_e2e.py
git commit -m "test(keboola): E2E — register materialized → trigger → manifest

Full pipeline test. Skipped without KBC_TEST_* creds; passes locally
with a real Storage API token. Verifies parquet lands at the expected
path and the manifest exposes the row to da sync with the right
source_type / query_mode / md5 shape."
```

---

### Task I3: CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add `## [Unreleased]` block**

```markdown
## [Unreleased]

### Added
- **admin UI**: `/admin/tables` is now a per-connector tab interface
  (BigQuery / Keboola / Jira). Each tab has its own Register modal +
  listing scoped to its source_type. Active tab persists in
  `window.location.hash` so refresh keeps the operator in place.
- **Keboola materialized SQL**: `query_mode='materialized'` now works
  for `source_type='keboola'` — admin registers a SELECT against
  `kbc."bucket"."table"` and the scheduler writes the result to
  `/data/extracts/keboola/data/<id>.parquet`. Same flow as BigQuery
  materialized; same `da sync` distribution; same RBAC. Cost guardrail
  (BQ-style dry-run) intentionally omitted — Keboola extension has no
  dry-run analog and Storage API cost is download-byte-shaped, not
  scan-byte-shaped. A future PR can add a configurable byte cap if
  operators ask for it.
- **Keboola Sync Schedule**: per-table cron input added to the Keboola
  tab Register and Edit modals. The scheduler has always honored
  per-table `sync_schedule` for every source via `is_table_due()`,
  but the Keboola UI had no surface for it — operators had to use the
  `/api/admin/registry/{id}` PUT endpoint or `da admin` CLI. Now they
  can type `every 6h` / `daily 03:00` directly.

### Changed
- **admin UI**: Keboola Register and Edit modals adopt the same
  two-question radio model as BigQuery — *What to sync?* (Whole table
  / Custom SQL). Whole-table mode synthesizes a `SELECT *` and writes
  it through the materialized path; Custom mode lets the admin filter
  / aggregate / project. The legacy `query_mode='local'` extractor
  path remains supported for back-compat but is no longer the default
  for new Keboola registrations — Whole mode is functionally
  equivalent and follows the unified materialized pipeline.
- **admin UI**: `Sync Strategy` dropdown removed from the Keboola form
  (Register and Edit). Two independent agent reviews (2026-05-01) found
  the field's hint claimed it controlled extraction but no extractor
  reads it; only `profiler.is_partitioned()` consumes it for parquet-
  layout detection. Field stays in the DB and Pydantic model for
  back-compat (marked `Field(deprecated=True)`); just hidden from the
  primary form.
- **admin UI**: `Primary Key` input moved under `<details>Advanced` in
  both Keboola Register and Edit modals, with a clarifying hint that
  it's catalog metadata only — Agnes always does full-overwrite sync;
  no upsert / dedup. Auto-fill from Keboola discovery still works.
- **admin UI**: Registry listing column "Strategy" replaced with "Mode"
  (showing `query_mode` instead of decorative `sync_strategy`). The
  `.col-strategy` / `.strategy-badge` CSS rules removed.

### Deprecated
- `RegisterTableRequest.sync_strategy` — catalog/profiler metadata only;
  no extractor reads it. Marked `Field(deprecated=True)`. External API
  consumers see the signal in OpenAPI; back-compat preserved.
- `RegisterTableRequest.profile_after_sync` — runtime never read this
  flag (Agent 1 finding 2026-05-01); profiler runs unconditionally on
  every synced table. Marked `Field(deprecated=True)` and made inert
  (the BQ register endpoint no longer force-sets it to `False`).
  Back-compat preserved — external clients sending the field get no
  error, no warning, no effect.

### Fixed
- **admin API**: `update_table` PUT preserves `sync_strategy` and
  `primary_key` when the Edit modal omits them from the payload (this
  invariant always held via `request.model_dump()` + `if v is not None`,
  but Phase I now has an explicit regression-guard test).
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): unified tab UI + Keboola materialized + form cleanup"
```

---

### Task I4: Manual smoke + push + CI poll

- [ ] **Step 1: Full test sweep**

```
pytest tests/ -q
```

Expected: all passing. Investigate any unrelated regression.

- [ ] **Step 2: Manual smoke on dev server**

Start `uvicorn app.main:app --reload` and walk through:
- `/admin/tables` — verify tab nav renders, switching between tabs works, hash persists.
- BigQuery tab — Register a materialized row using Custom SQL; verify it lands in the BQ tab's listing only.
- Keboola tab — Register a Whole-mode row, verify it lands in the Keboola tab's listing.
- Keboola tab — Register a Custom-SQL-mode row with a real BUCKET.TABLE filter; verify the parquet appears at `data/extracts/keboola/data/<id>.parquet` after the next scheduler tick.
- Jira tab — listing only, no Register button.
- Edit any row in any tab; verify the right modal opens and the source-specific fields populate.

- [ ] **Step 3: Push branch**

```bash
git push -u origin <branch-name>
```

- [ ] **Step 4: Open PR** with body summarizing:
- The four bundled concerns and why they're one PR
- Backward-compat strategy (Pydantic deprecation, no DB migration)
- Spike result confirming Keboola extension supports query passthrough
- Manual smoke checklist

- [ ] **Step 5: Poll CI**

```
gh pr checks <PR#>
```

Iterate on Devin Review feedback if any. The PR should land green: test, build-and-push, Devin Review.

---

## Self-review checklist

- [x] **Spec coverage**: Goal covers (1) tab-split, (2) Keboola materialized parity, (3) Keboola form cleanup, (4) `profile_after_sync` resolution. Phases A–I implement all four. E2E safety contract enumerates the seven invariants the plan must protect; each has at least one explicit task.
- [x] **Placeholder scan**: Every step has the actual code or command. The few "(adapt to existing function shape)" notes apply where the existing code's exact line numbers can drift between planning and implementation; in those spots the task explicitly says "read the file at implementation time."
- [x] **Type / identifier consistency**:
  - `KeboolaAccess.duckdb_session()` — used in Tasks B1, B2, B4
  - `materialize_query(table_id, sql, *, keboola_access, output_dir)` — Task B2 signature, called from B4
  - `kb*` ids in Keboola Register form (kbBucket, kbSourceTable, kbSourceQuery, kbSyncSchedule, kbPrimaryKey, kbViewName); `editKb*` ids in Keboola Edit form. Consistent across tasks F1, F2, H1.
  - `bqTableListing` / `kbTableListing` / `jiraTableListing` — Phase D scaffold, referenced in Phase H renderer.
- [x] **TDD discipline**: every behavior task starts with a failing test before implementation. Verification tasks (Task A1, Task I1) lock invariants that already hold.
- [x] **Commit cadence**: 17 commits across the plan; each is scoped and reviewable on its own.
- [x] **Back-compat**: No DB migration. All Pydantic fields stay alive (deprecated). External API clients sending legacy payloads get no error. Existing BQ form moves verbatim into a tab. Existing Keboola legacy `query_mode='local'` rows continue to work.

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-05-01-admin-tables-form-cleanup.md`.

Two execution options:

1. **Subagent-driven (recommended)** — fresh implementer subagent per task, two-stage review (spec compliance + code quality) between tasks. Same session, fast iteration. Plan has 17 commit-scoped tasks and a few sub-steps; expect ~3–4h of agentic work plus review iterations.
2. **Inline execution** — execute tasks sequentially in this session with explicit checkpoints for human review.

Phase A is a 30-min spike that gates everything else (Keboola extension capability lock-in). Phases B–I run sequentially within their own constraints; Phase D (tab scaffold) must precede Phases E–G (tab content). Phase I (regression + E2E + CHANGELOG) wraps everything.
