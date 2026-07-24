# Data Apps Platform (Waves 1+2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host user web apps ("data apps") inside Agnes using the upstream `keboolapublic.azurecr.io/data-app-python-js` runtime image — registry, deploy from internally-hosted git repos, RBAC-gated ingress, and auto-sleep/wake.

**Architecture:** A `data_apps` registry (dual-backend repo pair) drives a new `apps-runner` sidecar (the only container with the Docker socket) through a small token-gated HTTP API. Apps are cloned from per-app bare git repos served by Agnes over `git http-backend`, reached through a FastAPI streaming proxy at `/apps/{slug}/` (subdomain mode optional), and put to sleep by a scheduler-driven idle reaper with wake-on-request.

**Tech Stack:** FastAPI, DuckDB + Postgres (dual backend), docker SDK for Python, httpx, git http-backend, Caddy, typer CLI, FastMCP.

**Spec:** `docs/superpowers/specs/2026-07-21-data-apps-design.md` (read §2–§8, §10–§13 before starting). Wave 3 (AI authoring / broker endpoints / skill+templates) is deliberately NOT in this plan — it gets its own plan once this one lands.

## Global Constraints

- Dual-backend discipline: every `src/repositories/X.py` method gets a matching `X_pg.py` method **in the same task**, plus a `tests/db_pg/test_*_contract.py` extension. Reach repos only through factory functions (`data_apps_repo()`), never direct instantiation.
- DuckDB migration ladder (`src/db.py`, `SCHEMA_VERSION = 95` → `96`) and Alembic (`migrations/versions/0043_data_apps_v96.py`) move together in one task.
- Vendor-agnostic public repo: no customer names, project IDs, or internal hostnames in code/docs/commits. The upstream image reference `keboolapublic.azurecr.io/data-app-python-js` is the OSS upstream and is allowed.
- Run the full suite before every push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
- Every user-visible change adds a `CHANGELOG.md` bullet under `## [Unreleased]` (done once, in Task 13).
- No AI attribution in commits. Clean, concise commit messages.
- Web pages extend `base_page.html`/`base_ds.html`, never `base.html`; spread `_chrome_ctx(request, user)`; CSS in `{% block head_extra %}`, `var(--ds-*)` tokens only.
- New `/api/*` routes need CLI and/or MCP coverage (API-coverage ratchet, `tests/` gate) — satisfied by Tasks 9+10.
- Python `>=3.11,<3.14`. New dependency pins go into `[project].dependencies` in `pyproject.toml` with a comment.

## File Structure (what gets created)

```
src/repositories/data_apps.py            # DuckDB repo
src/repositories/data_apps_pg.py         # PG twin
src/db.py                                # v96 step + CREATE TABLE
migrations/versions/0043_data_apps_v96.py
src/data_apps/__init__.py                # domain logic package (importable from app+services)
src/data_apps/spec.py                    # config.json + container-spec builders
src/data_apps/runner_client.py           # httpx client for the sidecar
src/data_apps/git_repos.py               # per-app bare repos + agnes-live ref
services/apps_runner/__init__.py
services/apps_runner/__main__.py         # sidecar entry (uvicorn)
services/apps_runner/api.py              # FastAPI app: up/stop/resume/status/logs
app/api/data_apps.py                     # control-plane REST
app/api/data_apps_proxy.py               # ingress proxy (HTTP + WS) + holding page
app/api/data_apps_git.py                 # /data-apps.git/{slug}/... router
app/data_apps_subdomain.py               # ASGI middleware: Host → /apps/{slug} rewrite
app/web/templates/data_apps.html         # list page
app/web/templates/data_app_detail.html   # detail page
app/web/templates/data_app_waking.html   # holding page
cli/commands/data_apps.py                # `agnes app …`
tests/test_data_apps_repo.py
tests/db_pg/test_data_apps_contract.py
tests/test_apps_runner.py
tests/test_data_apps_spec.py
tests/test_data_apps_git.py
tests/test_data_apps_api.py
tests/test_data_apps_proxy.py
tests/test_data_apps_e2e_docker.py       # docker-marked, opt-in
```

---

### Task 1: `data_apps` registry — schema + dual-backend repo pair

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 95→96, CREATE TABLE, `_v95_to_v96` step, ladder call sites)
- Create: `migrations/versions/0043_data_apps_v96.py`
- Create: `src/repositories/data_apps.py`, `src/repositories/data_apps_pg.py`
- Modify: `src/repositories/__init__.py` (registry entry + factory)
- Test: `tests/test_data_apps_repo.py`, `tests/db_pg/test_data_apps_contract.py`

**Interfaces:**
- Produces: `data_apps_repo()` factory returning a repo with:
  - `create(*, slug: str, name: str, owner_user_id: str, description: str = "", repo_mode: str = "internal", repo_url: str = "", repo_branch: str = "main", idle_timeout_s: int = 1800, sleep_mode: str = "recreate", env: str = "{}") -> str` (returns `app_<uuid12>`; raises constraint error on slug collision)
  - `get(app_id: str) -> Optional[dict]`, `get_by_slug(slug: str) -> Optional[dict]`
  - `list(*, owner_user_id: Optional[str] = None, state: Optional[str] = None, limit: int = 1000) -> List[dict]`
  - `update(app_id: str, **fields) -> bool` (whitelist: name, description, repo_url, repo_branch, runtime_tag, secrets_enc, env, cpu_limit, mem_limit, idle_timeout_s, sleep_mode, service_token_id)
  - `set_state(app_id: str, state: str, detail: str = "") -> None`
  - `record_deploy(app_id: str, sha: str) -> None`
  - `touch_last_request(app_id: str) -> None`
  - `list_idle(older_than_s: int) -> List[dict]` (state='running' AND last_request_at older)
  - `delete(app_id: str) -> bool`

- [ ] **Step 1: Write the failing DuckDB repo test**

`tests/test_data_apps_repo.py`:

```python
import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.data_apps import DataAppsRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return DataAppsRepository(conn)


class TestCreateAndRead:
    def test_create_assigns_app_prefix_id(self, repo):
        aid = repo.create(slug="sales-dash", name="Sales dashboard",
                          owner_user_id="u1")
        assert aid.startswith("app_")
        row = repo.get(aid)
        assert row["slug"] == "sales-dash"
        assert row["state"] == "created"
        assert row["repo_mode"] == "internal"
        assert row["sleep_mode"] == "recreate"

    def test_slug_unique(self, repo):
        repo.create(slug="dup", name="A", owner_user_id="u1")
        with pytest.raises(duckdb.ConstraintException):
            repo.create(slug="dup", name="B", owner_user_id="u2")

    def test_get_by_slug(self, repo):
        aid = repo.create(slug="x", name="X", owner_user_id="u1")
        assert repo.get_by_slug("x")["id"] == aid
        assert repo.get_by_slug("nope") is None


class TestLifecycle:
    def test_state_and_deploy(self, repo):
        aid = repo.create(slug="s", name="S", owner_user_id="u1")
        repo.set_state(aid, "deploying")
        assert repo.get(aid)["state"] == "deploying"
        repo.record_deploy(aid, "abc123")
        row = repo.get(aid)
        assert row["deployed_sha"] == "abc123"
        assert row["last_deploy_at"] is not None

    def test_list_idle(self, repo):
        aid = repo.create(slug="i", name="I", owner_user_id="u1")
        repo.set_state(aid, "running")
        repo.conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [aid])
        assert [r["id"] for r in repo.list_idle(older_than_s=3600)] == [aid]
        assert repo.list_idle(older_than_s=3600 * 3) == []

    def test_update_whitelist(self, repo):
        aid = repo.create(slug="w", name="W", owner_user_id="u1")
        assert repo.update(aid, mem_limit="2g", service_token_id="t1") is True
        row = repo.get(aid)
        assert row["mem_limit"] == "2g"
        with pytest.raises(ValueError):
            repo.update(aid, state="running")  # state changes go via set_state
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_data_apps_repo.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.repositories.data_apps'`

- [ ] **Step 3: Add schema — `src/db.py`**

Set `SCHEMA_VERSION = 96` (line ~51). Add the CREATE TABLE to the fresh-install DDL (next to `memory_domains`, style-matched):

