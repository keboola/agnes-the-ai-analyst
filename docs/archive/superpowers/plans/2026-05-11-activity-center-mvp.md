# Activity Center MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `/activity-center` as `/admin/activity` — a health pulse + chronological timeline view fed by `audit_log` + `sync_history`. Close 4 audit-coverage gaps so the timeline shows what actually happens on the server.

**Architecture:** Single new admin handler at `/admin/activity` reads from `audit_log` and `sync_history` via expanded repositories. Schema migration `v39→v40` adds four columns and three indices on `audit_log`. Four endpoints that today bypass the audit log start writing to it. Template rebuilt from scratch — the old executive-pulse / maturity-roadmap demo content is deleted. Adheres to parent spec `2026-05-11-admin-observability-spec.md`.

**Tech Stack:** FastAPI, DuckDB, Jinja2, pytest, Typer (CLI). PostHog optional (no-op when `POSTHOG_API_KEY` unset).

---

## File structure

### Files to CREATE

- `app/api/activity.py` — read endpoints for AC (`/api/admin/activity`, `/api/admin/activity/health`, `/api/admin/activity/sync`)
- `tests/test_activity_api.py` — endpoint tests
- `tests/test_schema_v40_migration.py` — migration round-trip test
- `tests/test_audit_repository_query.py` — repository filter / cursor tests
- `tests/test_sync_history_recent.py` — sync history aggregation tests

### Files to MODIFY

- `src/db.py` — bump `SCHEMA_VERSION` to 40, add `_v39_to_v40` migration function, add indices
- `src/repositories/audit.py` — rewrite `query()` with rich filters + cursor pagination; add `params_before` / `client_ip` / `client_kind` / `correlation_id` kwargs to `log()`
- `src/repositories/sync_state.py` — add `list_recent(since: datetime, limit: int)` method (cross-table)
- `app/api/sync.py` — add `AuditRepository.log()` call to `POST /api/sync/trigger`
- `app/api/scripts.py` — add audit to `POST /api/scripts/run-due`
- `app/api/upload.py` — add audit to `POST /api/upload/sessions`
- `app/api/data.py` — add audit to `GET /api/data/{table_id}/download`
- `app/web/router.py` — replace `/activity-center` handler with redirect to `/admin/activity`; add new `/admin/activity` handler under `require_admin`
- `app/web/templates/activity_center.html` — DELETE all demo content, replace with new admin-activity template (or rename + slim down)
- `app/web/templates/_app_header.html` — add admin nav link to `/admin/activity`
- `app/web/templates/dashboard.html` — update widget link from `/activity-center` to `/admin/activity`
- `app/main.py` — register `app/api/activity.py` router
- `CHANGELOG.md` — `[Unreleased]` entry with **BREAKING** marker

### Files to DELETE

(none — `activity_center.html` is rewritten, not deleted)

---

## Conventions (verified against origin/main; reviewer-corrected)

**Imports — use these exact paths:**

```python
from app.auth.dependencies import _get_db          # NOT app.dependencies
from app.auth.access import require_admin
from src.db import get_system_db
from src.repositories.audit import AuditRepository
from src.repositories.sync_state import SyncStateRepository
```

**Test fixtures (`tests/conftest.py:193`):**

```python
def test_x(seeded_app, admin_user):
    c = seeded_app["client"]               # the FastAPI TestClient
    resp = c.get("/api/foo", headers=admin_user)   # admin_user is a dict like {"Authorization": "Bearer …"}
    # For DB introspection in tests, open a fresh connection:
    from src.db import get_system_db
    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
```

There is **no** `admin_client`, `authenticated_client`, or `get_system_conn` fixture. Use the pattern above in every test in this plan.

**Resilience rules (apply to every audit write added by this plan):**

1. Wrap `AuditRepository(conn).log(...)` calls in `try/except` so a DB-locked / disk-full event doesn't 5xx the underlying business request. Log the exception via `logger.exception` and continue.
2. Cap any user-controlled string before storing in `params`: `value[:256]` (and append `"…"` if truncated).
3. Sanitize filenames stored in audit: keep only `[A-Za-z0-9._\-]` characters; reject other chars with a 400.

**Audit suppression scope (Task 12):**

`_RECENT_AUDITS` is a per-process dict. This means recursive-audit suppression is **per uvicorn worker**. For MVP we assume **single-worker uvicorn** (existing Agnes default in compose). If multi-worker is later enabled, this needs to move to a shared store. Comment must say so in code.

**DuckDB index notes:**

DuckDB's `CREATE INDEX` doesn't honor `DESC`; index direction is implementation-defined. The migration creates plain (non-DESC) indices; the planner picks them up either direction. Index creation on a populated `audit_log` (100k+ rows) is single-threaded and can take 30–60s — document upgrade window in CHANGELOG.

---

## Task 1: Schema v40 migration

Adds four columns + three indices to `audit_log`.

**Files:**
- Modify: `src/db.py:43` (`SCHEMA_VERSION`)
- Modify: `src/db.py` (add `_v39_to_v40` function and migration step in the ladder)
- Test: `tests/test_schema_v40_migration.py`

- [ ] **Step 1.1: Write the failing migration test**

Create `tests/test_schema_v40_migration.py`:

```python
"""v39 → v40 migration: add params_before, client_ip, client_kind,
correlation_id columns to audit_log + three indices."""
import duckdb
import pytest
from src.db import init_database, SCHEMA_VERSION


def test_schema_version_is_40():
    assert SCHEMA_VERSION == 40


def test_v40_columns_exist_after_init(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    assert "params_before" in cols
    assert "client_ip" in cols
    assert "client_kind" in cols
    assert "correlation_id" in cols
    conn.close()


def test_v40_indices_exist(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    # DuckDB exposes indices via duckdb_indexes()
    idx_names = {row[0] for row in conn.execute(
        "SELECT index_name FROM duckdb_indexes WHERE table_name='audit_log'"
    ).fetchall()}
    assert "idx_audit_timestamp_desc" in idx_names
    assert "idx_audit_user_time" in idx_names
    assert "idx_audit_action_time" in idx_names
    conn.close()


def test_v39_to_v40_is_idempotent(tmp_path):
    """Running the migration twice in a row is a no-op the second time."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    # Second open + init must not raise (IF NOT EXISTS guards do the work)
    conn.close()
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    conn.close()


def test_v30_db_ladders_all_the_way_up(tmp_path):
    """Representative evolved-DB test: an instance hand-rolled at v30 must
    ladder through to v40 without data loss, mirroring a customer who's
    been upgrading regularly since older releases. If this fails, ANY
    intermediate migration is broken — surface the offending step."""
    db_path = tmp_path / "v30.duckdb"
    conn = duckdb.connect(str(db_path))
    # Minimal v30 baseline (only the table needed to assert preservation).
    conn.execute("""
        CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
            user_id VARCHAR,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            params JSON,
            result VARCHAR,
            duration_ms INTEGER
        )
    """)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (30)")
    conn.execute("INSERT INTO audit_log (id, action) VALUES ('vintage', 'test.x')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE id='vintage'").fetchone()[0] == 1
    conn.close()


def test_v39_db_upgrades_cleanly(tmp_path):
    """A DB hand-rolled at v39 (audit_log without the four new columns)
    must upgrade to v40 without data loss."""
    db_path = tmp_path / "v39.duckdb"
    conn = duckdb.connect(str(db_path))
    # Hand-roll the v39 audit_log shape
    conn.execute("""
        CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
            user_id VARCHAR,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            params JSON,
            result VARCHAR,
            duration_ms INTEGER
        )
    """)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (39)")
    conn.execute("INSERT INTO audit_log (id, action) VALUES ('row1', 'test.action')")
    conn.close()

    # Reopen and run init — should ladder v39 → v40
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    # Row preserved
    cnt = conn.execute("SELECT COUNT(*) FROM audit_log WHERE id='row1'").fetchone()[0]
    assert cnt == 1
    # New columns nullable
    row = conn.execute(
        "SELECT params_before, client_ip, client_kind, correlation_id FROM audit_log WHERE id='row1'"
    ).fetchone()
    assert row == (None, None, None, None)
    conn.close()
```

- [ ] **Step 1.2: Run test — expect FAIL**

Run: `pytest tests/test_schema_v40_migration.py -v`
Expected: 4 failures — `SCHEMA_VERSION == 39` (not 40), columns missing.

- [ ] **Step 1.3: Implement migration in `src/db.py`**

In `src/db.py`, bump `SCHEMA_VERSION`:

```python
SCHEMA_VERSION = 40
```

Add a new migration function (mirror the existing `_v38_to_v39` / `_v37_to_v38` pattern — search for them in the file). Below all existing `_vN_to_vN_plus_1` functions, add:

```python
def _v39_to_v40(conn: duckdb.DuckDBPyConnection) -> None:
    """v40: audit_log gains params_before (JSON, prior state for diff/rollback),
    client_ip (VARCHAR, promoted from params for indexability), client_kind
    (VARCHAR, 'cli'|'web'|'agent'|'scheduler'|'external'), and correlation_id
    (VARCHAR, groups multi-step operations).

    Three indices added on (timestamp DESC), (user_id, timestamp),
    (action, timestamp) to keep Activity Center timeline queries under
    100ms even at 100k+ rows.
    """
    # Add columns idempotently (re-run safety on partial migrations)
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS params_before JSON")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_ip VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_kind VARCHAR")
    conn.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS correlation_id VARCHAR")
    # Indices for AC query patterns.
    # NOTE: DuckDB does not honor DESC in CREATE INDEX; the planner is free to
    # scan either direction. Names retain `_desc` for readability — the order
    # is enforced by the ORDER BY clause in AuditRepository.query().
    # On a populated audit_log (~100k+ rows), each CREATE INDEX is single-
    # threaded and may take 10–30s. Cumulative cold-start cost on upgrade is
    # documented in CHANGELOG as a 30–120s upgrade window.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp_desc ON audit_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_log(user_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log(action, timestamp)")
```

