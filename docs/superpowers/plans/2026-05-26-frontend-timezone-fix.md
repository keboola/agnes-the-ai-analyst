# Frontend timezone fix — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render frontend timestamps in the analyst's local timezone, with UTC as the no-JS fallback and tooltip.

**Architecture:** Pin DuckDB session to UTC at connection time, so naive `TIMESTAMP` reads are UTC-clock. Add a FastAPI default response class that serializes any naive `datetime` with a `Z` suffix and aware datetimes via their native offset. Replace per-template `fmtDate` slice helpers and server-side `strftime` UTC labels with a single `window.AgnesTime` JS helper that hydrates `<time datetime>` tags client-side.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, DuckDB (ICU extension), Jinja2, vanilla JS (no framework), pytest, Playwright (for E2E smoke).

**Spec:** `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.

---

## Task 1 — DuckDB `_open_duckdb()` helper + tz pin test

**Files:**
- Modify: `src/db.py` (add helper near top, refactor in-file `duckdb.connect()` call sites)
- Test: `tests/test_duckdb_session_tz.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_duckdb_session_tz.py`:

```python
"""DuckDB connection helper pins session timezone to UTC."""

from datetime import datetime, timezone

import duckdb

from src.db import _open_duckdb


def test_open_duckdb_pins_session_to_utc():
    conn = _open_duckdb(":memory:")
    tz = conn.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert tz == "UTC"


def test_open_duckdb_aware_utc_roundtrip_no_shift():
    conn = _open_duckdb(":memory:")
    conn.execute("CREATE TABLE t (ts TIMESTAMP)")
    aware = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    conn.execute("INSERT INTO t VALUES (?)", [aware])
    (got,) = conn.execute("SELECT ts FROM t").fetchone()
    assert got.tzinfo is None
    assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 5, 26, 12, 0)


def test_open_duckdb_read_only_still_utc(tmp_path):
    db = tmp_path / "x.duckdb"
    rw = _open_duckdb(str(db))
    rw.execute("CREATE TABLE t (ts TIMESTAMP)")
    rw.close()
    ro = _open_duckdb(str(db), read_only=True)
    assert ro.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/pytest tests/test_duckdb_session_tz.py -v
```
Expected: `ImportError` on `_open_duckdb` from `src.db`.

- [ ] **Step 3: Add the helper in `src/db.py`**

Add near the top of `src/db.py`, after the existing `import duckdb` block:

```python
def _open_duckdb(path, **kwargs):
    """Open a DuckDB connection with session timezone pinned to UTC.

    All `duckdb.connect(...)` call sites in the codebase should funnel
    through this helper. DuckDB's TIMESTAMP type stores naive values, and
    the ICU extension's default session timezone is the host's local zone
    (not UTC). Without pinning, a `datetime.now(timezone.utc)` write gets
    shifted into the host zone before tzinfo is stripped, leading to
    naive-but-local-tz values on disk. Pinning the session to UTC keeps
    naive reads aligned with the wire / display contract documented in
    `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`.
    """
    conn = duckdb.connect(path, **kwargs)
    try:
        conn.execute("SET TimeZone='UTC'")
    except duckdb.Error:
        # Older DuckDB builds without the ICU extension already behave as
        # naive-UTC; nothing to pin.
        pass
    return conn
```

- [ ] **Step 4: Refactor in-file `duckdb.connect()` call sites in `src/db.py`**

Replace `duckdb.connect(...)` with `_open_duckdb(...)` at each of these sites (line numbers are at-time-of-writing; locate by surrounding code):

- `src/db.py:1115`: `conn = duckdb.connect(str(snapshot_path), read_only=True)` → `conn = _open_duckdb(str(snapshot_path), read_only=True)`
- `src/db.py:1143`: `return duckdb.connect(db_path)` → `return _open_duckdb(db_path)`
- `src/db.py:1222`: same pattern
- `src/db.py:1305`: `_analytics_db_conn = duckdb.connect(db_path)` → `_analytics_db_conn = _open_duckdb(db_path)`
- `src/db.py:1469`: `conn = duckdb.connect(str(db_path), read_only=False)` → `_open_duckdb(...)`
- `src/db.py:1475`: `conn = duckdb.connect(str(db_path), read_only=True)` → `_open_duckdb(...)`

- [ ] **Step 5: Run test to verify it passes**

```
.venv/bin/pytest tests/test_duckdb_session_tz.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Run full test suite for regressions**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: pre-existing pass count + 3, no new failures.

