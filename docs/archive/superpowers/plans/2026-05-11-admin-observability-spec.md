# Admin Observability — parent spec

> **Status:** spec / discussion. Verified against `origin/main` at `65342cd1` (release 0.49.0).
> Schema v39. Worktree: `tmp_oss-activity-spec`.
>
> **Children (executable plans):**
> - `2026-05-11-activity-center-mvp.md` — Activity Center rebuild + audit gap closure (this PR)
> - (next) `2026-05-NN-admin-sessions.md` — `/admin/sessions` + `failure_scan` processor
> - (next) `2026-05-NN-feedback-inbox.md` — `agnes report` CLI + `/admin/feedback` + Claude skill

---

## 1. Why this exists

Agnes today has dozens of moving server-side processes — scheduler ticks, syncs, materialized BQ runs, marketplace clones, memory pipeline, RBAC mutations, PAT issuance, session uploads, queries. Some land in `audit_log`, some in `sync_history`, some only in container stdout, some nowhere.

An admin who asks **"is my Agnes instance healthy and what happened?"** today does one of three things:

1. SSHs into the VM and `docker logs` across containers.
2. Opens DuckDB directly with `duckdb /data/state/system.duckdb`.
3. Clicks through five separate admin pages (`/admin/scheduler-runs`, `/admin/tokens`, `/admin/access`, `/admin/marketplaces`, `/admin/users`) and stitches the picture together.

`/activity-center` was supposed to fix this. It doesn't — the template renders fake "Executive Pulse / Maturity Roadmap / Business Processes" sections fed by an empty handler context. Issue #206.

This spec rebuilds it as **`/admin/activity`** and adds two adjacent observability surfaces:

- **`/admin/sessions`** — admin browses Claude Code session transcripts across users, finds failure patterns ("where Claude got stuck so we can fix the CLI / setup prompt / skill"). New `failure_scan` processor in the existing `services/session_pipeline/` framework.
- **`/admin/feedback`** — inbox for explicit user-reported problems. New `agnes report` CLI command + Claude skill + new `feedback_reports` table.

The three surfaces together turn Agnes from a black box into a glass box for operators.

---

## 2. Audience model (no personas, just resources + RBAC)

Per v13 RBAC, the only hard distinction is `is_admin=true` (god-mode) vs. everyone else. We do not introduce new role bits. Instead we frame everything as **resources** that admins control via existing `resource_grants`. When the spec says "admin sees X" it means "the page is gated by `require_admin`; admin can later grant the underlying resource to other groups if the customer asks".

### Resources used / introduced

| Resource | Read-own surface | Manage-all surface | New / existing |
|---|---|---|---|
| Server operations (`audit_log` + `sync_history` + `session_processor_state`) | — | `/admin/activity` | rebuilt |
| Session transcripts (`${DATA_DIR}/user_sessions/<user>/*.jsonl`) | `/profile/sessions` | `/admin/sessions` | NEW page |
| Failure findings (new `session_findings` table) | — | tab in `/admin/sessions` | NEW table |
| User feedback (`feedback_reports` table — NEW) | (write-only via `agnes report`) | `/admin/feedback` | NEW |
| All others | various existing pages | various existing pages | unchanged |

---

## 3. Non-goals

- ❌ Replacing `/admin/diagnose`. Different question (current state vs. history).
- ❌ Strategic / exec value-reporting. The current template's "maturity roadmap" / "decisions supported" framing is deleted.
- ❌ Live streaming (SSE / WebSocket). Polling every 30s is enough.
- ❌ Cross-instance / fleet view.
- ❌ Mandatory LLM features. Activity Center works fully without PostHog or any external service.
- ❌ Analyst-side `/profile/activity`. Their existing `/profile/sessions` is already their personal audit trail in practice; adding a third profile page is not justified.

---

## 4. State on `origin/main` — verified facts the spec depends on

### 4.1 Schema (`src/db.py:43`)

```python
SCHEMA_VERSION = 39
```

Tables relevant to this work:

- `audit_log` (`id, timestamp, user_id, action, resource, params JSON, result, duration_ms`) — the primary event source. **30+ writer call sites today.**
- `sync_history` (`id, table_id, synced_at, rows, duration_ms, status, error`) — per-table sync events.
- `session_processor_state` (`processor_name, session_file, username, processed_at, items_extracted, file_hash`) — composite PK `(processor_name, session_file)`. **Per-processor checkpoint.**
- `verification_evidence`, `knowledge_items`, `knowledge_contradictions`, `knowledge_item_relations` — memory pipeline output (read-only for AC).
- `telegram_links` (`user_id PK, chat_id, linked_at`) — for admin notifications.
- `users`, `user_groups`, `user_group_members`, `resource_grants` — RBAC.
- `instance_templates` (singleton template store from earlier PRs; #246 proposes folding it into a unified content store, not yet built).

### 4.2 session_pipeline framework (`services/session_pipeline/contract.py`)

```python
@dataclass(frozen=True)
class ProcessorResult:
    items_count: int = 0

class SessionProcessor(Protocol):
    name: str
    cadence_minutes: int
    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
    ) -> ProcessorResult: ...
```

- Runner: `services/session_pipeline/runner.py`. Idempotent per `(processor_name, session_file, file_hash)`.
- Registry: `services/session_processors/__init__.py:PROCESSORS = {"verification": …, "usage": …}`.
- Scheduler invokes: `POST /api/admin/run-session-processor?processor=<name>` (env-overridable interval per processor, e.g. `SCHEDULER_USAGE_PROCESSOR_INTERVAL=600`).

**Implication:** failure-scan is a third processor following the same protocol. No new framework code.

### 4.3 Audit coverage gaps (verified)

These endpoints exist today and do **not** write `audit_log`:

| Endpoint | File | Reason needed in AC |
|---|---|---|
| `POST /api/sync/trigger` | `app/api/sync.py:772` | The dominant scheduler-fired action; today only the call to the scheduler endpoint is audited, not what actually ran. |
| `POST /api/scripts/run-due` | `app/api/scripts.py:138` | Custom user scripts running on-server with no trail. |
| `POST /api/query` + variants | `app/api/query.py:140+` | Analyst queries — invisible without #158. |
| `POST /api/query-hybrid` | `app/api/query_hybrid.py` | Same. |
| `POST /api/upload/sessions` | `app/api/upload.py:55` | Session push — invisible. |
| `GET /api/data/{table_id}/download` | `app/api/data.py:45` | Parquet pulls — invisible. |

The MVP closes the four non-query gaps. Query attribution (#158) is its own scope.

### 4.4 PostHog (`src/observability/posthog_client.py`)

Singleton `get_posthog()`, methods:
- `.capture(event: str, distinct_id: str, properties: dict | None) -> None`
- `.capture_exception(exc, distinct_id, request, properties) -> None`
- `.is_feature_enabled(key, distinct_id, default)` — usable for opt-in feature flags inside AC

Off by default (`POSTHOG_API_KEY` unset). All call sites must be no-op-safe.

### 4.5 Telegram (`services/telegram_bot/sender.py`)

```python
async def send_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> bool
```

Lookup `telegram_links` row by `user_id`. No existing admin notification flow — feedback inbox is its first user.

---

## 5. Architecture decisions

### 5.1 Where the three surfaces live

```
/admin/activity     ← rebuilt /activity-center (this PR)
/admin/sessions     ← NEW (follow-up plan)
/admin/feedback     ← NEW (follow-up plan)
```

All three:
- Gated by `Depends(require_admin)` — no new resource type for now.
- Listed in `_app_header.html` admin dropdown.
- Share a common drawer / detail-modal pattern (one Jinja partial reused).
- Share the same audit-recursive rule: reading from these endpoints itself writes one `audit_log` row.
- Each gets a top-of-page health micro-summary that links to the Activity Center health pulse.

### 5.2 Data — separate `change_log` vs. fattened `audit_log`

**Decision: fatten `audit_log`** with two new columns.

Rationale: Adding a separate `change_log` table requires every mutating endpoint to write to two places, doubling the failure modes. The audit_log row IS the change log entry, plus `params_before` for diff/rollback purposes. The vast majority of audit rows are non-mutations (reads, ticks, queries) where `params_before` is null — null storage cost in DuckDB is trivial.

Schema migration **v40**:

```sql
ALTER TABLE audit_log ADD COLUMN params_before JSON;       -- prior state, null for non-mutations
ALTER TABLE audit_log ADD COLUMN client_ip VARCHAR;        -- promoted from params for indexability
ALTER TABLE audit_log ADD COLUMN client_kind VARCHAR;      -- 'cli' | 'web' | 'agent' | 'scheduler' | 'external'
ALTER TABLE audit_log ADD COLUMN correlation_id VARCHAR;   -- groups multi-step operations
CREATE INDEX idx_audit_timestamp_desc ON audit_log(timestamp);
CREATE INDEX idx_audit_user_time ON audit_log(user_id, timestamp);
CREATE INDEX idx_audit_action_time ON audit_log(action, timestamp);
```

`AuditRepository.log()` gains the four new kwargs. Existing callers compile-time-unbroken (kwargs default to None).

**Operational note (reviewer pass):** DuckDB does **not** honor `DESC` in `CREATE INDEX` — the planner picks direction at query time. The `_desc` suffix in the index name is informative, not directive. Direction is enforced by `ORDER BY ... DESC` in `AuditRepository.query()`.

**Upgrade window (reviewer pass):** index creation on a populated `audit_log` (>100k rows) is single-threaded and may take 30–60s per index. Customers upgrading to v40 should expect a 30–120s startup window on first launch. CHANGELOG entry for v40 must call this out.

### 5.3 Filtering & pagination

`AuditRepository.query()` today supports `user_id`, `action`, `limit`. Rewrite to:

```python
def query(
    self,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: str | None = None,
    action_prefix: str | None = None,   # 'sync.', 'query.', 'auth.', …
    action_in: list[str] | None = None,
    resource: str | None = None,
    result_pattern: str | None = None,  # 'success', 'error.%'
    correlation_id: str | None = None,
    q: str | None = None,                # full-text over params JSON
    cursor: tuple[datetime, str] | None = None,  # (timestamp, id)
    limit: int = 100,
) -> tuple[list[dict], tuple[datetime, str] | None]:
    ...
```

Returns `(rows, next_cursor)`. Cursor encodes `(timestamp, id)` to make pagination stable under same-second writes. All filters AND together. `q` does `LIKE '%substring%'` on `params::TEXT` for v1; FTS upgrade is later.

### 5.4 Health pulse

Single endpoint `GET /api/admin/activity/health` returning a JSON dict cached server-side 30s:

```json
{
  "status": "green | yellow | red",
  "fields": [
    {"key": "scheduler", "value": "47s ago", "raw_seconds": 47, "color": "green", "click_filter": "action_prefix=run_"},
    {"key": "sync_24h", "value": "18 ok / 2 fail", "ok": 18, "fail": 2, "color": "yellow", "click_filter": "action_prefix=sync."},
    {"key": "active_users_today", "value": "12", "color": "green"},
    {"key": "memory_pipeline", "value": "ok (3 runs)", "color": "green", "click_filter": "action_prefix=run_session_processor"},
    {"key": "diagnose_warnings", "value": "0", "color": "green"}
  ],
  "sentence": "All systems nominal — 12 active users, last sync 4 min ago, no warnings."
}
```

Thresholds in code, not config. Acceptance: each field can be tested deterministically by seeding `audit_log` / `sync_history` and frozen-clock fixtures.

### 5.5 What gets MVP and what gets P2

| Activity Center tab | MVP (this PR) | Phase B | Phase C |
|---|---|---|---|
| Health pulse | ✓ | — | — |
| Timeline | ✓ | params_before diff | — |
| Sync (per-table grid) | ✓ | — | — |
| Changes (mutations) | — | ✓ (read-only diff) | rollback |
| Queries | — | — | ✓ (gated on #158) |
| Performance | — | — | ✓ |
| Usage (DAU/WAU) | — | ✓ | — |
| Costs | — | — | ✓ |

### 5.6 `/admin/sessions` — failure_scan processor

New file `services/session_processors/failure_scan.py`. Heuristics (deterministic, no LLM in v1):

| Signal | Detection |
|---|---|
| Tool error | turn with `tool_use` followed by tool result containing `is_error: true` / `exit code [1-9]` |
| Permission denied | tool result contains `permission denied` (case-insensitive) |
| User rejection | user turn matching regex `\b(no|stop|wrong|not what|incorrect|broken)\b` AND length < 60 chars |
| Loop pattern | 3+ consecutive assistant turns with same `tool_use.name` and similar input hash |
| Abrupt end | last turn `role=user` (never closed by assistant) |

Writes findings to NEW table `session_findings`:

```sql
CREATE TABLE session_findings (
    id VARCHAR PRIMARY KEY,
    session_file VARCHAR NOT NULL,
    username VARCHAR NOT NULL,
    finding_type VARCHAR NOT NULL,    -- tool_error | permission_denied | user_rejection | loop | abrupt_end
    turn_index INTEGER NOT NULL,
    severity VARCHAR DEFAULT 'info',   -- info | warning | error
    excerpt TEXT,                      -- short context for UI display
    detected_at TIMESTAMP DEFAULT current_timestamp
);
CREATE INDEX idx_session_findings_session ON session_findings(session_file);
CREATE INDEX idx_session_findings_type ON session_findings(finding_type);
```

Admin UI (`/admin/sessions`):

- List view: one row per session JSONL file, sortable by recency / # findings / user, filters: user, date range, has finding of type X
- Detail view: chronological replay of the session JSONL with finding markers inline; click a finding → highlights the relevant turn(s)
- Aggregated view: heatmap "finding type × week" across all users

### 5.7 `/admin/feedback` — feedback_reports + `agnes report`

NEW table:

```sql
CREATE TABLE feedback_reports (
    id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    reporter_user VARCHAR,             -- nullable for anonymous (future)
    message TEXT NOT NULL,
    session_excerpt TEXT,              -- last N turns of JSONL serialized
    session_file VARCHAR,              -- pointer to full JSONL if uploaded
    environment JSON,                  -- agnes version, OS, claude code version
    fingerprint VARCHAR,               -- sha256 over (message + last error excerpt) for dedup
    status VARCHAR DEFAULT 'open',     -- open | triaged | resolved | wontfix
    assignee VARCHAR,
    tags JSON,                         -- ['cli', 'setup-prompt', 'skill-name', …]
    resolution TEXT,
    resolved_at TIMESTAMP,
    resolved_by VARCHAR
);
CREATE INDEX idx_feedback_status_created ON feedback_reports(status, created_at);
CREATE INDEX idx_feedback_fingerprint ON feedback_reports(fingerprint);
```

End-to-end flow:

1. Analyst (or Claude proactively) runs `agnes report --message "…"`.
2. CLI bundles last 50 turns of current session JSONL (via `cli/lib/claude_sessions.py:list_session_files`) + env info.
3. **CLI shows preview** ("This will be sent: …") and asks for confirmation. Mandatory — never silent submission.
4. `POST /api/feedback` with the bundle.
5. Server inserts row, computes `fingerprint`, writes `audit_log(action='feedback.report')`, returns `report_id`.
6. Server triggers Telegram notification to all admin users with linked `chat_id` (best-effort, swallowed errors).
7. Admin opens `/admin/feedback`, clicks row → modal with full message + session replay + env.
8. Admin actions: assign to self, tag, mark resolved (with resolution text), mark wontfix.

Claude-side trigger: a first-party skill `agnes-report` (in the OSS marketplace) that bundles current session and invokes `agnes report`. Skill manifest lives in `services/marketplace/oss/agnes-report/` (sibling to existing system plugins from #241).

---

## 6. Static content (CLAUDE.md template, copy on the new pages)

Issue #246 proposes a unified content framework. The MVP does NOT block on it — new pages embed copy directly in templates. When #246 lands, those strings move to `instance_content` slugs. Tracked as P2 follow-up; no migration debt incurred because the templates are small.

---

## 7. Security & privacy

### 7.1 Access

- All `/admin/activity/*`, `/admin/sessions/*`, `/admin/feedback/*` endpoints: `Depends(require_admin)`.
- No new resource type. Admin god-mode for v1. Future: optional `audit:read` grant for a hypothetical "compliance" group.

### 7.2 PII in `params`

- Default UI render: literal values in SQL strings masked to `?` placeholders; literal strings elsewhere truncated to 128 chars.
- **"Show raw" toggle + `audit.reveal_raw` logging deferred to Phase B** (reviewer pass): MVP ships with truncation-only display. The toggle UI + its dedicated audit action land alongside the Changes/Diff tab. Until then, admins who need raw values open DuckDB directly — that path itself does not leave a trace, which is documented as a known v40 gap.
- Database always stores raw values. Masking is render-side, not storage-side.

### 7.3 Recursive audit

Every read of `/admin/activity` / `/admin/sessions` / `/admin/feedback` writes `audit_log(action='activity.read' | 'sessions.read' | 'feedback.read')`. Suppressed when:
- Endpoint is the polling health endpoint (high-frequency, low signal).
- Same actor + same filter combination within last 60s.

**Reviewer note — single-worker assumption (v40):** The suppression cache (`_RECENT_AUDITS`) and health-pulse cache (`_HEALTH_CACHE`) are per-process module-level dicts. v40 ships with the existing **single-worker uvicorn** default (no compose change required). When multi-worker uvicorn is later enabled, both caches move to a shared store — a separate plan tracks that. Until then, dedup is per-worker and a multi-worker deployment would let one bad actor produce N rows / minute instead of 1.

### 7.4 Feedback privacy

`session_excerpt` is included in the feedback payload. Skill / CLI **must show preview before submit** — this is a hard requirement, not a UX suggestion. Logged in `audit_log(action='feedback.report', params={ack_preview: true})`.

Server stores excerpts as text. Retention default unbounded; admin can purge `feedback_reports` row directly (still leaves audit_log trace).

---

## 8. Observability of observability

- All new endpoints emit PostHog events when PostHog is enabled:
  - `activity_health_viewed`
  - `activity_timeline_filtered` (with filter keys, not values)
  - `feedback_report_submitted`
  - `session_failure_detected`
- All swallowed errors `posthog.capture_exception()`.
- PostHog events are best-effort; never block the user-visible flow.

---

## 9. Phasing across subsystems

```
WEEK 1  ┌─ Activity Center MVP (this PR) ─────────────────────────┐
        │  - schema v40 (audit_log columns + indices)              │
        │  - AuditRepository.query() rewrite                       │
        │  - SyncHistoryRepository.list_recent()                   │
        │  - close 4 audit gaps (sync.trigger, scripts.run-due,    │
        │    upload.sessions, data.download)                       │
        │  - /admin/activity handler + template                    │
        │  - Health pulse + Timeline + Sync tabs                   │
        │  - redirect /activity-center → /admin/activity           │
        │  - delete demo template content (BREAKING)               │
        └──────────────────────────────────────────────────────────┘

WEEK 2  ┌─ Admin sessions (separate plan) ────────────────────────┐
        │  - schema v41 (session_findings table)                  │
        │  - services/session_processors/failure_scan.py          │
        │  - register in PROCESSORS + scheduler JOBS              │
        │  - /admin/sessions list + detail                        │
        │  - integrate with Activity Center timeline              │
        └──────────────────────────────────────────────────────────┘

WEEK 3  ┌─ Feedback inbox (separate plan) ────────────────────────┐
        │  - schema v42 (feedback_reports table)                  │
        │  - POST /api/feedback endpoint                          │
        │  - cli/commands/report.py                               │
        │  - agnes-report skill in OSS marketplace                │
        │  - /admin/feedback list + detail                        │
        │  - Telegram admin notifications                         │
        └──────────────────────────────────────────────────────────┘

WEEK 4+ ┌─ Phase B / C (separate plans) ──────────────────────────┐
        │  - params_before + Changes tab + Rollback (B)           │
        │  - Usage tab (B)                                        │
        │  - Queries tab gated on #158 (C)                        │
        │  - Performance tab (C)                                  │
        │  - LLM scoring in failure_scan (C)                      │
        │  - GitHub issue auto-file from feedback row (C)         │
        └──────────────────────────────────────────────────────────┘
```

Each weekly chunk is a separate PR with its own CHANGELOG entry. Order matters: Activity Center first because closing the audit gaps benefits the other two surfaces' timelines.

---

## 10. Open questions (decisions still owed)

1. **Rollback in Phase B — generic vs. allowlist?** Recommendation: allowlist of 9 specific actions (`instance_config.update`, `registry.update/create/delete`, `resource_grants.add/remove`, `user_groups.*`, `user_group_members.*`, `instance_templates.set`). Generic rollback is a footgun.
2. **Telegram admin notification volume.** Feedback reports could come fast. Recommendation: rate-limit per admin to 1 message / 5 min; daily digest for the rest. Configurable later.
3. **Session replay in feedback** — store full JSONL or last 50 turns only? Recommendation: last 50 turns inline + pointer to full file if it still exists. Avoids storing duplicate JSONLs in the DB.
4. **`agnes report` always uploads the session, or opt-in?** Recommendation: prompt every time. Power-users can add `--yes` to bypass; default is interactive.
5. **failure_scan LLM scoring in v1 or v2?** Recommendation: v1 deterministic heuristics only. LLM scoring is v2 once we have data to validate heuristic precision against.
6. **`/admin/scheduler-runs` deprecation timing.** Recommendation: keep as a redirect to `/admin/activity?action_prefix=run_session_processor` after MVP ships; remove after one release cycle.

---

## 11. What this displaces / replaces

- `/activity-center` → redirected to `/admin/activity`. Demo template content deleted (BREAKING per CHANGELOG).
- `/admin/scheduler-runs` → redirected to `/admin/activity?action_in=run_session_processor:verification,run_session_processor:usage,marketplace.sync_all` after week 1.
- Dashboard widget pointing at `/activity-center` → URL updated to `/admin/activity`.

Nothing else removed. `/admin/diagnose`, `/admin/tokens`, `/admin/access`, `/admin/marketplaces`, `/admin/registry`, `/admin/server-config` remain as mutating surfaces; Activity Center deep-links into them.

---

## 12. Acceptance criteria for the whole programme (across all three subsystems)

When all three subsystems have shipped:

1. An admin opening `/admin/activity` sees, within 500ms p95, a health pulse and a chronological timeline of every event on the instance for the last 24h.
2. Every audit-writing endpoint (incl. the 4 newly instrumented in week 1) appears in the timeline within the same admin session as the action.
3. An admin clicking on a `sync` event sees the sync_history detail; clicking on a `feedback.report` event sees the feedback row; clicking on a `run_session_processor` event sees the per-processor state row.
4. An analyst running `agnes report --message "test"` produces a `feedback_reports` row, an `audit_log` row, a Telegram message to any admin with a linked chat_id, and visible entries in both `/admin/feedback` and `/admin/activity`.
5. A Claude Code session that contains a tool error triggers a row in `session_findings` after the next `failure_scan` processor tick, surfaced in `/admin/sessions`.
6. Removing the broken demo content from `activity_center.html` lands in a single PR with CHANGELOG `**BREAKING**` marker.
7. All three pages render correctly with PostHog disabled (no events emitted, no client snippet injected for AC's own analytics, page works fully).
8. Every new admin page passes a smoke test that asserts: invoking an audit-writing endpoint surfaces the row in the page's API response within the same test.

---

## 12a. Reviewer pass — applied & deferred

Three sub-agent reviews (security, production resilience, code architecture) ran against the original draft. Consolidated outcomes:

**Applied to spec:**
- §5.2 — DuckDB DESC behavior + upgrade-window note
- §7.2 — `audit.reveal_raw` mechanism deferred to Phase B
- §7.3 — explicit single-worker uvicorn assumption for v40

**Applied to plan (`2026-05-11-activity-center-mvp.md`):**
- Import path corrected (`app.auth.dependencies._get_db`)
- Test fixtures aligned with `seeded_app` / `admin_user` / `get_system_db()` pattern from existing `tests/conftest.py`
- All new audit writes wrapped in `try/except + logger.exception`
- Filename sanitization on `POST /api/upload/sessions`
- 256-char length cap on logged strings
- 7-day cap when `q` filter used without explicit `since`
- Migration idempotency + representative evolved-DB test
- Conventions section added at the top of the plan

**Deferred with rationale (out of MVP):**
- `audit.reveal_raw` toggle + UI (Phase B)
- Shared-cache multi-worker support (separate plan)
- Health pulse threshold env config (P2 polish)
- `diagnose_warnings` real count (depends on diagnose endpoint expansion)
- Default audit retention policy (Phase B follow-up)
- PostHog SDK timeout knob (add if observed in prod)

Reviewer reports are not separately archived in the repo — their consolidated outputs landed as the inline edits above and the "Revisions applied" appendix in the plan doc.

---

## 13. Implementation plan documents

This spec is the parent. The executable plans are:

- **`2026-05-11-activity-center-mvp.md`** — full TDD task list for Week 1 work. **Start here.**
- (next) `2026-05-NN-admin-sessions.md` — failure_scan + /admin/sessions.
- (next) `2026-05-NN-feedback-inbox.md` — agnes report + /admin/feedback.

Each child plan refers back to this spec for cross-cutting decisions.
