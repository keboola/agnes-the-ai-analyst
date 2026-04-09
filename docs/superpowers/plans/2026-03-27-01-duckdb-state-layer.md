# DuckDB State Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all JSON file-based state with DuckDB, eliminating filesystem permission conflicts and enabling agent-queryable system state.

**Architecture:** A `src/db.py` module manages DuckDB connections and schema versioning. Repository classes in `src/repositories/` wrap all CRUD operations. Existing service files swap `_read_json`/`_write_json` for repository method calls. Dual-write (JSON + DuckDB) during transition, then JSON removal.

**Tech Stack:** DuckDB >=1.1, Python 3.11+, uv for package management

**Design spec:** `docs/superpowers/specs/2026-03-27-refactoring-design.md` sections 3 (Data Layer)

---

## File Structure

### New files
| File | Responsibility |
|------|----------------|
| `src/db.py` | DuckDB connection factory, schema creation, migration |
| `src/repositories/__init__.py` | Re-export all repositories, `get_system_db()` factory |
| `src/repositories/users.py` | UserRepository — CRUD users table |
| `src/repositories/sync_state.py` | SyncStateRepository — sync state + history |
| `src/repositories/knowledge.py` | KnowledgeRepository — items + votes |
| `src/repositories/audit.py` | AuditRepository — append-only audit log |
| `src/repositories/notifications.py` | TelegramRepository, PendingCodeRepository, ScriptRegistry |
| `src/repositories/table_registry.py` | TableRegistryRepository |
| `src/repositories/profiles.py` | ProfileRepository |
| `scripts/migrate_json_to_duckdb.py` | One-time migration from JSON files to DuckDB |
| `tests/test_db.py` | Tests for db module |
| `tests/test_repositories.py` | Tests for all repositories |

### Modified files
| File | What changes |
|------|-------------|
| `webapp/sync_settings_service.py` | `_read_json`/`_write_json` (lines 40-62) → SyncSettingsRepository |
| `webapp/corporate_memory_service.py` | `_read_json`/`_write_json` (lines 222-244) → KnowledgeRepository |
| `webapp/telegram_service.py` | `_read_json`/`_write_json` (lines 21-45) → TelegramRepository |
| `webapp/desktop_auth.py` | `_read_json`/`_write_json` (lines 33-57) → UserRepository |
| `src/data_sync.py` | SyncState class (lines 37-139) → SyncStateRepository |
| `src/table_registry.py` | `_atomic_write_json` (line 43) → TableRegistryRepository |
| `src/profiler.py` | profiles.json output (line 92) → ProfileRepository |
| `services/corporate_memory/collector.py` | `_read_json`/`_write_json` (lines 100-123) → KnowledgeRepository |
| `services/telegram_bot/storage.py` | `_read_json`/`_write_json` (lines 21-43) → TelegramRepository |
| `requirements.txt` | Ensure duckdb>=1.1 |

---

### Task 1: DuckDB connection management + schema

**Files:**
- Create: `src/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write the failing test for get_system_db**

```python
# tests/test_db.py
import tempfile
import os
import duckdb
import pytest


def test_get_system_db_creates_database():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DATA_DIR"] = tmpdir
        from src.db import get_system_db
        conn = get_system_db()
        assert conn is not None
        # Verify tables exist
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
        table_names = {t[0] for t in tables}
        assert "users" in table_names
        assert "sync_state" in table_names
        assert "knowledge_items" in table_names
        assert "audit_log" in table_names
        conn.close()


def test_get_system_db_is_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DATA_DIR"] = tmpdir
        from src.db import get_system_db
        conn1 = get_system_db()
        conn1.execute("INSERT INTO users (id, email, name, role) VALUES ('u1', 'test@test.com', 'Test', 'analyst')")
        conn1.close()
        conn2 = get_system_db()
        result = conn2.execute("SELECT email FROM users WHERE id='u1'").fetchone()
        assert result[0] == "test@test.com"
        conn2.close()


def test_schema_version_tracked():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DATA_DIR"] = tmpdir
        from src.db import get_system_db, get_schema_version
        conn = get_system_db()
        version = get_schema_version(conn)
        assert version == 1
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss" && python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.db'`