- [ ] **Step 7: Commit**

```bash
git add src/db.py tests/test_duckdb_session_tz.py
git commit -m "fix(db): pin DuckDB session timezone to UTC via _open_duckdb helper

DuckDB's TIMESTAMP type strips tzinfo on write after shifting the value
into the session timezone (ICU default = host local zone). Pinning the
session to UTC makes naive reads UTC-clock, which the wire serializer
and JS hydrator both assume."
```

---

## Task 2 — refactor remaining `duckdb.connect()` call sites

**Files:**
- Modify: `src/orchestrator.py`, `src/profiler.py`, `app/api/v2_schema.py`, `app/api/v2_sample.py`, `app/api/v2_scan.py`

- [ ] **Step 1: Locate and refactor `src/orchestrator.py`**

```
grep -n "duckdb.connect" src/orchestrator.py
```
Three call sites (175, 248, 351 at writing). Replace each with `_open_duckdb(...)`. Add `from src.db import _open_duckdb` (or `from .db import _open_duckdb` if relative imports are the convention in this file — match the existing import style).

- [ ] **Step 2: Locate and refactor `src/profiler.py`**

One call site at `src/profiler.py:761`: `con = duckdb.connect()` → `con = _open_duckdb(":memory:")` (explicit `:memory:` for clarity; bare `connect()` is equivalent). Add import.

- [ ] **Step 3: Locate and refactor v2 API modules**

For each of `app/api/v2_schema.py:191`, `app/api/v2_sample.py:152`, `app/api/v2_scan.py:393`: `duckdb.connect(":memory:")` → `_open_duckdb(":memory:")`. Add `from src.db import _open_duckdb`.

- [ ] **Step 4: Sanity grep**

```
grep -rn "duckdb.connect(" src/ app/ | grep -v test
```
Expected: only the implementation inside `_open_duckdb` itself.

- [ ] **Step 5: Run full test suite**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: still green.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator.py src/profiler.py app/api/v2_schema.py app/api/v2_sample.py app/api/v2_scan.py
git commit -m "fix(db): route all duckdb.connect sites through _open_duckdb"
```

---

## Task 3 — fix `app/api/health.py` sync-lag (now UTC-aware on read)

**Files:**
- Modify: `app/api/health.py:201-210`
- Test: `tests/test_health_sync_lag.py` if it exists; otherwise no new test needed (manual smoke through `/api/health`)

- [ ] **Step 1: Read the existing block**

Open `app/api/health.py` and locate lines 201-210 (the `now_local_naive` comparison and the comment block above it).

- [ ] **Step 2: Replace the block**

```python
    # Both available — compare. `session_processor_state.processed_at` is
    # stored as DuckDB TIMESTAMP (naive). The DuckDB connection helper
    # pins the session timezone to UTC, so the naive read is UTC-clock.
    # Compare against UTC-naive `now` to keep both sides on the same axis.
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(last_processed, "tzinfo") and last_processed.tzinfo is not None:
        last_processed = last_processed.replace(tzinfo=None)
    proc_age_seconds = (now_utc_naive - last_processed).total_seconds()
```

- [ ] **Step 3: Run any health-related tests**

```
.venv/bin/pytest tests/ -k health --tb=short -q
```
Expected: green (or empty if no tests match).

- [ ] **Step 4: Commit**

```bash
git add app/api/health.py
git commit -m "fix(health): compare sync-lag against UTC-naive now after DuckDB tz pin"
```

---

## Task 4 — `app/serialization.py` + wire on FastAPI app

**Files:**
- Create: `app/serialization.py`
- Modify: `app/main.py` (wire `default_response_class`)
- Test: `tests/test_datetime_serialization.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_datetime_serialization.py`:

```python
"""FastAPI default response class labels naive datetimes as UTC on the wire."""

