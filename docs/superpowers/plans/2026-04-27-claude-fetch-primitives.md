# Claude-Driven Fetch Primitives Implementation Plan

> **Historical note (2026-04-29):** `CHANGELOG.md` was retired in favor of GitHub Releases. Wherever this plan instructs adding entries under `## [Unreleased]` or modifying `CHANGELOG.md`, the equivalent today is: write the change as the PR title bullet and put migration details in the PR description (Release Drafter auto-aggregates). See CLAUDE.md → "Release notes".
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken BQ-view-wrapping approach (issue #101) with primitive operations the Claude agent composes — `da catalog`, `da schema`, `da fetch`, `da snapshot *`, `da query` — backed by `/api/v2/{catalog,schema,sample,scan,scan/estimate}` server endpoints. Secrets stay server-side; agent does the planning.

**Architecture:** Two-tier query model (laptop DuckDB ↔ server DuckDB ↔ BQ) preserved. New v2 endpoints expose discovery + scoped scan. CLI commands materialize filtered subsets locally as parquet snapshots registered as DuckDB views. Server-side WHERE validator (sqlglot, allow-list-driven) is the security perimeter.

**Tech Stack:** Python 3.13, FastAPI, DuckDB, pyarrow (Arrow IPC over HTTP), sqlglot (server-side WHERE validation), `bigquery_query()` DuckDB BQ extension function, GCE metadata-token auth (#98 cache reused), pytest. No new dependencies beyond sqlglot (already optional in repo).

**Spec:** `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md`

---

## File structure

**New files (server):**
- `app/api/where_validator.py` — sqlglot-backed WHERE clause validator (§3.7 of spec)
- `app/api/v2_quota.py` — process-local concurrent + daily-byte quota tracker
- `app/api/v2_cache.py` — LRU+TTL cache helper for catalog/schema/sample
- `app/api/v2_arrow.py` — Arrow IPC streaming helper (response builder)
- `app/api/v2_catalog.py` — `GET /api/v2/catalog`
- `app/api/v2_schema.py` — `GET /api/v2/schema/{table_id}`
- `app/api/v2_sample.py` — `GET /api/v2/sample/{table_id}`
- `app/api/v2_scan.py` — `POST /api/v2/scan` + `POST /api/v2/scan/estimate`

**New files (client):**
- `cli/v2_client.py` — Arrow over HTTP client + JSON request helpers
- `cli/snapshot_meta.py` — sidecar JSON I/O + flock helper
- `cli/commands/fetch.py` — `da fetch`
- `cli/commands/snapshot.py` — `da snapshot list/refresh/drop/prune`
- `cli/commands/catalog.py` — `da catalog`
- `cli/commands/schema.py` — `da schema`
- `cli/commands/describe.py` — `da describe`
- `cli/commands/disk_info.py` — `da disk-info`

**New files (tests):**
- `tests/test_where_validator.py` — adversarial corpus (50+ cases)
- `tests/test_v2_quota.py`, `tests/test_v2_cache.py`, `tests/test_v2_arrow.py`
- `tests/test_v2_catalog.py`, `tests/test_v2_schema.py`, `tests/test_v2_sample.py`, `tests/test_v2_scan.py`, `tests/test_v2_scan_estimate.py`
- `tests/test_cli_fetch.py`, `tests/test_cli_snapshot.py`, `tests/test_cli_catalog.py`, `tests/test_cli_schema.py`, `tests/test_cli_describe.py`, `tests/test_cli_disk_info.py`
- `tests/test_snapshot_meta.py`, `tests/test_v2_client.py`

**New files (docs/skill):**
- `cli/skills/agnes-data-querying.md` — agent rails skill (§5.2)

**Modified files:**
- `app/main.py` — register v2 routers
- `cli/main.py` — register new command groups
- `connectors/bigquery/extractor.py` — drop wrap-view code path for VIEW entities + `legacy_wrap_views` toggle
- `tests/test_bigquery_extractor.py` — update tests for legacy toggle
- `CLAUDE.md` — agent rails addendum (§5.1)
- `CHANGELOG.md` — `**BREAKING**` entry under `[Unreleased]`
- `config/instance.yaml.example` — new `api.scan.*` knobs

---

## Task 1: WHERE validator — parser + structural rejects

Foundation for `/api/v2/scan` security perimeter. Spec §3.7 part 1.

**Files:**
- Create: `app/api/where_validator.py`
- Test: `tests/test_where_validator.py`

- [ ] **Step 1.1: Write failing tests for parse + structural rejects**

```python
# tests/test_where_validator.py
"""Adversarial test corpus for the WHERE clause validator (spec §3.7)."""

import pytest
from app.api.where_validator import (
    validate_where,
    WhereValidationError,
    REJECT_NESTED_SELECT,
    REJECT_MULTI_STATEMENT,
    REJECT_DDL_DML,
    REJECT_PARSE,
    REJECT_CROSS_TABLE,
)


# A schema-like dict the validator uses to verify column references.
SCHEMA = {
    "event_date": "DATE",
    "country_code": "STRING",
    "session_id": "STRING",
    "amount": "INT64",
}
TABLE_ID = "web_sessions_example"


class TestParse:
    def test_empty_string_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_PARSE

    def test_unparseable_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("SELECT * FROM", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_PARSE


class TestStructural:
    def test_nested_select_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where(
                "country_code IN (SELECT country FROM other_table)",
                TABLE_ID, SCHEMA,
            )
        assert e.value.kind == REJECT_NESTED_SELECT

    def test_multi_statement_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("amount = 1; DROP TABLE x", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_MULTI_STATEMENT

    def test_drop_table_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("amount = (DROP TABLE x)", TABLE_ID, SCHEMA)
        assert e.value.kind in (REJECT_DDL_DML, REJECT_PARSE)

    def test_cross_table_reference_rejected(self):
        """Predicates may only reference the target table."""
        with pytest.raises(WhereValidationError) as e:
            validate_where(
                "other_table.id = 1",
                TABLE_ID, SCHEMA,
            )
        assert e.value.kind == REJECT_CROSS_TABLE
```

- [ ] **Step 1.2: Run tests to verify failure**

Run: `pytest tests/test_where_validator.py::TestParse tests/test_where_validator.py::TestStructural -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.where_validator'`

- [ ] **Step 1.3: Implement parser + structural validator**

Create `app/api/where_validator.py`:

```python
"""WHERE clause validator for /api/v2/scan.

Single security perimeter — every analyst-supplied predicate flows through here
before reaching BigQuery. Allow-list-driven; explicit rejection codes per spec §3.7.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Mapping

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

# Rejection kind codes (stable; used by callers + tests + audit log)
REJECT_PARSE = "parse_error"
REJECT_NESTED_SELECT = "nested_select"
REJECT_MULTI_STATEMENT = "multi_statement"
REJECT_DDL_DML = "ddl_or_dml"
REJECT_CROSS_TABLE = "cross_table_reference"
REJECT_UNKNOWN_FUNCTION = "unknown_function"
REJECT_UNKNOWN_COLUMN = "unknown_column"
REJECT_DISALLOWED_NODE = "disallowed_node"


@dataclass
class WhereValidationError(Exception):
    kind: str
    message: str
    detail: dict | None = None

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


# Nodes that imply DDL/DML (rejected outright).
_DDL_DML_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Truncate,
    exp.Alter, exp.Create, exp.Copy, exp.Merge,
)


def validate_where(
    predicate: str,
    table_id: str,
    schema: Mapping[str, str],
) -> exp.Expression:
    """Validate a WHERE-clause fragment.

    Args:
        predicate: SQL fragment (without leading 'WHERE').
        table_id: target table id; cross-table references rejected.
        schema: {column_name: type} for the target table.

    Returns:
        Parsed sqlglot expression tree (caller may re-stringify or inspect).

    Raises:
        WhereValidationError: with .kind set to one of the REJECT_* codes.
    """
    if not predicate or not predicate.strip():
        raise WhereValidationError(REJECT_PARSE, "empty predicate")

    # Multi-statement detection: BQ statements separated by ';' would parse
    # as multiple expressions in sqlglot.parse() (returns a list).
    try:
        statements = sqlglot.parse(f"SELECT 1 FROM t WHERE {predicate}", dialect="bigquery")
    except ParseError as e:
        raise WhereValidationError(REJECT_PARSE, f"parse failed: {e}")

    if statements is None or len(statements) != 1 or statements[0] is None:
        raise WhereValidationError(REJECT_MULTI_STATEMENT, "multi-statement input not allowed")

    select = statements[0]
    where = select.find(exp.Where)
    if where is None:
        raise WhereValidationError(REJECT_PARSE, "no WHERE expression found in parsed input")

    _walk_structural(where, table_id, schema)
    return where


def _walk_structural(node: exp.Expression, table_id: str, schema: Mapping[str, str]) -> None:
    """Walk the WHERE AST and reject disallowed structures."""
    for sub in node.walk():
        # `node.walk()` yields the node itself first; check structural rules.
        if isinstance(sub, exp.Subquery) or (isinstance(sub, exp.Select) and sub is not node):
            raise WhereValidationError(REJECT_NESTED_SELECT, "nested SELECT/subquery not allowed")
        if isinstance(sub, _DDL_DML_NODES):
            raise WhereValidationError(REJECT_DDL_DML, f"DDL/DML node {type(sub).__name__} not allowed")

    # Cross-table reference detection: any column with a qualifier other than
    # the target table_id (or unqualified) is rejected.
    for col in node.find_all(exp.Column):
        qualifier = col.table  # e.g. "other_table" in `other_table.id`
        if qualifier and qualifier.lower() != table_id.lower():
            raise WhereValidationError(
                REJECT_CROSS_TABLE,
                f"column {col.sql()} references table {qualifier!r}, expected {table_id!r}",
            )
```

- [ ] **Step 1.4: Run tests to verify pass**

Run: `pytest tests/test_where_validator.py::TestParse tests/test_where_validator.py::TestStructural -v`
Expected: 5 passed

- [ ] **Step 1.5: Commit**

```bash
git add app/api/where_validator.py tests/test_where_validator.py
git commit -m "feat(validator): WHERE clause parser + structural rejects"
```

---

## Task 2: WHERE validator — function allow-list

Spec §3.7 enumerated function set. Reject unknown functions with explicit name in error.

**Files:**
- Modify: `app/api/where_validator.py`
- Modify: `tests/test_where_validator.py`

- [ ] **Step 2.1: Append failing tests**

```python
# tests/test_where_validator.py (append after TestStructural)

class TestFunctionAllowList:
    @pytest.mark.parametrize(
        "predicate",
        [
            # Comparison
            "amount = 1", "amount != 1", "amount IS NULL", "amount IS NOT NULL",
            "country_code IN ('CZ', 'SK')", "amount BETWEEN 1 AND 100",
            "country_code LIKE 'C%'", "country_code NOT LIKE 'X%'",
            # Boolean
            "amount = 1 AND country_code = 'CZ'",
            "amount = 1 OR amount = 2",
            "NOT (amount = 1)",
            # Date/Time
            "event_date > DATE '2026-01-01'",
            "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
            "EXTRACT(YEAR FROM event_date) = 2026",
            # String
            "STARTS_WITH(country_code, 'C')",
            "REGEXP_CONTAINS(country_code, r'C[ZS]')",
            "LENGTH(country_code) = 2",
            # Math
            "amount > ABS(-5)",
            "amount BETWEEN GREATEST(0, 10) AND LEAST(100, 200)",
            # Cast
            "CAST(country_code AS STRING) = 'CZ'",
            # Conditional
            "IFNULL(country_code, 'XX') = 'CZ'",
            "COALESCE(amount, 0) > 0",
        ],
    )
    def test_allowed_predicate(self, predicate):
        validate_where(predicate, TABLE_ID, SCHEMA)  # must not raise

    @pytest.mark.parametrize(
        "predicate,expected_func",
        [
            ("amount = EXTERNAL_QUERY('connection', 'SELECT 1')", "EXTERNAL_QUERY"),
            ("country_code = SESSION_USER()", "SESSION_USER"),
            ("amount = ML.PREDICT(MODEL m, TABLE t)", "ML.PREDICT"),
            ("amount = OBSCURE_BUILTIN(country_code)", "OBSCURE_BUILTIN"),
            ("amount = ARRAY_AGG(amount)", "ARRAY_AGG"),
            ("amount = ROW_NUMBER() OVER (PARTITION BY country_code)", "ROW_NUMBER"),
        ],
    )
    def test_disallowed_function(self, predicate, expected_func):
        with pytest.raises(WhereValidationError) as e:
            validate_where(predicate, TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        # The rejected function name must appear in detail or message
        assert expected_func.upper() in str(e.value).upper() or (
            e.value.detail and expected_func.upper() in str(e.value.detail).upper()
        )
```

- [ ] **Step 2.2: Run tests to verify failure**

Run: `pytest tests/test_where_validator.py::TestFunctionAllowList -v`
Expected: FAIL — current validator allows any function (no allow-list yet).

- [ ] **Step 2.3: Add allow-list + function check**

In `app/api/where_validator.py`, after the imports and `_DDL_DML_NODES`:

```python
# v1 BigQuery function allow-list (spec §3.7). Stored as upper-case names.
# Categorized for documentation; merged into one set for membership check.
_ALLOW_FUNCTIONS_COMPARISON = {
    # Operators are AST nodes, not functions; not listed here.
}
_ALLOW_FUNCTIONS_DATETIME = {
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME",
    "DATE", "DATETIME", "TIMESTAMP", "TIME",
    "DATE_ADD", "DATE_SUB", "DATE_DIFF", "DATE_TRUNC", "EXTRACT",
    "FORMAT_DATE", "FORMAT_TIMESTAMP", "PARSE_DATE", "PARSE_TIMESTAMP",
    "UNIX_SECONDS", "UNIX_MILLIS",
}
_ALLOW_FUNCTIONS_STRING = {
    "CONCAT", "LENGTH", "LOWER", "UPPER", "SUBSTR", "SUBSTRING",
    "TRIM", "LTRIM", "RTRIM", "REPLACE",
    "STARTS_WITH", "ENDS_WITH", "CONTAINS_SUBSTR",
    "REGEXP_CONTAINS", "REGEXP_EXTRACT", "SAFE_CAST",
}
_ALLOW_FUNCTIONS_MATH = {
    "ABS", "CEIL", "FLOOR", "ROUND", "MOD", "POWER", "SQRT",
    "LOG", "LN", "EXP", "SIGN", "GREATEST", "LEAST",
}
_ALLOW_FUNCTIONS_CAST = {"CAST"}
_ALLOW_FUNCTIONS_CONDITIONAL = {"IF", "IFNULL", "COALESCE", "NULLIF", "CASE"}

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    _ALLOW_FUNCTIONS_DATETIME
    | _ALLOW_FUNCTIONS_STRING
    | _ALLOW_FUNCTIONS_MATH
    | _ALLOW_FUNCTIONS_CAST
    | _ALLOW_FUNCTIONS_CONDITIONAL
)

# CAST target types allowed
_ALLOW_CAST_TYPES = {
    "INT64", "FLOAT64", "NUMERIC", "STRING", "BYTES", "BOOL",
    "DATE", "DATETIME", "TIMESTAMP", "TIME", "DECIMAL", "BIGNUMERIC",
}
```

Then add `_walk_functions()` and call it from `_walk_structural`. Add at the end of `_walk_structural`:

```python
    _walk_functions(node)
```

And new helper:

```python
def _walk_functions(node: exp.Expression) -> None:
    for func in node.find_all(exp.Func):
        # Window functions, aggregates, anonymous funcs — sqlglot uses subclasses.
        if isinstance(func, exp.Window):
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"window function not allowed: {func.sql()}",
                detail={"function": "WINDOW"},
            )
        if isinstance(func, exp.AggFunc):
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"aggregate function not allowed in WHERE: {type(func).__name__}",
                detail={"function": type(func).__name__.upper()},
            )

        # `func.name` is the SQL function name; might be empty for built-in operators.
        name = (func.name or "").upper()
        # Anonymous function nodes carry their identifier in a different slot
        if not name and hasattr(func, "this") and hasattr(func.this, "name"):
            name = (func.this.name or "").upper()

        # Skip operators-as-nodes (Add, Sub, Mul, Div, Eq, Neq, Lt, Gt, Like, In, Between, etc.)
        # — these are exp.Binary subclasses, not exp.Func subclasses, so usually not seen here.
        # But be defensive: if name is empty AFTER all heuristics, skip rather than flag.
        if not name:
            continue

        if name not in ALLOWED_FUNCTIONS:
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"function not in v1 allow-list: {name}",
                detail={"function": name},
            )
```

- [ ] **Step 2.4: Run tests to verify pass**

Run: `pytest tests/test_where_validator.py -v`
Expected: all previous + new TestFunctionAllowList pass.

- [ ] **Step 2.5: Commit**

```bash
git add app/api/where_validator.py tests/test_where_validator.py
git commit -m "feat(validator): function allow-list with explicit reject codes"
```

---

## Task 3: WHERE validator — column existence + identifier-path validation

Reject WHERE referring to columns not in the target schema. Spec §3.7 identifier-path section.

**Files:**
- Modify: `app/api/where_validator.py`
- Modify: `tests/test_where_validator.py`

- [ ] **Step 3.1: Append failing tests**

```python
# tests/test_where_validator.py (append)

class TestColumnExistence:
    def test_known_column_accepted(self):
        validate_where("country_code = 'CZ'", TABLE_ID, SCHEMA)

    def test_unknown_column_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("nonexistent_field = 'X'", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_UNKNOWN_COLUMN
        assert "nonexistent_field" in str(e.value).lower()

    def test_qualified_known_column_accepted(self):
        # Same-table qualifier is allowed
        validate_where(
            f"{TABLE_ID}.country_code = 'CZ'",
            TABLE_ID, SCHEMA,
        )

    def test_qualified_unknown_column_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where(
                f"{TABLE_ID}.bogus_field = 'X'",
                TABLE_ID, SCHEMA,
            )
        assert e.value.kind == REJECT_UNKNOWN_COLUMN
```

- [ ] **Step 3.2: Run tests to verify failure**

Run: `pytest tests/test_where_validator.py::TestColumnExistence -v`
Expected: 3 passes (qualified known + unqualified known will pass already), 1 fail on unknown_column expectation OR all 4 fail because the validator never checks columns.

- [ ] **Step 3.3: Add column-existence check**

Add new helper after `_walk_functions`:

```python
def _walk_columns(node: exp.Expression, schema: Mapping[str, str]) -> None:
    """Reject column references not present in the target table's schema."""
    known = {c.lower() for c in schema}
    for col in node.find_all(exp.Column):
        # `col.name` is the leaf column name (e.g. "country_code" in
        # "tbl.country_code"). For dotted struct fields like "rec.sub.leaf",
        # sqlglot models as nested exp.Dot; v1 only checks top-level names.
        leaf = (col.name or "").lower()
        if leaf and leaf not in known:
            raise WhereValidationError(
                REJECT_UNKNOWN_COLUMN,
                f"column {col.name!r} not in schema for {col.table!r}",
                detail={"column": col.name},
            )
```

Call from `_walk_structural` after `_walk_functions(node)`:

```python
    _walk_functions(node)
    _walk_columns(node, schema)
```

- [ ] **Step 3.4: Run tests to verify pass**

Run: `pytest tests/test_where_validator.py -v`
Expected: all pass (including the new TestColumnExistence).

- [ ] **Step 3.5: Commit**

```bash
git add app/api/where_validator.py tests/test_where_validator.py
git commit -m "feat(validator): column-existence check via target-table schema"
```

---

## Task 4: Process-local quota tracker

Spec §3.8. Per-user concurrent count + daily byte cap, in-memory. Multi-replica caveat documented in spec §9.4 — out of scope.

**Files:**
- Create: `app/api/v2_quota.py`
- Test: `tests/test_v2_quota.py`

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_v2_quota.py
"""Tests for the process-local v2 scan quota tracker (spec §3.8)."""

from datetime import datetime, timedelta, timezone
import pytest

from app.api.v2_quota import (
    QuotaTracker,
    QuotaExceededError,
    KIND_CONCURRENT,
    KIND_DAILY_BYTES,
)


def make_tracker(max_concurrent=5, max_daily_bytes=100):
    return QuotaTracker(
        max_concurrent_per_user=max_concurrent,
        max_daily_bytes_per_user=max_daily_bytes,
    )


class TestConcurrent:
    def test_acquire_within_cap_succeeds(self):
        q = make_tracker(max_concurrent=3)
        with q.acquire(user="alice"):
            with q.acquire(user="alice"):
                with q.acquire(user="alice"):
                    pass

    def test_acquire_above_cap_raises(self):
        q = make_tracker(max_concurrent=2)
        with q.acquire(user="alice"):
            with q.acquire(user="alice"):
                with pytest.raises(QuotaExceededError) as e:
                    with q.acquire(user="alice"):
                        pass
                assert e.value.kind == KIND_CONCURRENT
                assert e.value.current == 2
                assert e.value.limit == 2

    def test_release_on_context_exit(self):
        q = make_tracker(max_concurrent=1)
        with q.acquire(user="alice"):
            pass
        # Counter dropped on exit; new acquire works
        with q.acquire(user="alice"):
            pass

    def test_release_on_exception(self):
        q = make_tracker(max_concurrent=1)
        try:
            with q.acquire(user="alice"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with q.acquire(user="alice"):
            pass

    def test_per_user_isolation(self):
        q = make_tracker(max_concurrent=1)
        with q.acquire(user="alice"):
            with q.acquire(user="bob"):
                pass


class TestDailyBytes:
    def test_record_within_cap(self):
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=300)
        q.record_bytes(user="alice", n=400)
        assert q.bytes_used_today(user="alice") == 700

    def test_record_above_cap_raises(self):
        q = make_tracker(max_daily_bytes=1000)
        q.record_bytes(user="alice", n=600)
        with pytest.raises(QuotaExceededError) as e:
            q.record_bytes(user="alice", n=500)
        assert e.value.kind == KIND_DAILY_BYTES
        assert e.value.current == 1100  # would-be total
        assert e.value.limit == 1000

    def test_per_user_isolation(self):
        q = make_tracker(max_daily_bytes=100)
        q.record_bytes(user="alice", n=80)
        q.record_bytes(user="bob", n=80)  # bob's bucket independent
        with pytest.raises(QuotaExceededError):
            q.record_bytes(user="alice", n=30)

    def test_reset_on_utc_midnight(self, monkeypatch):
        q = make_tracker(max_daily_bytes=100)
        # Simulate the day boundary by injecting "now"
        d1 = datetime(2026, 4, 27, 23, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("app.api.v2_quota._utcnow", lambda: d1)
        q.record_bytes(user="alice", n=80)
        assert q.bytes_used_today(user="alice") == 80

        d2 = d1 + timedelta(hours=2)  # crosses UTC midnight
        monkeypatch.setattr("app.api.v2_quota._utcnow", lambda: d2)
        assert q.bytes_used_today(user="alice") == 0
        q.record_bytes(user="alice", n=80)  # ok, fresh bucket
```

- [ ] **Step 4.2: Run tests to verify failure**

Run: `pytest tests/test_v2_quota.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.v2_quota'`

- [ ] **Step 4.3: Implement quota tracker**

Create `app/api/v2_quota.py`:

```python
"""Process-local quota tracker for /api/v2/scan (spec §3.8).

In-memory only. Multi-replica deployments effectively multiply caps by N
(documented caveat — see spec §9.4). Future v2 should move to durable
storage if horizontal scale is needed.
"""

from __future__ import annotations
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

logger = logging.getLogger(__name__)

KIND_CONCURRENT = "concurrent_scans"
KIND_DAILY_BYTES = "daily_bytes"


@dataclass
class QuotaExceededError(Exception):
    kind: str
    current: int
    limit: int
    retry_after_seconds: int = 0

    def __str__(self) -> str:
        return f"{self.kind}: {self.current}/{self.limit}"


def _utcnow() -> datetime:  # patched in tests
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    """ISO date string in UTC, used as the daily-bucket key."""
    return _utcnow().strftime("%Y-%m-%d")


class QuotaTracker:
    """Thread-safe quota state. Caller wraps each request in `with q.acquire(user)`,
    and after the BQ result lands records bytes via `record_bytes(user, n)`.
    """

    def __init__(self, *, max_concurrent_per_user: int, max_daily_bytes_per_user: int):
        self._max_concurrent = max_concurrent_per_user
        self._max_daily_bytes = max_daily_bytes_per_user
        self._lock = threading.Lock()
        # state: { user_id: { "concurrent": int, "bucket_day": "YYYY-MM-DD", "bytes": int } }
        self._state: dict[str, dict] = {}

    def _ensure_bucket(self, user: str) -> dict:
        today = _utc_today()
        s = self._state.setdefault(user, {"concurrent": 0, "bucket_day": today, "bytes": 0})
        if s["bucket_day"] != today:
            s["bucket_day"] = today
            s["bytes"] = 0
        return s

    @contextlib.contextmanager
    def acquire(self, user: str) -> Iterator[None]:
        with self._lock:
            s = self._ensure_bucket(user)
            if s["concurrent"] >= self._max_concurrent:
                raise QuotaExceededError(
                    kind=KIND_CONCURRENT,
                    current=s["concurrent"],
                    limit=self._max_concurrent,
                )
            s["concurrent"] += 1
        try:
            yield
        finally:
            with self._lock:
                s = self._ensure_bucket(user)
                s["concurrent"] = max(0, s["concurrent"] - 1)

    def record_bytes(self, user: str, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            s = self._ensure_bucket(user)
            new_total = s["bytes"] + n
            if new_total > self._max_daily_bytes:
                # Surface the would-be total so caller can include it in 429 body.
                raise QuotaExceededError(
                    kind=KIND_DAILY_BYTES,
                    current=new_total,
                    limit=self._max_daily_bytes,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )
            s["bytes"] = new_total

    def bytes_used_today(self, user: str) -> int:
        with self._lock:
            return self._ensure_bucket(user)["bytes"]


def _seconds_until_utc_midnight() -> int:
    now = _utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).replace(day=now.day)
    # Next midnight = today's midnight + 1 day
    from datetime import timedelta
    next_midnight = midnight + timedelta(days=1)
    return int((next_midnight - now).total_seconds())
```

- [ ] **Step 4.4: Run tests to verify pass**

Run: `pytest tests/test_v2_quota.py -v`
Expected: 8 passed.

- [ ] **Step 4.5: Commit**

```bash
git add app/api/v2_quota.py tests/test_v2_quota.py
git commit -m "feat(v2): process-local quota tracker (concurrent + daily bytes)"
```

---

## Task 5: LRU+TTL cache helper

Used by catalog/schema/sample endpoints. Spec §3.6.

**Files:**
- Create: `app/api/v2_cache.py`
- Test: `tests/test_v2_cache.py`

- [ ] **Step 5.1: Write failing tests**

```python
# tests/test_v2_cache.py
import pytest
import time

from app.api.v2_cache import TTLCache


class TestTTLCache:
    def test_set_get(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("k", "v")
        assert c.get("k") == "v"

    def test_get_missing_returns_default(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        assert c.get("missing") is None
        assert c.get("missing", default="x") == "x"

    def test_expiry(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr("app.api.v2_cache._now", lambda: now[0])
        c = TTLCache(maxsize=10, ttl_seconds=10)
        c.set("k", "v")
        assert c.get("k") == "v"
        now[0] += 11
        assert c.get("k") is None  # expired

    def test_lru_eviction(self):
        c = TTLCache(maxsize=2, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)  # should evict 'a' (LRU)
        assert c.get("a") is None
        assert c.get("b") == 2
        assert c.get("c") == 3

    def test_invalidate(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("k", "v")
        c.invalidate("k")
        assert c.get("k") is None

    def test_clear(self):
        c = TTLCache(maxsize=10, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert c.get("a") is None
        assert c.get("b") is None
```

- [ ] **Step 5.2: Run tests to verify failure**

Run: `pytest tests/test_v2_cache.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 5.3: Implement TTLCache**

Create `app/api/v2_cache.py`:

```python
"""Simple thread-safe LRU + TTL cache for v2 endpoints."""

from __future__ import annotations
import threading
import time
from collections import OrderedDict
from typing import Any


def _now() -> float:  # patched in tests
    return time.monotonic()


class TTLCache:
    def __init__(self, *, maxsize: int, ttl_seconds: float):
        self._max = maxsize
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            expiry, value = entry
            if _now() > expiry:
                del self._data[key]
                return default
            self._data.move_to_end(key)  # mark as recently used
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            expiry = _now() + self._ttl
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (expiry, value)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
```

- [ ] **Step 5.4: Run tests to verify pass**

Run: `pytest tests/test_v2_cache.py -v`
Expected: 6 passed.

- [ ] **Step 5.5: Commit**

```bash
git add app/api/v2_cache.py tests/test_v2_cache.py
git commit -m "feat(v2): TTLCache helper (LRU + TTL, thread-safe)"
```

---

## Task 6: Arrow IPC streaming response helper

Spec §3.4 step 9. Used by `/api/v2/scan`. Wraps a pyarrow Table or RecordBatchReader as an HTTP streaming response.

**Files:**
- Create: `app/api/v2_arrow.py`
- Test: `tests/test_v2_arrow.py`

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_v2_arrow.py
import io
import pyarrow as pa
import pytest

from app.api.v2_arrow import arrow_table_to_ipc_bytes, parse_ipc_bytes


def test_round_trip_simple_table():
    src = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    body = arrow_table_to_ipc_bytes(src)
    assert isinstance(body, bytes) and len(body) > 0
    got = parse_ipc_bytes(body)
    assert got.equals(src)


def test_empty_table_round_trip():
    src = pa.table({"a": pa.array([], type=pa.int64())})
    body = arrow_table_to_ipc_bytes(src)
    got = parse_ipc_bytes(body)
    assert got.num_rows == 0
    assert got.schema.equals(src.schema)
```

- [ ] **Step 6.2: Run tests to verify failure**

Run: `pytest tests/test_v2_arrow.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 6.3: Implement Arrow helpers**

Create `app/api/v2_arrow.py`:

```python
"""Arrow IPC serialization helpers for /api/v2/scan responses.

Server side serializes a pyarrow.Table to IPC stream bytes; client side
deserializes back. Content-Type is `application/vnd.apache.arrow.stream`.
"""

from __future__ import annotations
import io
import pyarrow as pa


CONTENT_TYPE = "application/vnd.apache.arrow.stream"


def arrow_table_to_ipc_bytes(table: pa.Table) -> bytes:
    """Serialize a pyarrow.Table to Arrow IPC stream bytes."""
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        for batch in table.to_batches():
            writer.write_batch(batch)
    return sink.getvalue()


def parse_ipc_bytes(data: bytes) -> pa.Table:
    """Deserialize Arrow IPC stream bytes to a pyarrow.Table."""
    reader = pa.ipc.open_stream(io.BytesIO(data))
    return reader.read_all()
```

- [ ] **Step 6.4: Run tests to verify pass**

Run: `pytest tests/test_v2_arrow.py -v`
Expected: 2 passed.

- [ ] **Step 6.5: Commit**

```bash
git add app/api/v2_arrow.py tests/test_v2_arrow.py
git commit -m "feat(v2): Arrow IPC serialization helpers"
```

---

## Task 7: `GET /api/v2/catalog`

Spec §3.1. Lists tables visible to user (RBAC-filtered) with metadata.

**Files:**
- Create: `app/api/v2_catalog.py`
- Modify: `app/main.py`
- Test: `tests/test_v2_catalog.py`

- [ ] **Step 7.1: Write failing tests**

```python
# tests/test_v2_catalog.py
import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_two_tables(conn):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(conn)
    repo.register(
        id="orders", name="orders", source_type="keboola",
        bucket="sales", source_table="orders", query_mode="local",
        is_public=True,
    )
    repo.register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestCatalogShape:
    def test_admin_sees_both_tables(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            ids = {t["id"] for t in data["tables"]}
            assert {"orders", "bq_view"} <= ids
        finally:
            conn.close()

    def test_local_table_has_duckdb_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "orders")
            assert row["sql_flavor"] == "duckdb"
            assert row["query_mode"] == "local"

    def test_bq_table_has_bigquery_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "bq_view")
            assert row["sql_flavor"] == "bigquery"
            assert row["query_mode"] == "remote"
            assert "where_examples" in row
            assert "fetch_via" in row
```

- [ ] **Step 7.2: Run tests to verify failure**

Run: `pytest tests/test_v2_catalog.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 7.3: Implement catalog endpoint**

Create `app/api/v2_catalog.py`:

```python
"""GET /api/v2/catalog — list tables visible to caller (spec §3.1)."""

from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache

router = APIRouter(prefix="/api/v2", tags=["v2"])

_catalog_cache = TTLCache(maxsize=1024, ttl_seconds=300)  # per-user, 5 min


def _flavor_for(source_type: str) -> str:
    return "bigquery" if source_type == "bigquery" else "duckdb"


def _examples_for(source_type: str) -> list[str]:
    if source_type == "bigquery":
        return [
            "event_date > DATE '2026-01-01'",
            "country_code = 'CZ' AND platform = 'web'",
        ]
    return []


def _fetch_hint(table_id: str, source_type: str) -> str:
    if source_type == "bigquery":
        return f"da fetch {table_id} --select <cols> --where '<BQ predicate>' --limit <N>"
    return "already local — query directly via `da query`"


def build_catalog(conn: duckdb.DuckDBPyConnection, user: dict) -> dict:
    cache_key = f"{user.get('email', '?')}|catalog"
    cached = _catalog_cache.get(cache_key)
    if cached is not None:
        return cached

    repo = TableRegistryRepository(conn)
    rows = repo.list_all()

    visible = []
    for r in rows:
        if user.get("role") != "admin" and not can_access_table(user, r["id"], conn):
            continue
        visible.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "description": r.get("description") or "",
            "source_type": r.get("source_type") or "",
            "query_mode": r.get("query_mode") or "local",
            "sql_flavor": _flavor_for(r.get("source_type") or ""),
            "where_examples": _examples_for(r.get("source_type") or ""),
            "fetch_via": _fetch_hint(r["id"], r.get("source_type") or ""),
            "rough_size_hint": None,  # populated by Task 8 schema endpoint when called
        })

    payload = {
        "tables": visible,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    _catalog_cache.set(cache_key, payload)
    return payload


@router.get("/catalog")
async def catalog(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return build_catalog(conn, user)
```

- [ ] **Step 7.4: Mount in `app/main.py`**

Find the `app.include_router(...)` block (around line 279-287). Add:

```python
    from app.api.v2_catalog import router as v2_catalog_router
    app.include_router(v2_catalog_router)
```

- [ ] **Step 7.5: Run tests to verify pass**

Run: `pytest tests/test_v2_catalog.py -v`
Expected: 3 passed.

- [ ] **Step 7.6: Commit**

```bash
git add app/api/v2_catalog.py app/main.py tests/test_v2_catalog.py
git commit -m "feat(v2): GET /api/v2/catalog — RBAC-filtered table list"
```

---

## Task 8: `GET /api/v2/schema/{table_id}`

Spec §3.2. Column metadata + BQ flavor hints.

**Files:**
- Create: `app/api/v2_schema.py`
- Modify: `app/main.py`
- Test: `tests/test_v2_schema.py`

- [ ] **Step 8.1: Write failing tests**

```python
# tests/test_v2_schema.py
import importlib
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_bq_table(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestSchemaEndpoint:
    def test_bq_table_returns_columns_and_dialect_hints(self, reload_db, monkeypatch):
        from app.api import v2_schema
        # Stub the BQ schema fetch to avoid hitting real BQ
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda project, dataset, table: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a: {"partition_by": "event_date", "clustered_by": []})

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = v2_schema.build_schema(conn, user, "bq_view", project_id="my-proj")
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert data["sql_flavor"] == "bigquery"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        assert "where_dialect_hints" in data
        assert data["partition_by"] == "event_date"

    def test_unknown_table_raises_404(self, reload_db):
        from app.api.v2_schema import build_schema, NotFound
        conn = reload_db.get_system_db()
        try:
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(NotFound):
                build_schema(conn, user, "missing", project_id="my-proj")
        finally:
            conn.close()
```

- [ ] **Step 8.2: Run tests to verify failure**

Run: `pytest tests/test_v2_schema.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 8.3: Implement schema endpoint**

Create `app/api/v2_schema.py`:

```python
"""GET /api/v2/schema/{table_id} — table column metadata (spec §3.2)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_schema_cache = TTLCache(maxsize=512, ttl_seconds=3600)


class NotFound(Exception):
    pass


_BQ_DIALECT_HINTS = {
    "date_literal": "DATE '2026-01-01'",
    "timestamp_literal": "TIMESTAMP '2026-01-01 00:00:00 UTC'",
    "interval_subtract": "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
    "regex": "REGEXP_CONTAINS(field, r'pattern')",
    "cast": "CAST(x AS INT64)",
}


def _fetch_bq_schema(project: str, dataset: str, table: str) -> list[dict]:
    """Fetch column list via INFORMATION_SCHEMA.COLUMNS using DuckDB BQ extension."""
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        bq_sql = (
            f"SELECT column_name, data_type, is_nullable, description "
            f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = ? ORDER BY ordinal_position"
        )
        rows = conn.execute(
            "SELECT * FROM bigquery_query(?, ?, ?)",
            [project, bq_sql, table],
        ).fetchall()
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": r[2] == "YES",
                "description": r[3] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def _fetch_bq_table_options(project: str, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info; returns empty dict on miss."""
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    try:
        token = get_metadata_token()
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            escaped = token.replace("'", "''")
            conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
            bq_sql = (
                f"SELECT partition_column, cluster_columns "
                f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
                f"WHERE table_name = ?"
            )
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [project, bq_sql, table],
            ).fetchone()
            if not row:
                return {}
            return {
                "partition_by": row[0],
                "clustered_by": (row[1] or "").split(",") if row[1] else [],
            }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("BQ table options fetch failed for %s.%s.%s: %s", project, dataset, table, e)
        return {}


def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    project_id: str,
) -> dict:
    cache_key = f"{table_id}"
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if user.get("role") != "admin" and not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    source_type = row.get("source_type") or ""
    if source_type == "bigquery":
        dataset = row.get("bucket") or ""
        source_table = row.get("source_table") or table_id
        columns = _fetch_bq_schema(project_id, dataset, source_table)
        opts = _fetch_bq_table_options(project_id, dataset, source_table)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "bigquery",
            "columns": columns,
            "partition_by": opts.get("partition_by"),
            "clustered_by": opts.get("clustered_by", []),
            "where_dialect_hints": _BQ_DIALECT_HINTS,
        }
    else:
        # Local source — read schema from the parquet via DuckDB
        from pathlib import Path
        from app.utils import get_data_dir
        parquet = (
            get_data_dir() / "extracts" / source_type / "data" / f"{table_id}.parquet"
        )
        local_conn = duckdb.connect(":memory:")
        try:
            cols = local_conn.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)]
            ).fetchall()
        finally:
            local_conn.close()
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "duckdb",
            "columns": [
                {"name": c[0], "type": c[1], "nullable": c[2] == "YES", "description": ""}
                for c in cols
            ],
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }

    _schema_cache.set(cache_key, payload)
    return payload


