# Activity Center Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four observability surfaces (Activity Center, sessions browser, telemetry, adoption) agree with each other and with themselves, per `docs/superpowers/specs/2026-07-28-activity-center-consistency-design.md`.

**Architecture:** Three sequential PRs. PR-A fixes identity/classification at write time + backfill migration (v101). PR-B makes KPIs/facets accept the timeline's filters via a shared WHERE-builder, adds `result_class`/`source`/`include_self_reads`, auto-duration via contextvar, and the UI/glossary changes. PR-C adds `uploaded_at` (v102), the `anchor` param, and ingest reconciliation in the health pulse. Ends with prod verification.

**Tech Stack:** FastAPI, DuckDB + Postgres dual-backend repos (`src/repositories/audit.py` + `audit_pg.py`, `usage.py` + `usage_pg.py`), Alembic + `src/db.py` migration ladders, Jinja2 page templates, Typer CLI.

## Global Constraints

- Every repo method change lands in BOTH backends in the same PR + cross-engine contract test (`tests/db_pg/test_audit_contract.py`, `test_usage_contract.py`).
- Migration ladders stay in sync: `src/db.py` `_vN_to_v(N+1)` ↔ `migrations/versions/00NN_*.py`; `tests/test_db_schema_version.py` gates. Current: SCHEMA_VERSION=100, alembic head `0047_sync_state_parts_v100`.
- CHANGELOG bullet under `## [Unreleased]` in the same PR; release-cut (version bump from 0.76.38) as the LAST commit of each PR.
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- Vendor-agnostic: no hostnames/customer names in code, comments, PR bodies.
- Branch pushes as lowercase: `git push -u origin HEAD:refs/heads/zs/<slug>` (case-collision gotcha).
- Review loop before each merge: /agnes-review → fix → repeat until clean; resolve any Devin threads.

---

## PR-A — identity & classification at write (target v0.76.39)

### Task A1: Shared classification constants

**Files:**
- Modify: `src/audit_helpers.py`
- Test: `tests/test_audit_helpers.py` (create)

**Interfaces:**
- Produces: `SCHEDULER_ACTION_SQL: str`, `AUDIT_SOURCE_CASE_SQL: str`, `RESULT_CLASS_CASE_SQL: str`, `classify_result(value: str|None) -> str` — consumed by A4, B1, B2.

- [ ] **Step 1: failing test**

```python
# tests/test_audit_helpers.py
from src.audit_helpers import classify_result

def test_classify_result_classes():
    assert classify_result(None) == "none"
    assert classify_result("success") == "success"
    assert classify_result("ok") == "success"
    assert classify_result("error") == "error"
    assert classify_result("error.404") == "error"
    assert classify_result("denied") == "denied"
    assert classify_result("blocked") == "denied"
    assert classify_result("invalid_password") == "denied"
    assert classify_result("deactivated") == "denied"
    assert classify_result("skipped") == "other"
```

- [ ] **Step 2: run** `pytest tests/test_audit_helpers.py -q` → FAIL (ImportError)
- [ ] **Step 3: implement** — append to `src/audit_helpers.py`:

```python
# One scheduler rule for the whole codebase — the same predicate
# last_scheduler_tick() already uses. Facets/kpis/timeline must not
# maintain a second (stale) list of action names.
SCHEDULER_ACTION_SQL = "(action LIKE 'run_%' OR action = 'marketplace.sync_all')"

# Row → source bucket. Identical semantics in DuckDB and Postgres.
AUDIT_SOURCE_CASE_SQL = (
    "CASE "
    "WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind "
    f"WHEN {SCHEDULER_ACTION_SQL} THEN 'scheduler' "
    "WHEN user_id IS NULL THEN 'system' "
    "ELSE 'other' END"
)

# Row → result class. Read-side only; raw result values are preserved.
RESULT_CLASS_CASE_SQL = (
    "CASE "
    "WHEN result IS NULL THEN 'none' "
    "WHEN result IN ('success', 'ok') THEN 'success' "
    "WHEN result LIKE 'error%' THEN 'error' "
    "WHEN result IN ('denied', 'blocked', 'invalid_password', 'deactivated') THEN 'denied' "
    "ELSE 'other' END"
)

RESULT_CLASSES = ("success", "error", "denied", "none", "other")


def classify_result(value: "str | None") -> str:
    """Python mirror of RESULT_CLASS_CASE_SQL (kept in lockstep by tests)."""
    if value is None:
        return "none"
    if value in ("success", "ok"):
        return "success"
    if value.startswith("error"):
        return "error"
    if value in ("denied", "blocked", "invalid_password", "deactivated"):
        return "denied"
    return "other"
```