from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI

from app.serialization import AgnesJSONResponse, _encode_dt


def test_encode_naive_assumes_utc_emits_z():
    out = _encode_dt(datetime(2026, 5, 26, 12, 0, 0))
    assert out.endswith("+00:00") or out.endswith("Z")
    # JS new Date(out) must parse this as 12:00 UTC; the encoder must not
    # emit an offset-less string.
    assert ("+" in out) or out.endswith("Z")


def test_encode_aware_utc_keeps_offset():
    out = _encode_dt(datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc))
    assert out.endswith("+00:00") or out.endswith("Z")


def test_encode_aware_offset_preserves_offset():
    dt = datetime(2026, 5, 26, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    out = _encode_dt(dt)
    assert out.endswith("+03:00")


def test_response_renders_nested_datetimes_with_offset():
    app = FastAPI(default_response_class=AgnesJSONResponse)

    @app.get("/probe")
    def probe():
        return {
            "naive": datetime(2026, 5, 26, 12, 0, 0),
            "aware": datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            "nested": {"items": [{"ts": datetime(2026, 5, 26, 12, 0, 0)}]},
        }

    from fastapi.testclient import TestClient

    client = TestClient(app)
    body = client.get("/probe").json()
    assert "+" in body["naive"] or body["naive"].endswith("Z")
    assert "+" in body["aware"] or body["aware"].endswith("Z")
    assert "+" in body["nested"]["items"][0]["ts"] or body["nested"]["items"][0]["ts"].endswith("Z")


def test_response_passes_through_strings_unchanged():
    app = FastAPI(default_response_class=AgnesJSONResponse)

    @app.get("/probe")
    def probe():
        return {"label": "2026-05-26T12:00:00Z"}

    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/probe").json()["label"] == "2026-05-26T12:00:00Z"
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/pytest tests/test_datetime_serialization.py -v
```
Expected: `ImportError` on `app.serialization`.

- [ ] **Step 3: Create `app/serialization.py`**

```python
"""FastAPI JSON response class that labels datetime values with UTC offset.

DuckDB TIMESTAMP reads return naive datetimes whose clock value is UTC
(thanks to the SET TimeZone='UTC' pin in `src.db._open_duckdb`). Pydantic
and `jsonable_encoder` would serialize those as ISO strings *without* an
offset suffix, and `new Date(...)` in JS parses offset-less ISO datetimes
as local time per the ECMAScript spec — so an analyst in Europe/Prague
would see times 2 hours off. This response class assumes naive → UTC and
emits `...Z` (via `isoformat()`) so the browser converts correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def _encode_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class AgnesJSONResponse(JSONResponse):
    """Default response class — see module docstring."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            jsonable_encoder(content, custom_encoder={datetime: _encode_dt}),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
```

- [ ] **Step 4: Wire on the FastAPI app**

Open `app/main.py`. Locate the `FastAPI(...)` constructor call.

Add import near the other `from app.*` imports:

```python
from app.serialization import AgnesJSONResponse
```

Update the constructor:

```python
app = FastAPI(
    ...,                       # existing kwargs unchanged
    default_response_class=AgnesJSONResponse,
)
```

If the existing `FastAPI(...)` call has no kwargs, add only `default_response_class=AgnesJSONResponse`. Match the surrounding formatting.

- [ ] **Step 5: Run tests to verify they pass**

```
.venv/bin/pytest tests/test_datetime_serialization.py -v
```
Expected: 5 passed.

- [ ] **Step 6: Run full test suite for regressions**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: still green. If any existing tests assert offset-less ISO strings, update them to accept `+00:00` / `Z` — those tests were locking in a bug.

- [ ] **Step 7: Commit**

```bash
git add app/serialization.py app/main.py tests/test_datetime_serialization.py
git commit -m "fix(api): label naive datetimes as UTC on the wire via AgnesJSONResponse

JSON responses now always carry an explicit offset for datetime fields,
preventing the JS-side new Date() local-time misinterpretation that
caused frontend timestamps to render off by the user's UTC offset."
```

---

## Task 5 — API contract test (real endpoint)

**Files:**
- Modify: `tests/test_datetime_serialization.py` (extend with one TestClient hit on a real route)

- [ ] **Step 1: Add an integration test**

Append to `tests/test_datetime_serialization.py`:

```python
import re

ISO_WITH_OFFSET = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+\-]\d{2}:\d{2})$"
)