@router.get("/schema/{table_id}")
async def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    try:
        return build_schema(conn, user, table_id, project_id=project_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
```

- [ ] **Step 8.4: Mount in `app/main.py`**

Add alongside catalog router:

```python
    from app.api.v2_schema import router as v2_schema_router
    app.include_router(v2_schema_router)
```

- [ ] **Step 8.5: Run tests to verify pass**

Run: `pytest tests/test_v2_schema.py -v`
Expected: 2 passed.

- [ ] **Step 8.6: Commit**

```bash
git add app/api/v2_schema.py app/main.py tests/test_v2_schema.py
git commit -m "feat(v2): GET /api/v2/schema/{table_id} — column metadata + BQ hints"
```

---

## Task 9: `GET /api/v2/sample/{table_id}`

Spec §3.3. Returns N sample rows.

**Files:**
- Create: `app/api/v2_sample.py`
- Modify: `app/main.py`
- Test: `tests/test_v2_sample.py`

- [ ] **Step 9.1: Write failing tests**

```python
# tests/test_v2_sample.py
import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestSampleEndpoint:
    def test_returns_n_rows_for_bq_table(self, reload_db, monkeypatch):
        from app.api import v2_sample
        monkeypatch.setattr(
            v2_sample, "_fetch_bq_sample",
            lambda project, dataset, table, n: [
                {"event_date": "2026-04-27", "country_code": "CZ"},
                {"event_date": "2026-04-26", "country_code": "SK"},
            ],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=2, project_id="proj")
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert len(data["rows"]) == 2

    def test_caps_n_at_100(self, reload_db, monkeypatch):
        from app.api import v2_sample
        captured = {}
        def fake_fetch(project, dataset, table, n):
            captured["n"] = n
            return []
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", fake_fetch)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            v2_sample.build_sample(conn, user, "bq_view", n=999, project_id="proj")
        finally:
            conn.close()
        assert captured["n"] == 100
```

- [ ] **Step 9.2: Run tests to verify failure**

Run: `pytest tests/test_v2_sample.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 9.3: Implement sample endpoint**

Create `app/api/v2_sample.py`:

```python
"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


def _fetch_bq_sample(project: str, dataset: str, table: str, n: int) -> list[dict]:
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        bq_sql = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT {int(n)}"
        df = conn.execute(
            "SELECT * FROM bigquery_query(?, ?)",
            [project, bq_sql],
        ).fetchdf()
        return df.to_dict(orient="records")
    finally:
        conn.close()


def build_sample(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    n: int,
    project_id: str,
) -> dict:
    n = max(1, min(int(n), _MAX_N))
    cache_key = f"{table_id}|{n}"
    cached = _sample_cache.get(cache_key)
    if cached is not None:
        return cached

    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise FileNotFoundError(table_id)

    if user.get("role") != "admin" and not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    source_type = row.get("source_type") or ""
    if source_type == "bigquery":
        rows = _fetch_bq_sample(project_id, row.get("bucket") or "", row.get("source_table") or table_id, n)
    else:
        from app.utils import get_data_dir
        parquet = get_data_dir() / "extracts" / source_type / "data" / f"{table_id}.parquet"
        c = duckdb.connect(":memory:")
        try:
            df = c.execute(
                f"SELECT * FROM read_parquet(?) LIMIT {n}",
                [str(parquet)],
            ).fetchdf()
            rows = df.to_dict(orient="records")
        finally:
            c.close()

    payload = {"table_id": table_id, "rows": rows, "source": source_type}
    _sample_cache.set(cache_key, payload)
    return payload


@router.get("/sample/{table_id}")
async def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    try:
        return build_sample(conn, user, table_id, n=n, project_id=project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
```

- [ ] **Step 9.4: Mount + run tests**

Add `from app.api.v2_sample import router as v2_sample_router; app.include_router(v2_sample_router)` in `app/main.py`. Run: `pytest tests/test_v2_sample.py -v`. Expected: 2 passed.

- [ ] **Step 9.5: Commit**

```bash
git add app/api/v2_sample.py app/main.py tests/test_v2_sample.py
git commit -m "feat(v2): GET /api/v2/sample/{table_id} — N sample rows"
```

---

## Task 10: `POST /api/v2/scan/estimate`

Spec §3.5. BQ dryRun for cost estimate.

**Files:**
- Create: `app/api/v2_scan.py` (will be extended in Task 11 for `/scan` proper)
- Modify: `app/main.py`
- Test: `tests/test_v2_scan_estimate.py`

- [ ] **Step 10.1: Write failing test**

```python
# tests/test_v2_scan_estimate.py
import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestScanEstimate:
    def test_returns_scan_bytes_for_bq(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_bq_dry_run_bytes",
            lambda project, sql: 4_400_000_000,
        )
        # Stub the schema fetch the validator uses
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000000,
            }
            data = v2_scan.estimate(conn, user, req, project_id="proj")
        finally:
            conn.close()
        assert data["estimated_scan_bytes"] == 4_400_000_000
        assert "estimated_result_rows" in data
        assert "bq_cost_estimate_usd" in data
```

- [ ] **Step 10.2: Run test to verify failure**

Run: `pytest tests/test_v2_scan_estimate.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 10.3: Implement estimate endpoint**

Create `app/api/v2_scan.py`:

```python
"""POST /api/v2/scan and POST /api/v2/scan/estimate (spec §3.4 + §3.5)."""

from __future__ import annotations
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.where_validator import (
    validate_where, WhereValidationError,
)
from app.api.v2_schema import build_schema  # reused for column resolution

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])