- [ ] **Step 4: run** → PASS
- [ ] **Step 5: commit** `git add -A src/audit_helpers.py tests/test_audit_helpers.py && git commit -m "feat(audit): shared scheduler/source/result-class rules"`

### Task A2: Writers pass users.id, never email

**Files:**
- Modify: `app/chat/audit.py` (write_audit), `app/api/memory.py` (`_audit_action` + 3 inline sites at ~782/810/838), `app/api/authoring_suggestions.py` (3 sites), `services/slack_bot/binding.py` (~315)
- Test: `tests/test_audit_identity.py` (create)

**Interfaces:**
- Consumes: `users_repo().get_by_email(email)` (exists).
- Produces: `write_audit(*, user_email, action, details, user_id=None)` — resolves email→id with a 5-min TTL cache; falls back to the email when unresolvable.

- [ ] **Step 1: failing test** — `write_audit` resolves email→id; memory/authoring endpoints write UUID (seed user, call endpoint via TestClient, assert `audit_log.user_id == users.id`).

```python
# tests/test_audit_identity.py — core unit test
def test_write_audit_resolves_email_to_user_id(tmp_path, monkeypatch):
    from app.chat import audit as chat_audit
    logged = {}
    class FakeRepo:
        def log(self, **kw): logged.update(kw)
    class FakeUsers:
        def get_by_email(self, email): return {"id": "uuid-1", "email": email}
    monkeypatch.setattr("src.repositories.audit_repo", lambda: FakeRepo())
    monkeypatch.setattr("src.repositories.users_repo", lambda: FakeUsers())
    chat_audit._EMAIL_ID_CACHE.clear()
    chat_audit.write_audit(user_email="a@b.c", action="chat.x", details={})
    assert logged["user_id"] == "uuid-1"

def test_write_audit_falls_back_to_email_when_unresolvable(monkeypatch):
    from app.chat import audit as chat_audit
    logged = {}
    class FakeRepo:
        def log(self, **kw): logged.update(kw)
    class FakeUsers:
        def get_by_email(self, email): return None
    monkeypatch.setattr("src.repositories.audit_repo", lambda: FakeRepo())
    monkeypatch.setattr("src.repositories.users_repo", lambda: FakeUsers())
    chat_audit._EMAIL_ID_CACHE.clear()
    chat_audit.write_audit(user_email="ghost@b.c", action="chat.x", details={})
    assert logged["user_id"] == "ghost@b.c"
```

- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement**
  - `app/chat/audit.py`: add `_EMAIL_ID_CACHE: dict[str, tuple[str, float]] = {}` (5-min TTL, monotonic), `user_id: str | None = None` kwarg; when None → cached `users_repo().get_by_email(user_email)`, fallback email.
  - `app/api/memory.py`: `_audit_action(conn, admin_email, ...)` → change param to the admin dict (`admin: dict`), write `user_id=admin.get("id") or admin.get("email")`; update all call sites (grep `_audit_action(`). Inline sites: `user_id=user["email"]` → `user_id=user["id"]`.
  - `app/api/authoring_suggestions.py`: `user["email"]`/`admin["email"]` → `["id"]` at the 3 audit sites (leave `created_by` display fields alone).
  - `services/slack_bot/binding.py`: reuse the `_u` row already fetched: `user_id=_u["id"] if _u else user_email`.