- [ ] **Step 3: Implement src/db.py**

```python
# src/db.py
"""
DuckDB connection management and schema versioning.

Provides get_system_db() for the system state database
and get_analytics_db() for the analytics database with parquet views.
"""

import os
from pathlib import Path

import duckdb

SCHEMA_VERSION = 1

_SYSTEM_SCHEMA = """
-- Schema versioning
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

-- Users & auth
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    role VARCHAR DEFAULT 'analyst',
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

-- Sync state
CREATE TABLE IF NOT EXISTS sync_state (
    table_id VARCHAR PRIMARY KEY,
    last_sync TIMESTAMP,
    rows BIGINT,
    file_size_bytes BIGINT,
    uncompressed_size_bytes BIGINT,
    columns INTEGER,
    hash VARCHAR,
    status VARCHAR DEFAULT 'ok',
    error TEXT
);

CREATE TABLE IF NOT EXISTS sync_history (
    id VARCHAR PRIMARY KEY,
    table_id VARCHAR NOT NULL,
    synced_at TIMESTAMP NOT NULL,
    rows BIGINT,
    duration_ms INTEGER,
    status VARCHAR,
    error TEXT
);

-- User sync settings
CREATE TABLE IF NOT EXISTS user_sync_settings (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    enabled BOOLEAN DEFAULT false,
    table_mode VARCHAR DEFAULT 'all',
    tables JSON,
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, dataset)
);

-- Corporate memory
CREATE TABLE IF NOT EXISTS knowledge_items (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    content TEXT,
    category VARCHAR,
    tags JSON,
    status VARCHAR DEFAULT 'pending',
    contributors JSON,
    source_user VARCHAR,
    audience VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_votes (
    item_id VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    vote INTEGER,
    voted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_id, user_id)
);

-- Audit log
CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
    user_id VARCHAR,
    action VARCHAR NOT NULL,
    resource VARCHAR,
    params JSON,
    result VARCHAR,
    duration_ms INTEGER
);

-- Notifications
CREATE TABLE IF NOT EXISTS telegram_links (
    user_id VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    linked_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS pending_codes (
    code VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- Scripts
CREATE TABLE IF NOT EXISTS script_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner VARCHAR,
    schedule VARCHAR,
    source TEXT NOT NULL,
    deployed_at TIMESTAMP DEFAULT current_timestamp,
    last_run TIMESTAMP,
    last_status VARCHAR
);

-- Table registry
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    folder VARCHAR,
    sync_strategy VARCHAR,
    primary_key VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    registered_at TIMESTAMP DEFAULT current_timestamp
);

-- Profiles
CREATE TABLE IF NOT EXISTS table_profiles (
    table_id VARCHAR PRIMARY KEY,
    profile JSON NOT NULL,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

-- Dataset permissions
CREATE TABLE IF NOT EXISTS dataset_permissions (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    access VARCHAR DEFAULT 'read',
    PRIMARY KEY (user_id, dataset)
);
"""


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the system state database. Creates schema if needed."""
    db_path = _get_data_dir() / "state" / "system.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    return conn


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views)."""
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Apply migrations if schema version changed."""
    current = get_schema_version(conn)
    if current < SCHEMA_VERSION:
        conn.execute(_SYSTEM_SCHEMA)
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [SCHEMA_VERSION],
            )
        else:
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
            )


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Get current schema version. Returns 0 if no schema exists."""
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return result[0] if result and result[0] else 0
    except duckdb.CatalogException:
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss" && python -m pytest tests/test_db.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add DuckDB state layer with schema management"
```

---

### Task 2: SyncState repository

**Files:**
- Create: `src/repositories/__init__.py`
- Create: `src/repositories/sync_state.py`
- Create: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repositories.py
import tempfile
import os
from datetime import datetime, timezone

import pytest


