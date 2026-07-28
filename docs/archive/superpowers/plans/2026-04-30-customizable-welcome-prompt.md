# Customizable Welcome Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the analyst-bootstrap CLAUDE.md ("welcome prompt") customizable per Agnes instance via admin UI, while keeping a sensible vendor-agnostic default that ships with OSS. Server renders the template with Jinja2 against a vetted context (instance config, registered tables, marketplaces filtered by caller's RBAC, user identity).

**Architecture:**
- Default template stays at `config/claude_md_template.txt`, converted to Jinja2 syntax (`{{ name }}`). It is the seed and the fallback when no admin override exists.
- Override stored in `system.duckdb` as a single-row `welcome_template` table (schema bump v14 → v15). `NULL` content means "use shipped default".
- New module `src/welcome_template.py` resolves the active template (DB override or file) and renders it via `jinja2.Environment(undefined=StrictUndefined)` against a dataclass-built context.
- New endpoint `GET /api/welcome` (auth-required) returns the rendered markdown for the calling user — context includes RBAC-filtered marketplaces, user groups, etc.
- Admin endpoints `GET /PUT /DELETE /api/admin/welcome-template` manage the raw template; admin UI page `/admin/welcome` provides a textarea editor with a placeholder cheatsheet.
- CLI `da analyst setup` fetches the rendered markdown from `/api/welcome` instead of doing local `str.replace`. Falls back to embedded minimal template on 404 (older servers).
- Pre-existing bug fixed in passing: `_get_instance_name` calls `/api/health` expecting `instance_name`, but `/api/health` only returns `{"status": "ok"}`. The new `/api/welcome` flow makes that call obsolete; the helper is deleted.

**Tech Stack:** FastAPI, DuckDB, Jinja2 (already in `pyproject.toml`), Typer (CLI), pytest.

**Depends on:** Schema v14 already shipped; this builds v15 on top.

---

## File Structure

**Created:**
- `src/repositories/welcome_template.py` — DB CRUD for the override row (~50 LoC).
- `src/welcome_template.py` — context builder + renderer (~120 LoC).
- `app/api/welcome.py` — `GET /api/welcome` + admin CRUD router (~110 LoC).
- `app/web/templates/admin_welcome.html` — admin editor page.
- `tests/test_welcome_template_renderer.py` — renderer unit tests.
- `tests/test_welcome_template_api.py` — endpoint tests.
- `tests/test_welcome_template_migration.py` — v14→v15 migration test.
- `docs/welcome-template.md` — operator-facing reference (placeholders, examples).

**Modified:**
- `config/claude_md_template.txt` — convert `{name}` → `{{ name }}`, expand to use new placeholders.
- `src/db.py` — bump `SCHEMA_VERSION = 15`, add table to `_SYSTEM_SCHEMA`, add `_V14_TO_V15_MIGRATIONS`, wire it into the migration ladder.
- `app/instance_config.py` — add `get_sync_interval()` helper reading `instance.sync_interval` with default `"1 hour"`.
- `config/instance.yaml.example` — add commented `sync_interval` example under `instance:`.
- `app/main.py` — `app.include_router(welcome_router)`.
- `app/web/router.py` — add `/admin/welcome` GET handler.
- `cli/commands/analyst.py` — replace `_generate_claude_md` body, drop `_get_instance_name`, drop `--sync-interval` CLI flag (server now owns it).
- `CHANGELOG.md` — `[Unreleased]` Added entry.

---

## Task 1: DB schema migration v14 → v15

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_welcome_template_migration.py`

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_welcome_template_migration.py`:

```python
"""v14 → v15 migration: adds welcome_template singleton table."""

from pathlib import Path

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _open(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def test_v15_creates_welcome_template_table(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    # Pretend we're on v14: write a v14-shaped DB by running schema then
    # rolling the version row back.
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 14")
    conn.execute("DROP TABLE IF EXISTS welcome_template")
    conn.close()

    # Re-open: migration ladder runs.
    conn = _open(db_path)
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    # Singleton row must exist with NULL content (= use shipped default).
    rows = conn.execute(
        "SELECT id, content, updated_at, updated_by FROM welcome_template"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1  # singleton id
    assert rows[0][1] is None  # NULL = default
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_welcome_template_migration.py -v`
Expected: FAIL — `welcome_template` table does not exist.

- [ ] **Step 3: Add table to `_SYSTEM_SCHEMA` and bump version**

In `src/db.py`, change `SCHEMA_VERSION = 14` to:

```python
SCHEMA_VERSION = 15
```

Append to `_SYSTEM_SCHEMA` (the big string near the top):

```sql
-- v15: customizable analyst-bootstrap welcome prompt.
-- Singleton row (id=1). NULL content means "use the default template
-- shipped at config/claude_md_template.txt"; admin-edited override
-- stores the raw Jinja2 source string.
CREATE TABLE IF NOT EXISTS welcome_template (
    id INTEGER PRIMARY KEY DEFAULT 1,
    content TEXT,
    updated_at TIMESTAMP,
    updated_by VARCHAR,
    CONSTRAINT singleton CHECK (id = 1)
);
```

- [ ] **Step 4: Add migration ladder entry**

In `src/db.py`, add a module-level constant near the other `_VN_TO_VN1_MIGRATIONS`:

```python
_V14_TO_V15_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS welcome_template (
        id INTEGER PRIMARY KEY DEFAULT 1,
        content TEXT,
        updated_at TIMESTAMP,
        updated_by VARCHAR,
        CONSTRAINT singleton CHECK (id = 1)
    )""",
    "INSERT INTO welcome_template (id, content) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
]
```