```sql
CREATE TABLE IF NOT EXISTS data_apps (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    description     TEXT DEFAULT '',
    owner_user_id   VARCHAR NOT NULL,
    repo_mode       VARCHAR NOT NULL DEFAULT 'internal',
    repo_url        VARCHAR DEFAULT '',
    repo_branch     VARCHAR DEFAULT 'main',
    deployed_sha    VARCHAR DEFAULT '',
    runtime_tag     VARCHAR DEFAULT '',
    state           VARCHAR NOT NULL DEFAULT 'created',
    state_detail    TEXT DEFAULT '',
    secrets_enc     TEXT DEFAULT '',
    env             TEXT DEFAULT '{}',
    cpu_limit       VARCHAR DEFAULT '',
    mem_limit       VARCHAR DEFAULT '',
    idle_timeout_s  INTEGER DEFAULT 1800,
    sleep_mode      VARCHAR DEFAULT 'recreate',
    service_token_id VARCHAR DEFAULT '',
    last_request_at TIMESTAMP,
    last_deploy_at  TIMESTAMP,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);
```

Add the step function (after `_v94_to_v95`):

```python
def _v95_to_v96(conn: duckdb.DuckDBPyConnection) -> None:
    """v95→v96: data_apps registry (hosted user web apps)."""
    conn.execute(_DATA_APPS_CREATE_SQL)  # same string as the fresh-install DDL
    conn.execute("UPDATE schema_version SET version = 96")
```

Wire both ladder call sites exactly like `_v94_to_v95` is wired (the sequential fresh-path call list AND the `if current < 96:` branch). Extract the CREATE into a module-level `_DATA_APPS_CREATE_SQL` constant so fresh-install and migration share one string.

- [ ] **Step 4: Alembic revision**

`migrations/versions/0043_data_apps_v96.py`:

```python
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_data_apps_v96"
down_revision: Union[str, None] = "0042_usage_summary_idx_fix_v95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_apps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("repo_mode", sa.String(), nullable=False, server_default="internal"),
        sa.Column("repo_url", sa.String(), server_default=""),
        sa.Column("repo_branch", sa.String(), server_default="main"),
        sa.Column("deployed_sha", sa.String(), server_default=""),
        sa.Column("runtime_tag", sa.String(), server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="created"),
        sa.Column("state_detail", sa.Text(), server_default=""),
        sa.Column("secrets_enc", sa.Text(), server_default=""),
        sa.Column("env", sa.Text(), server_default="{}"),
        sa.Column("cpu_limit", sa.String(), server_default=""),
        sa.Column("mem_limit", sa.String(), server_default=""),
        sa.Column("idle_timeout_s", sa.Integer(), server_default="1800"),
        sa.Column("sleep_mode", sa.String(), server_default="recreate"),
        sa.Column("service_token_id", sa.String(), server_default=""),
        sa.Column("last_request_at", sa.TIMESTAMP()),
        sa.Column("last_deploy_at", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("data_apps")
```

- [ ] **Step 5: DuckDB repo**

`src/repositories/data_apps.py` — follow `memory_domains.py` verbatim style. Key parts:

```python
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb

_UPDATABLE = {
    "name", "description", "repo_url", "repo_branch", "runtime_tag",
    "secrets_enc", "env", "cpu_limit", "mem_limit", "idle_timeout_s",
    "sleep_mode", "service_token_id",
}


class DataAppsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id", "slug", "name", "description", "owner_user_id",
        "repo_mode", "repo_url", "repo_branch", "deployed_sha",
        "runtime_tag", "state", "state_detail", "secrets_enc", "env",
        "cpu_limit", "mem_limit", "idle_timeout_s", "sleep_mode",
        "service_token_id", "last_request_at", "last_deploy_at",
        "created_at", "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def create(self, *, slug: str, name: str, owner_user_id: str,
               description: str = "", repo_mode: str = "internal",
               repo_url: str = "", repo_branch: str = "main",
               idle_timeout_s: int = 1800, sleep_mode: str = "recreate",
               env: str = "{}") -> str:
        app_id = "app_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_apps"
            "(id, slug, name, description, owner_user_id, repo_mode,"
            " repo_url, repo_branch, idle_timeout_s, sleep_mode, env) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [app_id, slug, name, description, owner_user_id, repo_mode,
             repo_url, repo_branch, idle_timeout_s, sleep_mode, env],
        )
        return app_id

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE id = ?", [app_id]
        ).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE slug = ?", [slug]
        ).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def list(self, *, owner_user_id: Optional[str] = None,
             state: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?"); params.append(owner_user_id)
        if state is not None:
            clauses.append("state = ?"); params.append(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps {where} "
            "ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def update(self, app_id: str, **fields) -> bool:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        if not fields:
            return False
        if self.get(app_id) is None:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE data_apps SET {sets}, updated_at = now() WHERE id = ?",
            [*fields.values(), app_id])
        return True

    def set_state(self, app_id: str, state: str, detail: str = "") -> None:
        self.conn.execute(
            "UPDATE data_apps SET state = ?, state_detail = ?, updated_at = now() "
            "WHERE id = ?", [state, detail, app_id])

    def record_deploy(self, app_id: str, sha: str) -> None:
        self.conn.execute(
            "UPDATE data_apps SET deployed_sha = ?, last_deploy_at = now(), "
            "updated_at = now() WHERE id = ?", [sha, app_id])

    def touch_last_request(self, app_id: str) -> None:
        self.conn.execute(
            "UPDATE data_apps SET last_request_at = now() WHERE id = ?", [app_id])

    def list_idle(self, older_than_s: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE state = 'running' "
            "AND last_request_at IS NOT NULL "
            "AND last_request_at < now() - (? * INTERVAL 1 SECOND)",
            [older_than_s]).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def delete(self, app_id: str) -> bool:
        existed = self.get(app_id) is not None
        self.conn.execute("DELETE FROM data_apps WHERE id = ?", [app_id])
        return existed
```

- [ ] **Step 6: PG twin**

`src/repositories/data_apps_pg.py` — mirror every method with SQLAlchemy `Engine` + `sa.text()` named params (copy the `memory_domains_pg.py` idiom: `with self._engine.begin() as conn:` for writes, `.connect()` + `.mappings()` for reads). The idle interval predicate in PG flavor:

```python
sa.text("SELECT ... FROM data_apps WHERE state = 'running' "
        "AND last_request_at IS NOT NULL "
        "AND last_request_at < now() - make_interval(secs => :older)")
```

Slug uniqueness surfaces as `sqlalchemy.exc.IntegrityError` — the contract test asserts "raises" per-backend (see Step 8).

- [ ] **Step 7: Factory entry**

`src/repositories/__init__.py` — add to `_REPO_REGISTRY`:

```python
    "data_apps": {
        DUCKDB: ("src.repositories.data_apps", "DataAppsRepository"),
        PG: ("src.repositories.data_apps_pg", "DataAppsPgRepository"),
    },
```

and the factory:

```python
def data_apps_repo() -> Any:
    return _build("data_apps")
```

- [ ] **Step 8: Cross-backend contract test**

`tests/db_pg/test_data_apps_contract.py` — copy the `test_memory_domains_contract.py` harness (`@pytest.fixture(params=["duckdb", "pg"])`, `pg_engine` fixture, `_make_duckdb_repo`/`_make_pg_repo` helpers adapted for `DataAppsRepository`/`DataAppsPgRepository`). Cover: create+get round-trip field equality, slug uniqueness (catch `(duckdb.ConstraintException, sqlalchemy.exc.IntegrityError)`), `set_state`, `record_deploy`, `list_idle` (insert old timestamp with raw SQL per backend), `update` whitelist rejection, `delete`.

- [ ] **Step 9: Run all new tests + schema gates**

Run: `.venv/bin/pytest tests/test_data_apps_repo.py tests/db_pg/test_data_apps_contract.py tests/test_db_schema_version.py tests/test_repository_registry.py -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add src/db.py migrations/versions/0043_data_apps_v96.py \
  src/repositories/data_apps.py src/repositories/data_apps_pg.py \
  src/repositories/__init__.py tests/test_data_apps_repo.py \
  tests/db_pg/test_data_apps_contract.py
git commit -m "feat(data-apps): data_apps registry with dual-backend repo pair (v96)"
```

---

### Task 2: Config accessor + `DATA_APP` resource type

**Files:**
- Modify: `app/instance_config.py`, `config/instance.yaml.example`, `app/resource_types.py`
- Test: `tests/test_resource_types.py` (extend existing), `tests/test_instance_config.py` (extend existing; create if the accessor tests live elsewhere — check `grep -rn "get_corporate_memory_config" tests/` and put the new test beside that one)

**Interfaces:**
- Produces: `get_data_apps_config() -> dict` in `app/instance_config.py`; `ResourceType.DATA_APP = "data_app"`; grants use `resource_id = <slug>`.
- Consumes: `data_apps_repo()` from Task 1.

- [ ] **Step 1: Failing test for the accessor + resource type**

