# Named Source Connections — Phase 1 (Schema + Repos + Seeding) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the `source_connections` + `connection_secrets` data model (both backends), per-type config validation, the connection/token resolver, and first-boot seeding from env/yaml — invisible to users, unblocking phases 2–4.

**Architecture:** One generic `source_connections` table (per-type validation via a `ConnectionSpec` registry, no migration per type) + a vault-backed `connection_secrets` table reusing the Fernet helpers from `app/secrets_vault.py`. `table_registry` gains nullable `connection_id` (NULL = default connection of the row's `source_type`). A resolver module turns `(source_type, connection_id)` into a connection dict and a token (vault → `token_env` → legacy env). First-boot seeding creates `keboola`/`bigquery` default connections from today's env/yaml so existing deployments upgrade with zero behavior change.

**Tech Stack:** DuckDB migration ladder (`src/db.py`, v73→v74) + Alembic (`migrations/versions/`), SQLAlchemy Core for PG repos, Fernet vault, pytest (`tests/db_pg/` cross-engine contract pattern).

**Spec:** `docs/superpowers/specs/2026-06-12-named-source-connections-design.md`
**Conventions:** `.claude/skills/agnes-conventions/references/{migration,repo-parity}.md` — read both before starting.

---

### Task 1: Schema migration v74 (both ladders)

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION at `:50`, new `_v73_to_v74`, two dispatch sites in `_ensure_schema`)
- Create: `migrations/versions/0021_source_connections_v74.py`
- Modify: `src/db_pg.py` (`Base.metadata` models)
- Test: `tests/test_source_connections_schema.py`

- [ ] **Step 1: Write the failing schema test**

```python
# tests/test_source_connections_schema.py
"""Schema gate for the v74 source-connections tables (spec 2026-06-12)."""
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    return {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}


def test_v74_tables_exist(tmp_path):
    conn = _open_duckdb(str(tmp_path / "s.duckdb"))
    _ensure_schema(conn)
    assert _cols(conn, "source_connections") >= {
        "id", "name", "source_type", "config", "token_env",
        "is_default", "created_by", "created_at",
    }
    assert _cols(conn, "connection_secrets") >= {
        "connection_id", "ciphertext", "updated_at",
    }
    assert "connection_id" in _cols(conn, "table_registry")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_source_connections_schema.py -v`
Expected: FAIL (`source_connections` does not exist)

- [ ] **Step 3: DuckDB ladder — bump version + migration fn + dispatch**

In `src/db.py`: change `SCHEMA_VERSION = 73` → `SCHEMA_VERSION = 74`. Add next to `_v72_to_v73` (worked example shape at `src/db.py:4905`):

```python
def _v73_to_v74(conn):
    """Named source connections (spec 2026-06-12): generic connection
    registry + vault-backed secrets + table_registry.connection_id."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_connections (
            id          VARCHAR PRIMARY KEY,
            name        VARCHAR NOT NULL UNIQUE,
            source_type VARCHAR NOT NULL,
            config      TEXT NOT NULL,
            token_env   VARCHAR,
            is_default  BOOLEAN DEFAULT FALSE,
            created_by  VARCHAR,
            created_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connection_secrets (
            connection_id VARCHAR PRIMARY KEY,
            ciphertext    TEXT NOT NULL,
            updated_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute(
        "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS connection_id VARCHAR"
    )
    conn.execute("UPDATE schema_version SET version = 74")
```

Wire BOTH dispatch sites in `_ensure_schema`:
- fresh-install block (`current == 0`): add `_v73_to_v74(conn)` after the `_v72_to_v73(conn)` call;
- upgrade block: `if current < 74: _v73_to_v74(conn)` after the `< 73` guard.

- [ ] **Step 4: Run the schema test + ladder gate**

Run: `.venv/bin/pytest tests/test_source_connections_schema.py tests/test_db_schema_version.py -v`
Expected: both PASS

- [ ] **Step 5: Alembic revision (up + down)**