In the migration ladder inside `_ensure_schema` (the block starting `if current < 14:`), append after the v14 branch:

```python
            if current < 15:
                for sql in _V14_TO_V15_MIGRATIONS:
                    conn.execute(sql)
```

Also seed the singleton on fresh installs. In `_ensure_schema`, after the existing `INSERT INTO schema_version (version) VALUES (?)` for `current == 0`, add right below it (still inside the `if current == 0:` block):

```python
            conn.execute(
                "INSERT INTO welcome_template (id, content) VALUES (1, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_welcome_template_migration.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full DB-related test suite to check for regressions**

Run: `pytest tests/test_db.py tests/test_db_migrations.py -v 2>&1 | tail -30`
Expected: all green (or only pre-existing skips).

- [ ] **Step 7: Commit**

```bash
git add src/db.py tests/test_welcome_template_migration.py
git commit -m "feat(db): schema v15 — welcome_template singleton table"
```

---

## Task 2: WelcomeTemplateRepository

**Files:**
- Create: `src/repositories/welcome_template.py`
- Test: extend `tests/test_welcome_template_migration.py` (or new file `tests/test_welcome_template_repo.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_welcome_template_repo.py`:

```python
"""Unit tests for WelcomeTemplateRepository."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def test_get_returns_none_on_fresh_install(conn):
    repo = WelcomeTemplateRepository(conn)
    row = repo.get()
    assert row is not None
    assert row["content"] is None  # default sentinel


def test_set_stores_content(conn):
    repo = WelcomeTemplateRepository(conn)
    repo.set("Hello {{ instance.name }}", updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] == "Hello {{ instance.name }}"
    assert row["updated_by"] == "admin@example.com"
    assert row["updated_at"] is not None


def test_reset_clears_content(conn):
    repo = WelcomeTemplateRepository(conn)
    repo.set("custom", updated_by="admin@example.com")
    repo.reset(updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_welcome_template_repo.py -v`
Expected: FAIL — `WelcomeTemplateRepository` not importable.

- [ ] **Step 3: Implement the repository**

Create `src/repositories/welcome_template.py`:

```python
"""Repository for the per-instance welcome-prompt override (singleton row)."""

from datetime import datetime, timezone
from typing import Any, Optional

import duckdb


class WelcomeTemplateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self) -> dict[str, Any]:
        """Return the singleton row. Always exists post-migration; content
        is None when no override is set (= use shipped default)."""
        row = self.conn.execute(
            "SELECT id, content, updated_at, updated_by FROM welcome_template WHERE id = 1"
        ).fetchone()
        if row is None:
            # Defensive: re-seed if a previous admin manually deleted it.
            self.conn.execute(
                "INSERT INTO welcome_template (id, content) VALUES (1, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            )
            return {"id": 1, "content": None, "updated_at": None, "updated_by": None}
        return {
            "id": row[0],
            "content": row[1],
            "updated_at": row[2],
            "updated_by": row[3],
        }

    def set(self, content: str, *, updated_by: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO welcome_template (id, content, updated_at, updated_by)
               VALUES (1, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   content = excluded.content,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            [content, now, updated_by],
        )

    def reset(self, *, updated_by: str) -> None:
        """Clear override; renderer falls back to shipped default."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE welcome_template
               SET content = NULL, updated_at = ?, updated_by = ?
               WHERE id = 1""",
            [now, updated_by],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_welcome_template_repo.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/welcome_template.py tests/test_welcome_template_repo.py
git commit -m "feat(repo): WelcomeTemplateRepository singleton CRUD"
```

---

## Task 3: Convert default template to Jinja2 + add `sync_interval` instance config

**Files:**
- Modify: `config/claude_md_template.txt`
- Modify: `app/instance_config.py`
- Modify: `config/instance.yaml.example`

- [ ] **Step 1: Add `get_sync_interval` to `app/instance_config.py`**

After `get_instance_subtitle` in `app/instance_config.py`, append:

```python
def get_sync_interval() -> str:
    """Human-readable refresh cadence shown in the analyst welcome prompt."""
    return get_value("instance", "sync_interval", default="1 hour")
```

- [ ] **Step 2: Document `sync_interval` in `config/instance.yaml.example`**

Find the `instance:` block (lines 9-15) and append a new commented line so it reads:

```yaml
# --- Instance branding ---
instance:
  name: "AI Data Analyst"
  subtitle: "Your Organization"
  copyright: "Your Organization"
  # logo_svg: Full <svg> element for header logo (optional, default: Keboola logo)
  # Example: '<svg width="120" height="30" viewBox="0 0 100 30" xmlns="http://www.w3.org/2000/svg"><text y="22" font-size="24" fill="#333">Logo</text></svg>'
  # sync_interval: "1 hour"          # Cadence shown in analyst CLAUDE.md (e.g., "1 hour", "30 minutes", "daily")
```

- [ ] **Step 3: Rewrite `config/claude_md_template.txt` in Jinja2 syntax**

Replace the entire contents with:

```
{# Default analyst-onboarding welcome prompt for "da analyst setup".
   Rendered server-side by src/welcome_template.py. Edit this file to change
   the OSS default; admins override per-instance via /admin/welcome.

   Available context (see docs/welcome-template.md for the full reference):
     instance.name, instance.subtitle
     server.url, server.hostname
     sync_interval                — string from instance.yaml
     data_source.type             — keboola | bigquery | local
     tables                       — list of {name, description, query_mode}
     metrics.count, metrics.categories
     marketplaces                 — list of {slug, name, plugins:[name]}
     user.email, user.name, user.is_admin, user.groups
     now, today                   — datetime / date string
#}
# {{ instance.name }} — AI Data Analyst

This workspace is connected to {{ server.url }}.
{% if instance.subtitle %}Operated by **{{ instance.subtitle }}**.{% endif %}

## Rules
- Before computing any business metric: run `da metrics show <category>/<name>`
- For current schema: read `data/metadata/schema.json`
- Do not use DESCRIBE/SHOW COLUMNS — read metadata files instead
- Save work output to `user/artifacts/`
- Sync data regularly with `da sync`

## Metrics Workflow
1. `da metrics list` — find the relevant metric ({{ metrics.count }} available, categories: {{ metrics.categories | join(", ") or "none yet" }})
2. `da metrics show <category>/<name>` — read SQL and business rules
3. Use the canonical SQL from the metric definition, adapt to the question
4. Never invent metric calculations — always check existing definitions first

## Data Sync
- `da sync` — download current data from server
- `da sync --docs-only` — just metadata and metrics (fast refresh)
- `da sync --upload-only` — upload sessions and local notes to server
- Data on the server refreshes every {{ sync_interval }}

## Available Datasets
{% for t in tables -%}
- `{{ t.name }}`{% if t.description %} — {{ t.description }}{% endif %}{% if t.query_mode == "remote" %} *(remote, queried on demand)*{% endif %}
{% else -%}
- _No tables registered yet — ask an admin to register tables in the dashboard._
{% endfor %}

{% if marketplaces -%}
## Plugins available to you
{% for mp in marketplaces -%}
- **{{ mp.name }}** ({{ mp.slug }}): {{ mp.plugins | map(attribute="name") | join(", ") }}
{% endfor %}
{% endif -%}

## Directory Structure
- `data/` — read-only data downloaded from server
  - `data/parquet/` — table data in Parquet format
  - `data/duckdb/` — local analytics DuckDB database
  - `data/metadata/` — profiles, schema, metrics cache
- `user/` — your workspace (persistent across syncs)
  - `user/artifacts/` — analysis outputs, reports, charts
  - `user/sessions/` — Claude Code session logs
- `.claude/CLAUDE.local.md` — your personal notes (never overwritten, uploaded on sync)

_Hello {{ user.name or user.email }} — generated {{ today }}._
```

- [ ] **Step 4: Verify the file is valid UTF-8 and renders structurally**

Run: `python -c "from jinja2 import Environment, StrictUndefined; Environment(undefined=StrictUndefined).parse(open('config/claude_md_template.txt').read())"`
Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add config/claude_md_template.txt app/instance_config.py config/instance.yaml.example
git commit -m "feat(config): default welcome template in jinja2 + sync_interval"
```

---

## Task 4: Renderer module (`src/welcome_template.py`)

**Files:**
- Create: `src/welcome_template.py`
- Test: `tests/test_welcome_template_renderer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_welcome_template_renderer.py`:

```python
"""Unit tests for the welcome-prompt renderer."""

from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import build_context, render_welcome


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def _user(email="alice@example.com"):
    return {"id": "u1", "email": email, "name": "Alice", "is_admin": False, "groups": ["Everyone"]}


def test_renders_default_when_no_override(conn):
    out = render_welcome(conn, user=_user(), server_url="https://example.com")
    assert "AI Data Analyst" in out
    assert "https://example.com" in out
    assert "Alice" in out


def test_renders_override(conn):
    WelcomeTemplateRepository(conn).set(
        "# {{ instance.name }} for {{ user.email }}",
        updated_by="admin@example.com",
    )
    out = render_welcome(conn, user=_user(), server_url="https://example.com")
    assert out.startswith("# AI Data Analyst for alice@example.com")


def test_strict_undefined_raises_on_missing_placeholder(conn):
    WelcomeTemplateRepository(conn).set(
        "{{ does_not_exist }}", updated_by="admin@example.com"
    )
    with pytest.raises(Exception) as exc_info:
        render_welcome(conn, user=_user(), server_url="https://example.com")
    assert "does_not_exist" in str(exc_info.value)


def test_context_exposes_documented_keys(conn):
    ctx = build_context(conn, user=_user(), server_url="https://example.com")
    for top in ("instance", "server", "sync_interval", "data_source",
                "tables", "metrics", "marketplaces", "user", "now", "today"):
        assert top in ctx, f"missing top-level key: {top}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_welcome_template_renderer.py -v`
Expected: FAIL — `src.welcome_template` not importable.

- [ ] **Step 3: Implement the renderer**

Create `src/welcome_template.py`:

```python
"""Render the analyst-onboarding welcome prompt (CLAUDE.md).