- [ ] **Step 4: run** unit + `pytest tests/test_memory_governance*.py tests/test_authoring*.py -q` (adjust names to what exists) → PASS
- [ ] **Step 5: commit** `fix(audit): writers record users.id, not email`

### Task A3: run_* writers stamp client_kind

**Files:**
- Modify: `app/api/admin.py` — 12 sites: lines ~4166, 4291, 4345, 4393, 4439, 4512, 4562, 4589, 4627, 4661, 5257, 5290
- Test: extend `tests/test_audit_identity.py`

- [ ] **Step 1: failing test** — call one run_* endpoint (e.g. `POST /api/admin/run-session-processor/usage`, path per router) as admin via TestClient; assert the audit row has `client_kind == "web"` (JWT admin) — previously NULL.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — each `audit_repo().log(...)` at those sites gains `client_kind=client_kind_from_user(user)` (import from `src.audit_helpers`; the `user` dict is in scope at every site — verify each).
- [ ] **Step 4: run** → PASS
- [ ] **Step 5: commit** `fix(audit): scheduler-tick writers stamp client_kind`

### Task A4: One scheduler rule + server-computed source

**Files:**
- Modify: `src/repositories/audit.py` (`facets`, `query`), `src/repositories/audit_pg.py` (same), `app/api/observability.py` (drop `_SCHEDULER_ACTION_FALLBACK` + `scheduler_actions` plumbing), `app/api/activity.py` (no change needed if repo adds column), `app/web/templates/activity_center.html` (drop `classifySource`, use `r.source`)
- Test: `tests/db_pg/test_audit_contract.py` (extend), `tests/test_activity_api.py` (extend)

**Interfaces:**
- Consumes: `AUDIT_SOURCE_CASE_SQL` (A1).
- Produces: `query()` rows carry a computed `source` key; `facets()` loses the `scheduler_actions` parameter (rule is internal).

- [ ] **Step 1: failing contract test** — seed rows: one with `client_kind='cli'`, one `action='run_session_processor:usage'` + no client_kind, one `user_id=NULL`, one plain; assert `facets()["sources"]` buckets = cli/scheduler/system/other on BOTH backends, and `query()` rows include `source`.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — both backends: `SELECT *, {AUDIT_SOURCE_CASE_SQL} AS source FROM audit_log ...` in `query()`; facets source bucket uses the same CASE (PG: drop the expanding `sched` bindparam). `app/api/observability.py` deletes the fallback list. Template: `r._source` → `r.source` everywhere (renderRow, openPanel, sort key), delete `classifySource`.
- [ ] **Step 4: run** contract + activity tests → PASS
- [ ] **Step 5: commit** `fix(observability): one scheduler rule, server-computed source`

### Task A5: v101 backfill migration (both ladders)

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 100→101, `_v100_to_v101`, ladder registration — follow the existing `_vNN_to_vNN+1` pattern)
- Create: `migrations/versions/0048_audit_identity_backfill_v101.py` (down_revision `0047_sync_state_parts_v100`)
- Test: `tests/test_db_schema_version.py` (auto-gates), plus a backfill unit test in `tests/test_audit_identity.py`

- [ ] **Step 1: failing test** — DuckDB: create schema, insert user (id=`u-1`, email=`a@b.c`) + audit rows with `user_id='a@b.c'` and `user_id='ghost@x.y'`; run `_v100_to_v101`; assert first row now `u-1`, ghost unchanged.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — portable SQL (identical in both ladders):

```sql
UPDATE audit_log SET user_id = (
    SELECT min(u.id) FROM users u WHERE lower(u.email) = lower(audit_log.user_id)
)
WHERE user_id LIKE '%@%'
  AND (SELECT COUNT(*) FROM users u WHERE lower(u.email) = lower(audit_log.user_id)) = 1
```