def _all_iso_strings(obj):
    """Yield every string leaf that looks like an ISO datetime."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_iso_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_iso_strings(v)
    elif isinstance(obj, str) and len(obj) >= 10 and obj[4] == "-" and obj[7] == "-":
        yield obj


def test_real_endpoint_datetimes_have_offset(monkeypatch):
    """Smoke test against the live app — every datetime string in the response
    must carry an offset. Pick an endpoint that returns datetimes without
    requiring auth or DB seeding; `/api/health` works on most builds."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/health")
    if resp.status_code != 200:
        pytest.skip(f"/api/health returned {resp.status_code}; skip")
    for s in _all_iso_strings(resp.json()):
        # Skip date-only strings (length 10) and non-ISO labels.
        if len(s) == 10:
            continue
        if "T" not in s:
            continue
        assert ISO_WITH_OFFSET.match(s), f"datetime string lacks offset: {s!r}"
```

- [ ] **Step 2: Run it**

```
.venv/bin/pytest tests/test_datetime_serialization.py::test_real_endpoint_datetimes_have_offset -v
```
Expected: pass (or `skip` if `/api/health` returns non-200 in this build). If it fails, the response contains an offset-less datetime → trace the field back to the source.

- [ ] **Step 3: Commit**

```bash
git add tests/test_datetime_serialization.py
git commit -m "test: assert /api/health datetimes carry an explicit offset"
```

---

## Task 6 — `app/web/static/js/datetime.js` + load in base templates

**Files:**
- Create: `app/web/static/js/datetime.js`
- Modify: `app/web/templates/base.html`, `app/web/templates/base_ds.html`

- [ ] **Step 1: Create the helper**

```javascript
// app/web/static/js/datetime.js
//
// Single source of truth for rendering timestamps in the web UI.
// Renders in the browser's local timezone; keeps the UTC literal in the
// element's title attribute as a tooltip and as the no-JS fallback.
//
// Contract: every <time datetime="ISO_WITH_OFFSET"> in the DOM gets its
// text content replaced with the local representation.  Idempotent — the
// hydrator sets data-hydrated="1" so AJAX re-runs do not double-format.

(function () {
  "use strict";

  function parseIso(s) {
    if (!s) return null;
    // Defensive: if a caller forgets the offset, treat as UTC.  The
    // server serializer should make this branch unreachable.
    if (typeof s === "string" && /T\d{2}:\d{2}/.test(s) && !/(Z|[+\-]\d{2}:?\d{2})$/.test(s)) {
      s = s + "Z";
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function formatDateTime(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
           " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function formatDate(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function formatRelative(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    var sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (sec < 0) sec = 0;
    if (sec < 45) return "just now";
    if (sec < 90) return "1m ago";
    var min = Math.round(sec / 60);
    if (min < 45) return min + "m ago";
    if (min < 90) return "1h ago";
    var hr = Math.round(min / 60);
    if (hr < 24) return hr + "h ago";
    var day = Math.round(hr / 24);
    if (day < 7) return day + "d ago";
    return formatDate(iso);
  }

  function hydrateTimes(root) {
    root = root || document;
    var nodes = root.querySelectorAll("time[datetime]:not([data-hydrated])");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var iso = el.getAttribute("datetime");
      var label = formatDateTime(iso);
      if (!label) continue;
      // Preserve any explicit UTC label currently in the element as the
      // tooltip, unless the caller already set a title.
      if (!el.hasAttribute("title")) {
        var raw = (el.textContent || "").trim();
        if (raw) el.setAttribute("title", raw);
      }
      el.textContent = label;
      el.setAttribute("data-hydrated", "1");
    }
  }

  window.AgnesTime = {
    parse: parseIso,
    formatDateTime: formatDateTime,
    formatDate: formatDate,
    formatRelative: formatRelative,
    hydrateTimes: hydrateTimes,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { hydrateTimes(); });
  } else {
    hydrateTimes();
  }
})();
```

- [ ] **Step 2: Load it from `base.html`**

Open `app/web/templates/base.html`. Find the existing `<script>` tags in the `<head>` (or near `{% block scripts %}`). Add:

```html
<script src="/static/js/datetime.js" defer></script>
```

Place it before any page-specific script blocks so the global is available when page scripts run. If the project uses a static-asset cache-busting query string elsewhere, match it.

- [ ] **Step 3: Load it from `base_ds.html`**

Same edit, in `app/web/templates/base_ds.html`.

- [ ] **Step 4: Smoke check**

```
.venv/bin/pytest tests/ -k web --tb=short -q
```
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add app/web/static/js/datetime.js app/web/templates/base.html app/web/templates/base_ds.html
git commit -m "feat(web): single window.AgnesTime helper for local-tz timestamp rendering"
```