class ScanRequest(BaseModel):
    table_id: str
    select: Optional[list[str]] = None
    where: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1)
    order_by: Optional[list[str]] = None


def _resolve_schema(conn, user, table_id: str, project_id: str) -> dict:
    """Get {column: type} dict for the target table — used by validator + projection check."""
    s = build_schema(conn, user, table_id, project_id=project_id)
    return {c["name"]: c["type"] for c in s.get("columns", [])}


def _bq_dry_run_bytes(project: str, sql: str) -> int:
    """Run a BQ dry-run via the google-cloud-bigquery client and return totalBytesProcessed."""
    from google.cloud import bigquery
    from google.api_core.client_options import ClientOptions
    client = bigquery.Client(
        project=project,
        client_options=ClientOptions(quota_project_id=project),
    )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return int(job.total_bytes_processed or 0)


def _build_bq_sql(table_row: dict, project_id: str, req: ScanRequest) -> str:
    select_sql = ", ".join(req.select) if req.select else "*"
    table_ref = f"`{project_id}.{table_row.get('bucket') or ''}.{table_row.get('source_table') or req.table_id}`"
    sql = f"SELECT {select_sql} FROM {table_ref}"
    if req.where:
        sql += f" WHERE {req.where}"
    if req.order_by:
        sql += f" ORDER BY {', '.join(req.order_by)}"
    if req.limit:
        sql += f" LIMIT {int(req.limit)}"
    return sql