- [ ] **Step 4: run** ladder tests → PASS
- [ ] **Step 5: commit** `feat(db): v101 — audit_log identity backfill (email → users.id)`

### Task A6: PR-A close-out

- [ ] CHANGELOG bullet (Fixed: audit rows now record `users.id`; scheduler ticks classified correctly; timeline rows carry server-computed `source`).
- [ ] Full suite `.venv/bin/pytest tests/ --tb=short -n auto -q` → green (stash-check unrelated failures).
- [ ] Release-cut: `pyproject.toml` 0.76.38→0.76.39 + CHANGELOG rename + fresh `[Unreleased]`, last commit.
- [ ] Push `git push -u origin HEAD:refs/heads/zs/activity-center-data-inconsistency-ed589d`, open PR, /agnes-review loop until clean, watch CI, merge, tag `v0.76.39` + GitHub Release, watch post-merge `release.yml` (`smoke-test` green, rollback skipped).

---

## PR-B — query parity, result classes, duration, UI/glossary (target v0.76.40)

Branch: `zs/activity-center-filter-parity` off fresh main.

### Task B1: Shared WHERE-builder; facets/kpis accept timeline filters

**Files:**
- Modify: `src/repositories/audit.py`, `src/repositories/audit_pg.py`
- Test: `tests/db_pg/test_audit_contract.py`

**Interfaces:**
- Produces (both backends, identical semantics):
  - `query(..., result_class: str|None = None, source: str|None = None, include_self_reads: bool = True)`
  - `facets(*, since, limit=50, **same filters) -> dict` (drops `scheduler_actions`)
  - `kpis(*, since, **same filters) -> dict` with keys `events_total, active_users, errors, p95, duration_coverage` — `active_users` counts distinct user_id where source ∉ ('scheduler','system'); `errors` counts result_class='error'; `duration_coverage` = measured/total (0.0 when total=0).

- [ ] **Step 1: failing contract test** — seed the A4 rows + one `result='ok'`, one `result='denied'`, one `action='activity.read'`; assert on BOTH backends:
  - `kpis(since=..., user_id='u1')['events_total']` == number of u1 rows (filters honored)
  - `kpis(...)['active_users']` excludes the scheduler-classified row's user
  - `query(result_class='success')` returns both `success` and `ok` rows
  - `query(include_self_reads=False)` drops `activity.read`
  - `facets(user_id='u1')['actions']` only u1's actions
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — extract `_filters_where(...)` (DuckDB: `?` list; PG: named dict) used by query/facets/kpis; add the three new filters (`result_class` via `RESULT_CLASS_CASE_SQL = ?`, `source` via `AUDIT_SOURCE_CASE_SQL = ?`, `include_self_reads=False` → `action != 'activity.read'`). kpis SQL:

```sql
SELECT COUNT(*) AS events_total,
       COUNT(DISTINCT user_id) FILTER (
         WHERE user_id IS NOT NULL
           AND {SOURCE_CASE} NOT IN ('scheduler', 'system')) AS active_users,
       COUNT(*) FILTER (WHERE {RESULT_CLASS_CASE} = 'error') AS errors,
       CAST(approx_quantile(duration_ms, 0.95) AS INTEGER) AS p95,   -- PG: percentile_cont
       COUNT(duration_ms) AS measured,
       COUNT(*) AS total
```

- [ ] **Step 4: run** → PASS
- [ ] **Step 5: commit** `feat(audit): facets/kpis honor the timeline filter set`

### Task B2: Result vocabulary — normalize writers + guard test

**Files:**
- Modify: 8 sites writing `result="ok"` → `result="success"`: `app/api/sync.py:1716`, `app/api/me.py:53`, `app/api/admin.py:4872`, `app/api/admin_chat.py:160`, `app/api/news.py:138,175,197`, `src/store_guardrails/runner.py:391`
- Test: `tests/test_audit_result_vocabulary.py` (create)