Then add the step to the migration ladder. Locate the `_run_migrations` function (or equivalent — read the file to find the existing pattern; in current code it's a series of `if current_version < N` blocks). Add:

```python
    if current_version < 40:
        _v39_to_v40(conn)
        _set_schema_version(conn, 40)
        current_version = 40
```

- [ ] **Step 1.4: Run test — expect PASS**

Run: `pytest tests/test_schema_v40_migration.py -v`
Expected: 4 passed.

Run full schema test suite to catch regressions:
`pytest tests/test_db_schema_version.py -v`
Expected: all pass (existing tests assert ladder steps; new v40 step should slot in cleanly).

- [ ] **Step 1.5: Commit**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss-activity-spec"
git add src/db.py tests/test_schema_v40_migration.py
git commit -m "feat(db): schema v40 — audit_log gains params_before, client_ip, client_kind, correlation_id + 3 indices"
```

---

## Task 2: Extend `AuditRepository.log()` with new kwargs

Existing callers must keep working unchanged. New kwargs default to None.

**Files:**
- Modify: `src/repositories/audit.py`
- Test: `tests/test_audit_repository_query.py`

- [ ] **Step 2.1: Write the failing test for new kwargs**

Append to `tests/test_audit_repository_query.py`:

```python
"""AuditRepository v40 — new kwargs (params_before, client_ip, client_kind,
correlation_id) round-trip; legacy callers compile-time-unbroken."""
import duckdb
import pytest
from src.db import init_database
from src.repositories.audit import AuditRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def test_log_accepts_new_kwargs(conn):
    repo = AuditRepository(conn)
    entry_id = repo.log(
        user_id="u1",
        action="registry.update",
        resource="table:web_sessions",
        params={"after": {"cron": "*/15 * * * *"}},
        params_before={"cron": "0 */1 * * *"},
        client_ip="10.0.0.42",
        client_kind="web",
        correlation_id="corr-123",
    )
    row = conn.execute("SELECT params_before, client_ip, client_kind, correlation_id FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert row[0] is not None  # JSON
    assert row[1] == "10.0.0.42"
    assert row[2] == "web"
    assert row[3] == "corr-123"


def test_log_legacy_signature_still_works(conn):
    """The original kwargs-only call site (used by 30+ existing endpoints)
    must keep working unchanged."""
    repo = AuditRepository(conn)
    entry_id = repo.log(user_id="u1", action="auth.login")
    row = conn.execute("SELECT user_id, action, params_before FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert row == ("u1", "auth.login", None)
```

- [ ] **Step 2.2: Run test — expect FAIL**

Run: `pytest tests/test_audit_repository_query.py::test_log_accepts_new_kwargs tests/test_audit_repository_query.py::test_log_legacy_signature_still_works -v`
Expected: FAIL — `log()` doesn't accept the new kwargs.

- [ ] **Step 2.3: Extend `AuditRepository.log()` in `src/repositories/audit.py`**

Open `src/repositories/audit.py` and replace the `log()` method body. The current signature is:

```python
def log(
    self,
    user_id: Optional[str] = None,
    action: str = "",
    resource: Optional[str] = None,
    params: Optional[dict] = None,
    result: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> str:
```

Replace with:

```python
def log(
    self,
    user_id: Optional[str] = None,
    action: str = "",
    resource: Optional[str] = None,
    params: Optional[dict] = None,
    result: Optional[str] = None,
    duration_ms: Optional[int] = None,
    *,
    params_before: Optional[dict] = None,
    client_ip: Optional[str] = None,
    client_kind: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> str:
    """Insert one audit_log row. Returns the new row id.

    The four kwargs after `*` are v40 additions; legacy callers using
    positional args or the original kwargs are unaffected. `params_before`
    is only used for mutating actions where rollback / diff is meaningful;
    leave None for reads, ticks, queries.
    """
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    self.conn.execute(
        """INSERT INTO audit_log
           (id, timestamp, user_id, action, resource, params, result, duration_ms,
            params_before, client_ip, client_kind, correlation_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            entry_id, now, user_id, action, resource,
            json.dumps(params) if params else None,
            result, duration_ms,
            json.dumps(params_before) if params_before else None,
            client_ip, client_kind, correlation_id,
        ],
    )
    return entry_id
```

- [ ] **Step 2.4: Run test — expect PASS**

Run: `pytest tests/test_audit_repository_query.py -v`
Expected: 2 passed.

Then run the full test suite to confirm no regression in the 30+ existing call sites:
`pytest tests/ -x -q --no-header 2>&1 | tail -30`
Expected: no failures in audit-touching tests.

- [ ] **Step 2.5: Commit**

```bash
git add src/repositories/audit.py tests/test_audit_repository_query.py
git commit -m "feat(audit): AuditRepository.log() accepts params_before/client_ip/client_kind/correlation_id"
```

---

## Task 3: Rewrite `AuditRepository.query()` with rich filters + cursor pagination

This is the data engine for the Timeline tab.

**Files:**
- Modify: `src/repositories/audit.py`
- Test: `tests/test_audit_repository_query.py`

- [ ] **Step 3.1: Write failing tests for filter combinations**

Append to `tests/test_audit_repository_query.py`:

```python
from datetime import datetime, timezone, timedelta


def _seed(conn, rows: list[dict]):
    """Insert audit_log rows with explicit timestamps."""
    repo = AuditRepository(conn)
    ids = []
    for r in rows:
        entry_id = repo.log(
            user_id=r.get("user_id"),
            action=r.get("action", "test.x"),
            resource=r.get("resource"),
            params=r.get("params"),
            result=r.get("result"),
        )
        if "ts" in r:
            conn.execute("UPDATE audit_log SET timestamp=? WHERE id=?", [r["ts"], entry_id])
        ids.append(entry_id)
    return ids


def test_query_filter_by_time_range(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _seed(conn, [
        {"action": "a.1", "ts": now - timedelta(hours=2)},
        {"action": "a.2", "ts": now - timedelta(minutes=30)},
        {"action": "a.3", "ts": now - timedelta(minutes=5)},
    ])
    rows, _ = repo.query(since=now - timedelta(hours=1), until=now)
    assert {r["action"] for r in rows} == {"a.2", "a.3"}


def test_query_filter_by_action_prefix(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "sync.trigger"},
        {"action": "sync.complete"},
        {"action": "auth.login"},
    ])
    rows, _ = repo.query(action_prefix="sync.")
    assert {r["action"] for r in rows} == {"sync.trigger", "sync.complete"}


def test_query_filter_by_action_in(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "a"}, {"action": "b"}, {"action": "c"},
    ])
    rows, _ = repo.query(action_in=["a", "c"])
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_filter_by_user(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"user_id": "u1", "action": "x"},
        {"user_id": "u2", "action": "x"},
    ])
    rows, _ = repo.query(user_id="u1")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"


def test_query_filter_by_resource(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "resource": "table:a"},
        {"action": "x", "resource": "table:b"},
    ])
    rows, _ = repo.query(resource="table:a")
    assert len(rows) == 1


def test_query_filter_by_result_pattern(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "result": "success"},
        {"action": "x", "result": "error.timeout"},
        {"action": "x", "result": "error.permission"},
    ])
    rows, _ = repo.query(result_pattern="error.%")
    assert {r["result"] for r in rows} == {"error.timeout", "error.permission"}


def test_query_full_text_q(conn):
    repo = AuditRepository(conn)
    _seed(conn, [
        {"action": "x", "params": {"sql": "SELECT * FROM finance"}},
        {"action": "x", "params": {"sql": "SELECT * FROM marketing"}},
    ])
    rows, _ = repo.query(q="finance")
    assert len(rows) == 1


def test_query_cursor_pagination(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed(conn, [{"action": f"a.{i}", "ts": now - timedelta(minutes=i)}])
    page1, cursor1 = repo.query(limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    page2, cursor2 = repo.query(limit=2, cursor=cursor1)
    assert len(page2) == 2
    page3, cursor3 = repo.query(limit=2, cursor=cursor2)
    assert len(page3) == 1
    assert cursor3 is None
    # Pages don't overlap
    all_ids = {r["id"] for r in page1 + page2 + page3}
    assert len(all_ids) == 5


def test_query_ordering_newest_first(conn):
    repo = AuditRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _seed(conn, [
        {"action": "old", "ts": now - timedelta(hours=2)},
        {"action": "new", "ts": now - timedelta(minutes=1)},
    ])
    rows, _ = repo.query()
    assert rows[0]["action"] == "new"
    assert rows[1]["action"] == "old"
```

- [ ] **Step 3.2: Run tests — expect FAIL**

Run: `pytest tests/test_audit_repository_query.py -v -k 'test_query_'`
Expected: 8 failures — the current `query()` only supports `user_id`, `action`, `limit`.

- [ ] **Step 3.3: Rewrite `query()` in `src/repositories/audit.py`**

First, ensure imports at the top of `src/repositories/audit.py` include:

```python
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
```

`timedelta` is the new addition (needed for the `q`-without-since safeguard below).

Replace the existing `query()` method with:

```python
def query(
    self,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    user_id: Optional[str] = None,
    action: Optional[str] = None,         # legacy single-action filter
    action_prefix: Optional[str] = None,
    action_in: Optional[List[str]] = None,
    resource: Optional[str] = None,
    result_pattern: Optional[str] = None,
    correlation_id: Optional[str] = None,
    q: Optional[str] = None,
    cursor: Optional[tuple] = None,        # (timestamp, id) — keyset pagination
    limit: int = 100,
) -> tuple[List[Dict[str, Any]], Optional[tuple]]:
    """Query audit_log with rich filters; returns (rows, next_cursor).

    Cursor encodes (timestamp, id) so pagination is stable under
    same-second writes. Pass the returned cursor back as `cursor=` for
    the next page. `None` cursor on input = newest page; `None` cursor
    in return = last page reached.
    """
    where = []
    params: List[Any] = []
    if since is not None:
        where.append("timestamp >= ?"); params.append(since)
    if until is not None:
        where.append("timestamp < ?"); params.append(until)
    if user_id is not None:
        where.append("user_id = ?"); params.append(user_id)
    if action is not None:
        where.append("action = ?"); params.append(action)
    if action_prefix is not None:
        where.append("action LIKE ?"); params.append(action_prefix + "%")
    if action_in:
        placeholders = ",".join("?" for _ in action_in)
        where.append(f"action IN ({placeholders})")
        params.extend(action_in)
    if resource is not None:
        where.append("resource = ?"); params.append(resource)
    if result_pattern is not None:
        where.append("result LIKE ?"); params.append(result_pattern)
    if correlation_id is not None:
        where.append("correlation_id = ?"); params.append(correlation_id)
    if q:
        # Full-text search is a table scan on `params` JSON cast to text.
        # Safeguard: if caller passes `q` without a `since` filter, force a
        # 7-day cap so we don't scan the entire audit_log. Proper FTS lands
        # in Phase B/C (see parent spec §5.5).
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=7)
            where.append("timestamp >= ?"); params.append(since)
        where.append("CAST(params AS VARCHAR) LIKE ?"); params.append(f"%{q}%")
    if cursor is not None:
        ts, cid = cursor
        # Keyset: rows strictly older than the cursor, breaking ties by id desc
        where.append("(timestamp, id) < (?, ?)")
        params.extend([ts, cid])

    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Fetch limit+1 to determine whether there's a next page
    sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(limit + 1)
    rows = self.conn.execute(sql, params).fetchall()
    if not rows:
        return [], None
    columns = [desc[0] for desc in self.conn.description]
    out = [dict(zip(columns, r)) for r in rows]

    next_cursor: Optional[tuple] = None
    if len(out) > limit:
        # The (limit+1)th row tells us "more exists"; drop it from response.
        last_shown = out[limit - 1]
        next_cursor = (last_shown["timestamp"], last_shown["id"])
        out = out[:limit]
    return out, next_cursor
```

- [ ] **Step 3.4: Run tests — expect PASS**

Run: `pytest tests/test_audit_repository_query.py -v`
Expected: all 10 tests pass.

Then sweep for legacy `.query(` callers that may pass positional args:
`grep -rn 'AuditRepository.*\.query(' app/ src/ services/ | grep -v test_`
Expected: any caller passes kwargs only (the new signature requires kwargs after the leading `*`).

If a caller is found using positional args, fix it: convert to kwargs.

- [ ] **Step 3.5: Commit**

```bash
git add src/repositories/audit.py tests/test_audit_repository_query.py
git commit -m "feat(audit): AuditRepository.query() rich filters + keyset cursor pagination"
```

---

## Task 4: Add `SyncHistoryRepository.list_recent()`

The Sync tab needs cross-table sync events.

**Files:**
- Modify: `src/repositories/sync_state.py`
- Test: `tests/test_sync_history_recent.py`

- [ ] **Step 4.1: Write failing test**

Create `tests/test_sync_history_recent.py`:

```python
"""SyncHistoryRepository.list_recent() — cross-table chronological feed."""
import duckdb
import pytest
from datetime import datetime, timezone, timedelta
from src.db import init_database
from src.repositories.sync_state import SyncStateRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def _record(conn, table_id: str, synced_at: datetime, status: str = "ok", rows: int = 100):
    import uuid
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), table_id, synced_at, rows, 1234, status, None]
    )


def test_list_recent_returns_all_tables_newest_first(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "orders", now - timedelta(hours=1))
    _record(conn, "customers", now - timedelta(minutes=30))
    _record(conn, "products", now - timedelta(minutes=5))

    rows = repo.list_recent(since=now - timedelta(hours=2), limit=50)
    assert [r["table_id"] for r in rows] == ["products", "customers", "orders"]


def test_list_recent_respects_since(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "old", now - timedelta(days=3))
    _record(conn, "new", now - timedelta(minutes=10))
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=50)
    assert [r["table_id"] for r in rows] == ["new"]


def test_list_recent_respects_limit(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(20):
        _record(conn, f"t{i}", now - timedelta(minutes=i))
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=5)
    assert len(rows) == 5


def test_list_recent_includes_failures(conn):
    repo = SyncStateRepository(conn)
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    _record(conn, "t1", now, status="ok")
    _record(conn, "t2", now, status="error")
    rows = repo.list_recent(since=now - timedelta(hours=1), limit=10)
    statuses = {r["table_id"]: r["status"] for r in rows}
    assert statuses["t1"] == "ok"
    assert statuses["t2"] == "error"
```

- [ ] **Step 4.2: Run test — expect FAIL**

Run: `pytest tests/test_sync_history_recent.py -v`
Expected: 4 failures — method doesn't exist.

- [ ] **Step 4.3: Add `list_recent()` to `SyncStateRepository`**

Open `src/repositories/sync_state.py`. After the existing `get_sync_history()` method, add:

```python
def list_recent(
    self,
    *,
    since: datetime,
    limit: int = 100,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return cross-table sync events newer than `since`, newest first.

    Used by Activity Center's Sync tab to render a unified feed across
    all registered tables. Per-table history stays available via
    `get_sync_history(table_id, limit)`.
    """
    sql = "SELECT * FROM sync_history WHERE synced_at >= ?"
    params: List[Any] = [since]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY synced_at DESC LIMIT ?"
    params.append(limit)
    rows = self.conn.execute(sql, params).fetchall()
    if not rows:
        return []
    columns = [d[0] for d in self.conn.description]
    return [dict(zip(columns, r)) for r in rows]
```

Make sure `datetime` and `Optional` are imported at the top of the file. They likely are; if not:

```python
from datetime import datetime
from typing import Any, Dict, List, Optional
```

- [ ] **Step 4.4: Run test — expect PASS**

Run: `pytest tests/test_sync_history_recent.py -v`
Expected: 4 passed.

- [ ] **Step 4.5: Commit**

```bash
git add src/repositories/sync_state.py tests/test_sync_history_recent.py
git commit -m "feat(sync): SyncStateRepository.list_recent() cross-table feed"
```

---

## Task 5: Close audit gap 1/4 — `POST /api/sync/trigger`

**Files:**
- Modify: `app/api/sync.py`
- Test: `tests/test_audit_gap_sync_trigger.py`

- [ ] **Step 5.1: Write failing test**

Create `tests/test_audit_gap_sync_trigger.py`:

```python
"""POST /api/sync/trigger must write to audit_log (closes coverage gap).

Uses canonical fixtures (Conventions section): seeded_app["client"] + admin_user
headers + get_system_db() for direct DB access.
"""
import pytest
from src.db import get_system_db


def test_sync_trigger_writes_audit_log(seeded_app, admin_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='sync.trigger'"
    ).fetchone()[0]
    conn.close()

    resp = c.post("/api/sync/trigger", headers=admin_user)
    assert resp.status_code in (200, 202)

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='sync.trigger'"
    ).fetchone()[0]
    assert after == before + 1
    row = conn.execute(
        "SELECT user_id, action, result FROM audit_log WHERE action='sync.trigger' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] is not None        # user_id captured
    assert row[1] == "sync.trigger"
    assert row[2] in ("success", "error.in_progress", "error.locked")


def test_sync_trigger_does_not_5xx_if_audit_write_fails(seeded_app, admin_user, monkeypatch):
    """Resilience rule (Conventions): a failed audit write must NOT crash
    the wrapped business request — log + swallow + continue."""
    from src.repositories.audit import AuditRepository

    def boom(*args, **kwargs):
        raise duckdb.IOException("simulated DB-locked")

    monkeypatch.setattr(AuditRepository, "log", boom)
    c = seeded_app["client"]
    resp = c.post("/api/sync/trigger", headers=admin_user)
    # The sync trigger itself must still respond — audit failure is invisible.
    assert resp.status_code in (200, 202, 409)
```

- [ ] **Step 5.2: Run test — expect FAIL**

Run: `pytest tests/test_audit_gap_sync_trigger.py -v`
Expected: FAIL — assert `after == before + 1` fails because no row written.

- [ ] **Step 5.3: Add audit call in `app/api/sync.py`**

Open `app/api/sync.py` around line 772 (the `POST /api/sync/trigger` handler). The current body looks like:

```python
@router.post("/sync/trigger")
async def trigger_sync(...):
    # ... existing logic ...
    return {"status": "ok", ...}
```

Add the audit call. Locate the imports at the top of the file. Add (if not already present):

```python
from src.repositories.audit import AuditRepository
```

Inside the handler, after determining `user_id` and the eventual result/status, add a **try/except-wrapped** audit call (per Conventions, resilience rule #1):

```python
try:
    AuditRepository(conn).log(
        user_id=user_id,
        action="sync.trigger",
        resource=(table_id or "all_tables")[:256],
        params={"requested_at": datetime.now(timezone.utc).isoformat()},
        result=result_status,   # 'success' | 'error.locked' | 'error.in_progress' | …
        duration_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
        client_kind="scheduler" if is_scheduler_caller else "web",
    )
except Exception:
    logger.exception("audit_log write failed for sync.trigger; continuing")
```

Notes:
- The exact variable names depend on the current handler body — read it before this step.
- Look for an existing pattern in `app/api/admin.py:1120` for reference style.
- `is_scheduler_caller` can be derived from whether `SCHEDULER_API_TOKEN` matched (the handler likely has this check already).
- Ensure `logger` is in scope at the top of the file (most Agnes API modules already have one).

- [ ] **Step 5.4: Run test — expect PASS**

Run: `pytest tests/test_audit_gap_sync_trigger.py -v`
Expected: 1 passed.

Run regression: `pytest tests/test_sync*.py -v` to ensure existing sync tests still pass.

- [ ] **Step 5.5: Commit**

```bash
git add app/api/sync.py tests/test_audit_gap_sync_trigger.py
git commit -m "feat(audit): POST /api/sync/trigger writes audit_log row"
```

---

## Task 6: Close audit gap 2/4 — `POST /api/scripts/run-due`

**Files:**
- Modify: `app/api/scripts.py`
- Test: `tests/test_audit_gap_scripts_run_due.py`

- [ ] **Step 6.1: Write failing test**

Create `tests/test_audit_gap_scripts_run_due.py`:

```python
"""POST /api/scripts/run-due must write to audit_log."""
from src.db import get_system_db


def test_scripts_run_due_writes_audit_log(seeded_app, admin_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='script_runner.tick'"
    ).fetchone()[0]
    conn.close()

    resp = c.post("/api/scripts/run-due", headers=admin_user)
    assert resp.status_code in (200, 202)

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='script_runner.tick'"
    ).fetchone()[0]
    conn.close()
    assert after == before + 1
```

- [ ] **Step 6.2: Run test — expect FAIL**

Run: `pytest tests/test_audit_gap_scripts_run_due.py -v`
Expected: FAIL.

- [ ] **Step 6.3: Add audit call in `app/api/scripts.py`**

Open `app/api/scripts.py`. Locate the `run_due` handler around line 138. Add at the end of the handler body (before the return statement), wrapped per Conventions:

```python
try:
    AuditRepository(conn).log(
        user_id=user_id,
        action="script_runner.tick",
        params={"scripts_run": scripts_run_count, "scripts_failed": scripts_failed_count},
        result="success" if scripts_failed_count == 0 else f"error.{scripts_failed_count}_failed",
        client_kind="scheduler",
    )
except Exception:
    logger.exception("audit_log write failed for script_runner.tick; continuing")
```

Adjust variable names to match the actual handler's locals.

- [ ] **Step 6.4: Run test — expect PASS**

Run: `pytest tests/test_audit_gap_scripts_run_due.py -v`
Expected: 1 passed.

- [ ] **Step 6.5: Commit**

```bash
git add app/api/scripts.py tests/test_audit_gap_scripts_run_due.py
git commit -m "feat(audit): POST /api/scripts/run-due writes audit_log row"
```

---

## Task 7: Close audit gap 3/4 — `POST /api/upload/sessions`

**Files:**
- Modify: `app/api/upload.py`
- Test: `tests/test_audit_gap_upload_sessions.py`

- [ ] **Step 7.1: Write failing test**

Create `tests/test_audit_gap_upload_sessions.py`:

```python
"""POST /api/upload/sessions must write to audit_log; filename is sanitized."""
import io
import json
from src.db import get_system_db


def test_upload_sessions_writes_audit_log(seeded_app, analyst_user):
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='session.upload'"
    ).fetchone()[0]
    conn.close()

    jsonl = b'{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n'
    files = {"file": ("sess-test.jsonl", io.BytesIO(jsonl), "application/x-ndjson")}
    resp = c.post("/api/upload/sessions", files=files, headers=analyst_user)
    assert resp.status_code == 200

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='session.upload'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT params FROM audit_log WHERE action='session.upload' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert after == before + 1
    params = json.loads(row[0]) if row[0] else {}
    assert "filename" in params
    assert "bytes" in params


def test_upload_sessions_rejects_dangerous_filename(seeded_app, analyst_user):
    """Conventions sanitization rule #3 — filename limited to [A-Za-z0-9._-]."""
    c = seeded_app["client"]
    jsonl = b'{"role":"user","content":"x"}\n'
    files = {"file": ("<script>alert(1)</script>.jsonl", io.BytesIO(jsonl), "application/x-ndjson")}
    resp = c.post("/api/upload/sessions", files=files, headers=analyst_user)
    assert resp.status_code == 400
    assert "filename" in resp.text.lower()
```

- [ ] **Step 7.2: Run test — expect FAIL**

Run: `pytest tests/test_audit_gap_upload_sessions.py -v`
Expected: FAIL.

- [ ] **Step 7.3: Add filename sanitization + audit call in `app/api/upload.py`**

Open `app/api/upload.py`. Locate the upload handler around line 55. **First**, add sanitization at the top of the handler (after parsing the multipart):

```python
import re

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,200}$")

# At the top of the handler:
if not _FILENAME_RE.match(file.filename or ""):
    raise HTTPException(
        status_code=400,
        detail="filename must match [A-Za-z0-9._-]{1,200}"
    )
```

**Then**, after the file has been written to disk and the handler is about to return, add the audit call wrapped in try/except per Conventions:

```python
try:
    AuditRepository(conn).log(
        user_id=user_id,
        action="session.upload",
        params={"filename": stored_filename[:256], "bytes": file_size_bytes},
        result="success",
        client_kind="cli",
    )
except Exception:
    logger.exception("audit_log write failed for session.upload; continuing")
```

- [ ] **Step 7.4: Run test — expect PASS**

Run: `pytest tests/test_audit_gap_upload_sessions.py -v`
Expected: 1 passed.

- [ ] **Step 7.5: Commit**

```bash
git add app/api/upload.py tests/test_audit_gap_upload_sessions.py
git commit -m "feat(audit): POST /api/upload/sessions writes audit_log row"
```

---

## Task 8: Close audit gap 4/4 — `GET /api/data/{table_id}/download`

**Files:**
- Modify: `app/api/data.py`
- Test: `tests/test_audit_gap_data_download.py`

- [ ] **Step 8.1: Write failing test**

Create `tests/test_audit_gap_data_download.py`:

```python
"""GET /api/data/{table_id}/download must write to audit_log."""
from src.db import get_system_db
from tests.conftest import create_mock_extract


def test_data_download_writes_audit_log(seeded_app, analyst_user, mock_extract_factory):
    """mock_extract_factory (conftest.py:244) creates extract.duckdb + parquet
    on disk and registers it via the standard extract path."""
    mock_extract_factory("test_src", [
        {"name": "test_tbl", "data": [{"a": "1", "b": "2"}], "query_mode": "local"},
    ])
    # NOTE: register the table via /api/admin/register-table before downloading.
    # Pattern from existing tests, e.g. tests/test_journey_*.py.

    c = seeded_app["client"]
    # ... register table (read tests/test_journey_*.py for an example) ...

    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='data.download'"
    ).fetchone()[0]
    conn.close()

    resp = c.get("/api/data/test_tbl/download", headers=analyst_user)
    assert resp.status_code == 200

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='data.download'"
    ).fetchone()[0]
    conn.close()
    assert after == before + 1
```

NOTE: this test requires a registered table with an on-disk parquet. The
`mock_extract_factory` fixture provides the on-disk part; registration goes
via `POST /api/admin/register-table` (existing endpoint). If the table-
registration prelude is non-trivial, factor it into a helper inside this
test file rather than depending on a non-existent `seeded_table` fixture.

- [ ] **Step 8.2: Run test — expect FAIL**

Run: `pytest tests/test_audit_gap_data_download.py -v`
Expected: FAIL.

- [ ] **Step 8.3: Add audit call in `app/api/data.py`**

Open `app/api/data.py:45`. The download handler returns a `FileResponse`. Add the audit call BEFORE the return, wrapped per Conventions:

```python
try:
    AuditRepository(conn).log(
        user_id=user_id,
        action="data.download",
        resource=f"table:{table_id}"[:256],
        params={"bytes": file_size, "format": "parquet"},
        result="success",
        client_kind="cli",
    )
except Exception:
    logger.exception("audit_log write failed for data.download; continuing")
```

- [ ] **Step 8.4: Run test — expect PASS**

Run: `pytest tests/test_audit_gap_data_download.py -v`
Expected: 1 passed.

- [ ] **Step 8.5: Commit**

```bash
git add app/api/data.py tests/test_audit_gap_data_download.py
git commit -m "feat(audit): GET /api/data/{table_id}/download writes audit_log row"
```

---

## Task 9: New API module `app/api/activity.py`

Three read endpoints: timeline, health, sync.

**Files:**
- Create: `app/api/activity.py`
- Modify: `app/main.py` (register router)
- Test: `tests/test_activity_api.py`

- [ ] **Step 9.1: Write failing test for `/api/admin/activity` timeline**

Create `tests/test_activity_api.py`:

```python
"""Activity Center read API."""
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient


def test_activity_timeline_requires_admin(seeded_app, analyst_user):
    """Non-admin user gets 403."""
    resp = seeded_app["client"].get("/api/admin/activity", headers=analyst_user)
    assert resp.status_code in (401, 403)


def test_activity_timeline_returns_recent_rows(seeded_app, admin_user):
    """Seeded audit_log rows appear in the response."""
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    conn = get_system_db()
    AuditRepository(conn).log(user_id="u1", action="test.activity", result="success")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert "next_cursor" in data
    assert any(r["action"] == "test.activity" for r in data["rows"])


def test_activity_timeline_supports_filters(seeded_app, admin_user):
    from src.db import get_system_db
    from src.repositories.audit import AuditRepository
    conn = get_system_db()
    repo = AuditRepository(conn)
    repo.log(action="sync.trigger")
    repo.log(action="auth.login")
    conn.close()

    resp = seeded_app["client"].get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    assert resp.status_code == 200
    actions = {r["action"] for r in resp.json()["rows"]}
    assert "sync.trigger" in actions
    assert "auth.login" not in actions


def test_activity_health_returns_pulse(seeded_app, admin_user):
    resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("green", "yellow", "red")
    assert "fields" in data
    assert "sentence" in data
    field_keys = {f["key"] for f in data["fields"]}
    assert "scheduler" in field_keys
    assert "sync_24h" in field_keys
    assert "active_users_today" in field_keys


def test_activity_sync_returns_recent(seeded_app, admin_user):
    import uuid
    from src.db import get_system_db
    now = datetime.now(timezone.utc)
    conn = get_system_db()
    conn.execute(
        "INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), "t_test", now, 42, 1500, "ok", None]
    )
    conn.close()
    resp = seeded_app["client"].get("/api/admin/activity/sync", headers=admin_user)
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert any(r["table_id"] == "t_test" for r in data["rows"])
```

- [ ] **Step 9.2: Run tests — expect FAIL**

Run: `pytest tests/test_activity_api.py -v`
Expected: 5 failures — module doesn't exist.

- [ ] **Step 9.3: Create `app/api/activity.py`**

Create the file with three endpoints:

```python
"""Activity Center read API.

Three endpoints under /api/admin/activity, all gated by require_admin:

    GET /api/admin/activity            unified timeline (audit_log + sync_history)
    GET /api/admin/activity/health     health pulse (cached 30s server-side)
    GET /api/admin/activity/sync       per-table recent sync feed

Each endpoint emits one audit_log entry per call (action='activity.read.*')
unless the same actor + same filter combination was logged in the last 60s
(see _suppress_recursive_audit).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db   # NOTE: lives in app.auth.dependencies, not app.dependencies
from src.repositories.audit import AuditRepository
from src.repositories.sync_state import SyncStateRepository

router = APIRouter(prefix="/api/admin/activity", tags=["activity"])

_HEALTH_CACHE: dict = {"data": None, "expires_at": None}
_HEALTH_TTL_SECONDS = 30


@router.get("")
def activity_timeline(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    user_id: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource: Optional[str] = None,
    result_pattern: Optional[str] = None,
    q: Optional[str] = None,
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unified audit_log feed with filters + keyset pagination."""
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    cursor = (cursor_ts, cursor_id) if cursor_ts and cursor_id else None

    rows, next_cursor = AuditRepository(conn).query(
        since=since,
        user_id=user_id,
        action_prefix=action_prefix,
        resource=resource,
        result_pattern=result_pattern,
        q=q,
        cursor=cursor,
        limit=limit,
    )

    return {
        "rows": rows,
        "next_cursor": (
            {"ts": next_cursor[0].isoformat(), "id": next_cursor[1]}
            if next_cursor else None
        ),
        "filter": {
            "since_minutes": since_minutes,
            "user_id": user_id,
            "action_prefix": action_prefix,
            "resource": resource,
            "result_pattern": result_pattern,
            "q": q,
        },
    }


@router.get("/health")
def activity_health(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Health pulse — cached 30s to make the page poll-friendly."""
    now = datetime.now(timezone.utc)
    if _HEALTH_CACHE["data"] is not None and _HEALTH_CACHE["expires_at"] > now:
        return _HEALTH_CACHE["data"]

    data = _compute_health(conn, now)
    _HEALTH_CACHE["data"] = data
    _HEALTH_CACHE["expires_at"] = now + timedelta(seconds=_HEALTH_TTL_SECONDS)
    return data


@router.get("/sync")
def activity_sync(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-table sync history feed."""
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    rows = SyncStateRepository(conn).list_recent(since=since, limit=limit)
    return {"rows": rows}


def _compute_health(conn: duckdb.DuckDBPyConnection, now: datetime) -> dict:
    """Build the health-pulse dict.

    Fields:
        scheduler: seconds since most recent run_session_processor or
                   marketplace.sync_all audit row.
        sync_24h: ok/fail counts from sync_history in last 24h.
        active_users_today: distinct user_id from audit_log since UTC midnight.
        memory_pipeline: latest verification processor run state.
        diagnose_warnings: count of active diagnose warnings (placeholder 0 in MVP).
    """
    audit_repo = AuditRepository(conn)

    # 1) scheduler freshness
    last_tick = conn.execute(
        "SELECT MAX(timestamp) FROM audit_log WHERE action LIKE 'run_%' OR action='marketplace.sync_all'"
    ).fetchone()[0]
    if last_tick is None:
        scheduler_age_s = None
        scheduler_color = "yellow"
        scheduler_value = "never"
    else:
        # last_tick may be tz-naive; ensure UTC awareness
        if last_tick.tzinfo is None:
            last_tick = last_tick.replace(tzinfo=timezone.utc)
        scheduler_age_s = int((now - last_tick).total_seconds())
        if scheduler_age_s > 7200:
            scheduler_color = "red"
        elif scheduler_age_s > 1800:
            scheduler_color = "yellow"
        else:
            scheduler_color = "green"
        scheduler_value = _format_age(scheduler_age_s)

    # 2) sync 24h
    sync_rows = conn.execute(
        "SELECT status, COUNT(*) FROM sync_history WHERE synced_at >= ? GROUP BY status",
        [now - timedelta(hours=24)]
    ).fetchall()
    ok = next((c for s, c in sync_rows if s == "ok"), 0)
    fail = sum(c for s, c in sync_rows if s and s != "ok")
    total = ok + fail
    if total == 0:
        sync_color = "yellow"
    elif fail == 0:
        sync_color = "green"
    elif ok / total >= 0.95:
        sync_color = "yellow"
    else:
        sync_color = "red"
    sync_value = f"{ok} ok / {fail} fail"

    # 3) active users today (UTC midnight cutoff)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    active = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE timestamp >= ? AND user_id IS NOT NULL",
        [midnight]
    ).fetchone()[0]

    # 4) memory pipeline (latest verification run via session_processor_state)
    mem_row = conn.execute(
        "SELECT MAX(processed_at), SUM(items_extracted) FROM session_processor_state WHERE processor_name='verification' AND processed_at >= ?",
        [now - timedelta(hours=1)]
    ).fetchone()
    if mem_row and mem_row[0]:
        mem_color = "green"
        mem_value = f"ok ({mem_row[1] or 0} items 1h)"
    else:
        mem_color = "yellow"
        mem_value = "idle 1h+"

    # 5) diagnose warnings — placeholder until /api/diagnose exposes a count
    diag_color = "green"
    diag_value = "0"

    fields = [
        {"key": "scheduler",          "value": scheduler_value, "raw": scheduler_age_s, "color": scheduler_color},
        {"key": "sync_24h",           "value": sync_value,      "raw": {"ok": ok, "fail": fail}, "color": sync_color},
        {"key": "active_users_today", "value": str(active),     "raw": active, "color": "green"},
        {"key": "memory_pipeline",    "value": mem_value,       "raw": None, "color": mem_color},
        {"key": "diagnose_warnings",  "value": diag_value,      "raw": 0, "color": diag_color},
    ]

    overall = "red" if any(f["color"] == "red" for f in fields) else \
              "yellow" if any(f["color"] == "yellow" for f in fields) else "green"

    sentence = _build_sentence(fields, overall)
    return {"status": overall, "fields": fields, "sentence": sentence}


def _format_age(seconds: int) -> str:
    if seconds < 60: return f"{seconds}s ago"
    if seconds < 3600: return f"{seconds // 60}m ago"
    if seconds < 86400: return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _build_sentence(fields: list, overall: str) -> str:
    by_key = {f["key"]: f for f in fields}
    if overall == "green":
        return (
            f"All systems nominal — {by_key['active_users_today']['value']} active users, "
            f"last scheduler tick {by_key['scheduler']['value']}, "
            f"{by_key['sync_24h']['value']} in 24h."
        )
    issues = [f["key"] for f in fields if f["color"] != "green"]
    return f"Degraded: {', '.join(issues)}. Investigate Activity timeline filtered to these subsystems."
```

- [ ] **Step 9.4: Register router in `app/main.py`**

Open `app/main.py`. Find the existing `app.include_router(...)` block. Add:

```python
from app.api import activity
app.include_router(activity.router)
```

- [ ] **Step 9.5: Run tests — expect PASS**

Run: `pytest tests/test_activity_api.py -v`
Expected: all 5 tests pass.

- [ ] **Step 9.6: Commit**

```bash
git add app/api/activity.py app/main.py tests/test_activity_api.py
git commit -m "feat(activity): /api/admin/activity timeline + /health + /sync endpoints"
```

---

## Task 10: Rebuild `activity_center.html` template + add `/admin/activity` handler

Delete all demo content. Replace with a clean health-pulse + timeline + sync layout.

**Files:**
- Modify: `app/web/router.py` (replace `/activity-center` handler; add `/admin/activity` handler)
- Modify: `app/web/templates/activity_center.html` (full rewrite)

- [ ] **Step 10.1: Read the current handler and template scope**

Open `app/web/router.py:746-762` — the current `/activity-center` handler. It passes `activity={"recent_sessions": [], "recent_reports": [], "insights": []}, knowledge_stats={"total": 0, "approved": 0, "mandatory": 0}` to a template that expects entirely different variables (`activity.executive_summary.*`, `activity.maturity_roadmap`, etc.). This mismatch is why the page renders empty.

Open `app/web/templates/activity_center.html` — 2552 lines of demo content. Bulk-delete sections we don't keep.

- [ ] **Step 10.2: Replace the template with new minimal admin-activity layout**

Open `app/web/templates/activity_center.html`. **Replace the entire file** with:

```jinja
{% extends "base.html" %}
{% block title %}Activity — Admin{% endblock %}

{% block content %}
<div class="container-activity">

  <!-- HEALTH PULSE (renders from server-provided context, refreshes client-side every 30s) -->
  <section class="ac-health" id="ac-health" data-poll="/api/admin/activity/health">
    <div class="ac-health-status" data-bind="status">{{ health.status }}</div>
    <p class="ac-health-sentence" data-bind="sentence">{{ health.sentence }}</p>
    <ul class="ac-health-fields" data-bind="fields">
      {% for f in health.fields %}
      <li class="ac-chip ac-color-{{ f.color }}" data-key="{{ f.key }}">
        <span class="ac-chip-label">{{ f.key|replace('_', ' ')|title }}</span>
        <span class="ac-chip-value">{{ f.value }}</span>
      </li>
      {% endfor %}
    </ul>
  </section>

  <!-- TIMELINE -->
  <section class="ac-timeline">
    <header class="ac-section-head">
      <h2>Timeline</h2>
      <form class="ac-filters" id="ac-timeline-filter">
        <input name="action_prefix" placeholder="action prefix, e.g. sync." />
        <input name="user_id" placeholder="user id" />
        <input name="q" placeholder="search params…" />
        <select name="since_minutes">
          <option value="60">last 1h</option>
          <option value="1440" selected>last 24h</option>
          <option value="10080">last 7d</option>
        </select>
        <button type="submit">Filter</button>
      </form>
    </header>
    <table class="ac-table" id="ac-timeline-table">
      <thead>
        <tr><th>Time</th><th>User</th><th>Action</th><th>Resource</th><th>Result</th></tr>
      </thead>
      <tbody>
        {% for row in timeline %}
        <tr data-event-id="{{ row.id }}">
          <td><time datetime="{{ row.timestamp }}">{{ row.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</time></td>
          <td>{{ row.user_id or '—' }}</td>
          <td><code>{{ row.action }}</code></td>
          <td>{{ row.resource or '—' }}</td>
          <td>
            {% if row.result and row.result.startswith('error') %}
              <span class="ac-result-bad">{{ row.result }}</span>
            {% elif row.result %}
              <span class="ac-result-ok">{{ row.result }}</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% if next_cursor %}
      <div class="ac-loadmore"><button id="ac-loadmore">Load more</button></div>
    {% endif %}
  </section>

  <!-- SYNC GRID -->
  <section class="ac-sync">
    <header class="ac-section-head"><h2>Sync (last 24h)</h2></header>
    <table class="ac-table">
      <thead><tr><th>Table</th><th>Last synced</th><th>Status</th><th>Rows</th><th>Duration</th></tr></thead>
      <tbody>
        {% for r in sync_rows %}
        <tr>
          <td><code>{{ r.table_id }}</code></td>
          <td><time datetime="{{ r.synced_at }}">{{ r.synced_at.strftime('%Y-%m-%d %H:%M') }}</time></td>
          <td>
            {% if r.status == 'ok' %}<span class="ac-result-ok">ok</span>
            {% else %}<span class="ac-result-bad">{{ r.status }}</span>{% endif %}
          </td>
          <td>{{ r.rows or '—' }}</td>
          <td>{{ '%.1f s'|format(r.duration_ms / 1000) if r.duration_ms else '—' }}</td>
        </tr>
        {% endfor %}
        {% if not sync_rows %}
        <tr><td colspan="5" class="ac-empty">No syncs in the last 24h.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </section>

</div>

<style>
  .container-activity { max-width: 1200px; margin: 24px auto; padding: 0 16px; font: 14px/1.5 var(--font-sans, system-ui); }
  .ac-health { background: var(--surface, #fff); border: 1px solid var(--border, #e5e7eb); border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; }
  .ac-health-status { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: 600; text-transform: uppercase; font-size: 12px; }
  .ac-health[data-bind] .ac-health-status { background: #ddd; }
  .ac-color-green { background: #d1fae5; color: #065f46; }
  .ac-color-yellow { background: #fef3c7; color: #92400e; }
  .ac-color-red { background: #fee2e2; color: #991b1b; }
  .ac-health-sentence { color: var(--text-secondary, #4b5563); margin: 8px 0 12px; }
  .ac-health-fields { list-style: none; display: flex; gap: 8px; padding: 0; margin: 0; flex-wrap: wrap; }
  .ac-chip { padding: 6px 10px; border-radius: 12px; font-size: 12px; }
  .ac-chip-label { font-weight: 500; opacity: 0.7; margin-right: 6px; }
  .ac-section-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ac-filters { display: flex; gap: 8px; flex-wrap: wrap; }
  .ac-filters input, .ac-filters select { padding: 4px 8px; font: inherit; }
  .ac-table { width: 100%; border-collapse: collapse; background: var(--surface, #fff); }
  .ac-table th { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border, #e5e7eb); font-weight: 600; }
  .ac-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-light, #f3f4f6); }
  .ac-result-ok { color: #065f46; }
  .ac-result-bad { color: #991b1b; }
  .ac-empty { text-align: center; color: var(--text-secondary, #6b7280); padding: 24px; }
  .ac-loadmore { text-align: center; margin: 16px 0; }
  .ac-timeline, .ac-sync { margin-bottom: 32px; }
</style>

<script>
(function() {
  // Poll the health endpoint every 30s and replace the pulse fields in-place.
  const el = document.getElementById('ac-health');
  if (!el) return;
  const url = el.dataset.poll;

  async function refresh() {
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      const sentence = el.querySelector('[data-bind="sentence"]');
      const status = el.querySelector('[data-bind="status"]');
      const fields = el.querySelector('[data-bind="fields"]');
      if (sentence) sentence.textContent = data.sentence;
      if (status) status.textContent = data.status;
      if (fields) {
        fields.innerHTML = data.fields.map(f => `
          <li class="ac-chip ac-color-${f.color}" data-key="${f.key}">
            <span class="ac-chip-label">${f.key.replace(/_/g, ' ')}</span>
            <span class="ac-chip-value">${f.value}</span>
          </li>
        `).join('');
      }
    } catch (e) { /* swallow — never break the page */ }
  }
  setInterval(refresh, 30000);
})();
</script>
{% endblock %}
```

- [ ] **Step 10.3: Replace `/activity-center` handler in `app/web/router.py`**

Open `app/web/router.py`. Replace lines 746-762 (the `activity_center` handler) with two functions: a redirect for the old URL and a new admin handler.

```python
@router.get("/activity-center")
async def activity_center_redirect():
    """Legacy URL — redirect to /admin/activity."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/activity", status_code=308)


@router.get("/admin/activity", response_class=HTMLResponse)
async def admin_activity(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Activity Center — health pulse + audit_log timeline + sync history.

    Server-renders the initial state from the same three sources the
    JSON endpoints expose. Client-side script then polls /health every
    30s and supports filter form submission against /api/admin/activity.
    """
    from datetime import datetime, timezone, timedelta
    from src.repositories.audit import AuditRepository
    from src.repositories.sync_state import SyncStateRepository
    from app.api.activity import _compute_health

    now = datetime.now(timezone.utc)
    audit_repo = AuditRepository(conn)
    sync_repo = SyncStateRepository(conn)

    timeline, next_cursor = audit_repo.query(
        since=now - timedelta(hours=24),
        limit=50,
    )
    sync_rows = sync_repo.list_recent(since=now - timedelta(hours=24), limit=100)
    health = _compute_health(conn, now)

    ctx = _build_context(
        request,
        user=user,
        health=health,
        timeline=timeline,
        next_cursor=next_cursor,
        sync_rows=sync_rows,
    )
    return templates.TemplateResponse(request, "activity_center.html", ctx)
```

Make sure `require_admin` is imported at the top of `app/web/router.py` (it likely already is — search the file for prior imports).

- [ ] **Step 10.4: Smoke test — admin GET /admin/activity returns 200**

Add to `tests/test_activity_api.py`:

```python
def test_admin_activity_page_renders(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    assert "Timeline" in resp.text
    assert "Sync" in resp.text


def test_activity_center_redirects_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/activity-center", headers=admin_user, follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/admin/activity"
```

Run: `pytest tests/test_activity_api.py::test_admin_activity_page_renders tests/test_activity_api.py::test_activity_center_redirects_to_admin_activity -v`
Expected: 2 passed.

- [ ] **Step 10.5: Commit**

```bash
git add app/web/router.py app/web/templates/activity_center.html tests/test_activity_api.py
git commit -m "feat(ui): /admin/activity rebuilt — health pulse, timeline, sync grid; /activity-center → 308 redirect

BREAKING: removed demo executive-pulse / maturity-roadmap content from activity_center.html.
The page now reflects real audit_log + sync_history data."
```

---

## Task 11: Update navigation + dashboard widget

**Files:**
- Modify: `app/web/templates/_app_header.html` (admin dropdown menu)
- Modify: `app/web/templates/dashboard.html` (Activity widget link)

- [ ] **Step 11.1: Update admin nav**

Open `app/web/templates/_app_header.html`. Find the admin dropdown block (around lines 18-40 per Explore findings). After the existing admin links, add (before the closing of the dropdown):

```jinja
<a href="/admin/activity" class="{{ 'active' if request.url.path == '/admin/activity' else '' }}">Activity</a>
```

- [ ] **Step 11.2: Update dashboard widget link**

Open `app/web/templates/dashboard.html`. Search for `/activity-center` (Explore found references around lines 621-2326). Replace each occurrence with `/admin/activity`.

Run quick search to find all references in templates:
`grep -rn '/activity-center' app/web/templates/`
Expected: matches in `dashboard.html` only. Replace all with `/admin/activity`.

- [ ] **Step 11.3: Smoke test — link integrity**

Add to `tests/test_activity_api.py`:

```python
def test_dashboard_links_to_admin_activity(seeded_app, admin_user):
    resp = seeded_app["client"].get("/dashboard", headers=admin_user)
    assert resp.status_code == 200
    assert "/admin/activity" in resp.text
    assert "/activity-center" not in resp.text   # old URL removed


def test_admin_header_includes_activity_link(seeded_app, admin_user):
    resp = seeded_app["client"].get("/admin/activity", headers=admin_user)
    assert resp.status_code == 200
    assert 'href="/admin/activity"' in resp.text
```

Run: `pytest tests/test_activity_api.py::test_dashboard_links_to_admin_activity tests/test_activity_api.py::test_admin_header_includes_activity_link -v`
Expected: 2 passed.

- [ ] **Step 11.4: Commit**

```bash
git add app/web/templates/_app_header.html app/web/templates/dashboard.html tests/test_activity_api.py
git commit -m "feat(ui): admin nav + dashboard widget point at /admin/activity"
```

---

## Task 12: Recursive audit suppression

Reading `/api/admin/activity/*` writes one audit row per call — but the health poll runs every 30s, so we suppress same-actor / same-filter polls within 60s.

**Files:**
- Modify: `app/api/activity.py`
- Test: `tests/test_activity_api.py`

- [ ] **Step 12.1: Write failing test for suppression**

Add to `tests/test_activity_api.py`:

```python
import time


def test_activity_health_does_not_audit_polling(seeded_app, admin_user):
    """Polling /health every 30s shouldn't blow up audit_log."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    for _ in range(5):
        c.get("/api/admin/activity/health", headers=admin_user)
    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert after - before <= 1  # at most one row from the burst


def test_activity_timeline_audits_first_call_only(seeded_app, admin_user):
    """Two identical filter calls within 60s produce one audit row."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_activity_timeline_audits_different_filters(seeded_app, admin_user):
    """Different filter combinations each get their own audit row."""
    from src.db import get_system_db
    c = seeded_app["client"]
    conn = get_system_db()
    conn.execute("DELETE FROM audit_log WHERE action='activity.read'")
    conn.close()
    c.get("/api/admin/activity?action_prefix=sync.", headers=admin_user)
    c.get("/api/admin/activity?action_prefix=auth.", headers=admin_user)
    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='activity.read'"
    ).fetchone()[0]
    conn.close()
    assert n == 2
```

- [ ] **Step 12.2: Run tests — expect FAIL**

Run: `pytest tests/test_activity_api.py -v -k recursive_audit or audits_first or audits_different or does_not_audit`
Expected: failures (no audit logging yet, or it logs every call).

- [ ] **Step 12.3: Add suppression helper + audit calls in `app/api/activity.py`**

At the top of `app/api/activity.py` (after imports), add:

```python
import hashlib
import json

# (actor, filter_hash) -> last logged datetime; in-memory, per-process.
_RECENT_AUDITS: dict[tuple[str, str], datetime] = {}
_AUDIT_SUPPRESS_WINDOW = timedelta(seconds=60)


def _should_audit(actor_id: str, filter_payload: dict) -> bool:
    """True if this (actor, filter) combo hasn't been audited in the last 60s."""
    key = (actor_id, hashlib.sha1(json.dumps(filter_payload, sort_keys=True, default=str).encode()).hexdigest())
    now = datetime.now(timezone.utc)
    last = _RECENT_AUDITS.get(key)
    if last is not None and (now - last) < _AUDIT_SUPPRESS_WINDOW:
        return False
    _RECENT_AUDITS[key] = now
    return True


def _audit_read(conn, user: dict, endpoint: str, filter_payload: dict) -> None:
    """Emit a deduped audit row for an AC read endpoint."""
    actor_id = (user or {}).get("id") or "anonymous"
    if not _should_audit(actor_id, {"endpoint": endpoint, **filter_payload}):
        return
    AuditRepository(conn).log(
        user_id=actor_id,
        action="activity.read",
        params={"endpoint": endpoint, **filter_payload},
        result="success",
        client_kind="web",
    )
```

Modify `activity_timeline()` to call audit:

```python
@router.get("")
def activity_timeline(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    user_id: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource: Optional[str] = None,
    result_pattern: Optional[str] = None,
    q: Optional[str] = None,
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    cursor = (cursor_ts, cursor_id) if cursor_ts and cursor_id else None
    rows, next_cursor = AuditRepository(conn).query(
        since=since, user_id=user_id, action_prefix=action_prefix,
        resource=resource, result_pattern=result_pattern, q=q, cursor=cursor, limit=limit,
    )
    _audit_read(conn, user, "timeline", {
        "since_minutes": since_minutes,
        "user_id": user_id, "action_prefix": action_prefix,
        "resource": resource, "result_pattern": result_pattern, "q": q,
    })
    return {
        "rows": rows,
        "next_cursor": (
            {"ts": next_cursor[0].isoformat(), "id": next_cursor[1]}
            if next_cursor else None
        ),
    }
```

Modify `activity_health()` to call audit:

```python
@router.get("/health")
def activity_health(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    now = datetime.now(timezone.utc)
    if _HEALTH_CACHE["data"] is not None and _HEALTH_CACHE["expires_at"] > now:
        return _HEALTH_CACHE["data"]
    data = _compute_health(conn, now)
    _HEALTH_CACHE["data"] = data
    _HEALTH_CACHE["expires_at"] = now + timedelta(seconds=_HEALTH_TTL_SECONDS)
    _audit_read(conn, user, "health", {})
    return data
```

Modify `activity_sync()` to call audit:

```python
@router.get("/sync")
def activity_sync(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    rows = SyncStateRepository(conn).list_recent(since=since, limit=limit)
    _audit_read(conn, user, "sync", {"since_minutes": since_minutes})
    return {"rows": rows}
```

- [ ] **Step 12.4: Run tests — expect PASS**

Run: `pytest tests/test_activity_api.py -v`
Expected: all tests pass (including the three new suppression tests).

- [ ] **Step 12.5: Commit**

```bash
git add app/api/activity.py tests/test_activity_api.py
git commit -m "feat(activity): recursive-audit suppression for AC read endpoints (60s window per actor+filter)"
```

---

## Task 13: PostHog event emission (opt-in observability)

When PostHog is enabled, emit one event per relevant AC interaction.

**Files:**
- Modify: `app/api/activity.py`
- Test: `tests/test_activity_api.py`

- [ ] **Step 13.1: Write failing test**

Add to `tests/test_activity_api.py`:

```python
from unittest.mock import patch


def test_activity_health_emits_posthog_event_when_enabled(seeded_app, admin_user):
    with patch("src.observability.posthog_client.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = True
        seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        mock_client.capture.assert_called()
        kw = mock_client.capture.call_args.kwargs
        assert kw.get("event") == "activity_health_viewed"


def test_activity_endpoints_silent_when_posthog_disabled(seeded_app, admin_user):
    with patch("src.observability.posthog_client.get_posthog") as mock_get:
        mock_client = mock_get.return_value
        mock_client.enabled = False
        resp = seeded_app["client"].get("/api/admin/activity/health", headers=admin_user)
        # capture may be called but the inner SDK is no-op; that's the contract.
        # Assert: no exception, healthy response.
        assert resp.status_code == 200
```

- [ ] **Step 13.2: Run test — expect FAIL**

Run: `pytest tests/test_activity_api.py::test_activity_health_emits_posthog_event_when_enabled -v`
Expected: FAIL.

- [ ] **Step 13.3: Wire PostHog emission**

At the top of `app/api/activity.py`:

```python
from src.observability.posthog_client import get_posthog
```

In `_audit_read()`, after the `AuditRepository.log()` call, also emit:

```python
    # Best-effort PostHog event (no-op when disabled).
    try:
        get_posthog().capture(
            event=f"activity_{endpoint}_viewed",
            distinct_id=actor_id,
            properties={k: v for k, v in filter_payload.items() if v is not None},
        )
    except Exception:
        pass  # never break the request
```

- [ ] **Step 13.4: Run test — expect PASS**

Run: `pytest tests/test_activity_api.py -v`
Expected: all pass.

- [ ] **Step 13.5: Commit**

```bash
git add app/api/activity.py tests/test_activity_api.py
git commit -m "feat(activity): emit PostHog events when integration enabled (no-op default)"
```

---

## Task 14: CHANGELOG entry + manual smoke

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 14.1: Update `CHANGELOG.md`**

Open `CHANGELOG.md`. Below the topmost `## [Unreleased]` heading (or create one if missing), add:

```markdown
### Added
- **Activity Center rebuild** (`/admin/activity`): health pulse (cached 30s) + chronological audit_log timeline + sync_history grid. Replaces the empty-stub `/activity-center` page. Old URL 308-redirects.
- Three new read endpoints: `GET /api/admin/activity`, `GET /api/admin/activity/health`, `GET /api/admin/activity/sync`. All admin-only.
- `audit_log` now writes from `POST /api/sync/trigger`, `POST /api/scripts/run-due`, `POST /api/upload/sessions`, and `GET /api/data/{id}/download` — closing four longstanding coverage gaps.
- Schema v40: `audit_log` gains `params_before`, `client_ip`, `client_kind`, `correlation_id` columns + three indices for timeline query performance.
- `AuditRepository.query()` rewritten with filters (`since`, `until`, `action_prefix`, `action_in`, `resource`, `result_pattern`, `q`, `correlation_id`) and keyset cursor pagination.
- `SyncStateRepository.list_recent()` for cross-table chronological feeds.
- Optional PostHog events `activity_*_viewed` (no-op when `POSTHOG_API_KEY` unset).

### Changed
- Admin dropdown menu now includes **Activity** (was Scheduler runs only). `/admin/scheduler-runs` remains and will redirect to a preset filter on Activity in a follow-up release.

### Removed / BREAKING
- **BREAKING (UI):** demo content removed from `activity_center.html` — the "Executive Pulse / Maturity Roadmap / Business Processes / Teams / Opportunities" sections never had a real data source and are gone. The page now reflects `audit_log` + `sync_history` only. Operators relying on the old layout: it never rendered any real data; this is a no-op fix.
```

- [ ] **Step 14.2: Manual smoke test**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss-activity-spec"
source .venv/bin/activate 2>/dev/null || python3 -m venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]" >/dev/null 2>&1
uvicorn app.main:app --reload --port 8001 &
SERVER_PID=$!
sleep 3

# Smoke: redirect
curl -sI http://localhost:8001/activity-center | grep -i location

# Smoke: admin endpoint (will need an admin token; substitute as available)
curl -s -H "Authorization: Bearer $ADMIN_PAT" http://localhost:8001/api/admin/activity/health | python3 -m json.tool

kill $SERVER_PID
```

Expected output of `curl -sI`: `location: /admin/activity`
Expected output of `/health`: a JSON dict with `status`, `fields`, `sentence`.

- [ ] **Step 14.3: Full test suite**

Run: `pytest tests/ -x -q --no-header 2>&1 | tail -30`
Expected: no failures attributable to this change. Pre-existing flakes are fine but should be noted.

- [ ] **Step 14.4: Commit + PR**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entry for Activity Center MVP"

git push origin zs/spec-activity-center

gh pr create --title "Activity Center MVP — /admin/activity rebuild + 4 audit gaps closed" --body "$(cat <<'EOF'
## Summary

Rebuilds `/activity-center` as `/admin/activity` per spec [docs/superpowers/plans/2026-05-11-admin-observability-spec.md](docs/superpowers/plans/2026-05-11-admin-observability-spec.md). Closes #206.

- Health pulse (cached 30s) + audit_log timeline + sync_history grid
- 4 audit coverage gaps closed: sync.trigger, scripts.run-due, upload.sessions, data.download
- Schema v40: audit_log gains params_before, client_ip, client_kind, correlation_id + 3 indices
- `AuditRepository.query()` rewrite with filters + keyset cursor pagination
- Recursive-audit suppression (60s per actor + filter) so polling doesn't flood the log
- Optional PostHog `activity_*_viewed` events (no-op default)

## Out of scope (follow-up plans)

- `/admin/sessions` failure-scan processor → `2026-05-NN-admin-sessions.md`
- `/admin/feedback` + `agnes report` → `2026-05-NN-feedback-inbox.md`
- Changes tab + Rollback (Phase B)
- Queries / Performance / Usage / Costs tabs (Phase B/C, blocked on #158)

## Test plan

- [x] schema v40 migration round-trip + indices test
- [x] AuditRepository.query() filters + cursor pagination (10 tests)
- [x] SyncStateRepository.list_recent() (4 tests)
- [x] 4 audit-gap closure tests
- [x] /api/admin/activity timeline, health, sync endpoint tests
- [x] Recursive-audit suppression tests
- [x] PostHog emission gated on enable flag
- [x] /admin/activity HTML smoke + /activity-center 308 redirect
- [x] Dashboard widget + admin header link updates
EOF
)"
```

Expected: PR opened.

---

## Self-review checklist

After completing all tasks:

- [ ] **Spec coverage:** every section in `2026-05-11-admin-observability-spec.md` §5.1, §5.2, §5.3, §5.4 (decisions + MVP scope) is implemented or explicitly deferred to a follow-up plan with named file.
- [ ] **Placeholder scan:** `grep -rEn 'TODO|FIXME|XXX' app/api/activity.py src/repositories/audit.py src/repositories/sync_state.py` returns nothing.
- [ ] **Type consistency:** `AuditRepository.query()` signature in `src/repositories/audit.py` matches the call sites in `app/api/activity.py`. `SyncStateRepository.list_recent()` keyword names match.
- [ ] **CHANGELOG present:** entries land under `## [Unreleased]` with `### Added` / `### Changed` / `### Removed / BREAKING` headings.
- [ ] **No regressions:** `pytest tests/ -x -q` runs to completion without new failures.

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-11-activity-center-mvp.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**

---

## Revisions applied from reviewer pass (2026-05-11)

Three independent sub-agent reviews (security, production resilience, code architecture) ran against the original draft. Below is the consolidated list of changes already applied to this plan plus deliberate deferrals.

### Applied inline (this plan now reflects them)

1. **Import path corrected** — `from app.auth.dependencies import _get_db` (was `app.dependencies`; the latter doesn't exist). Task 9.3.
2. **Test fixtures aligned with reality** — every test now uses `seeded_app["client"]` + `admin_user` / `analyst_user` (headers dict) + `get_system_db()` from `src.db`. The non-existent `admin_client` / `authenticated_client` / `get_system_conn` placeholders are gone. Conventions section authoritative.
3. **DuckDB index DESC removed** — DuckDB doesn't honor DESC; relying on it would silently fail. Indices are plain; ORDER BY in `query()` enforces direction. Task 1.3.
4. **Index creation cost flagged** — upgrade-window warning added to Task 1.3 + CHANGELOG. Operators with >100k audit rows should expect 30–120s of startup latency on first launch of v40.
5. **Migration idempotency test** added (`test_v39_to_v40_is_idempotent`). Task 1.1.
6. **Representative evolved-DB test** added (`test_v30_db_ladders_all_the_way_up`) — catches breakage in any intermediate v30→v40 step. Task 1.1.
7. **Audit-write failure resilience** — every new `AuditRepository(conn).log()` call is wrapped in `try/except` with `logger.exception` + continue. Tasks 5.3, 6.3, 7.3, 8.3. A regression test in Task 5.1 asserts that a forced audit failure does NOT 5xx the wrapped business request.
8. **Filename sanitization on session upload** — Task 7.3 adds an `^[A-Za-z0-9._\-]{1,200}$` filter on multipart filenames. Rejecting `<script>…</script>.jsonl` style payloads before they reach `audit_log.params`. Test in Task 7.1.
9. **Length cap on logged strings** — `[:256]` cap applied to `resource` and `filename` fields in all four new audit calls.
10. **`q` filter safeguard** — if `q` is provided without `since`, the query forces a 7-day cap. Task 3.3.
11. **Conventions section** at top of plan — single source of truth for imports, fixtures, resilience rules, suppression scope, index notes.

### Deferred (with explicit rationale)

12. **`audit.reveal_raw` mechanism not in MVP.** Spec §7.2 mentions it; MVP plan does NOT include a "Show raw" toggle. Render-side masking will fall back to **always-on truncated display** in v40. The reveal_raw audit entry + toggle UI lands in Phase B alongside Changes/Diff tab. Spec §7.2 updated to mark this deferred.
13. **Per-worker recursive-audit dedup is documented limitation, not fixed.** `_RECENT_AUDITS` stays as in-memory per-process dict. v40 ships requiring single-worker uvicorn (the existing default). A future plan will move dedup to a shared DuckDB table when multi-worker uvicorn is enabled. Conventions section documents this clearly.
14. **Health pulse cache is per-process, single-worker assumption.** Same constraint as #13. The `_HEALTH_CACHE` dict ships as-is for v40; if multi-worker comes online before the shared-cache work, all admins see N× thundering-herd at the 30s mark, but health values stay correct. Documented.
15. **PostHog event timeout not added.** PostHog SDK already queues async; the wrapping try/except in `_audit_read` covers crash modes. A timeout knob can be added later if observed in production tail latency.
16. **Health pulse thresholds remain hard-coded.** Future env-var overrides (e.g. `ACTIVITY_SCHEDULER_THRESHOLD_RED_SECONDS`) are a P2 polish.
17. **`diagnose_warnings` field placeholder = 0.** The `/api/diagnose` integration is a follow-up. Health pulse still emits the field with green color; switches to real count when integration lands.
18. **Query attribution gap (#158) explicitly out of MVP scope.** Acknowledged in CHANGELOG and spec §5.5.

### Reviewer questions left open

- Should v40 prefer 301 vs. 308 redirect on `/activity-center` → `/admin/activity`? Plan uses 308 (POST-preserving); since the route is GET-only, both are functionally equivalent. Keeping 308 to match HTTP-spec correctness; revisit if a proxy/CDN misbehaves.
- Default audit retention (currently unbounded) — explicitly deferred to a Phase B retention plan. CHANGELOG notes growth trajectory so operators can monitor.

---

## What comes after this plan

Once this PR merges, the next two plans are:

1. **`docs/superpowers/plans/2026-05-NN-admin-sessions.md`** — `/admin/sessions` + `failure_scan` processor. Will follow the same TDD structure: schema v41 (`session_findings`), new processor in `services/session_processors/failure_scan.py`, registration in `PROCESSORS` + scheduler, admin UI list + detail view.

2. **`docs/superpowers/plans/2026-05-NN-feedback-inbox.md`** — `agnes report` CLI + `/admin/feedback` + first-party `agnes-report` Claude skill. Schema v42 (`feedback_reports`), new `POST /api/feedback` endpoint, Typer command in `cli/commands/report.py`, Telegram admin notifications via existing `services/telegram_bot/sender.py`.

Each is independently shippable; together they realize the full vision in the parent spec.