```python
# migrations/versions/0021_source_connections_v74.py
"""source connections + connection secrets (DuckDB v74)

Revision ID: 0021_source_connections_v74
Revises: 0020_chat_sandbox_refs_v73
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0021_source_connections_v74"
down_revision: str = "0020_chat_sandbox_refs_v73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_connections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("token_env", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now()),
    )
    op.create_table(
        "connection_secrets",
        sa.Column("connection_id", sa.String(), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now()),
    )
    op.add_column("table_registry", sa.Column("connection_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("table_registry", "connection_id")
    op.drop_table("connection_secrets")
    op.drop_table("source_connections")
```

- [ ] **Step 6: Mirror the models in `src/db_pg.py` `Base.metadata`**

Add next to the existing model classes (match the file's declarative style — copy the shape of the `MCPSource` model and adjust):

```python
class SourceConnection(Base):
    __tablename__ = "source_connections"
    id = sa.Column(sa.String, primary_key=True)
    name = sa.Column(sa.String, nullable=False, unique=True)
    source_type = sa.Column(sa.String, nullable=False)
    config = sa.Column(sa.Text, nullable=False)
    token_env = sa.Column(sa.String, nullable=True)
    is_default = sa.Column(sa.Boolean, server_default=sa.false())
    created_by = sa.Column(sa.String, nullable=True)
    created_at = sa.Column(sa.TIMESTAMP, server_default=sa.func.now())


class ConnectionSecret(Base):
    __tablename__ = "connection_secrets"
    connection_id = sa.Column(sa.String, primary_key=True)
    ciphertext = sa.Column(sa.Text, nullable=False)
    updated_at = sa.Column(sa.TIMESTAMP, server_default=sa.func.now())
```

Also add `connection_id = sa.Column(sa.String, nullable=True)` to the existing `TableRegistry` model.

- [ ] **Step 7: Run the Alembic gates**

Run: `.venv/bin/pytest tests/db_pg/test_alembic_roundtrip.py -v`
Expected: PASS (incl. `test_no_model_migration_drift`)

- [ ] **Step 8: Commit**

```bash
git add src/db.py src/db_pg.py migrations/versions/0021_source_connections_v74.py tests/test_source_connections_schema.py
git commit -m "feat(db): source_connections + connection_secrets schema (v74, both ladders)"
```

---

### Task 2: ConnectionSpec validation registry

**Files:**
- Create: `src/connection_specs.py`
- Test: `tests/test_connection_specs.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connection_specs.py
import pytest

from src.connection_specs import validate_connection_config


def test_keboola_normalizes_trailing_slash():
    cfg = validate_connection_config(
        "keboola", {"stack_url": "https://connection.example.com/"}
    )
    assert cfg["stack_url"] == "https://connection.example.com"


def test_keboola_requires_https_stack_url():
    with pytest.raises(ValueError, match="stack_url"):
        validate_connection_config("keboola", {})
    with pytest.raises(ValueError, match="https"):
        validate_connection_config("keboola", {"stack_url": "ftp://x"})


def test_bigquery_requires_project_defaults_location():
    cfg = validate_connection_config("bigquery", {"project": "my-proj"})
    assert cfg["location"] == "us"
    with pytest.raises(ValueError, match="project"):
        validate_connection_config("bigquery", {})


def test_unknown_source_type_rejected():
    with pytest.raises(ValueError, match="unknown source_type"):
        validate_connection_config("oracle", {})
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_connection_specs.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement the registry**

```python
# src/connection_specs.py
"""Per-type validation for source_connections.config (spec 2026-06-12 §3.1).

Mirrors the ResourceTypeSpec pattern in app/resource_types.py: adding a
source type registers a spec here — no DB migration. Validation runs at
registration time (admin API / seeding), so consumers downstream never
see a denormalized config (e.g. a trailing-slash stack URL).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ConnectionSpec:
    source_type: str
    validate: Callable[[Dict[str, Any]], Dict[str, Any]]  # returns normalized config


def _validate_keboola(config: Dict[str, Any]) -> Dict[str, Any]:
    url = str(config.get("stack_url") or "").strip().rstrip("/")
    if not url:
        raise ValueError("keboola connection requires config.stack_url")
    if not url.startswith("https://"):
        raise ValueError(f"stack_url must be https://, got: {url!r}")
    return {**config, "stack_url": url}


def _validate_bigquery(config: Dict[str, Any]) -> Dict[str, Any]:
    project = str(config.get("project") or "").strip()
    if not project:
        raise ValueError("bigquery connection requires config.project")
    out = {**config, "project": project}
    out.setdefault("location", "us")
    return out


_SPECS: Dict[str, ConnectionSpec] = {
    "keboola": ConnectionSpec("keboola", _validate_keboola),
    "bigquery": ConnectionSpec("bigquery", _validate_bigquery),
}


def validate_connection_config(source_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    spec = _SPECS.get(source_type)
    if spec is None:
        raise ValueError(f"unknown source_type: {source_type!r}")
    return spec.validate(config)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_connection_specs.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/connection_specs.py tests/test_connection_specs.py
git commit -m "feat: ConnectionSpec validation registry (keboola, bigquery)"
```

---

### Task 3: SourceConnections repository pair + factory + contract test

**Files:**
- Create: `src/repositories/source_connections.py`
- Create: `src/repositories/source_connections_pg.py`
- Modify: `src/repositories/__init__.py` (3 edits: `__all__`, `_REGISTRY`, factory fn)
- Test: `tests/db_pg/test_source_connections_contract.py`

- [ ] **Step 1: Write the failing contract test** (fixture shape copied from `tests/db_pg/test_mcp_sources_contract.py`)

```python
# tests/db_pg/test_source_connections_contract.py
"""Cross-engine contract tests for the source_connections repository."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.source_connections import SourceConnectionsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SourceConnectionsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.source_connections_pg import SourceConnectionsPgRepository
    return SourceConnectionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_get_roundtrip(repo):
    repo.create(
        id="c1", name="kbc_eu", source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
        token_env="KBC_EU_TOKEN", is_default=True, created_by="t@example.com",
    )
    row = repo.get("c1")
    assert row["name"] == "kbc_eu"
    assert row["config"]["stack_url"] == "https://connection.example.com"
    assert repo.get_by_name("kbc_eu")["id"] == "c1"
    assert repo.get("nope") is None


def test_list_filters_by_source_type(repo):
    repo.create(id="c1", name="kbc", source_type="keboola", config={"stack_url": "https://a"})
    repo.create(id="c2", name="bq", source_type="bigquery", config={"project": "p"})
    assert {r["id"] for r in repo.list()} == {"c1", "c2"}
    assert [r["id"] for r in repo.list(source_type="keboola")] == ["c1"]


def test_default_is_unique_per_source_type(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"}, is_default=True)
    repo.create(id="c2", name="b", source_type="keboola", config={"stack_url": "https://b"}, is_default=True)
    rows = repo.list(source_type="keboola")
    defaults = [r for r in rows if r["is_default"]]
    assert [r["id"] for r in defaults] == ["c2"]          # last set wins
    assert repo.get_default("keboola")["id"] == "c2"
    assert repo.get_default("bigquery") is None


def test_update_and_delete(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"})
    repo.update("c1", config={"stack_url": "https://b"}, token_env="X")
    assert repo.get("c1")["config"]["stack_url"] == "https://b"
    assert repo.get("c1")["token_env"] == "X"
    repo.delete("c1")
    assert repo.get("c1") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/db_pg/test_source_connections_contract.py -v -k duckdb`
Expected: FAIL (module not found)

- [ ] **Step 3: DuckDB repository**

```python
# src/repositories/source_connections.py
"""Repository for `source_connections` (v74) — named data-source connections.

Spec: docs/superpowers/specs/2026-06-12-named-source-connections-design.md.
`config` is stored as a JSON string and returned as a dict. `is_default`
is unique per source_type — enforced here (both backends), not by the DB.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import duckdb


class SourceConnectionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row, cols) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(zip(cols, row))
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _fetch_one(self, sql: str, params: list) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(sql, params).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        token_env: Optional[str] = None,
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> None:
        if is_default:
            self.conn.execute(
                "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                [source_type],
            )
        self.conn.execute(
            """INSERT INTO source_connections
               (id, name, source_type, config, token_env, is_default, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, name, source_type, json.dumps(config), token_env, is_default, created_by],
        )

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE id = ?", [connection_id]
        )

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE name = ?", [name]
        )

    def get_default(self, source_type: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = ? AND is_default ORDER BY created_at LIMIT 1",
            [source_type],
        )

    def list(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if source_type:
            rows = self.conn.execute(
                "SELECT * FROM source_connections WHERE source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM source_connections ORDER BY name"
            ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [self._row_to_dict(r, cols) for r in rows]

    def update(
        self,
        connection_id: str,
        *,
        config: Optional[Dict[str, Any]] = None,
        token_env: Optional[str] = None,
    ) -> None:
        if config is not None:
            self.conn.execute(
                "UPDATE source_connections SET config = ? WHERE id = ?",
                [json.dumps(config), connection_id],
            )
        if token_env is not None:
            self.conn.execute(
                "UPDATE source_connections SET token_env = ? WHERE id = ?",
                [token_env, connection_id],
            )

    def delete(self, connection_id: str) -> None:
        self.conn.execute(
            "DELETE FROM source_connections WHERE id = ?", [connection_id]
        )
```

- [ ] **Step 4: PG repository (mirror signatures exactly — AST parity guard)**

```python
# src/repositories/source_connections_pg.py
"""Postgres-backed SourceConnectionsRepository.

Mirrors ``src/repositories/source_connections.py``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SourceConnectionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        token_env: Optional[str] = None,
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> None:
        with self._engine.begin() as cx:
            if is_default:
                cx.execute(
                    sa.text("UPDATE source_connections SET is_default = FALSE WHERE source_type = :st"),
                    {"st": source_type},
                )
            cx.execute(
                sa.text(
                    """INSERT INTO source_connections
                       (id, name, source_type, config, token_env, is_default, created_by)
                       VALUES (:id, :name, :st, :config, :token_env, :is_default, :created_by)"""
                ),
                {
                    "id": id, "name": name, "st": source_type,
                    "config": json.dumps(config), "token_env": token_env,
                    "is_default": is_default, "created_by": created_by,
                },
            )

    def _fetch_one(self, sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as cx:
            row = cx.execute(sa.text(sql), params).mappings().fetchone()
        return self._decode(dict(row) if row else None)

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE id = :id", {"id": connection_id}
        )

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE name = :n", {"n": name}
        )

    def get_default(self, source_type: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = :st AND is_default ORDER BY created_at LIMIT 1",
            {"st": source_type},
        )

    def list(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM source_connections"
        params: Dict[str, Any] = {}
        if source_type:
            sql += " WHERE source_type = :st"
            params["st"] = source_type
        sql += " ORDER BY name"
        with self._engine.connect() as cx:
            rows = cx.execute(sa.text(sql), params).mappings().fetchall()
        return [self._decode(dict(r)) for r in rows]

    def update(
        self,
        connection_id: str,
        *,
        config: Optional[Dict[str, Any]] = None,
        token_env: Optional[str] = None,
    ) -> None:
        with self._engine.begin() as cx:
            if config is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET config = :c WHERE id = :id"),
                    {"c": json.dumps(config), "id": connection_id},
                )
            if token_env is not None:
                cx.execute(
                    sa.text("UPDATE source_connections SET token_env = :t WHERE id = :id"),
                    {"t": token_env, "id": connection_id},
                )

    def delete(self, connection_id: str) -> None:
        with self._engine.begin() as cx:
            cx.execute(
                sa.text("DELETE FROM source_connections WHERE id = :id"),
                {"id": connection_id},
            )
```

- [ ] **Step 5: The three `src/repositories/__init__.py` edits**

1. `__all__`: add `"source_connections_repo"` (alphabetical position).
2. `_REGISTRY` (next to the `"mcp_sources"` entry):

```python
    "source_connections": {
        DUCKDB: ("src.repositories.source_connections", "SourceConnectionsRepository"),
        PG: ("src.repositories.source_connections_pg", "SourceConnectionsPgRepository"),
    },
```

3. Factory fn (next to `mcp_sources_repo`):

```python
def source_connections_repo() -> Any: return _build("source_connections")
```

- [ ] **Step 6: Run contract + registry guards**

Run: `.venv/bin/pytest tests/db_pg/test_source_connections_contract.py tests/test_repository_registry.py tests/db_pg/test_repo_method_parity.py -v`
Expected: PASS (PG leg auto-skips when no PG fixture is available locally — CI runs it)

- [ ] **Step 7: Commit**

```bash
git add src/repositories/source_connections.py src/repositories/source_connections_pg.py src/repositories/__init__.py tests/db_pg/test_source_connections_contract.py
git commit -m "feat: source_connections repository pair + factory + contract test"
```

---

### Task 4: ConnectionSecrets vault repository pair

**Files:**
- Modify: `app/secrets_vault.py` (add `ConnectionSecretsRepository` next to `SharedSecretsRepository`)
- Modify: `src/repositories/secrets_vault_pg.py` (add the PG sibling — match how the existing `SharedSecrets`/`SystemSecrets` PG classes are laid out in that file)
- Modify: `src/repositories/__init__.py` (3 edits, same pattern as Task 3 — registry key `"connection_secrets"`, factory `connection_secrets_repo`, entries pointing at `("app.secrets_vault", "ConnectionSecretsRepository")` / `("src.repositories.secrets_vault_pg", "ConnectionSecretsPgRepository")` — copy the exact module-path style of the existing `"shared_secrets"` entry at `src/repositories/__init__.py:346`)
- Test: `tests/db_pg/test_connection_secrets_contract.py`

- [ ] **Step 1: Write the failing contract test**

```python
# tests/db_pg/test_connection_secrets_contract.py
"""Cross-engine contract tests for connection_secrets (vault scope)."""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from app.secrets_vault import ConnectionSecretsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return ConnectionSecretsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.secrets_vault_pg import ConnectionSecretsPgRepository
    return ConnectionSecretsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_upsert_get_has_delete_roundtrip(repo):
    assert repo.has("c1") is False
    assert repo.get("c1") is None
    repo.upsert("c1", "tok-secret-1")
    assert repo.has("c1") is True
    assert repo.get("c1") == "tok-secret-1"
    repo.upsert("c1", "tok-secret-2")          # rotate
    assert repo.get("c1") == "tok-secret-2"
    repo.delete("c1")
    assert repo.has("c1") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/db_pg/test_connection_secrets_contract.py -v -k duckdb`
Expected: FAIL (`ConnectionSecretsRepository` not found)

- [ ] **Step 3: DuckDB impl in `app/secrets_vault.py`**

Add after `SharedSecretsRepository` (copy its body shape — `upsert`/`get`/`delete`/`has` against the `connection_secrets` table; reuse `encrypt_secret`/`decrypt_secret`):

```python
class ConnectionSecretsRepository:
    """Vault scope for source_connections tokens (spec 2026-06-12 §3.1).

    Same write-only contract as ``SharedSecretsRepository``: API layers
    must only expose ``has()``; ``get()`` is for connector-side resolution.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert(self, connection_id: str, value: str) -> None:
        ct = encrypt_secret(value).decode()
        self.conn.execute(
            """INSERT INTO connection_secrets (connection_id, ciphertext, updated_at)
               VALUES (?, ?, current_timestamp)
               ON CONFLICT (connection_id) DO UPDATE SET
                   ciphertext = excluded.ciphertext,
                   updated_at = excluded.updated_at""",
            [connection_id, ct],
        )

    def get(self, connection_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT ciphertext FROM connection_secrets WHERE connection_id = ?",
            [connection_id],
        ).fetchone()
        if not row:
            return None
        try:
            return decrypt_secret(row[0].encode())
        except InvalidToken:
            logger.warning("connection secret for %s unreadable (key rotated?)", connection_id)
            return None

    def delete(self, connection_id: str) -> None:
        self.conn.execute(
            "DELETE FROM connection_secrets WHERE connection_id = ?", [connection_id]
        )

    def has(self, connection_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM connection_secrets WHERE connection_id = ?", [connection_id]
        ).fetchone()
        return row is not None
```

- [ ] **Step 4: PG sibling in `src/repositories/secrets_vault_pg.py`**

Mirror the existing `SharedSecretsPgRepository` class shape in that file 1:1, against `connection_secrets` with the same four methods (`upsert`, `get`, `delete`, `has`), `sa.text` + `:named` binds, writes under `engine.begin()`.

- [ ] **Step 5: Factory edits + run guards**

Make the three `src/repositories/__init__.py` edits (key `"connection_secrets"`, factory `connection_secrets_repo`).

Run: `.venv/bin/pytest tests/db_pg/test_connection_secrets_contract.py tests/test_repository_registry.py tests/db_pg/test_repo_method_parity.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/secrets_vault.py src/repositories/secrets_vault_pg.py src/repositories/__init__.py tests/db_pg/test_connection_secrets_contract.py
git commit -m "feat: connection_secrets vault scope (both backends)"
```

---

### Task 5: Connection + token resolver

**Files:**
- Create: `src/connection_resolver.py`
- Test: `tests/test_connection_resolver.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connection_resolver.py
"""Resolver: (source_type, connection_id|None) -> connection; token chain."""
import pytest

from src.connection_resolver import resolve_connection, resolve_token


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    # route the factory at a throwaway DuckDB system DB
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo
    repo = source_connections_repo()
    repo.create(id="c1", name="kbc", source_type="keboola",
                config={"stack_url": "https://a.example.com"},
                token_env="MY_KBC_TOKEN", is_default=True)
    repo.create(id="c2", name="kbc_eu", source_type="keboola",
                config={"stack_url": "https://eu.example.com"})
    return repo


def test_resolves_explicit_then_default(seeded_repo):
    assert resolve_connection("keboola", "c2")["name"] == "kbc_eu"
    assert resolve_connection("keboola", None)["name"] == "kbc"      # default
    assert resolve_connection("bigquery", None) is None              # none registered


def test_token_chain_vault_then_env(seeded_repo, monkeypatch):
    conn = resolve_connection("keboola", None)
    monkeypatch.setenv("MY_KBC_TOKEN", "tok-from-env")
    assert resolve_token(conn) == "tok-from-env"                     # env fallback
    from src.repositories import connection_secrets_repo
    connection_secrets_repo().upsert("c1", "tok-from-vault")
    assert resolve_token(conn) == "tok-from-vault"                   # vault wins
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_connection_resolver.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement**

```python
# src/connection_resolver.py
"""Resolve table_registry rows to a named connection + credentials.

Resolution (spec 2026-06-12 §3.2):
  connection_id -> that connection; NULL -> default for source_type;
  nothing registered -> None (caller falls back to the legacy env path
  during the deprecation window).
Token chain: vault -> token_env -> None.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def resolve_connection(source_type: str, connection_id: Optional[str]) -> Optional[Dict[str, Any]]:
    from src.repositories import source_connections_repo
    repo = source_connections_repo()
    if connection_id:
        return repo.get(connection_id)
    return repo.get_default(source_type)


def resolve_token(connection: Dict[str, Any]) -> Optional[str]:
    from src.repositories import connection_secrets_repo
    secret = connection_secrets_repo().get(connection["id"])
    if secret:
        return secret
    token_env = connection.get("token_env")
    if token_env:
        return os.environ.get(token_env) or None
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_connection_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/connection_resolver.py tests/test_connection_resolver.py
git commit -m "feat: connection + token resolver (vault -> token_env chain)"
```

---

### Task 6: First-boot seeding + deprecation warning

**Files:**
- Create: `app/connections_seed.py`
- Modify: `app/main.py` (one call in the startup seed block, right after the `ensure_internal_tables_registered` block at `app/main.py:411-417`)
- Test: `tests/test_connections_seed.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connections_seed.py
"""First-boot seeding: env/yaml -> default connections; idempotent."""
import pytest

from app.connections_seed import seed_default_connections


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo
    return source_connections_repo()


def test_seeds_keboola_from_env_normalized(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com/")
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    seed_default_connections()
    row = fresh_registry.get_by_name("keboola")
    assert row["is_default"] is True
    assert row["config"]["stack_url"] == "https://connection.example.com"  # slash gone
    assert row["token_env"] == "KEBOOLA_STORAGE_TOKEN"
    assert fresh_registry.get_by_name("bigquery") is None


def test_seeding_is_idempotent(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com")
    seed_default_connections()
    seed_default_connections()                          # second boot
    assert len(fresh_registry.list(source_type="keboola")) == 1


def test_existing_registry_not_overwritten(fresh_registry, monkeypatch):
    fresh_registry.create(id="c9", name="keboola", source_type="keboola",
                          config={"stack_url": "https://admin-set.example.com"},
                          is_default=True)
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://env-says.example.com")
    seed_default_connections()                          # must be a no-op + warn
    assert fresh_registry.get_by_name("keboola")["config"]["stack_url"] == "https://admin-set.example.com"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_connections_seed.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement**

```python
# app/connections_seed.py
"""First-boot seeding of default source connections (spec 2026-06-12 §3.4).

One-time: if no connection of a given source_type exists, seed it from
today's env vars / instance.yaml. Afterwards the registry is the sole
source of truth; a set-but-ignored env var earns a deprecation WARNING
(step 1 of the three-step env retirement).
"""
from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger(__name__)


def _yaml_value(*path: str) -> str:
    try:
        from app.instance_config import get_value
        return str(get_value(*path, default="") or "")
    except Exception:
        return ""


def seed_default_connections() -> None:
    from src.connection_specs import validate_connection_config
    from src.repositories import source_connections_repo

    repo = source_connections_repo()

    # --- keboola ---
    stack_url = os.environ.get("KEBOOLA_STACK_URL", "") or _yaml_value(
        "data_source", "keboola", "stack_url"
    )
    existing = repo.list(source_type="keboola")
    if existing:
        if stack_url and all(
            r["config"].get("stack_url") != stack_url.rstrip("/") for r in existing
        ):
            logger.warning(
                "KEBOOLA_STACK_URL is set but connections are managed in the "
                "registry (/admin/connections); the env value is ignored."
            )
    elif stack_url:
        cfg = validate_connection_config("keboola", {"stack_url": stack_url})
        repo.create(
            id=str(uuid.uuid4()), name="keboola", source_type="keboola",
            config=cfg, token_env="KEBOOLA_STORAGE_TOKEN",
            is_default=True, created_by="seed",
        )
        logger.info("Seeded default keboola connection from env/yaml")

    # --- bigquery ---
    project = os.environ.get("BIGQUERY_PROJECT", "") or _yaml_value(
        "data_source", "bigquery", "project"
    )
    if project and not repo.list(source_type="bigquery"):
        cfg = validate_connection_config("bigquery", {
            "project": project,
            "location": os.environ.get("BIGQUERY_LOCATION", "")
                        or _yaml_value("data_source", "bigquery", "location") or "us",
        })
        billing = _yaml_value("data_source", "bigquery", "billing_project")
        if billing:
            cfg["billing_project"] = billing
        repo.create(
            id=str(uuid.uuid4()), name="bigquery", source_type="bigquery",
            config=cfg, is_default=True, created_by="seed",
        )
        logger.info("Seeded default bigquery connection from env/yaml")
```

- [ ] **Step 4: Wire into startup**

In `app/main.py`, directly after the `ensure_internal_tables_registered` try-block (`app/main.py:411-417`), add:

```python
    # Seed default source connections from env/yaml on first boot
    # (spec 2026-06-12 §3.4). One-time; the registry rules afterwards.
    try:
        from app.connections_seed import seed_default_connections
        seed_default_connections()
    except Exception:
        logger.exception("source-connection seed failed; continuing")
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_connections_seed.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add app/connections_seed.py app/main.py tests/test_connections_seed.py
git commit -m "feat: first-boot seeding of default source connections + env deprecation warning"
```

---

### Task 7: CHANGELOG + full suite + wrap-up

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

- [ ] **Step 1: CHANGELOG bullet**

Under `## [Unreleased]` → `### Added`:

```markdown
- Named source connections (phase 1/5): `source_connections` + vault-backed
  `connection_secrets` registry (DuckDB v74 + Alembic), per-type config
  validation with URL normalization, connection/token resolver, and
  first-boot seeding of `keboola`/`bigquery` defaults from env/yaml.
  Invisible in this phase — extraction switches over in phase 2.
```

- [ ] **Step 2: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green (pre-existing failures: confirm with `git stash` they reproduce on a clean tree, note in PR body, don't fix here)

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG for source connections phase 1"
```

- [ ] **Step 4: Review + PR**

Run `/agnes-review` on the branch diff; fix findings; open the PR referencing the spec. Release-cut decision happens at merge time per `docs/RELEASING.md`.

---

## Out of scope for this plan (later phases)

- Phase 2: Keboola extraction/sync per connection (`extracts/<name>/`, `_remote_attach.connection`)
- Phase 3: BigQuery per connection
- Phase 4: Admin REST + UI + CLI + MCP + secret endpoints
- Phase 5: docs + deprecation + `KBC_STACK_URL` cleanup

Each gets its own plan once the previous phase merges.