```python
def test_data_apps_config_defaults(monkeypatch):
    from app import instance_config
    instance_config._instance_config = None  # reset cache — match how sibling tests do it
    cfg = instance_config.get_data_apps_config()
    assert cfg.get("enabled", False) is False


def test_data_app_resource_type_registered():
    from app.resource_types import RESOURCE_TYPES, ResourceType
    spec = RESOURCE_TYPES[ResourceType.DATA_APP]
    assert spec.id_format == "<slug>"
    assert callable(spec.list_blocks)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_resource_types.py -q -k data_app`
Expected: FAIL — `AttributeError: DATA_APP`

- [ ] **Step 3: Implement**

`app/instance_config.py` (next to `get_corporate_memory_config`):

```python
def get_data_apps_config() -> dict:
    return get_value("data_apps", default={})
```

`config/instance.yaml.example` — new top-level section (sibling of `chat:`), copied from spec §12:

```yaml
# Hosted data apps (user web applications run next to the data)
data_apps:
  enabled: false
  runtime_image: "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24"
  subdomain_base: ""            # e.g. "apps.example.com"; "" = path-prefix only
  default_idle_timeout_s: 1800
  default_sleep_mode: recreate  # recreate | pause
  default_mem_limit: 1g
  default_cpus: 1.0
  max_apps_per_user: 3
```

`app/resource_types.py` — add enum member `DATA_APP = "data_app"`, the projection:

```python
def _data_app_blocks() -> List[Block]:
    """Project ``data_apps`` into grant-picker blocks (resource_id = slug)."""
    from src.repositories import data_apps_repo

    rows = data_apps_repo().list(limit=_GRANT_PROJECTION_LIMIT)
    if not rows:
        return []
    return [
        {
            "id": "data_apps",
            "name": "Data apps",
            "items": [
                {
                    "resource_id": r["slug"],
                    "name": r["name"],
                    "category": "data_app",
                    "description": r.get("description"),
                    "slug": r["slug"],
                }
                for r in rows
            ],
        }
    ]
```

and the spec entry:

```python
    ResourceType.DATA_APP: ResourceTypeSpec(
        key=ResourceType.DATA_APP,
        display_name="Data apps",
        description="A hosted user web application served behind instance auth.",
        id_format="<slug>",
        list_blocks=_data_app_blocks,
    ),
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_resource_types.py tests/test_instance_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/instance_config.py config/instance.yaml.example app/resource_types.py tests/
git commit -m "feat(data-apps): data_apps config section and DATA_APP resource type"
```

---

### Task 3: Container spec + config.json builders

**Files:**
- Create: `src/data_apps/__init__.py` (empty), `src/data_apps/spec.py`
- Test: `tests/test_data_apps_spec.py`

**Interfaces:**
- Consumes: registry row dict (Task 1 shape), `encrypt_secret`/`decrypt_secret` from `app/secrets_vault.py` — NOTE: `src/` must not import from `app/`; check with `grep -rn "from app" src/ | head`. If that grep shows no precedent, move nothing — instead `spec.py` takes decrypted secrets as a plain dict argument and the *caller* (app layer, Task 6) does vault decryption.
- Produces:
  - `build_config_json(app_row: dict, *, secrets: dict[str, str], clone_url: str, clone_token: str) -> dict` — the `/data/config.json` content
  - `build_container_spec(app_row: dict, *, defaults: dict, data_dir: str) -> dict` — the runner `up` payload (JSON-safe)
  - `SLUG_RE` — `re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")`

- [ ] **Step 1: Failing tests**

```python
from src.data_apps.spec import SLUG_RE, build_config_json, build_container_spec

APP = {
    "id": "app_abc", "slug": "sales", "repo_mode": "internal",
    "repo_url": "", "repo_branch": "main", "runtime_tag": "",
    "mem_limit": "", "cpu_limit": "", "env": '{"FOO": "bar"}',
    "sleep_mode": "recreate",
}
DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "default_mem_limit": "1g", "default_cpus": 1.0,
}


def test_slug_re():
    assert SLUG_RE.match("sales-dash")
    assert not SLUG_RE.match("Sales")
    assert not SLUG_RE.match("-x")


def test_config_json_internal_repo_embeds_token():
    cfg = build_config_json(APP, secrets={"DB_PASSWORD": "s3"},
                            clone_url="http://app:8000/data-apps.git/sales",
                            clone_token="PATPAT")
    git = cfg["dataApp"]["git"]
    assert git["repository"] == "http://app:8000/data-apps.git/sales"
    assert git["branch"] == "agnes-live"
    assert git["username"] == "agnes"
    assert git["#password"] == "PATPAT"
    # secrets: caller-provided + injected platform vars
    assert cfg["dataApp"]["secrets"]["#DB_PASSWORD"] == "s3"
    assert cfg["dataApp"]["secrets"]["AGNES_TOKEN"] == "PATPAT"
    assert "input" not in cfg  # Data Loader never configured on this platform


def test_container_spec_defaults_and_overrides():
    spec = build_container_spec(APP, defaults=DEFAULTS, data_dir="/data")
    assert spec["name"] == "agnes-dataapp-sales"
    assert spec["image"] == DEFAULTS["runtime_image"]
    assert spec["mem_limit"] == "1g"
    assert spec["network"] == "agnes-apps"
    assert spec["labels"] == {"agnes.data-app": "app_abc"}
    assert spec["cache_volume"] == "agnes-dataapp-cache-sales"
    assert spec["env"]["AGNES_URL"] == "http://app:8000"
    assert spec["env"]["FOO"] == "bar"
    assert "DATA_LOADER_API_URL" not in spec["env"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_data_apps_spec.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `src/data_apps/spec.py`**

```python
"""Builders for the upstream python-js runtime contract.

The runtime image reads /data/config.json (dataApp.git + dataApp.secrets) and
never sees the platform: DATA_LOADER_API_URL stays unset by design (spec §2).
"""
from __future__ import annotations

import json
import re

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
LIVE_BRANCH = "agnes-live"
NETWORK = "agnes-apps"
AGNES_INTERNAL_URL = "http://app:8000"


def build_config_json(app_row: dict, *, secrets: dict[str, str],
                      clone_url: str, clone_token: str) -> dict:
    if app_row["repo_mode"] == "internal":
        git = {"repository": clone_url, "branch": LIVE_BRANCH,
               "username": "agnes", "#password": clone_token}
    else:
        git = {"repository": app_row["repo_url"],
               "branch": app_row["repo_branch"] or "main"}
    out_secrets = {f"#{k}": v for k, v in secrets.items()}
    out_secrets["AGNES_TOKEN"] = clone_token
    out_secrets["AGNES_URL"] = AGNES_INTERNAL_URL
    return {"dataApp": {"git": git, "secrets": out_secrets}}


def build_container_spec(app_row: dict, *, defaults: dict, data_dir: str) -> dict:
    slug = app_row["slug"]
    env = {k: str(v) for k, v in json.loads(app_row.get("env") or "{}").items()}
    env["AGNES_URL"] = AGNES_INTERNAL_URL
    env["AGNES_APP_ID"] = app_row["id"]
    image = defaults["runtime_image"]
    if app_row.get("runtime_tag"):
        image = image.rsplit(":", 1)[0] + ":" + app_row["runtime_tag"]
    return {
        "name": f"agnes-dataapp-{slug}",
        "image": image,
        "labels": {"agnes.data-app": app_row["id"]},
        "network": NETWORK,
        "config_dir": f"{data_dir}/apps/{slug}",
        "cache_volume": f"agnes-dataapp-cache-{slug}",
        "mem_limit": app_row.get("mem_limit") or defaults["default_mem_limit"],
        "cpus": float(app_row.get("cpu_limit") or defaults["default_cpus"]),
        "env": env,
    }
```

- [ ] **Step 4: Run tests, then commit**

Run: `.venv/bin/pytest tests/test_data_apps_spec.py -q` → PASS

```bash
git add src/data_apps/ tests/test_data_apps_spec.py
git commit -m "feat(data-apps): container spec and runtime config.json builders"
```

---

### Task 4: `apps-runner` sidecar

**Files:**
- Create: `services/apps_runner/__init__.py`, `services/apps_runner/api.py`, `services/apps_runner/__main__.py`
- Modify: `pyproject.toml` (add `"docker>=7.1.0",  # apps-runner sidecar` to `[project].dependencies`)
- Modify: `docker-compose.yml` (new service + network — see Step 6)
- Test: `tests/test_apps_runner.py`

