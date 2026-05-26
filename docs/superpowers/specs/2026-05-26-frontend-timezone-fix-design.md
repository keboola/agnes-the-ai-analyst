# Frontend timezone fix — design

**Date:** 2026-05-26
**Branch:** `vr/timezonesfix`
**Status:** Draft — awaiting user review

## Problem

Timestamps render incorrectly on the frontend. Four concrete failure modes:

1. **DuckDB session timezone defaults to the host's local zone, not UTC.**
   Empirical test (DuckDB Python client, default settings) shows
   `current_setting('TimeZone')` returns the ICU-derived host zone (e.g.
   `Asia/Dubai`, `Europe/Prague`, depending on machine). When server code
   writes `datetime.now(timezone.utc)` against a column typed `TIMESTAMP`
   (not `TIMESTAMPTZ`), DuckDB **converts the value into the session
   timezone, then drops `tzinfo`**. Example: `12:00 UTC` written from a
   `Asia/Dubai` (UTC+4) session is stored as naive `16:00`.
   `app/api/health.py:202-206` already notes this behavior in passing.
   Two consequences:
   - Existing rows reflect the session tz of whatever process wrote them.
     For Docker / k8s deployments the default `TZ` is UTC, so most
     production rows are UTC-naive; on bare-metal or developer machines
     they may be local-naive.
   - Any fix that assumes "naive = UTC" without first pinning the session
     tz is fragile — a new connection on a non-UTC host will silently
     start writing local-naive values again.

2. **Naive datetime → wrong local time on the wire.** Even when the stored
   value really is UTC-naive, FastAPI / Pydantic serialize naive datetimes
   as ISO strings **without an offset suffix** (e.g.
   `"2026-05-26T15:30:00"`). Per the ECMAScript spec, `new Date()` of an
   offset-less ISO datetime is parsed as **local time**, so an analyst in
   `Europe/Prague` (+02:00 in summer) sees a value two hours off.

3. **Per-template `fmtDate` helpers truncate the ISO string and never
   convert to local tz.** At least five templates (`admin_users.html`,
   `admin_groups.html`, `admin_group_detail.html`, `admin_user_detail.html`,
   `admin_marketplaces.html`) define:
   ```js
   function fmtDate(s) { return s ? s.slice(0,16).replace("T"," ") : "—"; }
   ```
   This keeps the UTC characters from the server string and presents them as
   if they were local time. Even if (1) is fixed, this still mis-renders.

4. **Server-rendered `strftime` is hard-coded to UTC literal.** Many Jinja
   templates render `{{ ts.strftime('%Y-%m-%d %H:%M UTC') }}` directly into
   HTML. Analysts in non-UTC tz cannot read these at a glance and there is no
   way to localize without JS.

There is no single helper or contract — each template improvises, and drift
is guaranteed.

## Constraints

- **No DuckDB schema migration.** A parallel Postgres migration is in
  flight; touching schema in this branch would conflict. Fix at the
  serialization layer only.
- **Minimal blast radius.** Only `app/web/`, `app/serialization` (new), and
  one tests file. No connector / orchestrator / DB code touched.
- **Backwards-compatible.** Existing API consumers (CLI, desktop, etc.)
  must keep working. ISO strings with explicit offset are valid input
  everywhere they were before; we only narrow ambiguity.
- **Vendor-agnostic OSS rules apply.** No customer-specific tz, no fixed
  display locale in code — use browser default.

## Decision

Adopt a single contract and four concrete pieces:

### Contract

> 1. Every DuckDB connection the app opens runs `SET TimeZone='UTC'`
>    immediately after `connect()`. Writes are normalized to UTC at the
>    DB boundary; reads of `TIMESTAMP` columns return naive datetimes
>    whose clock value is UTC.
>
> 2. Every datetime value in any JSON response emitted by the FastAPI app
>    carries an explicit UTC offset. Naive Python datetimes are assumed
>    UTC and serialized with the `Z` suffix. Aware datetimes are
>    serialized via their native `.isoformat()`.
>
> 3. Every datetime displayed in the web UI is rendered in the browser's
>    local timezone via a single shared JS helper. The UTC string remains
>    as the no-JS fallback and as a tooltip on hover.

### Piece 0 — pin DuckDB session timezone to UTC

Every `duckdb.connect(...)` site in the codebase should funnel through a
small helper that runs `SET TimeZone='UTC'` before returning the
connection. From the grep, the sites are:

- `src/db.py` — `connect()`, `get_analytics_db()`, snapshot opens, and
  several read-only opens (lines 1115, 1143, 1222, 1305, 1469, 1475 at
  the time of writing).