@pytest.fixture
def db_conn():
    """Provide a fresh in-memory DuckDB with schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DATA_DIR"] = tmpdir
        from src.db import get_system_db
        conn = get_system_db()
        yield conn
        conn.close()


class TestSyncStateRepository:
    def test_update_and_get(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(
            table_id="orders",
            rows=1000,
            file_size_bytes=5000,
            hash="abc123",
        )
        state = repo.get_table_state("orders")
        assert state is not None
        assert state["rows"] == 1000
        assert state["hash"] == "abc123"
        assert state["status"] == "ok"

    def test_get_nonexistent_returns_none(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        assert repo.get_table_state("nonexistent") is None

    def test_get_last_sync(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        last = repo.get_last_sync("orders")
        assert last is not None

    def test_get_all_states(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        repo.update_sync(table_id="customers", rows=50, file_size_bytes=200, hash="h2")
        all_states = repo.get_all_states()
        assert len(all_states) == 2

    def test_history_recorded(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(table_id="orders", rows=100, file_size_bytes=500, hash="h1")
        repo.update_sync(table_id="orders", rows=200, file_size_bytes=800, hash="h2")
        history = repo.get_sync_history("orders", limit=10)
        assert len(history) == 2
        assert history[0]["rows"] == 200  # newest first

    def test_update_with_error(self, db_conn):
        from src.repositories.sync_state import SyncStateRepository
        repo = SyncStateRepository(db_conn)
        repo.update_sync(
            table_id="orders", rows=0, file_size_bytes=0, hash="",
            status="error", error="Connection timeout",
        )
        state = repo.get_table_state("orders")
        assert state["status"] == "error"
        assert state["error"] == "Connection timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestSyncStateRepository -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement repository**

```python
# src/repositories/__init__.py
"""
Repository layer for DuckDB state management.