**Interfaces:**
- Consumes: container-spec dict from Task 3 `build_container_spec` + `config_json` payload.
- Produces (HTTP, all requests require header `X-Runner-Token: $APPS_RUNNER_TOKEN`):
  - `POST /apps/{slug}/up` body `{"spec": {...}, "config_json": {...}}` → `{"status": "started"}` — writes `<spec.config_dir>/config.json`, ensures network+cache volume, removes any stale container, `docker run` detached with mounts `config_dir→/data` and `cache_volume→/home/app/.cache`.
  - `POST /apps/{slug}/stop` body `{"mode": "recreate"|"pause"}` → recreate = `remove(force=True)`; pause = `container.pause()`.
  - `POST /apps/{slug}/resume` → `container.unpause()`.
  - `GET /apps/{slug}/status` → `{"container": "running"|"paused"|"absent", "ready": bool}` — ready = TCP connect to `agnes-dataapp-<slug>:8888` succeeds.
  - `GET /apps/{slug}/logs?tail=200` → `{"logs": "<str>"}`.
  - `GET /apps` → `{"apps": [{"name": ..., "status": ...}]}` for `agnes-dataapp-*`.
  - Image allowlist: rejects `up` when `spec.image` doesn't start with the value of env `APPS_RUNNER_IMAGE_PREFIX` (set to the configured runtime image repo, no tag) → 400 `image_not_allowed`.

- [ ] **Step 1: Failing tests with a fake docker client**

`tests/test_apps_runner.py`:

```python
import pytest
from fastapi.testclient import TestClient


class FakeContainer:
    def __init__(self, name, status="running"):
        self.name, self.status = name, status
        self.removed = self.paused = self.unpaused = False
    def remove(self, force=False): self.removed = True
    def pause(self): self.paused = True
    def unpause(self): self.unpaused = True
    def logs(self, tail=200): return b"hello\n"


class FakeDocker:
    def __init__(self):
        self.run_calls = []
        self.by_name = {}
        self.containers = self
        self.networks = self
        self.volumes = self
    # containers API
    def run(self, image, **kw):
        self.run_calls.append((image, kw))
        c = FakeContainer(kw["name"]); self.by_name[kw["name"]] = c; return c
    def get(self, name):
        if name not in self.by_name:
            import docker.errors
            raise docker.errors.NotFound(name)
        return self.by_name[name]
    def list(self, all=True, filters=None): return list(self.by_name.values())
    # networks / volumes API (idempotent ensure)
    def create(self, name, **kw): return None


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "tok")
    monkeypatch.setenv("APPS_RUNNER_IMAGE_PREFIX", "keboolapublic.azurecr.io/data-app-python-js")
    from services.apps_runner import api
    fake = FakeDocker()
    monkeypatch.setattr(api, "_docker", lambda: fake)
    return TestClient(api.app), fake, tmp_path


SPEC = lambda tmp: {"name": "agnes-dataapp-s", "image":
                    "keboolapublic.azurecr.io/data-app-python-js:1.6.2",
                    "labels": {"agnes.data-app": "app_1"}, "network": "agnes-apps",
                    "config_dir": str(tmp / "apps" / "s"),
                    "cache_volume": "agnes-dataapp-cache-s",
                    "mem_limit": "1g", "cpus": 1.0, "env": {"A": "1"}}


def test_auth_required(client):
    c, _, tmp = client
    assert c.post("/apps/s/up", json={"spec": SPEC(tmp), "config_json": {}}).status_code == 401


def test_up_writes_config_and_runs(client):
    c, fake, tmp = client
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"},
               json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}})
    assert r.status_code == 200
    assert (tmp / "apps" / "s" / "config.json").exists()
    image, kw = fake.run_calls[0]
    assert kw["name"] == "agnes-dataapp-s"
    assert kw["detach"] is True


def test_up_rejects_foreign_image(client):
    c, _, tmp = client
    spec = SPEC(tmp) | {"image": "evil/image:1"}
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "tok"},
               json={"spec": spec, "config_json": {}})
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_allowed"


def test_stop_and_status(client):
    c, fake, tmp = client
    c.post("/apps/s/up", headers={"X-Runner-Token": "tok"},
           json={"spec": SPEC(tmp), "config_json": {}})
    r = c.post("/apps/s/stop", headers={"X-Runner-Token": "tok"},
               json={"mode": "recreate"})
    assert r.status_code == 200
    assert fake.by_name["agnes-dataapp-s"].removed
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_apps_runner.py -q` → FAIL (module not found)

- [ ] **Step 3: Implement `services/apps_runner/api.py`**

```python
"""apps-runner — the only process holding the Docker socket.

Deliberately dumb: no registry access, no RBAC, no policy. The Agnes app
decides *what* should run; this sidecar only translates to Docker calls.
Bound on the internal compose network only; token-gated.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException

app = FastAPI(title="agnes-apps-runner", docs_url=None, redoc_url=None)


def _docker():
    import docker
    return docker.from_env()


def _check_token(x_runner_token: str | None) -> None:
    expected = os.environ.get("APPS_RUNNER_TOKEN", "")
    if not expected or x_runner_token != expected:
        raise HTTPException(status_code=401, detail="bad_runner_token")


def _container(name: str):
    import docker.errors
    try:
        return _docker().containers.get(name)
    except docker.errors.NotFound:
        return None


@app.post("/apps/{slug}/up")
def up(slug: str, payload: dict = Body(...),
       x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    spec, config_json = payload["spec"], payload["config_json"]
    prefix = os.environ.get("APPS_RUNNER_IMAGE_PREFIX", "")
    if not prefix or not str(spec["image"]).startswith(prefix + ":"):
        raise HTTPException(status_code=400, detail="image_not_allowed")
    cfg_dir = Path(spec["config_dir"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(config_json, indent=2))
    client = _docker()
    try:
        client.networks.create(spec["network"], driver="bridge",
                               check_duplicate=True)
    except Exception:
        pass  # already exists
    old = _container(spec["name"])
    if old is not None:
        old.remove(force=True)
    client.containers.run(
        spec["image"], name=spec["name"], detach=True,
        labels=spec["labels"], network=spec["network"],
        environment=spec["env"], mem_limit=spec["mem_limit"],
        nano_cpus=int(float(spec["cpus"]) * 1e9),
        volumes={str(cfg_dir): {"bind": "/data", "mode": "rw"},
                 spec["cache_volume"]: {"bind": "/home/app/.cache", "mode": "rw"}},
        restart_policy={"Name": "unless-stopped"},
    )
    return {"status": "started"}


@app.post("/apps/{slug}/stop")
def stop(slug: str, payload: dict = Body(...),
         x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"status": "absent"}
    if payload.get("mode") == "pause":
        c.pause()
        return {"status": "paused"}
    c.remove(force=True)
    return {"status": "removed"}


@app.post("/apps/{slug}/resume")
def resume(slug: str, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    c.unpause()
    return {"status": "running"}


@app.get("/apps/{slug}/status")
def status(slug: str, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"container": "absent", "ready": False}
    state = "paused" if c.status == "paused" else (
        "running" if c.status == "running" else c.status)
    ready = False
    if state == "running":
        try:
            with socket.create_connection((f"agnes-dataapp-{slug}", 8888), timeout=2):
                ready = True
        except OSError:
            ready = False
    return {"container": state, "ready": ready}


@app.get("/apps/{slug}/logs")
def logs(slug: str, tail: int = 200,
         x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    return {"logs": c.logs(tail=tail).decode("utf-8", errors="replace")}


@app.get("/apps")
def list_apps(x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    rows = [{"name": c.name, "status": c.status}
            for c in _docker().containers.list(all=True)
            if c.name.startswith("agnes-dataapp-")]
    return {"apps": rows}
```

`services/apps_runner/__main__.py`:

```python
import uvicorn

from services.apps_runner.api import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8600)
```