- [ ] **Step 1: guard test** — walk git-tracked `*.py` under `app/ src/ services/ cli/` (exclude tests), regex `result="([a-z_.0-9]+)"` on lines that sit inside an `audit_repo().log(`/`audit.log(`/`repo.log(` call block (pragmatic: line contains `result="`); assert `classify_result(literal) != "other"` for every hit, allowlist `{"skipped"}`. Also assert literal `"ok"` no longer appears.
- [ ] **Step 2: run** → FAIL (8 `ok` hits)
- [ ] **Step 3: implement** — sed the 8 sites; rerun.
- [ ] **Step 4: run** full `pytest tests/test_audit_result_vocabulary.py tests/test_activity_api.py -q` → PASS
- [ ] **Step 5: commit** `fix(audit): normalize result vocabulary (ok → success) + guard`

### Task B3: Auto-duration contextvar

**Files:**
- Create: `src/audit_context.py`, `app/middleware/audit_timing.py`
- Modify: `app/main.py` (register middleware next to `RequestIdMiddleware`), `src/repositories/audit.py::log`, `src/repositories/audit_pg.py::log`
- Test: `tests/test_audit_identity.py` (extend)

**Interfaces:**
- Produces: `mark_request_start()`, `auto_duration_ms() -> int|None`; `log(duration_ms=None)` auto-fills from the contextvar.

- [ ] **Step 1: failing test** — `mark_request_start(); repo.log(action='x'); row.duration_ms is not None and >= 0`; and without mark → None. Plus endpoint test: any audited API call yields a row with duration.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement**

```python
# src/audit_context.py
from contextvars import ContextVar
import time

_request_started: ContextVar["float | None"] = ContextVar("audit_request_started", default=None)

def mark_request_start() -> None:
    _request_started.set(time.monotonic())

def auto_duration_ms() -> "int | None":
    t0 = _request_started.get()
    if t0 is None:
        return None
    return int((time.monotonic() - t0) * 1000)
```

```python
# app/middleware/audit_timing.py — pure ASGI (no BaseHTTPMiddleware; this app
# streams SSE through middleware, keep the hot path untouched)
from src.audit_context import mark_request_start

class AuditTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            mark_request_start()
        await self.app(scope, receive, send)
```

Both repo `log()` methods: `if duration_ms is None: duration_ms = auto_duration_ms()`.

- [ ] **Step 4: run** → PASS
- [ ] **Step 5: commit** `feat(audit): auto duration_ms from request-start contextvar`

### Task B4: Observability endpoints wire the filters

**Files:**
- Modify: `app/api/observability.py` (facets, kpis), `app/api/activity.py` (timeline: `result_class`, `source`, `include_self_reads=False` default)
- Test: `tests/test_activity_api.py` — the KPI==table regression test

- [ ] **Step 1: failing test** — seed mixed rows; for each filter combo (`user_id`, `result_class=error`, `source=cli`) assert `kpis(...)['events_total'] == len(timeline rows)` fetched with the same filters (limit high). Assert both endpoints exclude `activity.read` by default and include it with `include_self_reads=1`.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — kpis/facets gain `user_id, action_prefix, resource_prefix, result_pattern, result_class, q, source, include_self_reads: bool=False` Query params, passed through; timeline gains the three new ones (same defaults). `kpis` response adds `duration_coverage: round(measured/total, 4) if total else 0.0`.
- [ ] **Step 4: run** → PASS
- [ ] **Step 5: commit** `feat(observability): KPI/facets/timeline share one filter surface`

### Task B5: Activity Center UI + glossary labels

**Files:**
- Modify: `app/web/templates/activity_center.html`; sub-labels in `app/web/templates/admin_usage.html` + `admin_sessions.html` (error-rate denominators)
- Test: `tests/test_design_system_contract.py` must stay green; template smoke via existing page tests