def estimate(conn, user, raw_request: dict, *, project_id: str) -> dict:
    req = ScanRequest(**raw_request)
    repo = TableRegistryRepository(conn)
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if user.get("role") != "admin" and not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    schema = _resolve_schema(conn, user, req.table_id, project_id)

    # Validate WHERE first
    if req.where:
        validate_where(req.where, req.table_id, schema)
    # Validate select columns exist
    if req.select:
        unknown = [c for c in req.select if c not in schema]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")

    if (row.get("source_type") or "") != "bigquery":
        return {
            "table_id": req.table_id,
            "estimated_scan_bytes": 0,
            "estimated_result_rows": None,
            "estimated_result_bytes": None,
            "bq_cost_estimate_usd": 0.0,
        }

    bq_sql = _build_bq_sql(row, project_id, req)
    scan_bytes = _bq_dry_run_bytes(project_id, bq_sql)

    cost_per_tb = float(get_value("api", "scan", "bq_cost_per_tb_usd", default=5.0) or 5.0)
    cost = (scan_bytes / 1_099_511_627_776) * cost_per_tb  # 1 TiB = 2^40

    # Heuristic for result row/byte estimate
    avg_row_bytes = max(1, sum(_avg_bytes_for_type(t) for t in schema.values()) // max(1, len(schema)))
    rows_est = scan_bytes // max(avg_row_bytes, 1)
    if req.limit:
        rows_est = min(rows_est, req.limit)

    return {
        "table_id": req.table_id,
        "estimated_scan_bytes": int(scan_bytes),
        "estimated_result_rows": int(rows_est),
        "estimated_result_bytes": int(rows_est * avg_row_bytes),
        "bq_cost_estimate_usd": round(cost, 4),
    }


def _avg_bytes_for_type(t: str) -> int:
    t = (t or "").upper()
    if t in ("INT64", "FLOAT64", "DATE", "TIMESTAMP", "DATETIME", "TIME"):
        return 8
    if t == "STRING":
        return 32  # rough average
    if t == "BYTES":
        return 64
    if t == "BOOL":
        return 1
    return 16


@router.post("/scan/estimate")
async def scan_estimate_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    try:
        return estimate(conn, user, raw, project_id=project_id)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}})
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="table not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 10.4: Mount + run test**

Add `from app.api.v2_scan import router as v2_scan_router; app.include_router(v2_scan_router)` in `app/main.py`.

Run: `pytest tests/test_v2_scan_estimate.py -v`. Expected: 1 passed.

- [ ] **Step 10.5: Commit**

```bash
git add app/api/v2_scan.py app/main.py tests/test_v2_scan_estimate.py
git commit -m "feat(v2): POST /api/v2/scan/estimate via BQ dryRun"
```

---

## Task 11: `POST /api/v2/scan` — full pipeline

Spec §3.4 — combine validator + RBAC + quota + max_result_bytes + Arrow IPC streaming.

**Files:**
- Modify: `app/api/v2_scan.py` (extend with `/scan` endpoint)
- Test: `tests/test_v2_scan.py`

- [ ] **Step 11.1: Write failing tests**

```python
# tests/test_v2_scan.py
import importlib
from unittest.mock import MagicMock
import pyarrow as pa
import pytest

from app.api.v2_arrow import parse_ipc_bytes


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestScan:
    def test_returns_arrow_ipc_for_simple_request(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        fake_table = pa.table(
            {"event_date": ["2026-04-27"], "country_code": ["CZ"]}
        )
        monkeypatch.setattr(
            v2_scan, "_run_bq_scan",
            lambda *a, **kw: fake_table,
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 100,
            }
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.num_rows == 1
        assert got.column_names == ["event_date", "country_code"]

    def test_quota_concurrent_exceeded_raises_429(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.v2_quota import QuotaTracker, QuotaExceededError, KIND_CONCURRENT
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda *a, **kw: fake_table)

        tracker = QuotaTracker(max_concurrent_per_user=1, max_daily_bytes_per_user=10**12)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "limit": 1}

            # Hold one concurrent slot
            with tracker.acquire(user="a@x.com"):
                with pytest.raises(QuotaExceededError) as e:
                    v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
                assert e.value.kind == KIND_CONCURRENT
        finally:
            conn.close()

    def test_validator_rejection_propagates(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.where_validator import WhereValidationError, REJECT_UNKNOWN_FUNCTION
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )

        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "where": "event_date = NUKE_FN()",
            }
            with pytest.raises(WhereValidationError) as e:
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
            assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        finally:
            conn.close()
```

- [ ] **Step 11.2: Run tests to verify failure**

Run: `pytest tests/test_v2_scan.py -v`
Expected: FAIL — `run_scan` and `_run_bq_scan` don't exist yet.

- [ ] **Step 11.3: Extend `app/api/v2_scan.py`**

Append to the file:

```python
import io
import pyarrow as pa
from app.api.v2_arrow import arrow_table_to_ipc_bytes, CONTENT_TYPE
from app.api.v2_quota import QuotaTracker, QuotaExceededError
from fastapi.responses import Response

# Module-level singleton (process-local quota state per spec §3.8)
_quota_singleton: QuotaTracker | None = None


def _build_quota_tracker() -> QuotaTracker:
    """Returns or constructs the process-local quota tracker."""
    global _quota_singleton
    if _quota_singleton is None:
        _quota_singleton = QuotaTracker(
            max_concurrent_per_user=int(get_value("api", "scan", "max_concurrent_per_user", default=5) or 5),
            max_daily_bytes_per_user=int(get_value("api", "scan", "max_daily_bytes_per_user", default=53687091200) or 53687091200),
        )
    return _quota_singleton


def _max_result_bytes() -> int:
    return int(get_value("api", "scan", "max_result_bytes", default=2_147_483_648) or 2_147_483_648)


def _max_limit() -> int:
    return int(get_value("api", "scan", "max_limit", default=10_000_000) or 10_000_000)


def _run_bq_scan(project: str, sql: str) -> pa.Table:
    """Execute SQL via DuckDB BQ extension, return pyarrow Table."""
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        # Use bigquery_query() since the SQL is already authored against the BQ jobs API
        return conn.execute(
            "SELECT * FROM bigquery_query(?, ?)",
            [project, sql],
        ).arrow()
    finally:
        conn.close()


def run_scan(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    raw_request: dict,
    *,
    project_id: str,
    quota: QuotaTracker,
) -> bytes:
    """Validate → quota → execute → serialize. Returns Arrow IPC bytes.

    Raises:
        WhereValidationError, QuotaExceededError, FileNotFoundError, PermissionError, ValueError
    """
    req = ScanRequest(**raw_request)
    repo = TableRegistryRepository(conn)
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if user.get("role") != "admin" and not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    if req.limit and req.limit > _max_limit():
        raise ValueError(f"limit {req.limit} exceeds max {_max_limit()}")

    schema = _resolve_schema(conn, user, req.table_id, project_id)
    if req.where:
        validate_where(req.where, req.table_id, schema)
    if req.select:
        unknown = [c for c in req.select if c not in schema]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")

    user_id = user.get("email") or "anon"

    with quota.acquire(user=user_id):
        if (row.get("source_type") or "") != "bigquery":
            # Local source: query parquet directly
            from app.utils import get_data_dir
            parquet = (
                get_data_dir() / "extracts" / row["source_type"] / "data" / f"{req.table_id}.parquet"
            )
            local = duckdb.connect(":memory:")
            try:
                projection = ", ".join(req.select) if req.select else "*"
                sql = f"SELECT {projection} FROM read_parquet(?)"
                if req.where:
                    sql += f" WHERE {req.where}"
                if req.order_by:
                    sql += f" ORDER BY {', '.join(req.order_by)}"
                if req.limit:
                    sql += f" LIMIT {int(req.limit)}"
                table = local.execute(sql, [str(parquet)]).arrow()
            finally:
                local.close()
        else:
            bq_sql = _build_bq_sql(row, project_id, req)
            table = _run_bq_scan(project_id, bq_sql)

        ipc = arrow_table_to_ipc_bytes(table)

        # Enforce max_result_bytes guard (spec §3.4 step 8)
        if len(ipc) > _max_result_bytes():
            # Truncate by taking only as many rows as fit roughly
            # Simple heuristic: cap rows to estimated avg per max_bytes
            row_count = table.num_rows
            avg = max(1, len(ipc) // max(row_count, 1))
            keep = min(row_count, _max_result_bytes() // max(avg, 1))
            table = table.slice(0, keep)
            ipc = arrow_table_to_ipc_bytes(table)

        # Record bytes for daily quota
        quota.record_bytes(user=user_id, n=len(ipc))
        return ipc


@router.post("/scan")
async def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    quota = _build_quota_tracker()
    try:
        ipc = run_scan(conn, user, raw, project_id=project_id, quota=quota)
        return Response(content=ipc, media_type=CONTENT_TYPE)
    except WhereValidationError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}},
        )
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "kind": e.kind,
                "current": e.current,
                "limit": e.limit,
                "retry_after_seconds": e.retry_after_seconds,
            },
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="table not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 11.4: Run tests to verify pass**

Run: `pytest tests/test_v2_scan.py -v`
Expected: 3 passed.

- [ ] **Step 11.5: Commit**

```bash
git add app/api/v2_scan.py tests/test_v2_scan.py
git commit -m "feat(v2): POST /api/v2/scan — validator + quota + Arrow IPC pipeline"
```

---

## Task 12: Drop wrap-view code path with `legacy_wrap_views` toggle

Spec §6.1. The wrap view in `connectors/bigquery/extractor.py` for VIEW entities is the source of #101 problem.

**Files:**
- Modify: `connectors/bigquery/extractor.py`
- Modify: `tests/test_bigquery_extractor.py`

- [ ] **Step 12.1: Write failing test for the new behavior**

Append to `tests/test_bigquery_extractor.py`:

```python
class TestDropWrapViewForBQViews:
    def test_view_entity_does_not_create_master_view_by_default(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract
        monkeypatch.setattr("connectors.bigquery.extractor.get_metadata_token", lambda: "tok")
        monkeypatch.setattr("connectors.bigquery.extractor._detect_table_type", lambda *a, **kw: "VIEW")

        # Stub BQ extension calls to avoid hitting real BQ
        real_connect = duckdb.connect

        def safe_connect(*a, **kw):
            return _CapturingProxy(real_connect(*a, **kw))
        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", safe_connect)

        # legacy toggle is OFF by default → expect no CREATE VIEW for the BQ view
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_value",
            lambda *args, default=None, **kw: False if "legacy_wrap_views" in args else default,
            raising=False,
        )

        result = init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "myview", "bucket": "ds", "source_table": "myview", "description": ""}],
        )

        # Confirm extract.duckdb has _meta + _remote_attach but NO master view for myview
        c = duckdb.connect(str(tmp_path / "extract.duckdb"), read_only=True)
        try:
            views = c.execute(
                "SELECT view_name FROM duckdb_views() WHERE view_name='myview'"
            ).fetchall()
            assert views == [], f"expected no wrap view for VIEW entity by default; got {views}"
            meta = c.execute("SELECT table_name FROM _meta").fetchall()
            assert ("myview",) in meta, "_meta must still record the view"
        finally:
            c.close()

    def test_legacy_wrap_views_toggle_restores_old_behavior(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract
        monkeypatch.setattr("connectors.bigquery.extractor.get_metadata_token", lambda: "tok")
        monkeypatch.setattr("connectors.bigquery.extractor._detect_table_type", lambda *a, **kw: "VIEW")

        real_connect = duckdb.connect
        def safe_connect(*a, **kw):
            return _CapturingProxy(real_connect(*a, **kw))
        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", safe_connect)

        # legacy toggle ON → should still create the wrap view
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_value",
            lambda *args, default=None, **kw: True if "legacy_wrap_views" in args else default,
            raising=False,
        )

        init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "myview", "bucket": "ds", "source_table": "myview", "description": ""}],
        )

        c = duckdb.connect(str(tmp_path / "extract.duckdb"), read_only=True)
        try:
            views = c.execute(
                "SELECT view_name FROM duckdb_views() WHERE view_name='myview'"
            ).fetchall()
            assert views == [("myview",)]
        finally:
            c.close()
```

- [ ] **Step 12.2: Run tests to verify failure**

Run: `pytest tests/test_bigquery_extractor.py::TestDropWrapViewForBQViews -v`
Expected: FAIL — current code always emits the wrap view for VIEW entities.

- [ ] **Step 12.3: Modify `connectors/bigquery/extractor.py`**

Find the section in `init_extract` that emits the wrap view for VIEW entities. Replace the dual-path branch:

```python
# OLD:
if entity_type == "BASE TABLE":
    view_sql = (...)  # direct ref
else:
    if entity_type not in ("VIEW", "MATERIALIZED_VIEW"):
        logger.warning(...)
    bq_inner = ...
    view_sql = (...)  # bigquery_query() wrap

conn.execute(view_sql)
```

With:

```python
# NEW: only emit wrap view for BASE TABLE; for VIEW types, just record in _meta.
from app.instance_config import get_value as _get_value
legacy_wrap_views = bool(_get_value("data_source", "bigquery", "legacy_wrap_views", default=False))

if entity_type == "BASE TABLE":
    view_sql = (
        f'CREATE OR REPLACE VIEW "{table_name}" AS '
        f'SELECT * FROM bq."{dataset}"."{source_table}"'
    )
    conn.execute(view_sql)
elif legacy_wrap_views:
    # Backwards compatibility — for one release cycle only.
    if entity_type not in ("VIEW", "MATERIALIZED_VIEW"):
        logger.warning(
            "Unknown BQ entity type %r for %s.%s.%s — using bigquery_query() path",
            entity_type, project_id, dataset, source_table,
        )
    bq_inner = f"SELECT * FROM `{project_id}.{dataset}.{source_table}`"
    bq_inner_escaped = bq_inner.replace("'", "''")
    view_sql = (
        f'CREATE OR REPLACE VIEW "{table_name}" AS '
        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
    )
    conn.execute(view_sql)
else:
    # Default: VIEW / MATERIALIZED_VIEW are recorded in _meta but no master view created.
    # Analyst must use `da fetch` (v2 primitives) to materialize a snapshot locally.
    logger.info(
        "Skipping wrap view for %s entity %s.%s.%s — use `da fetch`",
        entity_type, project_id, dataset, source_table,
    )

# _meta entry is recorded in ALL branches (existing code below stays as-is)
conn.execute(
    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
    [table_name, tc.get("description", ""), now],
)
```

- [ ] **Step 12.4: Run tests to verify pass**

Run: `pytest tests/test_bigquery_extractor.py -v`
Expected: ALL pass — including the 2 new TestDropWrapViewForBQViews tests + existing tests (some may need updating; if so, update them to reflect that VIEW now skips the master view by default).

If existing `TestViewVsTableTemplates::test_view_uses_bigquery_query_function` breaks, **update it** to enable the legacy toggle in its monkeypatch (per pattern in Step 12.1's second test).

- [ ] **Step 12.5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bigquery_extractor.py
git commit -m "feat(bq): drop wrap-view for VIEW entities by default; legacy toggle behind flag"
```

---

## Task 13: Arrow over HTTP client + JSON helpers

Client-side counterpart to v2 endpoints. Used by all `da` commands that talk to v2 API.

**Files:**
- Create: `cli/v2_client.py`
- Test: `tests/test_v2_client.py`

- [ ] **Step 13.1: Write failing tests**

```python
# tests/test_v2_client.py
import json
import pyarrow as pa
import pytest
from unittest.mock import MagicMock, patch

from cli.v2_client import (
    api_get_json,
    api_post_arrow,
    api_post_json,
    V2ClientError,
)


def _fake_response(*, status=200, json_body=None, arrow_body=None, content_type=None):
    resp = MagicMock()
    resp.status_code = status
    if json_body is not None:
        resp.json.return_value = json_body
        resp.text = json.dumps(json_body)
        resp.content = resp.text.encode()
    if arrow_body is not None:
        resp.content = arrow_body
    if content_type:
        resp.headers = {"content-type": content_type}
    else:
        resp.headers = {}
    return resp


class TestApiGetJson:
    def test_200_returns_parsed_json(self):
        with patch("cli.v2_client.requests.get") as m:
            m.return_value = _fake_response(json_body={"hello": "world"})
            assert api_get_json("/api/v2/catalog") == {"hello": "world"}

    def test_4xx_raises_v2clienterror(self):
        with patch("cli.v2_client.requests.get") as m:
            m.return_value = _fake_response(status=403, json_body={"detail": "nope"})
            with pytest.raises(V2ClientError) as e:
                api_get_json("/api/v2/catalog")
            assert e.value.status_code == 403


class TestApiPostArrow:
    def test_returns_arrow_table(self):
        from app.api.v2_arrow import arrow_table_to_ipc_bytes
        ipc = arrow_table_to_ipc_bytes(pa.table({"x": [1, 2, 3]}))
        with patch("cli.v2_client.requests.post") as m:
            m.return_value = _fake_response(
                arrow_body=ipc,
                content_type="application/vnd.apache.arrow.stream",
            )
            got = api_post_arrow("/api/v2/scan", {"table_id": "x"})
        assert got.num_rows == 3
        assert got.column_names == ["x"]
```

- [ ] **Step 13.2: Run tests to verify failure**

Run: `pytest tests/test_v2_client.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 13.3: Implement v2 client**

Create `cli/v2_client.py`:

```python
"""HTTP client helpers for /api/v2/* endpoints (CLI side)."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import io

import requests
import pyarrow as pa

from cli.config import get_server_url, get_pat


@dataclass
class V2ClientError(Exception):
    status_code: int
    body: Any
    message: str = ""

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.message or self.body}"


def _headers() -> dict:
    pat = get_pat()
    return {"Authorization": f"Bearer {pat}"} if pat else {}


def api_get_json(path: str, **params) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = requests.get(url, headers=_headers(), params=params or None, timeout=30)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    return r.json()


def api_post_json(path: str, payload: dict) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = requests.post(url, json=payload, headers=_headers(), timeout=120)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    return r.json()


def api_post_arrow(path: str, payload: dict) -> pa.Table:
    """Post JSON, expect Arrow IPC stream response."""
    url = f"{get_server_url().rstrip('/')}{path}"
    r = requests.post(url, json=payload, headers=_headers(), timeout=600)
    if r.status_code >= 400:
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        raise V2ClientError(status_code=r.status_code, body=body, message=str(body)[:200])
    reader = pa.ipc.open_stream(io.BytesIO(r.content))
    return reader.read_all()
```

If `cli/config.py` lacks `get_server_url` / `get_pat`, those helpers exist already under different names — adapt to whatever the existing CLI uses (`api_get` in `cli/client.py` is the existing helper; mirror its config-loading pattern).

- [ ] **Step 13.4: Run tests to verify pass**

Run: `pytest tests/test_v2_client.py -v`
Expected: 3 passed.

- [ ] **Step 13.5: Commit**

```bash
git add cli/v2_client.py tests/test_v2_client.py
git commit -m "feat(cli): v2 HTTP client (JSON + Arrow IPC)"
```

---

## Task 14: Snapshot metadata I/O + flock helper

Backing for `da fetch` and `da snapshot *`. Spec §4.2.

**Files:**
- Create: `cli/snapshot_meta.py`
- Test: `tests/test_snapshot_meta.py`

- [ ] **Step 14.1: Write failing tests**

```python
# tests/test_snapshot_meta.py
import json
import pytest
from pathlib import Path

from cli.snapshot_meta import (
    SnapshotMeta,
    write_meta,
    read_meta,
    list_snapshots,
    snapshot_lock,
)


@pytest.fixture
def snap_dir(tmp_path):
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


class TestMetaIO:
    def test_round_trip(self, snap_dir):
        meta = SnapshotMeta(
            name="cz_recent", table_id="bq_view",
            select=["a", "b"], where="a > 1", limit=100, order_by=None,
            fetched_at="2026-04-27T17:30:00Z",
            effective_as_of="2026-04-27T17:30:00Z",
            rows=10, bytes_local=1024,
            estimated_scan_bytes_at_fetch=5_000_000,
            result_hash_md5="abc",
        )
        write_meta(snap_dir, meta)
        got = read_meta(snap_dir, "cz_recent")
        assert got == meta

    def test_read_missing_returns_none(self, snap_dir):
        assert read_meta(snap_dir, "missing") is None

    def test_list_snapshots_empty(self, snap_dir):
        assert list_snapshots(snap_dir) == []

    def test_list_snapshots_with_data(self, snap_dir):
        for name in ("a", "b", "c"):
            (snap_dir / f"{name}.parquet").write_bytes(b"PAR1\\x00\\x00PAR1")
            write_meta(snap_dir, SnapshotMeta(
                name=name, table_id="t", select=None, where=None, limit=None, order_by=None,
                fetched_at="t", effective_as_of="t", rows=0, bytes_local=10,
                estimated_scan_bytes_at_fetch=0, result_hash_md5="",
            ))
        names = sorted(s.name for s in list_snapshots(snap_dir))
        assert names == ["a", "b", "c"]


class TestSnapshotLock:
    def test_lock_is_exclusive(self, snap_dir, tmp_path):
        """Two processes can't both hold the lock at once."""
        import threading, time
        held_at = []
        def worker(label, hold_seconds):
            with snapshot_lock(snap_dir):
                held_at.append((label, time.time()))
                time.sleep(hold_seconds)
                held_at.append((f"{label}-done", time.time()))

        t1 = threading.Thread(target=worker, args=("A", 0.2))
        t2 = threading.Thread(target=worker, args=("B", 0.2))
        t1.start(); time.sleep(0.05); t2.start()
        t1.join(); t2.join()
        # A acquired, A-done, B acquired, B-done — never interleaved
        labels = [x[0] for x in held_at]
        assert labels in (
            ["A", "A-done", "B", "B-done"],
            ["B", "B-done", "A", "A-done"],
        ), f"expected serialized acquisition; got {labels}"