Note the fake in tests monkeypatches `api._docker`, so the module must always go through `_docker()` (never a module-level client). Adjust the fake vs. `client.networks.create(..., check_duplicate=True)` signature mismatch if pytest surfaces one (the fake's `create(self, name, **kw)` already absorbs it).

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_apps_runner.py -q` → PASS

- [ ] **Step 5: Add `docker` dependency**

In `pyproject.toml` `[project].dependencies`, after the `httpx` pin:

```toml
    "docker>=7.1.0",           # apps-runner sidecar (Docker Engine API)
```

Run: `uv pip install -e ".[dev]"` (refresh env), then `.venv/bin/pytest tests/test_apps_runner.py -q` again.

- [ ] **Step 6: Compose service + network**

`docker-compose.yml` — new service (style-match the `scheduler` block) and network:

```yaml
  apps-runner:
    build: .
    command: python -m services.apps_runner
    profiles: ["apps"]
    volumes:
      - data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - APPS_RUNNER_TOKEN=${APPS_RUNNER_TOKEN:?set APPS_RUNNER_TOKEN in .env}
      - APPS_RUNNER_IMAGE_PREFIX=${APPS_RUNNER_IMAGE_PREFIX:-keboolapublic.azurecr.io/data-app-python-js}
    networks: [default, agnes-apps]
    restart: unless-stopped
    mem_limit: 256m
    cpus: 0.25
```

Attach the `app` service to the apps network as well — add to the existing `app` service:

```yaml
    networks: [default, agnes-apps]
```

and at top level (next to `volumes:`):

```yaml
networks:
  default: {}
  agnes-apps:
    name: agnes-apps
```

Sanity check: `docker compose config -q` → exit 0. (Compose rule: once any service declares `networks:`, services without the key still join `default` — only `app` and `apps-runner` need edits.)

- [ ] **Step 7: Commit**

```bash
git add services/apps_runner/ tests/test_apps_runner.py pyproject.toml docker-compose.yml
git commit -m "feat(data-apps): apps-runner sidecar with token-gated Docker lifecycle API"
```

---

### Task 5: Runner client

**Files:**
- Create: `src/data_apps/runner_client.py`
- Test: `tests/test_data_apps_spec.py` (append a class) or new `tests/test_runner_client.py`

**Interfaces:**
- Produces: `RunnerClient` with `up(slug, spec, config_json)`, `stop(slug, mode)`, `resume(slug)`, `status(slug) -> dict`, `logs(slug, tail=200) -> str`. Base URL from env `APPS_RUNNER_URL` (default `http://apps-runner:8600`), token from env `APPS_RUNNER_TOKEN`. Raises `RunnerUnavailable` on connect errors.

- [ ] **Step 1: Failing test** (httpx MockTransport)

```python
import httpx
import pytest

from src.data_apps.runner_client import RunnerClient, RunnerUnavailable


def _client(handler):
    return RunnerClient(base_url="http://runner", token="tok",
                        transport=httpx.MockTransport(handler))


def test_up_sends_token_and_payload():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers.get("x-runner-token")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": "started"})
    c = _client(handler)
    assert c.up("s", {"name": "n"}, {"dataApp": {}}) == {"status": "started"}
    assert seen["auth"] == "tok"
    assert seen["url"].endswith("/apps/s/up")


def test_unavailable_raises():
    def handler(request):
        raise httpx.ConnectError("boom")
    with pytest.raises(RunnerUnavailable):
        _client(handler).status("s")
```

- [ ] **Step 2: Run to fail**, **Step 3: Implement**

```python
from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class RunnerUnavailable(RuntimeError):
    pass


class RunnerClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self._base = (base_url or os.environ.get("APPS_RUNNER_URL",
                                                 "http://apps-runner:8600")).rstrip("/")
        self._token = token or os.environ.get("APPS_RUNNER_TOKEN", "")
        self._transport = transport

    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        try:
            with httpx.Client(transport=self._transport, timeout=60) as c:
                r = c.request(method, f"{self._base}{path}",
                              headers={"X-Runner-Token": self._token}, **kw)
        except httpx.TransportError as exc:
            raise RunnerUnavailable(str(exc)) from exc
        r.raise_for_status()
        return r.json()

    def up(self, slug: str, spec: dict, config_json: dict) -> dict:
        return self._request("POST", f"/apps/{slug}/up",
                             json={"spec": spec, "config_json": config_json})

    def stop(self, slug: str, mode: str = "recreate") -> dict:
        return self._request("POST", f"/apps/{slug}/stop", json={"mode": mode})

    def resume(self, slug: str) -> dict:
        return self._request("POST", f"/apps/{slug}/resume")

    def status(self, slug: str) -> dict:
        return self._request("GET", f"/apps/{slug}/status")

    def logs(self, slug: str, tail: int = 200) -> str:
        return self._request("GET", f"/apps/{slug}/logs",
                             params={"tail": tail})["logs"]
```

- [ ] **Step 4: Run tests → PASS, commit**

```bash
git add src/data_apps/runner_client.py tests/
git commit -m "feat(data-apps): httpx runner client"
```

---

### Task 6: Internal git hosting (`/data-apps.git/{slug}/…`)

**Files:**
- Create: `src/data_apps/git_repos.py`, `app/api/data_apps_git.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_data_apps_git.py`

**Interfaces:**
- Consumes: `token_from_basic_auth`, `_build_cgi_env`, `_run_git_http_backend` from `app/marketplace_server/git_router.py` (import them; if any is module-private and awkward to import, lift it into a shared `app/marketplace_server/git_cgi.py` and re-export — keep `git_router.py`'s behavior identical); `resolve_token_to_user` from `app/auth/pat_resolver.py`; `data_apps_repo()`.
- Produces (in `src/data_apps/git_repos.py`):
  - `repo_path(slug: str) -> Path` — `${DATA_DIR}/apps/git/<slug>.git`
  - `init_app_repo(slug: str) -> Path` — `git init --bare` + `git config http.receivepack true`; idempotent
  - `resolve_ref(slug: str, ref: str = "HEAD") -> Optional[str]` — `git rev-parse`
  - `fast_forward_live(slug: str, sha: Optional[str] = None) -> str` — sets `refs/heads/agnes-live` to `sha` (default: current HEAD of default branch) via `git update-ref`; returns the SHA
- Produces (router): `GET|POST /data-apps.git/{slug}/{path:path}` — read requires the caller to pass the app's RBAC gate (owner / Admin / granted); push (`git-receive-pack` in path or `service=git-receive-pack` query) additionally requires owner or Admin.

- [ ] **Step 1: Failing tests** (subprocess git against a live uvicorn thread — grep `tests/` for an existing "live server" fixture first: `grep -rn "uvicorn" tests/ | grep -v e2e | head`; reuse it if one exists)

```python
import subprocess

import pytest

from src.data_apps.git_repos import fast_forward_live, init_app_repo, resolve_ref


def test_init_and_fast_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    p = init_app_repo("sales")
    assert (p / "HEAD").exists()
    # push a commit into the bare repo from a scratch clone
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(p), str(work)], check=True, capture_output=True)
    (work / "f.txt").write_text("hi")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-m", "c1"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"],
                   check=True, capture_output=True)
    sha = fast_forward_live("sales")
    assert resolve_ref("sales", "agnes-live") == sha
```

HTTP-layer test (auth matrix; use FastAPI `TestClient` — info-refs GET is a plain HTTP request, so no real git client needed to assert authz):

```python
def test_git_http_requires_auth(app_client):  # app_client = existing TestClient fixture
    r = app_client.get("/data-apps.git/sales/info/refs?service=git-upload-pack")
    assert r.status_code == 401


def test_push_denied_for_non_owner(app_client, non_owner_pat):
    r = app_client.get(
        "/data-apps.git/sales/info/refs?service=git-receive-pack",
        auth=("git", non_owner_pat))
    assert r.status_code == 403
```

(Fixture wiring: create the app row with `data_apps_repo().create(...)` and mint PATs via the existing token-test helpers — copy the pattern from `tests/` files that test `/auth/tokens`; `grep -rn "create_token\|/auth/tokens" tests/ | head` to find them.)

- [ ] **Step 2: Run to fail**, **Step 3: Implement `src/data_apps/git_repos.py`**

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

LIVE_REF = "refs/heads/agnes-live"


def repo_path(slug: str) -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")) / "apps" / "git" / f"{slug}.git"


def init_app_repo(slug: str) -> Path:
    p = repo_path(slug)
    if not (p / "HEAD").exists():
        p.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(p)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(p), "config", "http.receivepack", "true"],
                       check=True, capture_output=True)
    return p