---

## Task 7 — Pattern A template migration (server `strftime` → `<time>`)

**Files (one commit per file is fine; the example pattern is the same everywhere):**
- `app/web/templates/admin_workspace_prompt.html:213`
- `app/web/templates/admin_welcome.html:213`
- `app/web/templates/admin_store_submissions.html:252`
- `app/web/templates/admin_store_submission_detail.html:178, 184, 223, 306, 327, 342`
- `app/web/templates/admin_scheduler_runs.html:64`
- `app/web/templates/admin/news_editor.html:238, 312, 314`
- `app/web/templates/news.html:68`

- [ ] **Step 1: Apply Pattern A on each line**

For each occurrence of:

```jinja
{{ x.strftime('%Y-%m-%d %H:%M UTC') }}
```

or any similar strftime that hard-codes a UTC label, replace with:

```jinja
<time datetime="{{ x.isoformat() }}">{{ x.strftime('%Y-%m-%d %H:%M') }} UTC</time>
```

Notes:
- The text content stays a valid UTC label (no-JS fallback).
- `x.isoformat()` for a naive datetime emits no offset; the JS helper's
  defensive `parseIso` treats that as UTC, and the server shim (Task 4)
  makes API-emitted values already carry `Z`. Both paths converge.
- For lines where `x` is rendered inside a title="..." attribute or
  embedded in a sentence, keep the surrounding markup and only wrap the
  date text.
- For `admin_store_submission_detail.html:185` (`<span class="relative">`
  + `data-rel-since="..."`), no change: the page already feeds JS the
  ISO via a data attr; only the static fallback text needs the `<time>`
  wrapper, and only if there is a static fallback. If the line is
  already `<span class="relative" data-rel-since="{{ x.isoformat() }}">`
  with a JS-replaced label, leave it.

- [ ] **Step 2: Open one of the touched templates in a browser**

Run the app locally (`uvicorn app.main:app --reload --port 8001`), sign in
as Admin, open `/admin/welcome` (or whichever page contains the touched
line), and verify that the timestamp text was replaced with a local-tz
formatted string and the tooltip shows the UTC literal.

- [ ] **Step 3: Run tests**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/
git commit -m "fix(web): wrap server-rendered UTC timestamps in <time> for client-side local-tz hydration"
```

---

## Task 8 — Pattern B template migration (broken `fmtDate` slice → `window.AgnesTime`)

**Files:**
- `app/web/templates/admin_users.html:324, 456, 457`
- `app/web/templates/admin_user_detail.html:375, 492, 757, 821`
- `app/web/templates/admin_groups.html:195, 306`
- `app/web/templates/admin_group_detail.html:259, 319`
- `app/web/templates/admin_marketplaces.html:413, 480, 481`

- [ ] **Step 1: Replace `fmtDate` definition and call sites**

In each file:

Old:
```js
function fmtDate(s) { return s ? s.slice(0,16).replace("T"," ") : "—"; }
```

New:
```js
function fmtDate(s) { return s ? (window.AgnesTime.formatDateTime(s) || "—") : "—"; }
```

(Keeping the wrapper rather than inlining `window.AgnesTime.formatDateTime` everywhere lets us preserve the "—" fallback semantics this codebase uses for nullable timestamps.)

For the variants that use `String(s).slice(0,16)` etc., same replacement — the helper handles `null`/`undefined`/empty.

- [ ] **Step 2: Manually smoke each touched page**

Sign in as Admin, open `/admin/users`, `/admin/groups`, `/admin/marketplaces`. Hover any date cell; expect the tooltip to be empty (Pattern B does not set one) and the cell text to be local-tz formatted.

- [ ] **Step 3: Run tests**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/
git commit -m "fix(web): swap broken fmtDate slice for window.AgnesTime.formatDateTime in admin tables"
```