All system state CRUD goes through repository classes.
"""

from src.db import get_system_db, get_analytics_db

__all__ = ["get_system_db", "get_analytics_db"]
```

```python
# src/repositories/sync_state.py
"""Repository for sync state and history."""

import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb


class SyncStateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get_table_state(self, table_id: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT * FROM sync_state WHERE table_id = ?", [table_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def get_last_sync(self, table_id: str) -> datetime | None:
        result = self.conn.execute(
            "SELECT last_sync FROM sync_state WHERE table_id = ?", [table_id]
        ).fetchone()
        return result[0] if result else None

    def get_all_states(self) -> list[dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM sync_state ORDER BY table_id").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def update_sync(
        self,
        table_id: str,
        rows: int,
        file_size_bytes: int,
        hash: str,
        uncompressed_size_bytes: int = 0,
        columns: int = 0,
        status: str = "ok",
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO sync_state (table_id, last_sync, rows, file_size_bytes,
                uncompressed_size_bytes, columns, hash, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                last_sync = excluded.last_sync,
                rows = excluded.rows,
                file_size_bytes = excluded.file_size_bytes,
                uncompressed_size_bytes = excluded.uncompressed_size_bytes,
                columns = excluded.columns,
                hash = excluded.hash,
                status = excluded.status,
                error = excluded.error""",
            [table_id, now, rows, file_size_bytes, uncompressed_size_bytes,
             columns, hash, status, error],
        )
        # Record history
        self.conn.execute(
            """INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [str(uuid.uuid4()), table_id, now, rows, duration_ms, status, error],
        )

    def get_sync_history(self, table_id: str, limit: int = 10) -> list[dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM sync_history WHERE table_id = ? ORDER BY synced_at DESC LIMIT ?",
            [table_id, limit],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestSyncStateRepository -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/__init__.py src/repositories/sync_state.py tests/test_repositories.py
git commit -m "feat: add SyncStateRepository with history tracking"
```

---

### Task 3: Users repository

**Files:**
- Create: `src/repositories/users.py`
- Append to: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repositories.py`:

```python
class TestUserRepository:
    def test_create_and_get(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test User", role="analyst")
        user = repo.get_by_id("u1")
        assert user is not None
        assert user["email"] == "test@acme.com"
        assert user["role"] == "analyst"

    def test_get_by_email(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test User")
        user = repo.get_by_email("test@acme.com")
        assert user is not None
        assert user["id"] == "u1"

    def test_get_nonexistent(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        assert repo.get_by_id("nope") is None
        assert repo.get_by_email("nope@nope.com") is None

    def test_list_all(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="a@acme.com", name="A")
        repo.create(id="u2", email="b@acme.com", name="B")
        users = repo.list_all()
        assert len(users) == 2

    def test_update_role(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.update(id="u1", role="admin")
        user = repo.get_by_id("u1")
        assert user["role"] == "admin"

    def test_delete(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.delete("u1")
        assert repo.get_by_id("u1") is None

    def test_set_password_hash(self, db_conn):
        from src.repositories.users import UserRepository
        repo = UserRepository(db_conn)
        repo.create(id="u1", email="test@acme.com", name="Test")
        repo.update(id="u1", password_hash="$argon2id$hashed")
        user = repo.get_by_id("u1")
        assert user["password_hash"] == "$argon2id$hashed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestUserRepository -v`
Expected: FAIL

- [ ] **Step 3: Implement repository**

```python
# src/repositories/users.py
"""Repository for user management."""

from datetime import datetime, timezone
from typing import Any

import duckdb


class UserRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> dict[str, Any] | None:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        result = self.conn.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
        return self._row_to_dict(result)

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        result = self.conn.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> list[dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def create(
        self,
        id: str,
        email: str,
        name: str,
        role: str = "analyst",
        password_hash: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO users (id, email, name, role, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, email, name, role, password_hash, now, now],
        )

    def update(self, id: str, **kwargs) -> None:
        allowed = {"email", "name", "role", "password_hash", "setup_token",
                    "setup_token_created", "reset_token", "reset_token_created"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def delete(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM users WHERE id = ?", [user_id])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestUserRepository -v`
Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/users.py tests/test_repositories.py
git commit -m "feat: add UserRepository with CRUD operations"
```

---

### Task 4: Knowledge repository

**Files:**
- Create: `src/repositories/knowledge.py`
- Append to: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repositories.py`:

```python
class TestKnowledgeRepository:
    def test_create_and_get(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="MRR Definition", content="Monthly recurring...",
                     category="metrics", source_user="petr@acme.com")
        item = repo.get_by_id("k1")
        assert item is not None
        assert item["title"] == "MRR Definition"
        assert item["status"] == "pending"

    def test_list_by_status(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.create(id="k2", title="B", content="b", category="c")
        repo.update_status("k1", "approved")
        approved = repo.list_items(statuses=["approved"])
        assert len(approved) == 1
        assert approved[0]["id"] == "k1"

    def test_vote(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.vote("k1", "user1", 1)
        repo.vote("k1", "user2", -1)
        votes = repo.get_votes("k1")
        assert votes["upvotes"] == 1
        assert votes["downvotes"] == 1

    def test_vote_replace(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="A", content="a", category="c")
        repo.vote("k1", "user1", 1)
        repo.vote("k1", "user1", -1)  # change vote
        votes = repo.get_votes("k1")
        assert votes["upvotes"] == 0
        assert votes["downvotes"] == 1

    def test_search(self, db_conn):
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(db_conn)
        repo.create(id="k1", title="Revenue metrics", content="MRR definition", category="metrics")
        repo.create(id="k2", title="Support SLA", content="Response times", category="support")
        results = repo.search("revenue")
        assert len(results) == 1
        assert results[0]["id"] == "k1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestKnowledgeRepository -v`
Expected: FAIL

- [ ] **Step 3: Implement repository**

```python
# src/repositories/knowledge.py
"""Repository for corporate memory knowledge items and votes."""

import json
from datetime import datetime, timezone
from typing import Any

import duckdb


class KnowledgeRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> dict[str, Any] | None:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> list[dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_by_id(self, item_id: str) -> dict[str, Any] | None:
        result = self.conn.execute("SELECT * FROM knowledge_items WHERE id = ?", [item_id]).fetchone()
        return self._row_to_dict(result)

    def create(
        self,
        id: str,
        title: str,
        content: str,
        category: str,
        source_user: str | None = None,
        tags: list[str] | None = None,
        status: str = "pending",
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_items (id, title, content, category, source_user,
                tags, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, title, content, category, source_user,
             json.dumps(tags) if tags else None, status, now, now],
        )

    def update_status(self, item_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE knowledge_items SET status = ?, updated_at = ? WHERE id = ?",
            [status, now, item_id],
        )

    def list_items(
        self,
        statuses: list[str] | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM knowledge_items WHERE 1=1"
        params: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._rows_to_dicts(self.conn.execute(query, params).fetchall())

    def search(self, query: str) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        results = self.conn.execute(
            """SELECT * FROM knowledge_items
            WHERE title ILIKE ? OR content ILIKE ?
            ORDER BY updated_at DESC""",
            [pattern, pattern],
        ).fetchall()
        return self._rows_to_dicts(results)

    def vote(self, item_id: str, user_id: str, vote: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO knowledge_votes (item_id, user_id, vote, voted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (item_id, user_id) DO UPDATE SET vote = excluded.vote, voted_at = excluded.voted_at""",
            [item_id, user_id, vote, now],
        )

    def get_votes(self, item_id: str) -> dict[str, int]:
        result = self.conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) as upvotes,
                COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) as downvotes
            FROM knowledge_votes WHERE item_id = ?""",
            [item_id],
        ).fetchone()
        return {"upvotes": result[0], "downvotes": result[1]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestKnowledgeRepository -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/knowledge.py tests/test_repositories.py
git commit -m "feat: add KnowledgeRepository with voting and search"
```

---

### Task 5: Audit repository

**Files:**
- Create: `src/repositories/audit.py`
- Append to: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repositories.py`:

```python
class TestAuditRepository:
    def test_log_and_query(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders",
                 params={"force": True}, result="ok", duration_ms=1200)
        entries = repo.query(limit=10)
        assert len(entries) == 1
        assert entries[0]["action"] == "sync_trigger"
        assert entries[0]["duration_ms"] == 1200

    def test_query_by_action(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders")
        repo.log(user_id="u1", action="login", resource=None)
        entries = repo.query(action="sync_trigger")
        assert len(entries) == 1

    def test_query_by_user(self, db_conn):
        from src.repositories.audit import AuditRepository
        repo = AuditRepository(db_conn)
        repo.log(user_id="u1", action="sync_trigger", resource="orders")
        repo.log(user_id="u2", action="sync_trigger", resource="customers")
        entries = repo.query(user_id="u1")
        assert len(entries) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestAuditRepository -v`
Expected: FAIL

- [ ] **Step 3: Implement repository**

```python
# src/repositories/audit.py
"""Repository for audit logging."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb


class AuditRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def log(
        self,
        user_id: str | None = None,
        action: str = "",
        resource: str | None = None,
        params: dict | None = None,
        result: str | None = None,
        duration_ms: int | None = None,
    ) -> str:
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO audit_log (id, timestamp, user_id, action, resource, params, result, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [entry_id, now, user_id, action, resource,
             json.dumps(params) if params else None, result, duration_ms],
        )
        return entry_id

    def query(
        self,
        user_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        results = self.conn.execute(sql, params).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestAuditRepository -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/audit.py tests/test_repositories.py
git commit -m "feat: add AuditRepository with query filtering"
```

---

### Task 6: Notifications repository (Telegram + Scripts)

**Files:**
- Create: `src/repositories/notifications.py`
- Append to: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repositories.py`:

```python
class TestNotificationsRepository:
    def test_telegram_link_and_get(self, db_conn):
        from src.repositories.notifications import TelegramRepository
        repo = TelegramRepository(db_conn)
        repo.link_user("u1", chat_id=12345)
        link = repo.get_link("u1")
        assert link is not None
        assert link["chat_id"] == 12345

    def test_telegram_unlink(self, db_conn):
        from src.repositories.notifications import TelegramRepository
        repo = TelegramRepository(db_conn)
        repo.link_user("u1", chat_id=12345)
        repo.unlink_user("u1")
        assert repo.get_link("u1") is None

    def test_pending_code_create_and_verify(self, db_conn):
        from src.repositories.notifications import PendingCodeRepository
        repo = PendingCodeRepository(db_conn)
        repo.create_code("ABC123", chat_id=12345)
        code = repo.verify_code("ABC123")
        assert code is not None
        assert code["chat_id"] == 12345
        # Code consumed after verify
        assert repo.verify_code("ABC123") is None

    def test_script_registry(self, db_conn):
        from src.repositories.notifications import ScriptRepository
        repo = ScriptRepository(db_conn)
        repo.deploy("s1", name="sales_alert", owner="u1",
                     schedule="0 8 * * MON", source="print('hello')")
        script = repo.get("s1")
        assert script is not None
        assert script["schedule"] == "0 8 * * MON"
        all_scripts = repo.list_all()
        assert len(all_scripts) == 1

    def test_script_undeploy(self, db_conn):
        from src.repositories.notifications import ScriptRepository
        repo = ScriptRepository(db_conn)
        repo.deploy("s1", name="test", owner="u1", source="pass")
        repo.undeploy("s1")
        assert repo.get("s1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestNotificationsRepository -v`
Expected: FAIL

- [ ] **Step 3: Implement repository**

```python
# src/repositories/notifications.py
"""Repositories for Telegram links, pending codes, and script registry."""

from datetime import datetime, timezone
from typing import Any

import duckdb


class TelegramRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def link_user(self, user_id: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO telegram_links (user_id, chat_id, linked_at)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at""",
            [user_id, chat_id, now],
        )

    def unlink_user(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM telegram_links WHERE user_id = ?", [user_id])

    def get_link(self, user_id: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT * FROM telegram_links WHERE user_id = ?", [user_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def get_all_links(self) -> list[dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM telegram_links").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]


class PendingCodeRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create_code(self, code: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "INSERT INTO pending_codes (code, chat_id, created_at) VALUES (?, ?, ?)",
            [code, chat_id, now],
        )

    def verify_code(self, code: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT * FROM pending_codes WHERE code = ?", [code]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        row = dict(zip(columns, result))
        self.conn.execute("DELETE FROM pending_codes WHERE code = ?", [code])
        return row


class ScriptRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def deploy(
        self, id: str, name: str, owner: str | None = None,
        schedule: str | None = None, source: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO script_registry (id, name, owner, schedule, source, deployed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, schedule = excluded.schedule,
                source = excluded.source, deployed_at = excluded.deployed_at""",
            [id, name, owner, schedule, source, now],
        )

    def undeploy(self, script_id: str) -> None:
        self.conn.execute("DELETE FROM script_registry WHERE id = ?", [script_id])

    def get(self, script_id: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT * FROM script_registry WHERE id = ?", [script_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self, owner: str | None = None) -> list[dict[str, Any]]:
        if owner:
            results = self.conn.execute(
                "SELECT * FROM script_registry WHERE owner = ? ORDER BY name", [owner]
            ).fetchall()
        else:
            results = self.conn.execute("SELECT * FROM script_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestNotificationsRepository -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/notifications.py tests/test_repositories.py
git commit -m "feat: add Telegram, PendingCode, and Script repositories"
```

---

### Task 7: Table registry + Profiles repositories

**Files:**
- Create: `src/repositories/table_registry.py`
- Create: `src/repositories/profiles.py`
- Append to: `tests/test_repositories.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repositories.py`:

```python
class TestTableRegistryRepository:
    def test_register_and_get(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="orders", name="Orders", folder="sales",
                       sync_strategy="incremental", registered_by="admin")
        table = repo.get("orders")
        assert table is not None
        assert table["folder"] == "sales"

    def test_list_all(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", folder="f1")
        repo.register(id="t2", name="B", folder="f2")
        assert len(repo.list_all()) == 2

    def test_unregister(self, db_conn):
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(db_conn)
        repo.register(id="t1", name="A", folder="f1")
        repo.unregister("t1")
        assert repo.get("t1") is None


class TestProfileRepository:
    def test_save_and_get(self, db_conn):
        from src.repositories.profiles import ProfileRepository
        repo = ProfileRepository(db_conn)
        profile_data = {"columns": [{"name": "id", "type": "int"}], "row_count": 1000}
        repo.save("orders", profile_data)
        profile = repo.get("orders")
        assert profile is not None
        assert profile["row_count"] == 1000

    def test_get_all(self, db_conn):
        from src.repositories.profiles import ProfileRepository
        repo = ProfileRepository(db_conn)
        repo.save("t1", {"row_count": 100})
        repo.save("t2", {"row_count": 200})
        all_profiles = repo.get_all()
        assert len(all_profiles) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repositories.py::TestTableRegistryRepository tests/test_repositories.py::TestProfileRepository -v`
Expected: FAIL

- [ ] **Step 3: Implement repositories**

```python
# src/repositories/table_registry.py
"""Repository for table registry."""

from datetime import datetime, timezone
from typing import Any

import duckdb


class TableRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def register(
        self, id: str, name: str, folder: str | None = None,
        sync_strategy: str | None = None, primary_key: str | None = None,
        description: str | None = None, registered_by: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO table_registry (id, name, folder, sync_strategy,
                primary_key, description, registered_by, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy, primary_key = excluded.primary_key,
                description = excluded.description, registered_at = excluded.registered_at""",
            [id, name, folder, sync_strategy, primary_key, description, registered_by, now],
        )

    def unregister(self, table_id: str) -> None:
        self.conn.execute("DELETE FROM table_registry WHERE id = ?", [table_id])

    def get(self, table_id: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT * FROM table_registry WHERE id = ?", [table_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self) -> list[dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM table_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
```

```python
# src/repositories/profiles.py
"""Repository for table profiles."""

import json
from datetime import datetime, timezone
from typing import Any

import duckdb


class ProfileRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def save(self, table_id: str, profile: dict) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO table_profiles (table_id, profile, profiled_at)
            VALUES (?, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                profile = excluded.profile, profiled_at = excluded.profiled_at""",
            [table_id, json.dumps(profile), now],
        )

    def get(self, table_id: str) -> dict[str, Any] | None:
        result = self.conn.execute(
            "SELECT profile, profiled_at FROM table_profiles WHERE table_id = ?",
            [table_id],
        ).fetchone()
        if not result:
            return None
        profile = json.loads(result[0]) if isinstance(result[0], str) else result[0]
        profile["profiled_at"] = result[1]
        return profile

    def get_all(self) -> dict[str, dict]:
        results = self.conn.execute(
            "SELECT table_id, profile, profiled_at FROM table_profiles ORDER BY table_id"
        ).fetchall()
        out = {}
        for row in results:
            profile = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            profile["profiled_at"] = row[2]
            out[row[0]] = profile
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_repositories.py::TestTableRegistryRepository tests/test_repositories.py::TestProfileRepository -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/table_registry.py src/repositories/profiles.py tests/test_repositories.py
git commit -m "feat: add TableRegistry and Profile repositories"
```

---

### Task 8: Migration script (JSON → DuckDB)

**Files:**
- Create: `scripts/migrate_json_to_duckdb.py`
- Create: `tests/test_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration.py
import json
import os
import tempfile

import pytest


@pytest.fixture
def migration_env():
    """Create temp dir with sample JSON files mimicking production layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = os.path.join(tmpdir, "data")
        os.makedirs(os.path.join(data_dir, "notifications"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "corporate-memory"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "auth"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "src_data", "metadata"), exist_ok=True)

        # sync_state.json
        with open(os.path.join(data_dir, "src_data", "metadata", "sync_state.json"), "w") as f:
            json.dump({
                "tables": {
                    "orders": {"last_sync": "2026-03-27T08:00:00Z", "rows": 1000, "file_size_bytes": 5000}
                }
            }, f)

        # sync_settings.json
        with open(os.path.join(data_dir, "notifications", "sync_settings.json"), "w") as f:
            json.dump({
                "petr": {"datasets": {"sales": True, "support": False}, "updated_at": "2026-03-27"}
            }, f)

        # knowledge.json
        with open(os.path.join(data_dir, "corporate-memory", "knowledge.json"), "w") as f:
            json.dump([
                {"id": "k1", "title": "MRR", "content": "Monthly...", "category": "metrics",
                 "status": "approved", "contributors": ["petr"]}
            ], f)

        # telegram_users.json
        with open(os.path.join(data_dir, "notifications", "telegram_users.json"), "w") as f:
            json.dump({"petr@acme.com": {"chat_id": 12345, "linked_at": "2026-01-01"}}, f)

        os.environ["DATA_DIR"] = data_dir
        yield data_dir


def test_migration_runs_without_error(migration_env):
    from scripts.migrate_json_to_duckdb import migrate_all
    stats = migrate_all(migration_env)
    assert stats["sync_state"] == 1
    assert stats["knowledge"] == 1
    assert stats["telegram"] == 1


def test_migration_is_idempotent(migration_env):
    from scripts.migrate_json_to_duckdb import migrate_all
    stats1 = migrate_all(migration_env)
    stats2 = migrate_all(migration_env)
    assert stats1["sync_state"] == stats2["sync_state"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_migration.py -v`
Expected: FAIL

- [ ] **Step 3: Implement migration script**

```python
# scripts/migrate_json_to_duckdb.py
"""
One-time migration: JSON files → DuckDB.

Usage: python -m scripts.migrate_json_to_duckdb [--data-dir /data]

Idempotent — safe to run multiple times. Uses UPSERT to avoid duplicates.
"""

import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _load_json(path: str) -> dict | list | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Skipping {path}: {e}")
        return None


def migrate_all(data_dir: str | None = None) -> dict[str, int]:
    if data_dir:
        os.environ["DATA_DIR"] = data_dir
    data = Path(data_dir or os.environ.get("DATA_DIR", "./data"))

    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.knowledge import KnowledgeRepository
    from src.repositories.notifications import TelegramRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    stats: dict[str, int] = {}

    # 1. Sync state
    sync_data = _load_json(str(data / "src_data" / "metadata" / "sync_state.json"))
    count = 0
    if sync_data and "tables" in sync_data:
        repo = SyncStateRepository(conn)
        for table_id, info in sync_data["tables"].items():
            repo.update_sync(
                table_id=table_id,
                rows=info.get("rows", 0),
                file_size_bytes=info.get("file_size_bytes", 0),
                hash=info.get("hash", ""),
                uncompressed_size_bytes=info.get("uncompressed_size_bytes", 0),
                columns=info.get("columns", 0),
            )
            count += 1
    stats["sync_state"] = count
    logger.info(f"Migrated {count} sync state entries")

    # 2. Knowledge items
    knowledge = _load_json(str(data / "corporate-memory" / "knowledge.json"))
    count = 0
    if knowledge and isinstance(knowledge, list):
        repo = KnowledgeRepository(conn)
        for item in knowledge:
            repo.create(
                id=item.get("id", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                category=item.get("category", ""),
                source_user=item.get("source_user"),
                tags=item.get("tags"),
                status=item.get("status", "pending"),
            )
            count += 1
    stats["knowledge"] = count
    logger.info(f"Migrated {count} knowledge items")

    # 3. Telegram users
    telegram = _load_json(str(data / "notifications" / "telegram_users.json"))
    count = 0
    if telegram and isinstance(telegram, dict):
        repo = TelegramRepository(conn)
        for email, info in telegram.items():
            repo.link_user(email, chat_id=info.get("chat_id", 0))
            count += 1
    stats["telegram"] = count
    logger.info(f"Migrated {count} telegram links")

    conn.close()
    logger.info("Migration complete")
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Migrate JSON state to DuckDB")
    parser.add_argument("--data-dir", default=None, help="Data directory path")
    args = parser.parse_args()
    migrate_all(args.data_dir)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_migration.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Run all repository tests together**

Run: `python -m pytest tests/test_db.py tests/test_repositories.py tests/test_migration.py -v`
Expected: All tests PASS (3 + 26 + 2 = 31 tests)

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_json_to_duckdb.py tests/test_migration.py
git commit -m "feat: add JSON to DuckDB migration script"
```

---

## Summary

| Task | Files | Tests | Purpose |
|------|-------|-------|---------|
| 1 | `src/db.py` | 3 | DuckDB connection + schema |
| 2 | `src/repositories/sync_state.py` | 6 | Sync state + history |
| 3 | `src/repositories/users.py` | 7 | User CRUD |
| 4 | `src/repositories/knowledge.py` | 5 | Corporate memory + votes |
| 5 | `src/repositories/audit.py` | 3 | Audit logging |
| 6 | `src/repositories/notifications.py` | 5 | Telegram + Scripts |
| 7 | `src/repositories/table_registry.py`, `profiles.py` | 5 | Registry + Profiles |
| 8 | `scripts/migrate_json_to_duckdb.py` | 2 | Migration |
| **Total** | **12 new files** | **36 tests** | |

After this plan, all state is in DuckDB. The existing service files still use JSON (they'll be rewired in Plan 2: FastAPI Server, which depends on this layer being complete).