def resolve_ref(slug: str, ref: str = "HEAD") -> Optional[str]:
    r = subprocess.run(["git", "-C", str(repo_path(slug)), "rev-parse", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def fast_forward_live(slug: str, sha: Optional[str] = None) -> str:
    target = sha or resolve_ref(slug, "main") or resolve_ref(slug, "HEAD")
    if not target:
        raise ValueError(f"app repo {slug} has no commits to deploy")
    subprocess.run(["git", "-C", str(repo_path(slug)), "update-ref", LIVE_REF, target],
                   check=True, capture_output=True)
    return target
```

- [ ] **Step 4: Implement `app/api/data_apps_git.py`**

Mirror `_marketplace_git` (`app/marketplace_server/git_router.py:322`) with these changes: resolve `{slug}` → `data_apps_repo().get_by_slug` (404 if absent); authenticate PAT via `token_from_basic_auth` + `resolve_token_to_user`; authorize read with the same predicate the proxy uses (owner / Admin / `resource_grants` on `data_app:<slug>` — import the check from `app.auth.access`; the exact helper is whatever `require_resource_access` uses internally — `grep -n "def .*resource_access" app/auth/access.py` and call the non-dependency form); detect push via `"git-receive-pack" in (request.query_params.get("service") or "") or path.endswith("git-receive-pack")` and require owner/Admin (else 403); set `GIT_PROJECT_ROOT` to `repo_path(slug)`; stream via `_run_git_http_backend`. Register with two `add_api_route` calls (GET+POST, distinct `operation_id`s: `data_apps_git_get` / `data_apps_git_post`) and include the router in `app/main.py` next to `marketplace_git_router`.

- [ ] **Step 5: Run tests → PASS, commit**

Run: `.venv/bin/pytest tests/test_data_apps_git.py -q`

```bash
git add src/data_apps/git_repos.py app/api/data_apps_git.py app/main.py tests/test_data_apps_git.py
git commit -m "feat(data-apps): writable per-app git repos over http-backend with push authz"
```

---

### Task 7: Control-plane REST (`/api/data-apps/…`)

**Files:**
- Create: `app/api/data_apps.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_data_apps_api.py`

**Interfaces:**
- Consumes: everything above. `RunnerClient` is instantiated per-request via a module-level `_runner() -> RunnerClient` indirection so tests can monkeypatch it.
- Produces:
  - `GET /api/data-apps` — apps the caller can see (Admin: all; otherwise owner OR granted). Response rows: registry dict minus `secrets_enc`, `service_token_id`, plus `url` (`/apps/<slug>/` or subdomain form).
  - `POST /api/data-apps` `{slug, name, description?, repo_mode?, repo_url?, repo_branch?}` — validates `SLUG_RE`, enforces `max_apps_per_user` quota (Admin exempt), `init_app_repo` for internal mode, 201 `{id, slug, git_url}`.
  - `GET /api/data-apps/{slug}` — detail (RBAC-gated).
  - `POST /api/data-apps/{slug}/deploy` `{sha?}` — owner/Admin. Flow: `fast_forward_live` → revoke previous service token (`access_token_repo()` — use the same method the `DELETE /auth/tokens/{id}` route calls; read `app/api/tokens.py` to confirm the name) → mint new PAT for the **owner** (`create_access_token(user_id=owner.id, email=owner.email, token_id=new_id, typ="pat", extra_claims={"scope": f"data-app:{slug}"})` + `access_token_repo().create(...)`, exactly the Task-13-verified pattern from `app/api/tokens.py:95-159`) → `update(service_token_id=...)` → decrypt secrets (`decrypt_secret(row["secrets_enc"])` → dict) → `build_config_json` + `build_container_spec` → `_runner().up(...)` → `record_deploy` + `set_state("running")`. On `RunnerUnavailable` → `set_state("error", detail)` + 502 `runner_unavailable`.
  - `POST /api/data-apps/{slug}/stop` — owner/Admin → runner stop (recreate) → state `stopped`.
  - `DELETE /api/data-apps/{slug}` — owner/Admin → runner stop, revoke token, delete row (repo dir left on disk; note in response).
  - `PUT /api/data-apps/{slug}/secrets` `{secrets: {K: v}}` — owner/Admin → `encrypt_secret(json.dumps(secrets))` → update; effective on next deploy.
  - `GET /api/data-apps/{slug}/logs?tail=200` — owner/Admin → runner logs.
  - `GET /api/data-apps/{slug}/readiness` — any RBAC-passing caller → `{"state": row.state, "ready": bool}` (runner status when running/deploying).
  - `POST /api/data-apps/reap-idle` — `Depends(require_admin)` (the scheduler's synthetic user is Admin). For each `list_idle(row.idle_timeout_s)` match: runner stop(mode=row.sleep_mode) → state `sleeping`. Returns `{"reaped": [slugs]}`.

- [ ] **Step 1: Failing tests** — key cases (use the repo's existing FastAPI test-app fixture; `grep -rn "TestClient(app" tests/test_memory_domains*.py tests/test_recipes*.py | head` and copy that setup, including auth-user fixtures):

```python
class TestCrud:
    def test_create_and_quota(self, client_as_user):
        for i in range(3):
            assert client_as_user.post("/api/data-apps",
                json={"slug": f"a{i}", "name": f"A{i}"}).status_code == 201
        r = client_as_user.post("/api/data-apps", json={"slug": "a3", "name": "A3"})
        assert r.status_code == 403
        assert r.json()["detail"] == "app_quota_exceeded"

    def test_slug_validation(self, client_as_user):
        r = client_as_user.post("/api/data-apps", json={"slug": "Bad_Slug", "name": "x"})
        assert r.status_code == 400

    def test_list_hides_secrets(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "s", "name": "S"})
        rows = client_as_user.get("/api/data-apps").json()
        assert "secrets_enc" not in rows[0] and "service_token_id" not in rows[0]


class TestDeploy:
    def test_deploy_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/s/deploy", json={})
        assert r.status_code == 200
        assert fake_runner.up_calls  # runner received the spec
        row = client_as_user.get("/api/data-apps/s").json()
        assert row["state"] == "running"
        assert row["deployed_sha"]

    def test_deploy_forbidden_for_stranger(self, client_as_other_user, ...):
        assert client_as_other_user.post("/api/data-apps/s/deploy",
                                         json={}).status_code == 403

    def test_runner_down_sets_error(self, client_as_user, dead_runner, ...):
        r = client_as_user.post("/api/data-apps/s/deploy", json={})
        assert r.status_code == 502


class TestReap:
    def test_reap_idle(self, admin_client, fake_runner, running_idle_app):
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.json()["reaped"] == ["s"]
```

`fake_runner` fixture: monkeypatch `app.api.data_apps._runner` to return a stub object recording `up_calls`/`stop_calls` and returning canned dicts; `dead_runner` raises `RunnerUnavailable`.

- [ ] **Step 2: Run to fail**, **Step 3: Implement**

Router skeleton (auth deps copied from `app/api/memory_domains.py` / `app/api/tokens.py` imports — verify names in those files before writing):

```python
router = APIRouter(prefix="/api/data-apps", tags=["data-apps"])


def _runner() -> RunnerClient:
    return RunnerClient()


def _can_view(user: dict, row: dict) -> bool: ...   # Admin or owner or granted
def _require_owner_or_admin(user: dict, row: dict) -> None: ...  # else 403
```

Grant check: reuse the internal predicate behind `require_resource_access(ResourceType.DATA_APP, ...)` from `app.auth.access` (found in Task 6 Step 4). Feature flag: every handler starts with `if not get_data_apps_config().get("enabled"): raise HTTPException(404, "data_apps_disabled")`. Audit mutations via the `_audit(...)` idiom from `app/api/memory_domains.py:177`.

- [ ] **Step 4: Run tests → PASS**

Run: `.venv/bin/pytest tests/test_data_apps_api.py -q`

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps.py app/main.py tests/test_data_apps_api.py
git commit -m "feat(data-apps): control-plane REST — CRUD, deploy, logs, reap-idle"
```

---

### Task 8: Ingress proxy + wake-on-request + holding page

**Files:**
- Create: `app/api/data_apps_proxy.py`, `app/web/templates/data_app_waking.html`, `app/data_apps_subdomain.py`
- Modify: `app/main.py` (include router + add middleware)
- Test: `tests/test_data_apps_proxy.py`

**Interfaces:**
- Consumes: `data_apps_repo()`, `RunnerClient` (same `_runner()` monkeypatch indirection), `coordination()` from `app.coordination.factory`, `require_resource_access(ResourceType.DATA_APP, "{slug}")`, `get_data_apps_config()`.
- Produces:
  - `GET|POST|PUT|PATCH|DELETE /apps/{slug}/{path:path}` (+ `GET /apps/{slug}` → redirect to trailing slash) — auth (session cookie or PAT), RBAC, touch-debounce, then: `running` → streamed httpx proxy to `http://agnes-dataapp-{slug}:8888/{path}` with `X-Forwarded-Prefix: /apps/{slug}`; `sleeping|stopped` → trigger wake + holding page (HTML) or 503 JSON `{"status": "waking"}` when `Accept: application/json`; `created|error` → 409 JSON with `state_detail`.
  - `WEBSOCKET /apps/{slug}/{path:path}` — bridge via the `websockets` package (already a transitive dep of `uvicorn[standard]`; verify with `.venv/bin/python -c "import websockets"`).
  - Debounce: coordination `kv_get/kv_set` key `dataapp:touch:{slug}` ttl 30 s → skip DB write when present.
  - Wake lock: `coordination().lease_acquire(f"dataapp:wake:{slug}", holder_id, ttl_s=120)` — only the acquirer calls runner up/resume; others just render the holding page.
  - Subdomain middleware (`app/data_apps_subdomain.py`): pure-ASGI middleware; when `subdomain_base` configured and `Host == "<slug>." + base`, rewrite `scope["path"]` to `/apps/<slug>` + original path before routing. Registered in `app/main.py` with `app.add_middleware(...)`.

- [ ] **Step 1: Failing tests** — wake trigger, debounce, header hygiene, subdomain rewrite:

```python
def test_running_app_is_proxied(client_granted, fake_runner, respx_upstream):
    # respx (or monkeypatched httpx.AsyncClient) fakes agnes-dataapp-s:8888
    r = client_granted.get("/apps/s/hello")
    assert r.status_code == 200
    assert respx_upstream.calls[0].request.headers["x-forwarded-prefix"] == "/apps/s"


def test_sleeping_app_returns_holding_page_and_wakes(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert "waking" in r.text.lower()
    assert fake_runner.up_calls  # wake fired exactly once


def test_sleeping_app_json_accept(client_granted, fake_runner, sleeping_app):
    r = client_granted.get("/apps/s/", headers={"accept": "application/json"})
    assert r.status_code == 503
    assert r.json()["status"] == "waking"


def test_stranger_gets_403(client_stranger, running_app):
    assert client_stranger.get("/apps/s/").status_code == 403


def test_touch_debounced(client_granted, running_app, respx_upstream):
    client_granted.get("/apps/s/")
    first = data_apps_repo().get_by_slug("s")["last_request_at"]
    client_granted.get("/apps/s/")
    assert data_apps_repo().get_by_slug("s")["last_request_at"] == first


def test_subdomain_host_rewrite(client_granted, running_app, respx_upstream, monkeypatch):
    # data_apps.subdomain_base = "apps.example.com" via config overlay fixture
    r = client_granted.get("/", headers={"host": "s.apps.example.com"})
    assert r.status_code == 200  # reached the proxy handler for slug s
```

(For faking the upstream: prefer `respx` if it's already in dev deps — `grep respx pyproject.toml`; otherwise monkeypatch the module-level `_upstream_client()` indirection the implementation must provide.)

- [ ] **Step 2: Run to fail**, **Step 3: Implement**

Core handler sketch (complete the obvious glue; keep every name shown here):

```python
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host"}


def _upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300,
                                                   write=60, pool=5))


async def _proxy(request: Request, slug: str, path: str) -> Response:
    url = f"http://agnes-dataapp-{slug}:8888/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP}
    headers["X-Forwarded-Prefix"] = f"/apps/{slug}"
    async with _upstream_client() as client:
        upstream = client.build_request(request.method, url, headers=headers,
                                        params=request.query_params,
                                        content=request.stream())
        resp = await client.send(upstream, stream=True)
        return StreamingResponse(
            resp.aiter_raw(), status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() not in _HOP_BY_HOP},
            background=BackgroundTask(resp.aclose))
```

Touch-debounce + wake:

```python
def _touch(app_row: dict) -> None:
    key = f"dataapp:touch:{app_row['slug']}"
    try:
        if coordination().kv_get(key) is None:
            coordination().kv_set(key, "1", ttl_s=30)
            data_apps_repo().touch_last_request(app_row["id"])
    except CoordinationUnavailable:
        data_apps_repo().touch_last_request(app_row["id"])


def _trigger_wake(app_row: dict) -> None:
    holder = f"proxy-{os.getpid()}"
    if not coordination().lease_acquire(f"dataapp:wake:{app_row['slug']}",
                                        holder, ttl_s=120):
        return
    slug = app_row["slug"]
    data_apps_repo().set_state(app_row["id"], "deploying", "waking")
    if app_row["sleep_mode"] == "pause":
        _runner().resume(slug)
    else:
        _redeploy_current(app_row)  # shared helper exported by app/api/data_apps.py
```

Wake completion: the `readiness` endpoint (Task 7) flips state to `running` when runner reports `ready` — the holding page polls it:

`app/web/templates/data_app_waking.html` (standalone page, no chrome — it must render even for iframe/deep links):

```html
<!doctype html>
<title>Starting…</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0}</style>
<div><h2>⏳ App is waking up…</h2><p>This page reloads automatically.</p></div>
<script>
  const poll = async () => {
    try {
      const r = await fetch("/api/data-apps/{{ slug }}/readiness");
      const j = await r.json();
      if (j.ready) { location.reload(); return; }
    } catch (e) {}
    setTimeout(poll, 2000);
  };
  poll();
</script>
```

Subdomain middleware (`app/data_apps_subdomain.py`):

```python
class DataAppSubdomainMiddleware:
    """Rewrite <slug>.<base> host requests to /apps/<slug>/... paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            from app.instance_config import get_data_apps_config
            base = (get_data_apps_config().get("subdomain_base") or "").strip(".")
            if base:
                host = dict(scope.get("headers") or {}).get(b"host", b"").decode().split(":")[0]
                if host.endswith("." + base):
                    slug = host[: -(len(base) + 1)]
                    if "." not in slug:
                        scope = dict(scope)
                        scope["path"] = f"/apps/{slug}" + scope["path"]
        await self.app(scope, receive, send)
```

WebSocket bridge: `@router.websocket("/apps/{slug}/{path:path}")` — after the same auth/RBAC/state checks, `import websockets`, connect to `ws://agnes-dataapp-{slug}:8888/{path}`, then two `asyncio.gather`-ed pump loops (client→upstream, upstream→client), closing both on either side's disconnect.

Session-cookie note for subdomain mode: find the session-cookie `set_cookie` call sites (`grep -rn "set_cookie" app/ | grep -iv csrf`) and add `domain=("." + subdomain_base.split(".", 1)[1]) if get_data_apps_config().get("subdomain_base") else None` — i.e. cookie domain becomes the parent of `subdomain_base` only when configured; otherwise exactly today's behavior. Add one regression test asserting no `Domain=` attribute when `subdomain_base` is empty.

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_data_apps_proxy.py -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps_proxy.py app/data_apps_subdomain.py \
  app/web/templates/data_app_waking.html app/main.py tests/test_data_apps_proxy.py
git commit -m "feat(data-apps): auth-gated ingress proxy with wake-on-request and subdomain mode"
```

---

### Task 9: Scheduler idle-reaper job

**Files:**
- Modify: `services/scheduler/__main__.py` (`_DEFAULTS`, `build_jobs()`)
- Test: extend the scheduler's job-list test (`grep -rn "build_jobs" tests/ | head` to find it)

**Interfaces:**
- Consumes: `POST /api/data-apps/reap-idle` (Task 7).

- [ ] **Step 1: Failing test**

```python
def test_data_app_reaper_job_present(monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_APP_REAP_INTERVAL", "300")
    from services.scheduler.__main__ import build_jobs
    names = [j[0] for j in build_jobs()]
    assert "data-app-idle-reaper" in names
```

- [ ] **Step 2: Run to fail**, **Step 3: Implement**

Add `"SCHEDULER_DATA_APP_REAP_INTERVAL": 300` to `_DEFAULTS`; in `build_jobs()`:

```python
    reap = _read_positive_int("SCHEDULER_DATA_APP_REAP_INTERVAL")
    jobs.append(("data-app-idle-reaper", _seconds_to_schedule(reap),
                 "/api/data-apps/reap-idle", "POST", 120))
```

Check the `SCHEDULER_TICK_SECONDS ≤ min-interval` validation still holds with the default (300 s ≥ tick default — confirm, it will be).

- [ ] **Step 4: Run scheduler tests → PASS, commit**

```bash
git add services/scheduler/__main__.py tests/
git commit -m "feat(data-apps): scheduler idle-reaper job"
```

---

### Task 10: CLI — `agnes app …`

**Files:**
- Create: `cli/commands/data_apps.py`
- Modify: `cli/main.py` (import + `app.add_typer(data_apps_app, name="app")`)
- Test: follow the existing CLI test idiom (`grep -rn "glossary_app\|CliRunner" tests/ | head`)

**Interfaces:**
- Consumes: REST from Task 7 via `cli/client.py` helpers (`api_get`, and the POST sibling — check `cli/client.py` for its name, likely `api_post`).
- Produces commands: `list`, `show <slug>`, `create <slug> <name>`, `deploy <slug> [--sha]`, `logs <slug> [--tail N]`, `open <slug>`, `stop <slug>`, `delete <slug> [--yes]`. All list/read commands take `--json`; `list` takes `--limit`.

- [ ] **Step 1: Failing test** (typer `CliRunner` with `api_get` monkeypatched), **Step 2: fail**, **Step 3: implement** following `cli/commands/glossary.py` structure exactly (typer app named `data_apps_app`, help `"Manage hosted data apps"`; `open` prints the URL from the `show` payload and does NOT launch a browser — keep it print-only for headless parity). Not-found errors go through `cli/query_hints.py` helper.

- [ ] **Step 4: Ratchet** — run the API-coverage ratchet test (`.venv/bin/pytest tests/ -k "coverage" -q`); add the new route↔command mappings wherever the ratchet's allowlist/mapping lives so it passes without growing the grandfather list.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/data_apps.py cli/main.py tests/
git commit -m "feat(data-apps): agnes app CLI commands"
```

---

### Task 11: MCP foundation tools

**Files:**
- Modify: `app/api/mcp/foundation_tools.py`
- Test: `tests/test_mcp_tool_parity.py` (extend expected-tool list)

**Interfaces:**
- Produces tools (inside `register_foundation_tools`, same closure style as `glossary_search`): `data_apps_list()`, `data_app_get(slug)`, `data_app_deploy(slug, sha="")`, `data_app_logs(slug, tail=200)` — each a thin httpx call to the Task-7 REST endpoints using `base_url` + `headers_fn()`.

- [ ] **Step 1: Extend parity test to expect the 4 new tools → run → FAIL**
- [ ] **Step 2: Implement the 4 `@mcp.tool()` functions** (copy `glossary_search` body shape; docstrings state RBAC tier: list/get = any authenticated user with app access; deploy/logs = owner or Admin)
- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_mcp_tool_parity.py -q` → PASS
- [ ] **Step 4: Commit**

```bash
git add app/api/mcp/foundation_tools.py tests/test_mcp_tool_parity.py
git commit -m "feat(data-apps): MCP foundation tools for data apps"
```

---

### Task 12: Web UI — `/apps` list + detail

**Files:**
- Create: `app/web/templates/data_apps.html`, `app/web/templates/data_app_detail.html`
- Modify: `app/web/router.py` (two routes), nav include (find the nav template: `grep -rln "admin/studio\|/catalog" app/web/templates/ | head` — add an "Apps" item gated on `data_apps.enabled`)
- Test: `tests/test_design_system_contract.py` runs automatically; add route smoke tests beside existing web-router tests

**Interfaces:**
- Consumes: `GET /api/data-apps`, detail/logs/deploy/stop endpoints (page JS calls them with `fetch`).

- [ ] **Step 1: Failing smoke test** — authenticated GET `/apps` returns 200 and contains `Data apps`; anonymous GET redirects to login (copy assertion style from existing web tests).
- [ ] **Step 2: Implement routes** in `app/web/router.py` — follow the `studio_index` pattern verbatim (`{% extends "base_page.html" %}` templates, `**_chrome_ctx(request, user)` spread, CSS only in `{% block head_extra %}`, `var(--ds-*)` tokens only, no raw hex). List page: state badge (created/deploying/running/sleeping/stopped/error), open-app link, owner. Detail page: metadata, logs `<pre>` (fetched from `/api/data-apps/{slug}/logs`), Deploy/Stop buttons (fetch POST + reload), link to `/admin/access` for granting.
- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_design_system_contract.py tests/ -k "web" -q` → PASS. **Take a screenshot** of `/apps` against a locally running app (per the repo's web-page verification convention) and eyeball chrome/CSS presence.
- [ ] **Step 4: Commit**

```bash
git add app/web/ tests/
git commit -m "feat(data-apps): /apps web pages"
```

---

### Task 13: E2E (docker-marked), Caddy example, docs, CHANGELOG

**Files:**
- Create: `tests/test_data_apps_e2e_docker.py`, `deploy/caddy/Caddyfile.apps-subdomain` (example snippet)
- Modify: `docs/DEPLOYMENT.md`, `docs/architecture.md` (one section each), `CHANGELOG.md`, `CLAUDE.md` (project-structure tree + one line under Extensibility)
- Test: the E2E file itself

**Interfaces:** consumes everything.

- [ ] **Step 1: E2E test** — gated: `pytest.mark.docker` + `skipif(not os.environ.get("AGNES_DATA_APPS_E2E"))`. Flow: build a fixture app repo in `tmp_path` (minimal Flask app + `keboola-config/` — nginx conf proxying :8888→127.0.0.1:5000, supervisord conf `command=uv run flask --app app run --host 127.0.0.1 --port 5000`, `setup.sh` with `uv sync`, `pyproject.toml` with flask) → start the real runner API in-process with the real docker SDK → `up` with the real runtime image → poll `status` until `ready` (timeout 300 s) → HTTP GET `http://localhost:<mapped>/` (for the test only, pass a `ports` mapping through the spec — add an optional `ports` key to the runner `up` handler, absent in production specs) → assert 200 → `stop(recreate)` → `up` again → ready again (wake path). Document the invocation in the test docstring: `AGNES_DATA_APPS_E2E=1 .venv/bin/pytest tests/test_data_apps_e2e_docker.py -q`.
- [ ] **Step 2: Caddy subdomain example** — `deploy/caddy/Caddyfile.apps-subdomain`:

```caddy
# Optional: subdomain routing for data apps. Append to your Caddyfile and
# provide a wildcard DNS record + wildcard TLS for *.{$APPS_SUBDOMAIN_BASE}.
*.{$APPS_SUBDOMAIN_BASE} {
	{$CADDY_TLS:tls internal}
	reverse_proxy app:8000 {
		header_up X-Forwarded-Proto https
		header_up X-Forwarded-Host {host}
	}
}
```

- [ ] **Step 3: Docs** — `docs/DEPLOYMENT.md`: "Data apps" section — enable flag, `APPS_RUNNER_TOKEN` generation (`openssl rand -hex 32`), `docker compose --profile apps up -d`, subdomain setup pointer, security note (docker socket confinement + owner-grants publication semantics, spec §8/§10). `docs/architecture.md`: extract.duckdb-style short section describing registry→runner→proxy flow with the spec link. `CLAUDE.md`: add `src/data_apps/`, `services/apps_runner/` to the tree.
- [ ] **Step 4: CHANGELOG** — under `## [Unreleased]` → `### Added`:

```markdown
- Data Apps: host user web applications next to the data using the upstream
  `data-app-python-js` runtime image — internal git repos with push-to-deploy,
  RBAC-gated ingress at `/apps/<slug>/` (optional per-app subdomains),
  auto-sleep with wake-on-request, `agnes app` CLI, MCP tools, and an `/apps`
  dashboard. Off by default (`data_apps.enabled`) + compose profile `apps`.
```

- [ ] **Step 5: Full suite + commit**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (E2E auto-skips without the env gate)

```bash
git add tests/test_data_apps_e2e_docker.py deploy/caddy/ docs/ CHANGELOG.md CLAUDE.md
git commit -m "feat(data-apps): e2e harness, subdomain Caddy example, docs, changelog"
```

---

## Self-review notes (already applied)

- Spec §4 registry ↔ Task 1 (added `service_token_id` column the spec implies in §8 token rotation).
- Spec §5 runner ↔ Task 4 (image allowlist, no published ports, fixed mounts). Reconciliation `GET /apps` exists but no reconciler loop — YAGNI for v1, the registry is authoritative and `up` is idempotent.
- Spec §6 ingress ↔ Task 8 (both routing modes, holding page, JSON 503, `X-Forwarded-Prefix`, cookie-domain change gated on config).
- Spec §7 auto-sleep ↔ Tasks 7 (reap endpoint) + 8 (wake, lease) + 9 (cron). Timeout clamp `[300, 86400]`: enforce in the Task 7 create/update validators.
- Spec §8 data access ↔ Task 7 deploy (owner PAT mint/rotate/revoke) + Task 3 (AGNES_URL/AGNES_TOKEN injection).
- Spec §9 (AI authoring) — intentionally out; follow-up plan.
- Spec §11 surfaces ↔ Tasks 7/10/11/12; §12 config ↔ Task 2; §13 testing ↔ per-task tests + Task 13 E2E.
- Type consistency: `RunnerClient` method names (`up/stop/resume/status/logs`) match runner routes; `_runner()` indirection is the single test seam in both Task 7 and Task 8; `fast_forward_live`/`init_app_repo`/`resolve_ref` names consistent across Tasks 6/7.