---

## Task 9 — Mixed / one-off template audit + fix

**Files (each needs a small targeted change; quick-look table):**

| File | Line | Current | Action |
|---|---|---|---|
| `admin_sessions.html` | 238-240 | `new Date(iso).toLocaleString()` | replace with `window.AgnesTime.formatDateTime(iso)` for format consistency |
| `admin_session_detail.html` | 107 | `new Date(iso)` | same |
| `admin_corporate_memory.html` | 3488 | `new Date(entry.timestamp).toLocaleString()` | same |
| `me_activity.html` | 219-230 | mixed; relative + absolute | use `formatRelative` / `formatDateTime` |
| `activity_center.html` | 635-642 | inline `toLocaleString` with options | replace with `formatDateTime` |
| `_home_stats.html` | 144 | inline relative | use `formatRelative` |
| `_version_badge.html` | 10 | `new Date(v.deployed_at)` | use `formatDateTime` |
| `marketplace.html` | 790 | `new Date(it.added).toISOString().slice(0,10)` | use `formatDate` |
| `marketplace_plugin_detail.html` | 983 | inline | use `formatDateTime` |
| `marketplace_item_detail.html` | 965 | inline | use `formatDateTime` |
| `_profile_tokens.html` | 800-807 | `parseDate(...)` wrapping `new Date()` | replace with `window.AgnesTime.parse` |
| `admin_tokens.html` | 733-740 | same parseDate helper | same |

- [ ] **Step 1: Walk the table top-to-bottom**

For each row, open the file, locate the line(s), apply the change. Read the surrounding 5 lines to ensure the replacement variable name matches. Where a helper function (e.g. `parseDate`, `fmtDate`) is now a trivial wrapper, leave the wrapper in place and update its body — that keeps call sites untouched.

- [ ] **Step 2: Sanity grep**

```
grep -rn "new Date(" app/web/templates | grep -v -E "new Date\(\)" | wc -l
```
Compare to the pre-task count. The number should be lower, and the remaining hits should all be `new Date()` (constructing a fresh stamp), not `new Date(serverValue)`.

- [ ] **Step 3: Run tests**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/
git commit -m "fix(web): route remaining one-off timestamp renders through window.AgnesTime"
```

---

## Task 10 — Playwright smoke (best-effort; skip if env not present)

**Files:**
- Test: `tests/e2e/test_timestamp_hydration.py` (only if a `tests/e2e/` directory already exists in the repo and other Playwright tests live there)

- [ ] **Step 1: Check if Playwright is wired**

```
ls tests/e2e/ 2>/dev/null
```

If empty / missing: skip this task. The serializer tests + manual browser smoke already cover the contract; a Playwright run is a nice-to-have, not a gate.

- [ ] **Step 2: If wired, add a smoke test**

Pattern (adjust to local fixtures):

```python
def test_admin_users_renders_created_at_in_local_tz(page, admin_session):
    # Container TZ env should be Europe/Prague for this test (CI default).
    page.goto("/admin/users")
    cell = page.locator("td.date-cell").first
    # The fixture created_at = 2026-01-01T12:00:00Z;
    # CET (winter) = +01:00, so local = 13:00.
    assert "2026-01-01 13:00" in cell.text_content()
```

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_timestamp_hydration.py
git commit -m "test(e2e): assert admin_users renders created_at in container local tz"
```

---

## Task 11 — CHANGELOG bullet + open PR