- [ ] **Step 1: implement UI state** — `state.result` values become `class:success|class:error|class:denied|class:none` sent as `result_class` (legacy `result`/`result_pattern` URL params still honored on read); `state.source` sent server-side; new `state.self_reads` checkbox ("include Activity Center reads") wired into buildTimelineUrl + loadKpis + loadFacets; Reset clears `resource_prefix` + `source` too; KPI card sub-labels: Events → "incl. system", Active users → "people", p95 sub shows `duration_coverage` as "N% measured"; Result dropdown options rebuilt from facet classes with counts.
- [ ] **Step 2: run** page tests + design contract → PASS
- [ ] **Step 3: commit** `feat(web): Activity Center filters drive KPIs; honest labels`

### Task B6: CLI parity + close-out

**Files:**
- Modify: `cli/commands/admin_activity.py` (`--source`, `--result-class`, `--include-self-reads`)
- Test: `tests/test_cli_admin_activity.py` (extend)

- [ ] Steps: failing CLI test (flags map to query params) → implement → pass → commit.
- [ ] **Glossary pin test** (spec Phase D): `tests/test_activity_api.py::test_sessions_kpis_match_adoption_kpis` — seed summary rows via `usage_repo().upsert_summary`, GET `/api/admin/sessions/kpis?since_minutes=10080` and `/api/admin/adoption/kpis?window=7d`, assert `sessions_total == sessions` and `distinct_users == active_users`. Failing first, then green (no code change expected — it pins existing behavior).
- [ ] CHANGELOG bullets (Changed: AC KPIs/facets honor filters + default-exclude self reads — **note behavior change**; Added: result classes, server-side source, auto duration; Fixed: reset button).
- [ ] Full suite; release-cut 0.76.40; push `zs/activity-center-filter-parity`; PR + review loop; merge; tag; watch release.yml.

---

## PR-C — uploaded_at anchor + reconciliation (target v0.76.41)

Branch: `zs/sessions-uploaded-anchor` off fresh main.

### Task C1: v102 — uploaded_at column + backfill

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 101→102, `_v101_to_v102`)
- Create: `migrations/versions/0049_sessions_uploaded_at_v102.py` (down_revision 0048)
- Test: ladder gate + unit test on backfill

- [ ] **Step 1: failing test** — DuckDB: schema + summary row (`session_file='u-1/abc.jsonl'`, started 2026-06-01) + audit row `action='session.upload'`, `params` containing `"filename": "abc.jsonl"`, ts 2026-07-13; run migration; assert `uploaded_at == 2026-07-13...`; second summary row without matching audit → `uploaded_at == started_at`.
- [ ] **Step 2: run** → FAIL
- [ ] **Step 3: implement** — portable SQL, both ladders:

```sql
ALTER TABLE usage_session_summary ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMP;
UPDATE usage_session_summary SET uploaded_at = (
    SELECT max(a.timestamp) FROM audit_log a
    WHERE a.action = 'session.upload'
      AND CAST(a.params AS VARCHAR) LIKE
          '%' || substr(usage_session_summary.session_file,
                        position('/' in usage_session_summary.session_file) + 1) || '%'
) WHERE uploaded_at IS NULL;
UPDATE usage_session_summary SET uploaded_at = COALESCE(uploaded_at, started_at, CURRENT_TIMESTAMP);
```

(Alembic: `ADD COLUMN IF NOT EXISTS` → plain `op.add_column` guarded by inspector check, per existing migration style.)
- [ ] **Step 4: run** ladder tests → PASS
- [ ] **Step 5: commit** `feat(db): v102 — usage_session_summary.uploaded_at + backfill`

### Task C2: Ingest stamps uploaded_at (first arrival wins)

**Files:**
- Modify: `services/session_processors/usage.py` (summary dict gains `uploaded_at=datetime.now(timezone.utc)`), `src/repositories/usage.py::upsert_summary`, `src/repositories/usage_pg.py::upsert_summary` — INSERT includes `uploaded_at`; `ON CONFLICT DO UPDATE` keeps the existing value (`uploaded_at = COALESCE(usage_session_summary.uploaded_at, excluded.uploaded_at)`)
- Test: `tests/db_pg/test_usage_contract.py` (extend)