- `src/orchestrator.py` — extract.duckdb opens (lines 175, 248, 351).
- `src/profiler.py:761`.
- `app/api/v2_schema.py:191`, `app/api/v2_sample.py:152`,
  `app/api/v2_scan.py:393` — `:memory:` analysis sessions.

Add a private helper `_open_duckdb(path, **kwargs)` in `src/db.py`:

```python
def _open_duckdb(path, **kwargs):
    conn = duckdb.connect(path, **kwargs)
    try:
        conn.execute("SET TimeZone='UTC'")
    except Exception:
        # older DuckDB without ICU — already naive-UTC equivalent
        pass
    return conn
```

Refactor each `duckdb.connect(...)` site to call `_open_duckdb(...)`.
Same signature; only the import changes.

**Existing data caveat.** Rows already written before this change reflect
the session tz at write time. We will not backfill. The assumption that
production deployments default to `TZ=UTC` (Docker, k8s) makes this an
acceptable cost; deployments that ran on a non-UTC host will have legacy
rows mis-labeled by their local offset. Adding a migration to rewrite
them is out of scope and would conflict with the parallel Postgres
migration.

### Piece 1 — server serialization shim

New module `app/serialization.py`:

```python
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import json

def _encode_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()  # always carries offset

class AgnesJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            jsonable_encoder(content, custom_encoder={datetime: _encode_dt}),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
```

Wire as `default_response_class=AgnesJSONResponse` on the FastAPI app
(`app/main.py`). Endpoints that already return custom `Response` /
`StreamingResponse` are unaffected.

**Trade-off considered:** registering a custom Pydantic
`model_serializer` for `datetime` was rejected because not every endpoint
returns a Pydantic model — many return raw dicts. The
`default_response_class` route catches both.

**Naive-but-not-UTC edge case:** the only intentionally local-naive
datetime in the audit is `app/api/health.py:207` (`now_local_naive`,
used for sync-lag comparison). After Piece 0 the DuckDB session is
pinned to UTC, so `last_processed` is now UTC-naive on read; the
sync-lag computation can — and should — switch to comparing against
`datetime.utcnow()` (or `datetime.now(timezone.utc).replace(tzinfo=None)`)
and the comment block at lines 201-206 should be updated. The
`now_local_naive` value never crosses the API boundary in any branch, so
the serialization shim is unaffected either way. If a future caller adds
a local-naive datetime to a response payload, the shim will mislabel it
as UTC; a grep-based CI guard is out of scope.

### Piece 2 — central JS helper

New file `app/web/static/js/datetime.js`. Exposes a single namespace
`window.AgnesTime` with these functions:

```js
window.AgnesTime = {
  // Parse ISO string. Treats offset-less ISO as UTC (defensive fallback
  // — the server shim should make this branch unreachable).
  parse(iso) { ... },

  // "2026-05-26 15:30" in browser local tz
  formatDateTime(iso) { ... },

  // "2026-05-26" in browser local tz
  formatDate(iso) { ... },

  // "just now" / "5m ago" / "2h ago" / "3d ago" / falls back to formatDateTime
  formatRelative(iso) { ... },

  // Scan a DOM subtree for <time datetime="..."> elements:
  //   - text content -> formatDateTime(datetime)
  //   - title attr   -> the original UTC literal (for tooltip)
  // Idempotent (sets data-hydrated="1" sentinel).
  hydrateTimes(root = document) { ... },
};

document.addEventListener("DOMContentLoaded", () => {
  window.AgnesTime.hydrateTimes();
});
```

Loaded in `app/web/templates/base.html` and `base_ds.html` via a single
`<script src="/static/js/datetime.js" defer></script>` tag. AJAX-loading
templates call `window.AgnesTime.hydrateTimes(newNode)` after inserting
content.

Format options for `toLocaleString`: `{ year: 'numeric', month: '2-digit',
day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }`.
Browser handles the locale ordering (US → MM/DD/YYYY, EU → DD.MM.YYYY,
etc.).

### Piece 3 — template migration

Two find-and-replace patterns, applied to each touched template:

**Pattern A — replace server `strftime` UTC labels:**
```jinja
{{ x.strftime('%Y-%m-%d %H:%M UTC') }}
```
→
```jinja
<time datetime="{{ x.isoformat() }}">{{ x.strftime('%Y-%m-%d %H:%M') }} UTC</time>
```
The fallback text remains correct UTC; hydration replaces with local;
tooltip retains the UTC string.

**Pattern B — replace per-template `fmtDate` slice helpers:**
```js
function fmtDate(s) { return s ? s.slice(0,16).replace("T"," ") : "—"; }
…fmtDate(u.created_at)…
```
→
```js
// fmtDate removed — use window.AgnesTime.formatDateTime
…window.AgnesTime.formatDateTime(u.created_at) ?? "—"…
```