```

- [ ] **Step 14.2: Run tests to verify failure**

Run: `pytest tests/test_snapshot_meta.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 14.3: Implement snapshot meta + lock**

Create `cli/snapshot_meta.py`:

```python
"""Snapshot sidecar metadata + file lock helpers (spec §4.2)."""

from __future__ import annotations
import contextlib
import fcntl
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class SnapshotMeta:
    name: str
    table_id: str
    select: Optional[list[str]]
    where: Optional[str]
    limit: Optional[int]
    order_by: Optional[list[str]]
    fetched_at: str               # ISO 8601 UTC
    effective_as_of: str          # ISO 8601 UTC, server-side eval time
    rows: int
    bytes_local: int
    estimated_scan_bytes_at_fetch: int
    result_hash_md5: str


def _meta_path(snap_dir: Path, name: str) -> Path:
    return snap_dir / f"{name}.meta.json"


def write_meta(snap_dir: Path, meta: SnapshotMeta) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    with _meta_path(snap_dir, meta.name).open("w") as f:
        json.dump(asdict(meta), f, indent=2)


def read_meta(snap_dir: Path, name: str) -> Optional[SnapshotMeta]:
    p = _meta_path(snap_dir, name)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return SnapshotMeta(**data)


def list_snapshots(snap_dir: Path) -> list[SnapshotMeta]:
    if not snap_dir.exists():
        return []
    out = []
    for meta_file in snap_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_file.read_text())
            out.append(SnapshotMeta(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def delete_snapshot(snap_dir: Path, name: str) -> bool:
    """Delete the snapshot's parquet + meta. Returns True if removed, False if missing."""
    parquet = snap_dir / f"{name}.parquet"
    meta = _meta_path(snap_dir, name)
    removed = False
    if parquet.exists():
        parquet.unlink(); removed = True
    if meta.exists():
        meta.unlink(); removed = True
    return removed


@contextlib.contextmanager
def snapshot_lock(snap_dir: Path):
    """Exclusive flock on snap_dir/.lock — serializes snapshot installs.

    Concurrent `da fetch` invocations queue here.
    """
    snap_dir.mkdir(parents=True, exist_ok=True)
    lock_file = snap_dir / ".lock"
    lock_file.touch(exist_ok=True)
    fd = open(lock_file, "r+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
```

- [ ] **Step 14.4: Run tests to verify pass**

Run: `pytest tests/test_snapshot_meta.py -v`
Expected: 5 passed.

- [ ] **Step 14.5: Commit**

```bash
git add cli/snapshot_meta.py tests/test_snapshot_meta.py
git commit -m "feat(cli): snapshot metadata sidecar + flock helper"
```

---

## Task 15: `da catalog` / `da schema` / `da describe`

Spec §4.1. Discovery commands.

**Files:**
- Create: `cli/commands/catalog.py`, `cli/commands/schema.py`, `cli/commands/describe.py`
- Modify: `cli/main.py`
- Test: `tests/test_cli_catalog.py`

- [ ] **Step 15.1: Write failing tests**

```python
# tests/test_cli_catalog.py
import json
from typer.testing import CliRunner
from unittest.mock import patch
import pytest


def test_da_catalog_json_output(monkeypatch):
    """`da catalog --json` emits the server's JSON verbatim."""
    payload = {
        "tables": [
            {"id": "orders", "name": "orders", "source_type": "keboola",
             "query_mode": "local", "sql_flavor": "duckdb",
             "where_examples": [], "fetch_via": "...", "rough_size_hint": None},
        ],
        "server_time": "2026-04-27T17:30:00Z",
    }
    with patch("cli.commands.catalog.api_get_json", return_value=payload):
        from cli.main import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["catalog", "--json"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["tables"][0]["id"] == "orders"


def test_da_catalog_table_output(monkeypatch):
    payload = {
        "tables": [
            {"id": "orders", "name": "orders", "source_type": "keboola",
             "query_mode": "local", "sql_flavor": "duckdb",
             "where_examples": [], "fetch_via": "...", "rough_size_hint": None},
        ],
        "server_time": "2026-04-27T17:30:00Z",
    }
    with patch("cli.commands.catalog.api_get_json", return_value=payload):
        from cli.main import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["catalog"])
    assert result.exit_code == 0
    assert "orders" in result.stdout
    assert "keboola" in result.stdout
```

- [ ] **Step 15.2: Run tests to verify failure**

Run: `pytest tests/test_cli_catalog.py -v`
Expected: FAIL — `cli.commands.catalog` doesn't exist.

- [ ] **Step 15.3: Implement `cli/commands/catalog.py`**

```python
"""`da catalog` — list registered tables (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

catalog_app = typer.Typer(help="List tables visible to you")


@catalog_app.callback(invoke_without_command=True)
def catalog(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass client-side cache"),
):
    """List tables visible to you (RBAC-filtered)."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        data = api_get_json("/api/v2/catalog", refresh=int(refresh))
    except V2ClientError as e:
        typer.echo(f"Error: catalog fetch failed: {e}", err=True)
        raise typer.Exit(5)

    if json:
        typer.echo(json_lib.dumps(data, indent=2))
        return
    # Human-readable table
    typer.echo(f"{'ID':30s}  {'SOURCE':10s}  {'MODE':8s}  {'FLAVOR':10s}  NAME")
    for t in data.get("tables", []):
        typer.echo(
            f"{t['id']:30s}  {t['source_type']:10s}  {t['query_mode']:8s}  "
            f"{t['sql_flavor']:10s}  {t.get('name', '')}"
        )
```

- [ ] **Step 15.4: Implement `cli/commands/schema.py`**

```python
"""`da schema <table>` — show columns + BQ flavor hints (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

schema_app = typer.Typer(help="Show column metadata for a table")


@schema_app.callback(invoke_without_command=True)
def schema(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    json: bool = typer.Option(False, "--json"),
):
    """Show column metadata for a table."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        data = api_get_json(f"/api/v2/schema/{table_id}")
    except V2ClientError as e:
        typer.echo(f"Error: schema fetch failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    if json:
        typer.echo(json_lib.dumps(data, indent=2))
        return

    flavor = data.get("sql_flavor", "duckdb")
    typer.echo(f"Table: {data['table_id']}  ({data['source_type']} — use {flavor.upper()} SQL dialect)")
    typer.echo("")
    typer.echo(f"{'COLUMN':30s}  {'TYPE':15s}  {'NULL':5s}  DESCRIPTION")
    for c in data.get("columns", []):
        typer.echo(
            f"{c['name']:30s}  {c['type']:15s}  "
            f"{'YES' if c.get('nullable') else 'NO':5s}  {c.get('description', '')}"
        )
    if data.get("partition_by"):
        typer.echo(f"\\nPartition: {data['partition_by']}")
    if data.get("clustered_by"):
        typer.echo(f"Clustered: {', '.join(data['clustered_by'])}")
    if data.get("where_dialect_hints"):
        typer.echo("\\nWHERE dialect hints:")
        for k, v in data["where_dialect_hints"].items():
            typer.echo(f"  {k:25s}  {v}")
```

- [ ] **Step 15.5: Implement `cli/commands/describe.py`**

```python
"""`da describe <table>` — schema + sample rows (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

describe_app = typer.Typer(help="Show schema + sample rows for a table")


@describe_app.callback(invoke_without_command=True)
def describe(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    n: int = typer.Option(5, "-n", "--rows", help="Sample rows count"),
    json: bool = typer.Option(False, "--json"),
):
    """Show schema + sample rows for a table."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=n)
    except V2ClientError as e:
        typer.echo(f"Error: describe failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    if json:
        typer.echo(json_lib.dumps({"schema": sch, "sample": sam}, indent=2, default=str))
        return

    # Reuse schema printing
    from cli.commands.schema import schema as schema_cmd
    typer.echo(f"Table: {sch['table_id']}")
    typer.echo("")
    typer.echo("Schema:")
    for c in sch.get("columns", []):
        typer.echo(f"  {c['name']:30s} {c['type']}")
    typer.echo("")
    typer.echo(f"Sample ({len(sam.get('rows', []))} rows):")
    for row in sam.get("rows", []):
        typer.echo(f"  {row}")
```

- [ ] **Step 15.6: Register commands in `cli/main.py`**

Find where other subcommands are added (`app.add_typer(...)` calls). Add:

```python
from cli.commands.catalog import catalog_app
from cli.commands.schema import schema_app
from cli.commands.describe import describe_app

app.add_typer(catalog_app, name="catalog")
app.add_typer(schema_app, name="schema")
app.add_typer(describe_app, name="describe")
```

(Adjust based on the actual `cli/main.py` pattern — could be flat commands instead of typers.)

- [ ] **Step 15.7: Run tests to verify pass**

Run: `pytest tests/test_cli_catalog.py -v`
Expected: 2 passed.

- [ ] **Step 15.8: Commit**

```bash
git add cli/commands/catalog.py cli/commands/schema.py cli/commands/describe.py cli/main.py tests/test_cli_catalog.py
git commit -m "feat(cli): da catalog/schema/describe — discovery commands"
```

---

## Task 16: `da fetch`

Spec §4.2. The headline command.

**Files:**
- Create: `cli/commands/fetch.py`
- Modify: `cli/main.py`
- Test: `tests/test_cli_fetch.py`

- [ ] **Step 16.1: Write failing tests**

```python
# tests/test_cli_fetch.py
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
import pyarrow as pa
import json
import pytest


def _seed_local_dir(tmp_path):
    """Set up the user's agnes-data directory for the CLI to find."""
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "snapshots").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(_seed_local_dir(tmp_path)))
    yield tmp_path


class TestDaFetch:
    def test_estimate_only_does_not_create_snapshot(self, cli_env, monkeypatch):
        from cli.main import app as cli_app
        with patch("cli.commands.fetch.api_post_json") as m:
            m.return_value = {
                "estimated_scan_bytes": 1_000_000,
                "estimated_result_rows": 100,
                "estimated_result_bytes": 1_000,
                "bq_cost_estimate_usd": 0.0001,
            }
            runner = CliRunner()
            result = runner.invoke(cli_app, [
                "fetch", "bq_view",
                "--select", "a,b",
                "--where", "a > 1",
                "--limit", "100",
                "--estimate",
            ])
        assert result.exit_code == 0, result.stdout
        # No parquet should be created
        assert not list((cli_env / "user" / "snapshots").glob("*.parquet"))

    def test_fetch_creates_snapshot_with_meta(self, cli_env, monkeypatch):
        from cli.main import app as cli_app
        # Estimate path
        with patch("cli.commands.fetch.api_post_json") as m_est, \
             patch("cli.commands.fetch.api_post_arrow") as m_scan:
            m_est.return_value = {
                "estimated_scan_bytes": 1000,
                "estimated_result_rows": 2,
                "estimated_result_bytes": 100,
                "bq_cost_estimate_usd": 0.0,
            }
            m_scan.return_value = pa.table({"a": [1, 2], "b": ["x", "y"]})
            runner = CliRunner()
            result = runner.invoke(cli_app, [
                "fetch", "bq_view",
                "--select", "a,b",
                "--limit", "10",
                "--no-estimate",
            ])
        assert result.exit_code == 0, result.stdout
        snap = cli_env / "user" / "snapshots" / "bq_view.parquet"
        meta = cli_env / "user" / "snapshots" / "bq_view.meta.json"
        assert snap.exists()
        assert meta.exists()
        assert json.loads(meta.read_text())["rows"] == 2

    def test_fetch_existing_snapshot_without_force_fails(self, cli_env, monkeypatch):
        from cli.main import app as cli_app
        # Pre-create a snapshot
        snap = cli_env / "user" / "snapshots" / "bq_view.parquet"
        snap.write_bytes(b"PAR1\\x00\\x00PAR1")
        meta = cli_env / "user" / "snapshots" / "bq_view.meta.json"
        meta.write_text('{"name": "bq_view", "table_id": "bq_view", "select": null, "where": null, "limit": null, "order_by": null, "fetched_at": "x", "effective_as_of": "x", "rows": 0, "bytes_local": 0, "estimated_scan_bytes_at_fetch": 0, "result_hash_md5": ""}')

        runner = CliRunner()
        result = runner.invoke(cli_app, ["fetch", "bq_view", "--no-estimate"])
        assert result.exit_code == 6, f"expected exit code 6 (snapshot_exists); got {result.exit_code}\\n{result.stdout}"
```