Two layers:
  1. Template source — admin override from welcome_template.content,
     or the shipped default at config/claude_md_template.txt.
  2. Render context — built from instance config, table_registry,
     metric_definitions, and the calling user's RBAC-filtered marketplaces.

The Jinja2 environment uses StrictUndefined so that any typo in the
template raises immediately rather than rendering empty strings.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import Environment, StrictUndefined
from urllib.parse import urlparse

from app.instance_config import (
    get_data_source_type,
    get_instance_name,
    get_instance_subtitle,
    get_sync_interval,
)
from src.marketplace_filter import resolve_allowed_plugins
from src.repositories.welcome_template import WelcomeTemplateRepository

_DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "claude_md_template.txt"
)


def _load_default_template() -> str:
    if _DEFAULT_TEMPLATE_PATH.exists():
        return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    # Last-resort embedded fallback if the OSS template file is missing
    # from the install (e.g., partial Docker COPY).
    return (
        "# {{ instance.name }} — AI Data Analyst\n\n"
        "This workspace is connected to {{ server.url }}.\n"
        "Data refreshes every {{ sync_interval }}.\n"
    )


def _list_tables(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT name, description, query_mode
           FROM table_registry
           ORDER BY name"""
    ).fetchall()
    return [
        {"name": r[0], "description": r[1] or "", "query_mode": r[2] or "local"}
        for r in rows
    ]


def _metrics_summary(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM metric_definitions GROUP BY category"
        ).fetchall()
    except duckdb.CatalogException:
        return {"count": 0, "categories": []}
    return {
        "count": sum(r[1] for r in rows),
        "categories": sorted({r[0] for r in rows if r[0]}),
    }


def _marketplaces_for_user(
    conn: duckdb.DuckDBPyConnection, user_id: str
) -> list[dict[str, Any]]:
    """Return marketplaces with the plugins the user is allowed to see."""
    allowed = resolve_allowed_plugins(conn, user_id)  # set[str] of "<slug>/<plugin>"
    if not allowed:
        return []
    rows = conn.execute(
        """SELECT mr.id, mr.slug, mr.name, mp.name
           FROM marketplace_registry mr
           JOIN marketplace_plugins mp ON mp.marketplace_id = mr.id
           ORDER BY mr.slug, mp.name"""
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for mp_id, slug, mp_name, plugin_name in rows:
        key = f"{slug}/{plugin_name}"
        if key not in allowed:
            continue
        bucket = grouped.setdefault(
            slug, {"slug": slug, "name": mp_name, "plugins": []}
        )
        bucket["plugins"].append({"name": plugin_name})
    return list(grouped.values())


def build_context(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any],
    server_url: str,
) -> dict[str, Any]:
    """Compose the Jinja2 render context. Pure, no side effects."""
    now = datetime.now(timezone.utc)
    parsed = urlparse(server_url)
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "sync_interval": get_sync_interval(),
        "data_source": {"type": get_data_source_type()},
        "tables": _list_tables(conn),
        "metrics": _metrics_summary(conn),
        "marketplaces": _marketplaces_for_user(conn, user.get("id", "")),
        "user": {
            "id": user.get("id", ""),
            "email": user.get("email", ""),
            "name": user.get("name") or "",
            "is_admin": bool(user.get("is_admin")),
            "groups": user.get("groups") or [],
        },
        "now": now,
        "today": date.today().isoformat(),
    }


def _resolve_template_source(conn: duckdb.DuckDBPyConnection) -> str:
    row = WelcomeTemplateRepository(conn).get()
    return row["content"] if row.get("content") else _load_default_template()


def render_welcome(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any],
    server_url: str,
) -> str:
    """Resolve the active template and render it for the given user."""
    source = _resolve_template_source(conn)
    env = Environment(undefined=StrictUndefined, autoescape=False)
    template = env.from_string(source)
    return template.render(**build_context(conn, user=user, server_url=server_url))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_welcome_template_renderer.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/welcome_template.py tests/test_welcome_template_renderer.py
git commit -m "feat: server-side jinja2 renderer for welcome prompt"
```

---

## Task 5: REST endpoints (`/api/welcome` + admin CRUD)

**Files:**
- Create: `app/api/welcome.py`
- Test: `tests/test_welcome_template_api.py`

- [ ] **Step 1: Write the failing endpoint tests**

Create `tests/test_welcome_template_api.py`:

```python
"""End-to-end tests for /api/welcome and /api/admin/welcome-template."""

from fastapi.testclient import TestClient

# Existing helpers in tests/helpers/ provide an authenticated client +
# admin client. Mirror the style used by tests/test_marketplaces_api.py.
from tests.helpers.auth import client_for_user, client_for_admin


def test_get_welcome_returns_rendered_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = client_for_user(email="alice@example.com")
    resp = client.get("/api/welcome", params={"server_url": "https://example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert "content" in body
    assert "AI Data Analyst" in body["content"]
    assert "https://example.com" in body["content"]


def test_get_welcome_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    resp = TestClient(app).get("/api/welcome", params={"server_url": "https://example.com"})
    assert resp.status_code == 401


def test_admin_can_set_and_reset_template(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = client_for_admin()

    # GET initial state
    r = client.get("/api/admin/welcome-template")
    assert r.status_code == 200
    assert r.json()["content"] is None
    assert r.json()["default"].startswith("{# Default")

    # PUT override
    r = client.put(
        "/api/admin/welcome-template",
        json={"content": "Hello {{ user.email }}"},
    )
    assert r.status_code == 200

    # Verify rendered output uses override
    r = client.get("/api/welcome", params={"server_url": "https://example.com"})
    assert r.json()["content"].startswith("Hello ")

    # DELETE = reset
    r = client.delete("/api/admin/welcome-template")
    assert r.status_code == 204
    r = client.get("/api/admin/welcome-template")
    assert r.json()["content"] is None


def test_non_admin_cannot_edit_template(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = client_for_user(email="alice@example.com")
    r = client.put("/api/admin/welcome-template", json={"content": "x"})
    assert r.status_code == 403


def test_invalid_jinja2_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = client_for_admin()
    r = client.put(
        "/api/admin/welcome-template",
        json={"content": "{% for x in y %}"},  # unclosed
    )
    assert r.status_code == 400
    assert "syntax" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_welcome_template_api.py -v`
Expected: FAIL — `/api/welcome` not registered.

- [ ] **Step 3: Implement the router**

Create `app/api/welcome.py`:

```python
"""REST endpoints for the analyst-onboarding welcome prompt.

- GET  /api/welcome                  : render for the calling user (auth required)
- GET  /api/admin/welcome-template   : raw template + shipped default (admin)
- PUT  /api/admin/welcome-template   : set override (admin)
- DELETE /api/admin/welcome-template : reset to default (admin)
"""

from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from jinja2 import TemplateSyntaxError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import _load_default_template, render_welcome


router = APIRouter(tags=["welcome"])


class WelcomeResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]  # None when no override is set
    default: str            # always the shipped default
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TemplatePutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


@router.get("/api/welcome", response_model=WelcomeResponse)
async def get_welcome(
    server_url: str = Query(..., description="The server URL the analyst is bootstrapping against"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render the welcome prompt for the calling user. Returns rendered markdown."""
    try:
        rendered = render_welcome(conn, user=user, server_url=server_url)
    except TemplateSyntaxError as e:
        # Admin-saved a broken override; surface a hint rather than 500.
        raise HTTPException(
            status_code=500,
            detail=f"Welcome template has a syntax error: {e.message}. Reset via /admin/welcome.",
        )
    return WelcomeResponse(content=rendered)


@router.get("/api/admin/welcome-template", response_model=TemplateGetResponse)
async def admin_get_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = WelcomeTemplateRepository(conn).get()
    return TemplateGetResponse(
        content=row["content"],
        default=_load_default_template(),
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
    )


@router.put("/api/admin/welcome-template")
async def admin_put_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Validate Jinja2 syntax up front; reject bad templates with 400.
    from jinja2 import Environment, StrictUndefined
    try:
        Environment(undefined=StrictUndefined).parse(payload.content)
    except TemplateSyntaxError as e:
        raise HTTPException(status_code=400, detail=f"Jinja2 syntax error: {e.message}")
    WelcomeTemplateRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/welcome-template", status_code=204)
async def admin_reset_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    WelcomeTemplateRepository(conn).reset(updated_by=user["email"])
    return Response(status_code=204)
```

- [ ] **Step 4: Register router in `app/main.py`**

In `app/main.py`, alongside the other `app.include_router(...)` calls (around line 310-329), add:

```python
    from app.api.welcome import router as welcome_router
    app.include_router(welcome_router)
```

Also add the import near the top with the other API imports if the file uses top-level imports for routers; otherwise keep the local import (match the existing pattern in that file).

- [ ] **Step 5: Run the API tests**

Run: `pytest tests/test_welcome_template_api.py -v`
Expected: PASS (5 tests). If `tests/helpers/auth` doesn't already expose `client_for_user`/`client_for_admin`, copy the pattern from `tests/test_marketplaces_api.py` (the existing auth-fixture style) into the new test file inline.

- [ ] **Step 6: Run the full test suite for regressions**

Run: `pytest tests/ -x -q 2>&1 | tail -20`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add app/api/welcome.py app/main.py tests/test_welcome_template_api.py
git commit -m "feat(api): /api/welcome + /api/admin/welcome-template endpoints"
```

---

## Task 6: Admin web UI (`/admin/welcome`)

**Files:**
- Create: `app/web/templates/admin_welcome.html`
- Modify: `app/web/router.py`

- [ ] **Step 1: Add the route handler**

In `app/web/router.py`, after the existing `admin_marketplaces_page` handler (around line 676-683), add:

```python
@router.get("/admin/welcome", response_class=HTMLResponse)
async def admin_welcome_page(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.welcome_template import WelcomeTemplateRepository
    from src.welcome_template import _load_default_template

    row = WelcomeTemplateRepository(conn).get()
    ctx = {
        "request": request,
        "user": user,
        "current": row["content"] or "",
        "default_template": _load_default_template(),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "is_override": row["content"] is not None,
    }
    return templates.TemplateResponse(request, "admin_welcome.html", ctx)
```

If `require_admin`, `_get_db`, or `duckdb` are not already imported in this file, add the imports following the surrounding admin handlers' style (grep `app/web/router.py` for `require_admin` to confirm).

- [ ] **Step 2: Create the template**

Create `app/web/templates/admin_welcome.html`:

```html
{% extends "base.html" %}
{% block title %}Welcome Prompt — Admin{% endblock %}
{% block content %}
<div class="admin-page">
  <h1>Analyst Welcome Prompt</h1>
  <p class="muted">
    This is the CLAUDE.md generated for analysts when they run
    <code>da analyst setup</code>. Edit it to customize the onboarding
    instructions for this instance. Leave empty (or click <em>Reset to default</em>)
    to use the OSS-shipped default.
  </p>

  {% if is_override %}
    <p class="status">
      Overridden by <strong>{{ updated_by }}</strong> on
      {{ updated_at.strftime("%Y-%m-%d %H:%M UTC") if updated_at else "—" }}.
    </p>
  {% else %}
    <p class="status">Using shipped default.</p>
  {% endif %}

  <h2>Available placeholders</h2>
  <pre class="placeholder-cheatsheet">
{{ "{{ instance.name }}" }}                 — instance display name
{{ "{{ instance.subtitle }}" }}             — operator name
{{ "{{ server.url }}" }}                    — full server URL
{{ "{{ server.hostname }}" }}               — host part
{{ "{{ sync_interval }}" }}                 — refresh cadence (instance.yaml)
{{ "{{ data_source.type }}" }}              — keboola | bigquery | local
{{ "{{ tables }}" }}                        — list of {name, description, query_mode}
{{ "{{ metrics.count }}" }}, {{ "{{ metrics.categories }}" }}
{{ "{{ marketplaces }}" }}                  — RBAC-filtered list of {slug, name, plugins[]}
{{ "{{ user.email }}" }}, {{ "{{ user.name }}" }}, {{ "{{ user.is_admin }}" }}, {{ "{{ user.groups }}" }}
{{ "{{ now }}" }}, {{ "{{ today }}" }}
  </pre>

  <form id="welcome-form" onsubmit="return false">
    <textarea id="content" rows="30" cols="100">{{ current or default_template }}</textarea>
    <div class="actions">
      <button type="button" id="save-btn">Save override</button>
      <button type="button" id="reset-btn" class="secondary">Reset to default</button>
      <button type="button" id="preview-btn" class="secondary">Preview</button>
    </div>
    <div id="result" class="result"></div>
    <pre id="preview" class="preview" hidden></pre>
  </form>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const result = $("result");

  $("save-btn").addEventListener("click", async () => {
    result.textContent = "Saving…";
    const r = await fetch("/api/admin/welcome-template", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content: $("content").value}),
    });
    if (r.ok) {
      result.textContent = "Saved.";
    } else {
      const err = await r.json();
      result.textContent = "Error: " + (err.detail || r.statusText);
    }
  });

  $("reset-btn").addEventListener("click", async () => {
    if (!confirm("Reset to OSS default? Your override will be lost.")) return;
    const r = await fetch("/api/admin/welcome-template", {method: "DELETE"});
    if (r.ok) {
      result.textContent = "Reset. Reload to see the default.";
    } else {
      result.textContent = "Error: " + r.statusText;
    }
  });

  $("preview-btn").addEventListener("click", async () => {
    // Render against the calling admin's identity, with a placeholder URL.
    const r = await fetch("/api/welcome?server_url=" + encodeURIComponent(window.location.origin));
    if (r.ok) {
      const j = await r.json();
      $("preview").textContent = j.content;
      $("preview").hidden = false;
    } else {
      const err = await r.json();
      result.textContent = "Render error: " + (err.detail || r.statusText);
    }
  });
</script>
{% endblock %}
```

- [ ] **Step 3: Add a nav entry**

Search for where `admin_marketplaces` is linked in the existing admin nav (likely `app/web/templates/base.html` or `_app_header.html`). Add a sibling link `<a href="/admin/welcome">Welcome Prompt</a>` under the same admin-only menu block.

```bash
grep -nE 'admin_marketplaces|admin/marketplaces' app/web/templates/*.html
```

Open the matched file and insert next to the existing admin-marketplaces link, copying the surrounding markup exactly.

- [ ] **Step 4: Smoke test the page**

```bash
uvicorn app.main:app --reload &
SERVER_PID=$!
sleep 2
# Log in as admin in your browser, navigate to /admin/welcome — verify
# the textarea loads with the default template and Save / Reset / Preview
# all return success.
kill $SERVER_PID
```

If you can't run a browser interactively, at least confirm the page returns 200 for an admin and 403 for a non-admin with `curl` against an authenticated session cookie.

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py app/web/templates/admin_welcome.html app/web/templates/base.html
git commit -m "feat(web): /admin/welcome editor page"
```

---

## Task 7: CLI `da analyst setup` fetches rendered template from server

**Files:**
- Modify: `cli/commands/analyst.py`
- Test: `tests/test_cli_analyst_welcome.py`

- [ ] **Step 1: Write the failing CLI test**

Create `tests/test_cli_analyst_welcome.py`:

```python
"""Integration tests for da analyst setup → /api/welcome wiring."""

from pathlib import Path

import httpx
import pytest

from cli.commands.analyst import _generate_claude_md


class _MockClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        body, status = self._responses.get(url, ({}, 404))
        return httpx.Response(status_code=status, json=body, request=httpx.Request("GET", url))


def test_generate_claude_md_uses_server_render(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    rendered = "# CUSTOM\n\nFrom server.\n"
    mock = _MockClient({
        "https://example.com/api/welcome?server_url=https%3A%2F%2Fexample.com": (
            {"content": rendered}, 200
        ),
    })
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == rendered


def test_generate_claude_md_falls_back_on_404(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    mock = _MockClient({})  # everything 404s
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    body = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    assert "AI Data Analyst" in body  # embedded fallback contains this string
    assert "https://example.com" in body
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_cli_analyst_welcome.py -v`
Expected: FAIL — current `_generate_claude_md` signature is `(workspace, instance_name, server_url, sync_interval)`, not `(workspace, server_url, token)`.

- [ ] **Step 3: Rewrite `_generate_claude_md`, drop `_get_instance_name`, drop `--sync-interval`**

In `cli/commands/analyst.py`:

Replace the `_get_instance_name` function (lines 255-274) with a deletion (the function is no longer needed — server renders everything).

Replace the entire `_generate_claude_md` function (lines 281-323) with:

```python
def _generate_claude_md(workspace: Path, server_url: str, token: str) -> None:
    """Fetch the rendered welcome prompt from the server and write CLAUDE.md.

    Falls back to a minimal embedded template if the server endpoint is
    unavailable (e.g., older server versions before /api/welcome shipped).
    """
    import httpx
    from urllib.parse import quote

    server_url = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{server_url}/api/welcome?server_url={quote(server_url, safe='')}"

    rendered: str | None = None
    try:
        resp = httpx.get(url, headers=headers, timeout=15.0)
        if resp.status_code == 200:
            rendered = resp.json().get("content")
    except Exception:
        pass

    if rendered is None:
        # Fallback for older servers — keeps the CLI usable, just less rich.
        rendered = (
            "# AI Data Analyst\n\n"
            f"This workspace is connected to {server_url}.\n\n"
            "## Rules\n"
            "- Before computing any business metric: run `da metrics show <category>/<name>`\n"
            "- Save work output to `user/artifacts/`\n"
            "- Sync data regularly with `da sync`\n"
        )

    (workspace / "CLAUDE.md").write_text(rendered, encoding="utf-8")

    local_md = workspace / ".claude" / "CLAUDE.local.md"
    if not local_md.exists():
        local_md.write_text(
            "# My Notes\n\n"
            "Personal notes for this workspace. Uploaded to the server on `da sync --upload-only`.\n",
            encoding="utf-8",
        )

    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        settings = {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Grep", "Glob"]}}
        settings_path.write_text(json.dumps(settings, indent=2))
```

In the `setup` command (around line 353-394):

- Drop the `sync_interval` parameter from the function signature.
- Replace the call site at line 393-394:

```python
    # 7. Generate CLAUDE.md (rendered server-side)
    typer.echo("Fetching welcome prompt from server...")
    _generate_claude_md(workspace, server_url, token)
```

- Drop the `instance_name = _get_instance_name(...)` call at line 393.
- In the summary block (line 397-406), replace `f"  Instance : {instance_name}"` with a server-only line:

```python
    typer.echo(f"  Server   : {server_url}")
    typer.echo(f"  Tables   : {n_downloaded} downloaded, {total_rows} total rows")
    typer.echo(f"  Workspace: {workspace}")
```

- [ ] **Step 4: Run the CLI tests**

Run: `pytest tests/test_cli_analyst_welcome.py tests/test_cli.py tests/test_analyst_bootstrap.py -v`
Expected: PASS. Existing analyst-bootstrap tests may have hard-coded `sync_interval` arguments; update them to call the new signature (or remove the arg).

If existing tests reference `_get_instance_name`, delete those test cases — the helper is gone.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/analyst.py tests/test_cli_analyst_welcome.py tests/test_cli.py tests/test_analyst_bootstrap.py
git commit -m "feat(cli): da analyst setup fetches rendered welcome from /api/welcome"
```

---

## Task 8: Operator-facing docs

**Files:**
- Create: `docs/welcome-template.md`

- [ ] **Step 1: Write the doc**

Create `docs/welcome-template.md`:

```markdown
# Welcome prompt customization

The welcome prompt is the `CLAUDE.md` file generated in an analyst's local
workspace by `da analyst setup`. It instructs Claude Code on how to behave in
that workspace — which commands to use, where to read schema metadata, what
metrics exist, what plugins are available.

## Defaults

The OSS distribution ships a generic welcome prompt at
`config/claude_md_template.txt`. Every Agnes instance starts with this default;
no admin action is required.

## Customizing per instance

Admins can override the template via:

- **Admin UI:** `/admin/welcome` — textarea editor with placeholder cheatsheet
  and live preview button. Save sends a `PUT` to `/api/admin/welcome-template`.
- **REST API:**
  - `GET /api/admin/welcome-template` — returns `{content, default, updated_at, updated_by}`. `content` is `null` when no override is set.
  - `PUT /api/admin/welcome-template` with body `{"content": "..."}` — validates Jinja2 syntax, stores the override.
  - `DELETE /api/admin/welcome-template` — clears the override; renderer falls back to the shipped default.

The override lives in `system.duckdb` (table `welcome_template`, singleton
row id=1). Resetting via the UI or `DELETE` simply NULL-s `content` — the
audit trail (`updated_at`, `updated_by`) is preserved.

## Template language

[Jinja2](https://jinja.palletsprojects.com/) with `StrictUndefined`. Any
typo in a placeholder name raises an error at render time rather than
silently emitting an empty string. Server returns HTTP 500 with a hint
pointing at `/admin/welcome`; the admin UI rejects syntax errors with HTTP
400 on save.

## Available placeholders

| Placeholder | Type | Source |
|---|---|---|
| `instance.name` | string | `instance.name` in `instance.yaml` |
| `instance.subtitle` | string | `instance.subtitle` in `instance.yaml` |
| `server.url` | string | passed by the CLI (`?server_url=` query) |
| `server.hostname` | string | parsed from `server.url` |
| `sync_interval` | string | `instance.sync_interval` in `instance.yaml` (default `"1 hour"`) |
| `data_source.type` | string | `keboola` \| `bigquery` \| `local` |
| `tables` | list | rows from `table_registry`, each `{name, description, query_mode}` |
| `metrics.count` | int | total rows in `metric_definitions` |
| `metrics.categories` | list[str] | distinct categories from `metric_definitions` |
| `marketplaces` | list | RBAC-filtered for the calling user, each `{slug, name, plugins:[{name}]}` |
| `user.email` | string | calling user |
| `user.name` | string | calling user |
| `user.is_admin` | bool | calling user |
| `user.groups` | list[str] | calling user's group names |
| `now` | datetime (UTC) | server time at render |
| `today` | string (`YYYY-MM-DD`) | server date |

## RBAC

`marketplaces` is filtered through `src.marketplace_filter.resolve_allowed_plugins`
— the same logic that gates `/marketplace.zip`. Two analysts with different
group memberships will see different plugin lists in their `CLAUDE.md`.

## Example: minimal override

```jinja2
# {{ instance.name }}

This workspace is connected to {{ server.url }}.
You have access to {{ tables | length }} dataset(s):
{% for t in tables %}
- `{{ t.name }}`{% if t.description %}: {{ t.description }}{% endif %}
{%- endfor %}
```

## Falling back to the default

Click **Reset to default** in the admin UI or `DELETE
/api/admin/welcome-template`. The shipped default is always available as
`response.default` in the GET endpoint, so admins can copy-paste it into
the editor as a starting point for a new override.
```

- [ ] **Step 2: Commit**

```bash
git add docs/welcome-template.md
git commit -m "docs: welcome-template customization reference"
```

---

## Task 9: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the Unreleased entry**

Open `CHANGELOG.md`. Find the topmost `## [Unreleased]` heading (create one if missing — it sits above the latest released version). Add under `### Added`:

```markdown
- Customizable analyst welcome prompt (`CLAUDE.md` generated by `da analyst setup`). Default ships at `config/claude_md_template.txt` (now Jinja2 syntax). Admins override per instance via `/admin/welcome` or `PUT /api/admin/welcome-template`. New endpoint `GET /api/welcome` returns the rendered prompt for the calling user, with marketplaces filtered by RBAC. See `docs/welcome-template.md` for the full placeholder reference.
- DuckDB schema v15: `welcome_template` singleton table for the per-instance override. Auto-migration v14→v15 on first start.
- New `instance.sync_interval` setting in `instance.yaml` (default `"1 hour"`) — surfaced in the welcome prompt as `{{ sync_interval }}`.
```

Add under `### Changed`:

```markdown
- **BREAKING (CLI):** `da analyst setup` no longer accepts `--sync-interval`. The cadence shown in the analyst CLAUDE.md now comes from the server's `instance.yaml`. Operators who relied on the flag should set `instance.sync_interval` in `instance.yaml` instead.
- `da analyst setup` now fetches `CLAUDE.md` from `GET /api/welcome` instead of substituting placeholders client-side. The CLI keeps a minimal embedded fallback for older servers without the endpoint.
```

Add under `### Fixed`:

```markdown
- Pre-existing bug: `_get_instance_name` in the CLI parsed `instance_name` from `/api/health`, but `/api/health` only ever returned `{"status": "ok"}`, so the configured `instance.name` was never propagated to the analyst's `CLAUDE.md`. The new server-side render path uses `app.instance_config.get_instance_name()` directly.
```

- [ ] **Step 2: Verify changelog format**

Run: `head -40 CHANGELOG.md`
Expected: the new bullets appear under the topmost `## [Unreleased]` heading, in the right `### Added` / `### Changed` / `### Fixed` sections.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): customizable welcome prompt"
```

---

## Final integration sanity check

- [ ] **Step 1: Full test suite**

Run: `pytest tests/ -q 2>&1 | tail -10`
Expected: all green.

- [ ] **Step 2: Manual smoke test of the live flow**

```bash
# 1. Start the server
uvicorn app.main:app --reload &
SERVER_PID=$!
sleep 2

# 2. As admin, GET the raw template
curl -s -H "Authorization: Bearer $ADMIN_PAT" http://localhost:8000/api/admin/welcome-template | jq .

# 3. As any user, GET the rendered welcome
curl -s -H "Authorization: Bearer $USER_PAT" "http://localhost:8000/api/welcome?server_url=http://localhost:8000" | jq -r .content | head -30

# 4. As admin, PUT a custom override
curl -s -X PUT -H "Authorization: Bearer $ADMIN_PAT" -H "Content-Type: application/json" \
  -d '{"content":"# Custom for {{ user.email }}"}' \
  http://localhost:8000/api/admin/welcome-template

# 5. Re-render — should now show the custom content
curl -s -H "Authorization: Bearer $USER_PAT" "http://localhost:8000/api/welcome?server_url=http://localhost:8000" | jq -r .content

# 6. Reset
curl -s -X DELETE -H "Authorization: Bearer $ADMIN_PAT" http://localhost:8000/api/admin/welcome-template

kill $SERVER_PID
```

- [ ] **Step 3: PR-ready check**

Run: `grep -niE 'foundryai|groupon|prj-grp|<private-org>' $(git diff --name-only origin/main..HEAD)`
Expected: no matches (vendor-agnostic OSS hygiene per CLAUDE.md).

- [ ] **Step 4: Open the PR**

Standard branch flow; CHANGELOG already updated. PR title: `feat: customizable analyst welcome prompt (admin UI + Jinja2)`.

---

## Self-review notes

**Spec coverage:**
- ✓ Default standard prompt that ships with OSS — `config/claude_md_template.txt`, used as fallback when DB row is NULL.
- ✓ Per-customer customization — DB-backed override with admin UI.
- ✓ Jinja2 templating — `Environment(undefined=StrictUndefined)`.
- ✓ System placeholders — documented in `docs/welcome-template.md` and the default template's leading comment block.

**Type consistency:**
- `WelcomeTemplateRepository.get` always returns `dict` (defensive re-seed if singleton missing).
- `render_welcome(conn, *, user, server_url) -> str` — keyword-only, used identically in CLI test, API endpoint, and admin web preview path.
- `build_context` is the single source of the placeholder schema; tests assert all top-level keys exist so changes show up immediately.

**Open questions deferred to follow-ups:**
- Per-user-group templates (different welcome for analysts vs. data scientists). Out of scope here; the current `user.groups` placeholder lets template authors do conditional rendering inside one template.
- Versioning / history of overrides (current schema only retains the latest). Add a `welcome_template_history` table later if needed.
- i18n / multiple languages. Punted — fold into a future `welcome_template.locale` column if requested.