Templates in scope (from the audit, grouped by pattern):

- **Pattern A (server `strftime`):** `admin_workspace_prompt.html`,
  `admin_welcome.html`, `admin_store_submissions.html`,
  `admin_store_submission_detail.html`, `admin_scheduler_runs.html`,
  `admin/news_editor.html`, `news.html`.
- **Pattern B (broken client `fmtDate`):** `admin_users.html`,
  `admin_user_detail.html`, `admin_groups.html`, `admin_group_detail.html`,
  `admin_marketplaces.html`.
- **Mixed / one-off:** `admin_sessions.html`, `admin_session_detail.html`,
  `admin_corporate_memory.html`, `me_activity.html`, `activity_center.html`,
  `_home_stats.html`, `_version_badge.html`, `marketplace.html`,
  `marketplace_plugin_detail.html`, `marketplace_item_detail.html`,
  `_profile_tokens.html`, `admin_tokens.html`.
- **Already correct (verify only):** `admin_server_config.html` (uses
  `toLocaleString` already), `install.html` and `_claude_setup_cta.jinja`
  (construct `new Date()` for fresh stamps — no-op).

## Out of scope

- Migrating DuckDB `TIMESTAMP` columns to `TIMESTAMPTZ` (blocked by ongoing
  Postgres migration).
- User-selectable display timezone or 12/24-hour preference.
- Locale-aware month names / weekday strings beyond browser defaults.
- Server-side template strftime that does not render a UTC label and is
  not user-facing (e.g. log lines, internal debug dumps).
- The `parseDate` helpers in `admin_tokens.html` and `_profile_tokens.html`
  — they already parse explicit-offset strings correctly. We will replace
  them with `window.AgnesTime.parse` for consistency, but no behavior
  change is expected.

## Risks

- **Legacy rows on non-UTC hosts.** Rows written before Piece 0 reflect
  the write-time session tz. Deployments that ran on a non-UTC host will
  display those legacy rows offset by their local-vs-UTC delta. No
  in-scope mitigation (no schema migration). Operators of such
  deployments will see this as a one-time correction at upgrade.
- **Hydration flash.** Server emits `2026-05-26 15:30 UTC`, JS swaps to
  `2026-05-26 17:30` (local). Visible for ~50 ms on slow networks.
  Mitigation: fallback is already a correct, readable UTC value, so a
  flash from "correct UTC" to "correct local" is acceptable. No layout
  shift because the string width is similar.
- **Double-encoding.** If a downstream caller already produced a string
  with `Z`, the shim must not touch it. The encoder only fires on
  `datetime` instances, so strings pass through unchanged.
- **Pydantic v2 model serialization bypass.** A Pydantic model with its
  own `@field_serializer` for `datetime` would still win over the
  app-level encoder. Audit: no such serializer exists in `app/`. Test
  guard added.
- **`base.html` vs `base_ds.html` inheritance.** Both must load the
  script. Plan step explicitly adds the tag to both.

## Validation

- **Unit** — `tests/test_duckdb_session_tz.py`:
  - `_open_duckdb(":memory:")` → `SELECT current_setting('TimeZone')`
    returns `'UTC'`.
  - Aware UTC datetime written to a `TIMESTAMP` column reads back as
    naive with the same clock value (no shift).
- **Unit** — `tests/test_datetime_serialization.py`:
  - `_encode_dt(naive)` → ends with `Z`
  - `_encode_dt(aware_utc)` → `+00:00` or `Z` (whichever Python emits)
  - `_encode_dt(aware_offset)` → preserves offset
  - `_encode_dt(None)` → not reached (jsonable_encoder filters)
  - `TestClient.get("/api/admin/users")` → every datetime field in the
    response body matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?
    (Z|[+\-]\d{2}:\d{2})$`.
- **JS unit (no framework today)** — defer; rely on E2E.
- **E2E** — extend an existing Playwright test (e.g. `tests/e2e/`) to
  load `/admin/users` with `TZ=Europe/Prague` in the test container and
  assert that the rendered `<time>` text matches the expected local-tz
  format for a fixture row with a known UTC `created_at`.
- **Manual smoke** — every touched template loaded once in a browser;
  hover any timestamp; confirm tooltip shows UTC and visible text shows
  local. JS disabled in DevTools → confirm UTC fallback still renders.

## Rollback

Revert the merge commit. The serializer shim is additive; the JS helper
loads independently; template changes are textual. No data migration.

## References

- Branch: `vr/timezonesfix`
- Related: ongoing Postgres migration (separate effort) — coordinate
  before re-opening the DuckDB schema question.