- [ ] **Step 16.2: Run tests to verify failure**

Run: `pytest tests/test_cli_fetch.py -v`
Expected: FAIL — `cli.commands.fetch` doesn't exist.

- [ ] **Step 16.3: Implement `cli/commands/fetch.py`**

```python
"""`da fetch` — materialize a filtered subset of a remote table locally (spec §4.2)."""

from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import typer

from cli.snapshot_meta import (
    SnapshotMeta, write_meta, read_meta, snapshot_lock,
)
from cli.v2_client import api_post_json, api_post_arrow, V2ClientError

fetch_app = typer.Typer(help="Fetch a filtered subset of a remote table locally")


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _print_estimate(d: dict) -> None:
    typer.echo(f"  estimated_scan_bytes:   {d.get('estimated_scan_bytes', 0):>15,} bytes")
    typer.echo(f"  estimated_result_rows:  {d.get('estimated_result_rows', 0):>15,}")
    typer.echo(f"  estimated_result_bytes: {d.get('estimated_result_bytes', 0):>15,} bytes")
    typer.echo(f"  bq_cost_estimate_usd:   $ {d.get('bq_cost_estimate_usd', 0):.4f}")


@fetch_app.callback(invoke_without_command=True)
def fetch(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    select: str = typer.Option(None, "--select", help="Comma-separated column list"),
    where: str = typer.Option(None, "--where", help="WHERE predicate (BQ flavor for remote tables)"),
    limit: int = typer.Option(None, "--limit"),
    order_by: str = typer.Option(None, "--order-by", help="Comma-separated"),
    as_name: str = typer.Option(None, "--as", help="Local snapshot name (default: <table_id>)"),
    estimate: bool = typer.Option(False, "--estimate", help="Run dry-run only, do not fetch"),
    no_estimate: bool = typer.Option(False, "--no-estimate", help="Skip the pre-fetch estimate"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing snapshot of the same name"),
):
    """Fetch a filtered subset of a remote table locally."""
    if ctx.invoked_subcommand is not None:
        return

    name = as_name or table_id
    snap_dir = _local_dir() / "user" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Build request
    req = {"table_id": table_id}
    if select:
        req["select"] = [c.strip() for c in select.split(",") if c.strip()]
    if where:
        req["where"] = where
    if limit:
        req["limit"] = int(limit)
    if order_by:
        req["order_by"] = [c.strip() for c in order_by.split(",") if c.strip()]

    # Estimate (always shown unless --no-estimate)
    if not no_estimate:
        try:
            est = api_post_json("/api/v2/scan/estimate", req)
        except V2ClientError as e:
            typer.echo(f"Error: estimate failed: {e}", err=True)
            raise typer.Exit(_exit_code_for(e))
        typer.echo(f"Estimate for {table_id}:")
        _print_estimate(est)
        if estimate:
            return

    # Snapshot existence check
    if not force and read_meta(snap_dir, name) is not None:
        existing = read_meta(snap_dir, name)
        typer.echo(
            f"Error: snapshot {name!r} already exists "
            f"(fetched {existing.fetched_at}, {existing.rows:,} rows). "
            f"Pass --force to overwrite, or 'da snapshot refresh {name}' to update in place.",
            err=True,
        )
        raise typer.Exit(6)

    # Fetch
    try:
        table = api_post_arrow("/api/v2/scan", req)
    except V2ClientError as e:
        typer.echo(f"Error: fetch failed: {e}", err=True)
        raise typer.Exit(_exit_code_for(e))

    # Install under flock
    parquet_path = snap_dir / f"{name}.parquet"
    with snapshot_lock(snap_dir):
        pq.write_table(table, parquet_path)
        # Register view in user analytics.duckdb
        local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
        local_db.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(local_db))
        try:
            conn.execute(
                f'CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM read_parquet(?)',
                [str(parquet_path)],
            )
        finally:
            conn.close()

        # Compute hash + write meta
        result_hash = hashlib.md5(parquet_path.read_bytes()[:1_000_000]).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        meta = SnapshotMeta(
            name=name, table_id=table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=int(table.num_rows),
            bytes_local=parquet_path.stat().st_size,
            estimated_scan_bytes_at_fetch=int(est.get("estimated_scan_bytes", 0)) if not no_estimate else 0,
            result_hash_md5=result_hash,
        )
        write_meta(snap_dir, meta)

    typer.echo(f"Fetched {table.num_rows:,} rows -> {name}")


def _exit_code_for(e: V2ClientError) -> int:
    if e.status_code == 400:
        # Inspect body for 'kind'
        body = e.body if isinstance(e.body, dict) else {}
        if body.get("error") == "validator_rejected":
            return 2
        return 2
    if e.status_code == 401:
        return 7
    if e.status_code == 403:
        return 8
    if e.status_code == 404:
        return 8  # treat unknown table as RBAC-equivalent
    if e.status_code == 429:
        return 3
    if e.status_code >= 500:
        return 5
    return 9
```

- [ ] **Step 16.4: Register in `cli/main.py`**

```python
from cli.commands.fetch import fetch_app
app.add_typer(fetch_app, name="fetch")
```

- [ ] **Step 16.5: Run tests to verify pass**

Run: `pytest tests/test_cli_fetch.py -v`
Expected: 3 passed.

- [ ] **Step 16.6: Commit**

```bash
git add cli/commands/fetch.py cli/main.py tests/test_cli_fetch.py
git commit -m "feat(cli): da fetch — materialize filtered remote subset locally"
```

---

## Task 17: `da snapshot list/refresh/drop/prune`

Spec §4.2.

**Files:**
- Create: `cli/commands/snapshot.py`
- Modify: `cli/main.py`
- Test: `tests/test_cli_snapshot.py`

- [ ] **Step 17.1: Write failing tests**

```python
# tests/test_cli_snapshot.py
from typer.testing import CliRunner
from unittest.mock import patch
import json
import pytest

from cli.snapshot_meta import SnapshotMeta, write_meta


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    snap_dir = tmp_path / "user" / "snapshots"
    snap_dir.mkdir(parents=True)
    yield tmp_path


def _seed_meta(tmp_path, name="cz_recent", rows=100):
    snap_dir = tmp_path / "user" / "snapshots"
    parquet = snap_dir / f"{name}.parquet"
    parquet.write_bytes(b"PAR1\\x00\\x00PAR1")
    write_meta(snap_dir, SnapshotMeta(
        name=name, table_id="bq_view", select=None, where=None, limit=None, order_by=None,
        fetched_at="2026-04-27T10:00:00+00:00",
        effective_as_of="2026-04-27T10:00:00+00:00",
        rows=rows, bytes_local=parquet.stat().st_size,
        estimated_scan_bytes_at_fetch=0, result_hash_md5="abc",
    ))


class TestSnapshotList:
    def test_list_empty(self, cli_env):
        from cli.main import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["snapshot", "list"])
        assert result.exit_code == 0


class TestSnapshotDrop:
    def test_drop_removes_files(self, cli_env):
        from cli.main import app as cli_app
        _seed_meta(cli_env, "cz_recent")
        snap_dir = cli_env / "user" / "snapshots"
        assert (snap_dir / "cz_recent.parquet").exists()

        runner = CliRunner()
        result = runner.invoke(cli_app, ["snapshot", "drop", "cz_recent"])
        assert result.exit_code == 0
        assert not (snap_dir / "cz_recent.parquet").exists()
        assert not (snap_dir / "cz_recent.meta.json").exists()

    def test_drop_missing_returns_2(self, cli_env):
        from cli.main import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["snapshot", "drop", "nonexistent"])
        assert result.exit_code != 0
```

- [ ] **Step 17.2: Run tests to verify failure**

Run: `pytest tests/test_cli_snapshot.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 17.3: Implement `cli/commands/snapshot.py`**

```python
"""`da snapshot list/refresh/drop/prune` (spec §4.2)."""

from __future__ import annotations
import hashlib
import os
import json as json_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import typer

from cli.snapshot_meta import (
    list_snapshots, read_meta, write_meta, delete_snapshot,
    snapshot_lock, SnapshotMeta,
)
from cli.v2_client import api_post_arrow, V2ClientError

snapshot_app = typer.Typer(help="Manage local snapshots")


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _snap_dir() -> Path:
    return _local_dir() / "user" / "snapshots"


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n} TB"


@snapshot_app.command("list")
def list_cmd(
    json: bool = typer.Option(False, "--json"),
):
    """List local snapshots."""
    snaps = list_snapshots(_snap_dir())
    if json:
        typer.echo(json_lib.dumps([s.__dict__ for s in snaps], indent=2))
        return
    if not snaps:
        typer.echo("(no snapshots)")
        return
    typer.echo(f"{'NAME':30s}  {'ROWS':>10s}  {'SIZE':>10s}  {'AGE':>10s}  {'TABLE':30s}  WHERE")
    now = datetime.now(timezone.utc)
    for s in sorted(snaps, key=lambda x: x.name):
        try:
            age = now - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
            age_str = f"{age.days}d" if age.days else f"{int(age.total_seconds() // 3600)}h"
        except (ValueError, TypeError):
            age_str = "?"
        where = (s.where or "")[:40]
        typer.echo(
            f"{s.name:30s}  {s.rows:>10,}  {_format_size(s.bytes_local):>10s}  "
            f"{age_str:>10s}  {s.table_id:30s}  {where}"
        )


@snapshot_app.command("drop")
def drop_cmd(name: str):
    """Delete a snapshot."""
    snap_dir = _snap_dir()
    if read_meta(snap_dir, name) is None:
        typer.echo(f"Error: snapshot {name!r} not found", err=True)
        raise typer.Exit(2)

    with snapshot_lock(snap_dir):
        delete_snapshot(snap_dir, name)
        # Also drop the view from user analytics DB
        local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
        if local_db.exists():
            conn = duckdb.connect(str(local_db))
            try:
                conn.execute(f'DROP VIEW IF EXISTS "{name}"')
            finally:
                conn.close()
    typer.echo(f"Dropped {name}")