**Files:**
- Modify: `CHANGELOG.md`
- (no version bump in this branch — release-cut comes with the PR that merges it; see `docs/RELEASING.md`)

- [ ] **Step 1: Add CHANGELOG bullet under `[Unreleased]`**

Open `CHANGELOG.md`. Under the existing `## [Unreleased]` header, add:

```markdown
### Fixed
- **Frontend timestamps now render in the analyst's local timezone.** Three coupled fixes: (1) every `duckdb.connect(...)` is now routed through `src.db._open_duckdb`, which pins the session timezone to UTC — DuckDB's `TIMESTAMP` type strips tzinfo on write after shifting into the session zone, and ICU's default session zone is the host's local zone, so a UTC-aware write on a non-UTC host was previously stored as local-naive. (2) FastAPI now uses `app.serialization.AgnesJSONResponse` as its default response class; naive `datetime` values are assumed UTC and serialized with an explicit offset, so `new Date(...)` in the browser stops mis-parsing them as local. (3) A new `window.AgnesTime` helper (`app/web/static/js/datetime.js`) hydrates `<time datetime="...">` tags client-side and replaces the per-template `fmtDate` slice helpers across `admin_users.html`, `admin_groups.html`, `admin_marketplaces.html`, and their detail pages. The UTC label remains as the no-JS fallback and tooltip. No DuckDB schema migration — deferred until the parallel Postgres migration lands.
```

- [ ] **Step 2: Run full test suite one more time**

```
.venv/bin/pytest tests/ --tb=short -n auto -q
```
Expected: green.

- [ ] **Step 3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): note frontend timezone fix"
git push -u origin vr/timezonesfix
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "fix(web): render frontend timestamps in analyst's local timezone" --body "$(cat <<'EOF'
## Summary

- DuckDB session timezone pinned to UTC on every connection (`src.db._open_duckdb` helper) so naive `TIMESTAMP` reads are UTC-clock regardless of host tz.
- FastAPI `AgnesJSONResponse` labels naive datetimes with an explicit UTC offset on the wire, so JS `new Date()` stops misinterpreting them as local.
- `window.AgnesTime` JS helper hydrates `<time datetime>` tags to local tz; replaces broken `fmtDate` slice helpers across admin tables; UTC label stays as no-JS fallback + tooltip.

## Scope

No DuckDB schema migration — deferred until parallel Postgres migration lands. Legacy rows written on non-UTC hosts before this branch may be off by their host's UTC offset; new writes are correct.

## Test plan

- [ ] `.venv/bin/pytest tests/test_duckdb_session_tz.py -v` — green
- [ ] `.venv/bin/pytest tests/test_datetime_serialization.py -v` — green
- [ ] `.venv/bin/pytest tests/ --tb=short -n auto -q` — full suite green
- [ ] Manual: load `/admin/users`, `/admin/groups`, `/admin/marketplaces`, `/admin/welcome` in a non-UTC tz; hover a timestamp; confirm tooltip = UTC, displayed text = local.
- [ ] Manual: disable JS in DevTools; confirm UTC fallback still renders.

## Spec

`docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`
EOF
)"
```

---

## Self-review (run after the plan is complete, before execution)

- **Spec coverage:** Piece 0 ↔ Task 1+2, Piece 1 ↔ Task 4+5, Piece 2 ↔ Task 6, Pattern A ↔ Task 7, Pattern B ↔ Task 8, mixed templates ↔ Task 9. Health.py update ↔ Task 3. Validation suite ↔ Tasks 1/4/5 + manual smoke in 7/8/9.
- **Placeholder scan:** None. Every code step contains the actual code.
- **Type consistency:** `_open_duckdb` signature (`path, **kwargs`) used identically across Tasks 1, 2. `window.AgnesTime.formatDateTime` / `formatDate` / `formatRelative` / `hydrateTimes` referenced consistently in Tasks 6/8/9. `AgnesJSONResponse` referenced identically in Task 4 wiring and Task 5 test.
- **Out-of-scope discipline:** No DuckDB schema change. No tz user-preference UI. No log-line strftime rewrites. Matches the spec's "Out of scope" section.