- [ ] Steps: failing contract test (upsert twice with different uploaded_at → first wins; both backends) → implement → pass → commit `feat(sessions): stamp uploaded_at at first ingest`.

### Task C3: anchor param — repo, endpoints, CLI

**Files:**
- Modify: `src/repositories/usage.py::_sessions_where` + `_SESSION_SORT_KEYS` (+ PG sibling), `app/api/admin_sessions.py` (list/kpis/facets: `anchor: Literal["started","uploaded"] = "uploaded"`), `cli/commands/admin_sessions.py` (`--anchor`)
- Test: contract + endpoint tests

**Interfaces:**
- Produces: `filters["anchor"]` → window column `COALESCE(uploaded_at, started_at)` when `"uploaded"`, else `started_at`. Adoption endpoints are untouched (their SQL windows on `started_at` internally).

- [ ] **Step 1: failing test** — summary row started 60d ago, uploaded 1d ago: `list(since=7d, anchor='uploaded')` finds it; `anchor='started'` does not; kpis counts match list under both anchors.
- [ ] **Step 2–5:** implement → pass → commit `feat(sessions): window on arrival (anchor=uploaded default)`.

### Task C4: Health-pulse ingest reconciliation

**Files:**
- Modify: `app/api/activity.py::_compute_health` (new field `session_ingest`), `src/repositories/audit.py` + `audit_pg.py` (new `upload_filenames_since(since) -> list[str]` — parse `params` JSON in Python, portable), `src/repositories/usage.py` + `usage_pg.py` (new `session_file_basenames_since(since) -> set[str]`)
- Test: `tests/test_activity_api.py` (extend), contract tests for both new methods

- [ ] **Step 1: failing test** — seed 3 `session.upload` audit rows + 2 matching summary rows → health field `session_ingest` value `"3 up / 2 ingested"`, color yellow; equal counts → green. **Join on the audit `filename` vs the basename of `session_file` — never on session_id.**
- [ ] **Step 2–5:** implement → pass → commit `feat(activity): health pulse reconciles uploads vs ingested sessions`.

### Task C5: Browser shows the file id when it differs + close-out

**Files:**
- Modify: `app/web/templates/admin_sessions.html` — when `session_id != basename(session_file)` stem, render the stem as a `<span class="ds-muted">file: <stem-prefix>…</span>` tooltip/sub-label; column/sort for uploaded_at.
- [ ] Template change + page test green → commit.
- [ ] CHANGELOG (Added: uploaded_at anchor — **note default change** for the browser; health reconciliation. Fixed: late-uploaded sessions invisible in recent windows).
- [ ] Full suite; release-cut 0.76.41; push; PR + review loop; merge; tag; watch release.yml.

---

## Final: prod verification (the goal gate)

- [ ] Wait for the deployment channel to pick up the release (auto-upgrade cadence; verify server version via health endpoint).
- [ ] Re-run the probe suite (scratchpad scripts) against prod:
  1. `kpis?user_id=<real user>` ≡ timeline row count with same filter (several combos incl. `result_class`, `source`).
  2. Result classes partition: Σ class counts == events_total; `success` includes former `ok` rows.
  3. Facet sources: `other` bucket no longer contains `run_*` rows; scheduler bucket ≈ scheduler traffic.
  4. User dropdown: duplicate email/UUID pairs collapsed (post-backfill).
  5. `duration_coverage` > 0.5 within a fresh window; p95 plausible.
  6. Sessions browser (anchor=uploaded, 30d) count == distinct upload files in audit for the same window; `anchor=started` still shows the old behavior.
  7. Health pulse `session_ingest` green (0 gap).
  8. sessions kpis == adoption kpis (same window).
- [ ] Report the numbers side-by-side; the goal closes only when all eight checks pass.