@snapshot_app.command("refresh")
def refresh_cmd(
    name: str,
    where: str = typer.Option(None, "--where", help="Override stored WHERE"),
):
    """Re-fetch a snapshot using its stored fetch parameters (spec §4.2)."""
    snap_dir = _snap_dir()
    meta = read_meta(snap_dir, name)
    if meta is None:
        typer.echo(f"Error: snapshot {name!r} not found", err=True)
        raise typer.Exit(2)

    req = {
        "table_id": meta.table_id,
        "select": meta.select,
        "where": where if where else meta.where,
        "limit": meta.limit,
        "order_by": meta.order_by,
    }
    try:
        table = api_post_arrow("/api/v2/scan", req)
    except V2ClientError as e:
        typer.echo(f"Error: refresh failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    parquet_path = snap_dir / f"{name}.parquet"
    with snapshot_lock(snap_dir):
        pq.write_table(table, parquet_path)
        new_hash = hashlib.md5(parquet_path.read_bytes()[:1_000_000]).hexdigest()
        identical = new_hash == meta.result_hash_md5
        old_rows = meta.rows
        old_bytes = meta.bytes_local
        new_rows = int(table.num_rows)
        new_bytes = parquet_path.stat().st_size
        now = datetime.now(timezone.utc).isoformat()
        new_meta = SnapshotMeta(
            name=name, table_id=meta.table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=new_rows, bytes_local=new_bytes,
            estimated_scan_bytes_at_fetch=meta.estimated_scan_bytes_at_fetch,
            result_hash_md5=new_hash,
        )
        write_meta(snap_dir, new_meta)

    typer.echo(f"Refreshed {name}")
    typer.echo(f"  rows:           {old_rows:>10,}  ->  {new_rows:>10,}  ({new_rows - old_rows:+,})")
    typer.echo(f"  bytes_local:    {_format_size(old_bytes)}  ->  {_format_size(new_bytes)}")
    typer.echo(f"  effective_as_of:{meta.effective_as_of}  ->  {now}")
    typer.echo(f"  identical:      {'yes' if identical else 'no'}")


@snapshot_app.command("prune")
def prune_cmd(
    older_than: str = typer.Option(None, "--older-than", help="e.g. 7d, 24h"),
    larger_than: str = typer.Option(None, "--larger-than", help="e.g. 1g, 500m"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Drop snapshots matching predicates."""
    snap_dir = _snap_dir()
    snaps = list_snapshots(snap_dir)

    matches = []
    for s in snaps:
        keep = True
        if older_than:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
                threshold = _parse_duration(older_than)
                if age < threshold:
                    keep = False
            except ValueError:
                pass
        if larger_than:
            threshold = _parse_size(larger_than)
            if s.bytes_local < threshold:
                keep = False
        if not keep and (older_than or larger_than):
            continue
        if older_than or larger_than:
            matches.append(s)
    
    # When BOTH conditions provided, intersection. We've used `keep` to mean "both pass".
    # Simplified: re-compute with explicit AND
    matches = []
    for s in snaps:
        ok = True
        if older_than:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
                if age < _parse_duration(older_than):
                    ok = False
            except (ValueError, TypeError):
                ok = False
        if larger_than and s.bytes_local < _parse_size(larger_than):
            ok = False
        if ok:
            matches.append(s)

    for s in matches:
        if dry_run:
            typer.echo(f"would drop: {s.name}  ({_format_size(s.bytes_local)}, {s.fetched_at})")
        else:
            with snapshot_lock(snap_dir):
                delete_snapshot(snap_dir, s.name)
            typer.echo(f"dropped: {s.name}")
    if not matches:
        typer.echo("(no matches)")


def _parse_duration(s: str) -> timedelta:
    s = s.strip().lower()
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    raise ValueError(f"unknown duration: {s!r}")


def _parse_size(s: str) -> int:
    s = s.strip().lower()
    multipliers = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    if s[-1] in multipliers:
        return int(float(s[:-1]) * multipliers[s[-1]])
    return int(s)
```

- [ ] **Step 17.4: Register in `cli/main.py`**

```python
from cli.commands.snapshot import snapshot_app
app.add_typer(snapshot_app, name="snapshot")
```

- [ ] **Step 17.5: Run tests to verify pass**

Run: `pytest tests/test_cli_snapshot.py -v`
Expected: 3 passed.

- [ ] **Step 17.6: Commit**

```bash
git add cli/commands/snapshot.py cli/main.py tests/test_cli_snapshot.py
git commit -m "feat(cli): da snapshot list/refresh/drop/prune"
```

---

## Task 18: `da disk-info`

Spec §4.3. Trivial.

**Files:**
- Create: `cli/commands/disk_info.py`
- Modify: `cli/main.py`
- Test: `tests/test_cli_disk_info.py`

- [ ] **Step 18.1: Write failing test**

```python
# tests/test_cli_disk_info.py
import os
from typer.testing import CliRunner
import pytest


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    snap = tmp_path / "user" / "snapshots"
    snap.mkdir(parents=True)
    yield tmp_path


def test_disk_info_runs_and_reports(cli_env):
    (cli_env / "user" / "snapshots" / "x.parquet").write_bytes(b"A" * 1024)
    from cli.main import app as cli_app
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "Snapshots dir" in result.stdout
```

- [ ] **Step 18.2: Run test to verify failure**

Run: `pytest tests/test_cli_disk_info.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 18.3: Implement `cli/commands/disk_info.py`**

```python
"""`da disk-info` — show snapshot dir disk usage (spec §4.3)."""

import os
import shutil
from pathlib import Path
import typer

disk_info_app = typer.Typer(help="Show snapshot disk usage")


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n} TB"


@disk_info_app.callback(invoke_without_command=True)
def disk_info(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json"),
):
    """Show snapshots disk usage."""
    if ctx.invoked_subcommand is not None:
        return
    snap_dir = _local_dir() / "user" / "snapshots"
    used = sum(p.stat().st_size for p in snap_dir.rglob("*") if p.is_file()) if snap_dir.exists() else 0
    count = len(list(snap_dir.glob("*.parquet"))) if snap_dir.exists() else 0
    free = shutil.disk_usage(snap_dir).free if snap_dir.exists() else 0
    quota_gb = int(os.environ.get("AGNES_SNAPSHOT_QUOTA_GB", "10"))

    if json:
        import json as json_lib
        typer.echo(json_lib.dumps({
            "snapshots_dir": str(snap_dir),
            "used_bytes": used, "snapshot_count": count,
            "free_bytes": free, "quota_gb": quota_gb,
        }))
        return

    typer.echo(f"Snapshots dir:    {snap_dir}")
    typer.echo(f"Used by Agnes:    {_format_size(used)} across {count} snapshots")
    typer.echo(f"Free disk:        {_format_size(free)}")
    typer.echo(f"Configured cap:   {quota_gb} GB (set AGNES_SNAPSHOT_QUOTA_GB to override)")
```

- [ ] **Step 18.4: Register in `cli/main.py`**

```python
from cli.commands.disk_info import disk_info_app
app.add_typer(disk_info_app, name="disk-info")
```

- [ ] **Step 18.5: Run test to verify pass**

Run: `pytest tests/test_cli_disk_info.py -v`
Expected: 1 passed.

- [ ] **Step 18.6: Commit**

```bash
git add cli/commands/disk_info.py cli/main.py tests/test_cli_disk_info.py
git commit -m "feat(cli): da disk-info — snapshot dir usage report"
```

---

## Task 19: CLAUDE.md agent rails addendum

Spec §5.1.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 19.1: Add the section**

Open `CLAUDE.md`. Find a sensible location (after "Business Metrics" or before "Hybrid Queries"). Add the full section from spec §5.1. The literal markdown is large but verbatim from the spec — copy from `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md` lines 437-528 (the "## Querying Agnes data — agent rails" block), and match the existing CLAUDE.md heading style.

- [ ] **Step 19.2: Verify tabs/style**

Run: `grep -n '^## ' CLAUDE.md` — confirm new section is at H2 level and ordered sensibly with neighboring sections.

- [ ] **Step 19.3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): agent rails for v2 primitives (catalog -> schema -> fetch)"
```

---

## Task 20: Standalone skill file `agnes-data-querying`

Spec §5.2.

**Files:**
- Create: `cli/skills/agnes-data-querying.md`

- [ ] **Step 20.1: Write the skill**

Mirror the CLAUDE.md addendum but framed as a skill. ~200 lines max. Structure:

```markdown
---
name: agnes-data-querying
description: Query Agnes data correctly — discovery first (`da catalog` → `da schema` → `da describe`), then `da fetch` for remote tables, then `da query` locally. Use BigQuery SQL flavor for `--where` on remote tables.
---

# Agnes Data Querying

[full content per spec §5 + a quick-reference BQ flavor card]
```

- [ ] **Step 20.2: Verify path matches existing skill loader convention**

Other skills in this repo live at `cli/skills/<name>.md` — confirm and adjust the spec path if convention is different (e.g. `cli/skills/<name>/SKILL.md`).

- [ ] **Step 20.3: Commit**

```bash
git add cli/skills/agnes-data-querying.md
git commit -m "docs(skills): agnes-data-querying — agent rails for v2 fetch primitives"
```

---

## Task 21: CHANGELOG `**BREAKING**` entry + `instance.yaml.example` knobs

Spec §6, §10.4. CLAUDE.md changelog discipline.

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `config/instance.yaml.example`

- [ ] **Step 21.1: Add CHANGELOG entries**

Under `## [Unreleased]`, add:

```markdown
### Added
- `/api/v2/{catalog,schema,sample,scan,scan/estimate}` — discovery + scoped fetch primitives. See `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md`.
- `da catalog`, `da schema`, `da describe`, `da fetch`, `da snapshot {list,refresh,drop,prune}`, `da disk-info` — CLI primitives backed by the v2 API.
- `cli/skills/agnes-data-querying.md` — Claude rails skill loaded for Agnes-flavored projects.
- `instance.yaml: api.scan.*` knobs (`max_limit`, `max_result_bytes`, `max_concurrent_per_user`, `max_daily_bytes_per_user`, `bq_cost_per_tb_usd`, `request_timeout_seconds`).

### Changed
- **BREAKING:** BigQuery views (`query_mode='remote'` with `_meta.query_mode='remote'`) are no longer wrapped as DuckDB master views in `analytics.duckdb`. `da query --remote "SELECT * FROM <bq_view>"` no longer resolves the view name; analysts must use `da fetch` to materialize a snapshot or `da query --remote "SELECT * FROM bigquery_query('proj', '<inner BQ SQL>')"` directly. To restore the previous behavior for one release cycle, set `instance.yaml: data_source.bigquery.legacy_wrap_views: true`.
```

- [ ] **Step 21.2: Add config knob defaults to `instance.yaml.example`**

Append a section showing the new knobs with sensible defaults + comments.

- [ ] **Step 21.3: Commit**

```bash
git add CHANGELOG.md config/instance.yaml.example
git commit -m "docs: CHANGELOG + instance.yaml.example for v2 fetch primitives (BREAKING)"
```

---

## Task 22: E2E verification on dev VM

Spec §11 manual gates. Pure verification — no code changes.

- [ ] **Step 22.1: Push branch + wait for image rebuild**

```bash
git push origin zs/test-bq-e2e
gh run watch --branch zs/test-bq-e2e
```

- [ ] **Step 22.2: Auto-upgrade VM + verify health**

```bash
ssh foundryai-dev-zsrotyr   # or via gcloud compute ssh
sudo /usr/local/bin/agnes-auto-upgrade.sh
sudo docker exec agnes-app-1 curl -sS http://localhost:8000/api/health \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["commit_sha"], d["schema_version"])'
```

Expected: commit sha matches the latest local commit; schema_version is current.

- [ ] **Step 22.3: Verify v2 endpoints respond**

```bash
PAT=$(... your pat ...)
curl -sS -H "Authorization: Bearer $PAT" https://<vm-host>/api/v2/catalog | jq '.tables[].id'
curl -sS -H "Authorization: Bearer $PAT" https://<vm-host>/api/v2/schema/<some-bq-table> | jq
curl -sS -H "Authorization: Bearer $PAT" -X POST -H "Content-Type: application/json" \
    -d '{"table_id": "<some-bq-table>", "select": ["..."], "where": "...", "limit": 100}' \
    https://<vm-host>/api/v2/scan/estimate | jq
```

Expected: each returns 200 with reasonable JSON.

- [ ] **Step 22.4: Run `da fetch` end-to-end**

From a laptop with `da` CLI installed (and pointed at the dev VM):

```bash
da catalog --json | jq '.tables[].id'
da schema <bq-table>
da fetch <bq-table> \
    --select <cols> \
    --where "<bq predicate>" \
    --limit 1000 \
    --as test_snap \
    --estimate
da fetch <bq-table> --select <cols> --where "<pred>" --limit 1000 --as test_snap --no-estimate
da query "SELECT COUNT(*) FROM test_snap"
da snapshot list
da snapshot drop test_snap
```

All commands should succeed. `da query` on the snapshot returns the expected row count.

- [ ] **Step 22.5: Verify Claude rails (manual gate)**

Open Claude Code in a fresh session against the Agnes repo. Ask: "Show me the count of rows in `<bq-table>` for the last 7 days."

Expected agent flow (visible in the conversation):
1. Run `da catalog`
2. Run `da schema <bq-table>`
3. Run `da fetch <bq-table> --where "..." --limit ... --as ...`
4. Run `da query "SELECT COUNT(*) FROM ..."`
5. Report the count

Repeat 2 more times in fresh sessions. Document the transcripts in the PR description.

- [ ] **Step 22.6: Open PR**

```bash
gh pr create --title "v2 fetch primitives + Claude agent rails (#101)" --body "$(cat <<EOF
## Summary
Replaces the BQ-view-wrapping approach with primitives the Claude agent composes:
- `/api/v2/{catalog,schema,sample,scan,scan/estimate}` server endpoints
- `da catalog/schema/describe/fetch/snapshot/disk-info` CLI commands
- CLAUDE.md addendum + standalone skill for agent rails
- BREAKING: BQ view wrap dropped; `legacy_wrap_views` toggle for one release cycle

## Test plan
- [x] All unit tests green
- [x] CI on branch passes
- [x] E2E on dev VM: discover -> estimate -> fetch -> query loop in <2 min
- [x] Three unguided Claude sessions follow the protocol

## Closes
- #101 (BQ view-wrapping outer-query pushdown)

## Spec & plan
- docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md
- docs/superpowers/plans/2026-04-27-claude-fetch-primitives.md
EOF
)"
```

- [ ] **Step 22.7: Mark E2E gate complete in PR description**

Update the PR body with:
- Commit SHAs for each phase
- Demo recording link or summary
- Three Claude transcripts confirming agent rails are followed

---

## Self-Review

**Spec coverage:**
- §1 motivation, §2 architecture: covered by tasks 7-11 + 12 (drop wrap)
- §3.0 identifier conventions: enforced via existing `validate_identifier` + Task 7+8+9 RBAC use of registry id
- §3.1 catalog: Task 7
- §3.2 schema: Task 8
- §3.3 sample: Task 9
- §3.4 scan: Task 11
- §3.5 scan/estimate: Task 10
- §3.6 caching: Task 5 + reuse in Tasks 7/8/9
- §3.7 validator: Tasks 1-3
- §3.8 quotas: Task 4 (used in Task 11)
- §4.1 catalog/schema/describe CLI: Task 15
- §4.2 fetch + snapshot mgmt: Tasks 16, 17
- §4.3 disk-info: Task 18
- §5 CLAUDE.md + skill: Tasks 19, 20
- §6 migration: Task 12
- §10 implementation contracts: distributed across tasks (audit shape in Task 11, exit codes in Tasks 16/17, error UX in `_exit_code_for` helpers, config knobs in Task 21)
- §11 success criteria: Task 22

**Placeholder scan:** No "TBD"/"TODO" found.

**Type consistency:**
- `validate_where(predicate, table_id, schema)` — same signature in tasks 1-3 and Task 11.
- `QuotaTracker.acquire(user)` / `record_bytes(user, n)` — same in Task 4 + Task 11.
- `SnapshotMeta` dataclass — same fields in Task 14 + Tasks 16-17.
- `ScanRequest` pydantic model — same in Tasks 10 + 11.
- `_resolve_schema(conn, user, table_id, project_id)` — defined in Task 10, reused in Task 11.

No drift detected.

---

**Total tasks: 22**
**Estimated effort: ~16-18 person-days (1 dev) / 8-9 days (2 devs in parallel)**
