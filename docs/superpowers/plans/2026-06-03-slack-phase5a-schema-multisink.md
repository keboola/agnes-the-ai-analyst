# Phase 5a — Schema v68→v69 + Multi-Sink Fan-Out + Stdin Lock (Co-Presence Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Land the additive v68→v69 schema (three `chat_sessions`/`chat_messages` columns + the `chat_session_participants` table) across both backends, refactor `ChatManager` to fan out runner frames to N sinks behind a single stdin lock, and thread `sender_email` through persistence — the reusable foundation that single-principal Slack threads and (later) co-drive both ride.

**Architecture:** Schema is additive-only — guarded `ADD COLUMN`s + one table, mirrored in `_SYSTEM_SCHEMA` (fresh installs build v69 directly), the DuckDB ladder `_v68_to_v69`, and an Alembic step that reaches the same endpoint. `LiveSession.ws` becomes `LiveSession.sinks: list[SinkEntry]`; `_pump_subprocess_to_ws` snapshots and broadcasts to every sink while persistence stays singular (one `append_message` per assistant turn regardless of sink count). `attach` keeps a `*, is_primary=True` parameter (per spec §6.3) so 5b co-drive can call it for the seat-conditionally path; in 5a the primary sink is always seated. `send_user_message` gains `sender_email` and serializes the stdin write+drain under an `asyncio.Lock`. Every new repo method lands in DuckDB (`app/chat/persistence.py`) and Postgres (`src/repositories/chat_session_participants_pg.py`) in the same task with cross-engine contract tests.

**Tech Stack:** Python 3.11, FastAPI, DuckDB 1.5.x (in-process, single-worker), Postgres + SQLAlchemy + Alembic, asyncio, pytest (`asyncio.run()` convention, no pytest-asyncio).

---

## File Structure

**Modified**
- `src/db.py` — bump `SCHEMA_VERSION` 68→69; add `_v68_to_v69`; wire into both ladder dispatch sites; mirror DDL into `_SYSTEM_SCHEMA`.
- `app/chat/types.py` — `ChatSession.is_co_session`/`ephemeral` (default False), `ChatMessage.sender_email` (Optional), new `SessionParticipant` dataclass.
- `app/chat/persistence.py` — `_SESSION_SELECT`/`_SESSION_GROUP`/`_row_to_session` carry the two flags; `append_message`/`list_messages` carry `sender_email`; participant CRUD + `fork_session_as_co_session`; `hard_delete_user_sessions` deletes participants first.
- `src/repositories/chat_sessions_pg.py` — `_row_to_session` carries the two flags; FK cascade handles participant cleanup.
- `src/repositories/chat_messages_pg.py` — `append_message`/`list_messages` carry `sender_email` (no `_row_to_message` helper exists — both inline their own `ChatMessage(...)`).
- `app/chat/manager.py` — `SinkEntry`; `LiveSession.sinks` + `_stdin_lock` (both **appended after** the existing defaulted fields to avoid a dataclass field-ordering error); `attach(*, is_primary=True)`; `add_sink`; `_pump_subprocess_to_ws` multi-sink broadcast + `_broadcast`/`_safe_close`; `send_user_message(*, sender_email=None)`; `cancel`/`_wait_for_exit_and_respawn`/`_run_auto_title` broadcast; `active_count_for_user`.
- `tests/test_chat_manager.py` — convert the 4 `LiveSession(ws=ws)` constructor sites (lines ~367, ~421, ~472, ~583) to `sinks=[SinkEntry(...)]`.
- `tests/test_admin_chat.py` — convert the `LiveSession(ws=MagicMock())` constructor site (line ~151) to `sinks=[SinkEntry(...)]`.
- `tests/test_db_schema_version.py` — expect 69.
- `CHANGELOG.md` — `[Unreleased]` bullet.

**Created**
- `src/repositories/chat_session_participants_pg.py` — Postgres participant repository.
- `migrations/versions/0016_cloud_chat_v69.py` — Alembic step (`down_revision="0015_cloud_chat_v68"`).
- `tests/test_chat_v69_migration.py` — DuckDB migration-path coverage (v68 DB → v69, fresh-install shape).
- `tests/test_chat_multisink.py` — `ChatManager` multi-sink + stdin-lock + `sender_email` unit tests.

Cross-engine contract tests are appended to the existing `tests/db_pg/test_chat_pg.py`.

**Explicitly deferred to 5b (NOT in 5a scope):** Spec §7 (line 434) requires the participant pre-delete step *also* in "the ephemeral-GC sweep". No GC sweep exists in the codebase yet (`grep -rn "ephemeral\|reclaim\|gc" app/chat src/repositories` returns nothing for a sweep). 5a adds the pre-delete only to `hard_delete_user_sessions`; the GC-sweep half lands with 5b's ephemeral-workspace lifecycle, which is where the sweep itself is introduced. This is an intentional, documented deferral, not an omission.

---

## Task 1 — DuckDB schema v68→v69 (migration ladder + `_SYSTEM_SCHEMA`)

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_chat_v69_migration.py` (Create), `tests/test_db_schema_version.py` (Modify)

> **Line numbers below are approximate** (the worktree has drifted ~1–15 lines from the original spec anchors). Every Edit is anchored by surrounding text; locate the quoted `old_string` rather than trusting the cited line. Confirmed live anchors at authoring time: `SCHEMA_VERSION = 68` near line 50; `_v67_to_v68` body ends near line 4781; fast-path `_v67_to_v68(conn)` near line 5096; sequential `if current < 68:` near lines 5284–5285; `_SYSTEM_SCHEMA` `chat_sessions` end near lines 1133–1134, `chat_messages` near 1147–1148, `idx_chat_messages_session` near 1149–1150.

- [ ] Write a failing fresh-install test. Create `tests/test_chat_v69_migration.py`:
```python
"""DuckDB v68→v69 migration coverage: co-presence columns + participants table."""
import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _cols(conn, table):
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def test_fresh_install_has_v69_shape(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION == 69
    assert {"is_co_session", "ephemeral"} <= _cols(conn, "chat_sessions")
    assert "sender_email" in _cols(conn, "chat_messages")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "chat_session_participants" in tables
    conn.close()
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_chat_v69_migration.py::test_fresh_install_has_v69_shape -v` → fails because `SCHEMA_VERSION == 68` and the new columns/table don't exist.
- [ ] Bump the version. In `src/db.py`, change the `SCHEMA_VERSION = 68` line (near line 50) to `SCHEMA_VERSION = 69`.
- [ ] Add the migration function. Insert `_v68_to_v69` immediately after the end of the `_v67_to_v68` function body (near line 4781):
```python
def _v68_to_v69(conn: duckdb.DuckDBPyConnection) -> None:
    """v69: live co-drive foundation — co-session flags + participants table.

    Additive-only and forward-safe on populated prod DBs:
    - chat_sessions.is_co_session / ephemeral (BOOLEAN NOT NULL DEFAULT FALSE)
    - chat_messages.sender_email (VARCHAR, nullable; backfilled to the
      session owner for existing role='user' rows — every pre-v69 session
      is single-principal)
    - chat_session_participants table + index

    Each ADD COLUMN is PRAGMA-guarded because this migration may re-run on a
    partially-migrated DB (the ladder is idempotent). DuckDB has no
    ON DELETE CASCADE, so ChatRepository.hard_delete_user_sessions deletes
    participant rows by hand (see Task 4).
    """
    sess_cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info('chat_sessions')").fetchall()
    }
    if "is_co_session" not in sess_cols:
        conn.execute(
            "ALTER TABLE chat_sessions ADD COLUMN is_co_session BOOLEAN NOT NULL DEFAULT FALSE"
        )
    if "ephemeral" not in sess_cols:
        conn.execute(
            "ALTER TABLE chat_sessions ADD COLUMN ephemeral BOOLEAN NOT NULL DEFAULT FALSE"
        )
    msg_cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info('chat_messages')").fetchall()
    }
    if "sender_email" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN sender_email VARCHAR")
        # Backfill: pre-v69 user turns are owned by the session's user_email.
        conn.execute(
            "UPDATE chat_messages SET sender_email = ("
            "  SELECT s.user_email FROM chat_sessions s WHERE s.id = chat_messages.session_id"
            ") WHERE role = 'user' AND sender_email IS NULL"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_session_participants (
            id          VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
            user_email  VARCHAR NOT NULL,
            user_id     VARCHAR NOT NULL,
            role        VARCHAR NOT NULL,
            joined_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            left_at     TIMESTAMP,
            UNIQUE (session_id, user_email)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_session_participants_user "
        "ON chat_session_participants(user_email, session_id)"
    )
    conn.execute("UPDATE schema_version SET version = 69")
```
- [ ] Wire it into the fast-path ladder. Find the line `_v67_to_v68(conn)` in the fast-path block (near line 5096) and add immediately after it:
```python
            # v68→v69: live co-drive foundation — co-session flags +
            # chat_session_participants. Additive; _SYSTEM_SCHEMA builds it
            # on fresh installs (no-op here).
            _v68_to_v69(conn)
```
- [ ] Wire it into the sequential ladder. Find the `if current < 68:` block (near lines 5284–5285) that calls `_v67_to_v68(conn)` and add immediately after that block:
```python
            if current < 69:
                _v68_to_v69(conn)
```
- [ ] Mirror the columns into `_SYSTEM_SCHEMA`'s `chat_sessions`. Locate the `chat_sessions` DDL block inside `_SYSTEM_SCHEMA` (its closing `archived ... );` is near lines 1133–1134). Change the closing two lines so the table ends with the two new columns. Replace:
```python
    message_count    INTEGER NOT NULL DEFAULT 0,
    archived         BOOLEAN NOT NULL DEFAULT FALSE
);
```
with:
```python
    message_count    INTEGER NOT NULL DEFAULT 0,
    archived         BOOLEAN NOT NULL DEFAULT FALSE,
    is_co_session    BOOLEAN NOT NULL DEFAULT FALSE,
    ephemeral        BOOLEAN NOT NULL DEFAULT FALSE
);
```
- [ ] Mirror `sender_email` into `_SYSTEM_SCHEMA`'s `chat_messages`. Locate the `chat_messages` DDL block (near lines 1147–1148). Replace:
```python
    model       VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
with:
```python
    model       VARCHAR,
    sender_email VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
- [ ] Mirror the participants table into `_SYSTEM_SCHEMA`. Immediately after the `CREATE INDEX ... idx_chat_messages_session ...` statement (near lines 1149–1150), add the new table + index DDL into the same `_SYSTEM_SCHEMA` string:
```sql
CREATE TABLE IF NOT EXISTS chat_session_participants (
    id          VARCHAR PRIMARY KEY,
    session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
    user_email  VARCHAR NOT NULL,
    user_id     VARCHAR NOT NULL,
    role        VARCHAR NOT NULL,
    joined_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    left_at     TIMESTAMP,
    UNIQUE (session_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_chat_session_participants_user
    ON chat_session_participants(user_email, session_id);
```
- [ ] Run the fresh-install test, expect PASS: `.venv/bin/pytest tests/test_chat_v69_migration.py::test_fresh_install_has_v69_shape -v`.
- [ ] Add a failing upgrade-path test for the v68→v69 step + backfill. Append to `tests/test_chat_v69_migration.py`:
```python
def test_v68_db_migrates_to_v69_with_backfill(tmp_path):
    """A pre-existing v68 DB upgrades cleanly: new columns default FALSE,
    sender_email backfills to the owner for existing user turns, and the
    participants table is created."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (68)")
    conn.execute("""CREATE TABLE chat_sessions (
        id VARCHAR PRIMARY KEY, user_email VARCHAR NOT NULL, surface VARCHAR NOT NULL,
        slack_channel_id VARCHAR, slack_thread_ts VARCHAR, title VARCHAR,
        started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_message_at TIMESTAMP, message_count INTEGER NOT NULL DEFAULT 0,
        archived BOOLEAN NOT NULL DEFAULT FALSE
    )""")
    conn.execute("""CREATE TABLE chat_messages (
        id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL REFERENCES chat_sessions(id),
        role VARCHAR NOT NULL, content TEXT NOT NULL, tool_calls JSON,
        tokens_in INTEGER, tokens_out INTEGER, model VARCHAR,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("INSERT INTO chat_sessions (id, user_email, surface) VALUES ('s1', 'owner@x.com', 'web')")
    conn.execute("INSERT INTO chat_messages (id, session_id, role, content) VALUES ('m1', 's1', 'user', 'hi')")
    conn.execute("INSERT INTO chat_messages (id, session_id, role, content) VALUES ('m2', 's1', 'assistant', 'hello')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION == 69
    assert {"is_co_session", "ephemeral"} <= _cols(conn, "chat_sessions")
    flags = conn.execute("SELECT is_co_session, ephemeral FROM chat_sessions WHERE id = 's1'").fetchone()
    assert flags == (False, False)
    # user turn backfilled to owner; assistant turn left NULL.
    user_sender = conn.execute("SELECT sender_email FROM chat_messages WHERE id = 'm1'").fetchone()[0]
    asst_sender = conn.execute("SELECT sender_email FROM chat_messages WHERE id = 'm2'").fetchone()[0]
    assert user_sender == "owner@x.com"
    assert asst_sender is None
    conn.close()
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_chat_v69_migration.py::test_v68_db_migrates_to_v69_with_backfill -v`.
- [ ] Update the schema-version guard test. In `tests/test_db_schema_version.py`, find the `assert SCHEMA_VERSION >= 66` assertion and change it to `assert SCHEMA_VERSION >= 69`, adding a comment line above it: `# v68 → v69: live co-drive foundation — chat_session_participants + is_co_session/ephemeral/sender_email.`
- [ ] Run the schema-version test, expect PASS: `.venv/bin/pytest tests/test_db_schema_version.py -v`.
- [ ] Commit:
```
git add src/db.py tests/test_chat_v69_migration.py tests/test_db_schema_version.py
git commit -m "schema: v68->v69 DuckDB ladder — co-session flags + chat_session_participants"
```

---

## Task 2 — Alembic step `0016_cloud_chat_v69` (Postgres, same endpoint)

**Files:**
- Create: `migrations/versions/0016_cloud_chat_v69.py`
- Test: covered by the alembic-head fixture in `tests/db_pg/test_chat_pg.py` (Task 6)

- [ ] Create `migrations/versions/0016_cloud_chat_v69.py`:
```python
"""Live co-drive foundation (DuckDB v69 parity).

Additive-only, reaching the same endpoint as DuckDB ``_v68_to_v69``:
  - chat_sessions.is_co_session / ephemeral (BOOLEAN NOT NULL DEFAULT FALSE)
  - chat_messages.sender_email (VARCHAR, nullable; backfilled to the owner
    for existing role='user' rows)
  - chat_session_participants table (FK → chat_sessions ON DELETE CASCADE,
    a constraint the DuckDB side cannot express — DuckDB deletes child
    participant rows by hand in ChatRepository.hard_delete_user_sessions).

Revision ID: 0016_cloud_chat_v69
Revises: 0015_cloud_chat_v68
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_cloud_chat_v69"
down_revision: Union[str, None] = "0015_cloud_chat_v68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "is_co_session", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "ephemeral", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
    )
    op.add_column(
        "chat_messages", sa.Column("sender_email", sa.String(), nullable=True)
    )
    op.execute(
        "UPDATE chat_messages SET sender_email = s.user_email "
        "FROM chat_sessions s "
        "WHERE s.id = chat_messages.session_id "
        "AND chat_messages.role = 'user' "
        "AND chat_messages.sender_email IS NULL"
    )
    op.create_table(
        "chat_session_participants",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "user_email", name="uq_participant_session_user"),
    )
    op.create_index(
        "idx_chat_session_participants_user",
        "chat_session_participants",
        ["user_email", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_chat_session_participants_user", "chat_session_participants")
    op.drop_table("chat_session_participants")
    op.drop_column("chat_messages", "sender_email")
    op.drop_column("chat_sessions", "ephemeral")
    op.drop_column("chat_sessions", "is_co_session")
```
- [ ] Verify the revision chains to head without error: `.venv/bin/python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config('alembic.ini'); c.set_main_option('script_location','migrations'); s=ScriptDirectory.from_config(c); print(s.get_current_head())"` → expect `0016_cloud_chat_v69`.
- [ ] Commit:
```
git add migrations/versions/0016_cloud_chat_v69.py
git commit -m "alembic: 0016 cloud chat v69 — co-session flags + participants table"
```

---

## Task 3 — Dataclasses: flags, `sender_email`, `SessionParticipant`

**Files:**
- Modify: `app/chat/types.py`
- Test: `tests/test_chat_v69_migration.py` (Modify — add a dataclass default test)

- [ ] Write a failing dataclass test. Append to `tests/test_chat_v69_migration.py`:
```python
def test_dataclass_defaults_and_participant():
    from app.chat.types import ChatMessage, ChatSession, SessionParticipant
    import inspect

    sig = inspect.signature(ChatSession)
    assert sig.parameters["is_co_session"].default is False
    assert sig.parameters["ephemeral"].default is False
    assert inspect.signature(ChatMessage).parameters["sender_email"].default is None
    p = SessionParticipant(
        id="p1", session_id="s1", user_email="a@x.com", user_id="u1",
        role="owner", joined_at=None, left_at=None,
    )
    assert p.role == "owner" and p.left_at is None
```
- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_chat_v69_migration.py::test_dataclass_defaults_and_participant -v` → `TypeError`/`ImportError` (fields/class missing).
- [ ] Add the flags to `ChatSession`. In `app/chat/types.py`, the `ChatSession` dataclass currently ends with `archived: bool` (line 34). Change that trailing line to add the two defaulted fields after it:
```python
    message_count: int
    archived: bool
    is_co_session: bool = False
    ephemeral: bool = False
```
- [ ] Add `sender_email` to `ChatMessage`. In `app/chat/types.py`, the `ChatMessage` dataclass currently ends with `created_at: datetime` (line 47). Change the trailing two lines to:
```python
    model: Optional[str]
    created_at: datetime
    sender_email: Optional[str] = None
```
- [ ] Add the `SessionParticipant` dataclass. Append to `app/chat/types.py` after the `UserWorkdir` dataclass:
```python
@dataclass
class SessionParticipant:
    id: str
    session_id: str
    user_email: str
    user_id: str
    role: str  # 'owner' | 'collaborator'
    joined_at: Optional[datetime]
    left_at: Optional[datetime]  # None = active
```
- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_chat_v69_migration.py::test_dataclass_defaults_and_participant -v`.
- [ ] Commit:
```
git add app/chat/types.py tests/test_chat_v69_migration.py
git commit -m "chat types: co-session flags, sender_email, SessionParticipant"
```

---

## Task 4 — DuckDB persistence: flags + `sender_email` plumbing, participant CRUD, fork, cascade

**Files:**
- Modify: `app/chat/persistence.py`
- Test: `tests/test_chat_persistence.py` (Modify — add DuckDB-only unit tests)

- [ ] Write failing DuckDB persistence tests. Append to `tests/test_chat_persistence.py`:
```python
def test_v69_flags_default_false_and_sender_email_roundtrip(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    assert s.is_co_session is False and s.ephemeral is False
    repo.append_message(session_id=s.id, role="user", content="hi", sender_email="b@x.com")
    msgs = repo.list_messages(s.id)
    assert msgs[0].sender_email == "b@x.com"


def test_participant_crud_and_list_for_participant(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    repo.add_session_participant(session_id=s.id, user_email="o@x.com", user_id="u-o", role="owner")
    repo.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    active = repo.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com", "c@x.com"}
    repo.update_participant_role(s.id, "c@x.com", "owner")
    assert {p.role for p in repo.get_session_participants(s.id)} == {"owner"}
    repo.remove_participant(s.id, "c@x.com")
    active = repo.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com"}
    assert s.id in {x.id for x in repo.list_sessions_for_participant("o@x.com")}


def test_fork_session_as_co_session(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="o@x.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id, owner_email="o@x.com", owner_user_id="u-o",
        invitee_email="c@x.com", invitee_user_id="u-c", seed_summary="prior context",
    )
    # S0 untouched.
    assert repo.get_session(s0.id).is_co_session is False
    # S1 flags set, two participant rows, summary seeded as a system message.
    assert s1.is_co_session is True and s1.ephemeral is True
    parts = repo.get_session_participants(s1.id)
    assert {(p.user_email, p.role) for p in parts} == {("o@x.com", "owner"), ("c@x.com", "collaborator")}
    seeded = repo.list_messages(s1.id)
    assert seeded and seeded[0].content == "prior context"


def test_hard_delete_removes_participants_first(tmp_path):
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    s = repo.create_session(user_email="gone@x.com", surface=Surface.WEB)
    repo.add_session_participant(session_id=s.id, user_email="gone@x.com", user_id="u-g", role="owner")
    repo.append_message(session_id=s.id, role="user", content="bye", sender_email="gone@x.com")
    n = repo.hard_delete_user_sessions("gone@x.com")
    assert n == 1
    remaining = conn.execute(
        "SELECT COUNT(*) FROM chat_session_participants WHERE session_id = ?", [s.id]
    ).fetchone()[0]
    assert remaining == 0
```
- [ ] Run them, expect FAIL: `.venv/bin/pytest tests/test_chat_persistence.py -k "v69_flags or participant_crud or fork_session or removes_participants" -v` → `AttributeError`/wrong row shape.
- [ ] Carry the flags in `_SESSION_SELECT`/`_SESSION_GROUP`. In `app/chat/persistence.py` (lines 52–64), replace the two module constants:
```python
_SESSION_SELECT = (
    "SELECT s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, "
    "MAX(m.created_at) AS last_message_at, "
    "COUNT(m.id) AS message_count, "
    "s.archived, s.is_co_session, s.ephemeral "
    "FROM chat_sessions s "
    "LEFT JOIN chat_messages m ON m.session_id = s.id"
)
_SESSION_GROUP = (
    " GROUP BY s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
    "s.title, s.started_at, s.archived, s.is_co_session, s.ephemeral"
)
```
- [ ] Carry the flags in `_row_to_session`. In `app/chat/persistence.py` (lines 43–45), replace the trailing return lines:
```python
        message_count=int(row[8]) if row[8] is not None else 0,
        archived=bool(row[9]),
        is_co_session=bool(row[10]),
        ephemeral=bool(row[11]),
    )
```
- [ ] Carry `sender_email` on `append_message` (DuckDB branch). In `app/chat/persistence.py`, add `sender_email: Optional[str] = None` to the `append_message` signature (after the `model` param, line 327), forward it to the PG delegate (add `sender_email=sender_email` to the delegate call near line 337), change the DuckDB INSERT (lines 351–358) and the returned `ChatMessage` (lines 359–362):
```python
        self._conn.execute(
            "INSERT INTO chat_messages "
            "(id, session_id, role, content, tool_calls, tokens_in, tokens_out, model, sender_email, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [msg_id, session_id, role, content,
             json.dumps(tool_calls) if tool_calls else None,
             tokens_in, tokens_out, model, sender_email, now],
        )
        return ChatMessage(
            id=msg_id, session_id=session_id, role=role, content=content,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            model=model, created_at=now, sender_email=sender_email,
        )
```
- [ ] Carry `sender_email` on `list_messages` (DuckDB branch). In `app/chat/persistence.py`, change the SELECT column list (lines 380–383) and the row mapping (lines 392–398). Replace:
```python
        q = (
            "SELECT id, session_id, role, content, tool_calls, tokens_in, tokens_out, "
            "model, created_at FROM chat_messages WHERE session_id = ?"
        )
```
with:
```python
        q = (
            "SELECT id, session_id, role, content, tool_calls, tokens_in, tokens_out, "
            "model, sender_email, created_at FROM chat_messages WHERE session_id = ?"
        )
```
and replace the comprehension:
```python
        return [
            ChatMessage(
                id=r[0], session_id=r[1], role=r[2], content=r[3],
                tool_calls=json.loads(r[4]) if r[4] else None,
                tokens_in=r[5], tokens_out=r[6], model=r[7],
                sender_email=r[8], created_at=r[9],
            )
            for r in rows
        ]
```
- [ ] Make `hard_delete_user_sessions` delete participants first (DuckDB branch). In `app/chat/persistence.py`, in the DuckDB branch (lines 302–314), insert the participant delete BEFORE the existing `chat_messages` delete. Replace:
```python
        self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
```
with:
```python
        # DuckDB has no ON DELETE CASCADE. Delete participant rows first so
        # the chat_session_participants FK can't block the parent delete.
        self._conn.execute(
            "DELETE FROM chat_session_participants WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
        self._conn.execute(
            "DELETE FROM chat_messages WHERE session_id IN ("
            " SELECT id FROM chat_sessions WHERE user_email = ?)",
            [user_email],
        )
```
- [ ] Wire the PG participant delegate. In `app/chat/persistence.py::ChatRepository.__init__`, add `self._participants_pg = None` alongside the other `= None` initializers at lines 84–86, and add a matching `self._participants_pg = None` in the `except` fallback at lines 103–105. Then inside the `if use_pg():` body (after `self._workdirs_pg = UserWorkdirPgRepository(engine)`, line 99), add:
```python
                from src.repositories.chat_session_participants_pg import (
                    ChatSessionParticipantPgRepository,
                )
                self._participants_pg = ChatSessionParticipantPgRepository(engine)
```
- [ ] Add participant CRUD + fork to `ChatRepository`. Add a new section after the `# --- messages` block (after `list_messages`, near line 399, before `# --- workdirs`):
```python
    # --- participants ------------------------------------------------------

    def add_session_participant(
        self, *, session_id: str, user_email: str, user_id: str, role: str,
    ) -> SessionParticipant:
        if self._participants_pg is not None:
            return self._participants_pg.add_session_participant(
                session_id=session_id, user_email=user_email,
                user_id=user_id, role=role,
            )
        pid = _gen_id("part")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_session_participants "
            "(id, session_id, user_email, user_id, role, joined_at, left_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            [pid, session_id, user_email, user_id, role, now],
        )
        return SessionParticipant(
            id=pid, session_id=session_id, user_email=user_email,
            user_id=user_id, role=role, joined_at=now, left_at=None,
        )

    def get_session_participants(self, session_id: str) -> list[SessionParticipant]:
        """Active participants (left_at IS NULL) for a session — the live
        membership set co-drive authorization reads as its source of truth."""
        if self._participants_pg is not None:
            return self._participants_pg.get_session_participants(session_id)
        rows = self._conn.execute(
            "SELECT id, session_id, user_email, user_id, role, joined_at, left_at "
            "FROM chat_session_participants "
            "WHERE session_id = ? AND left_at IS NULL "
            "ORDER BY joined_at ASC",
            [session_id],
        ).fetchall()
        return [
            SessionParticipant(
                id=r[0], session_id=r[1], user_email=r[2], user_id=r[3],
                role=r[4], joined_at=r[5], left_at=r[6],
            )
            for r in rows
        ]

    def remove_participant(self, session_id: str, user_email: str) -> None:
        """Stamp left_at so the participant is no longer active. Idempotent."""
        if self._participants_pg is not None:
            self._participants_pg.remove_participant(session_id, user_email)
            return
        self._conn.execute(
            "UPDATE chat_session_participants SET left_at = ? "
            "WHERE session_id = ? AND user_email = ? AND left_at IS NULL",
            [datetime.now(timezone.utc), session_id, user_email],
        )

    def update_participant_role(self, session_id: str, user_email: str, role: str) -> None:
        if self._participants_pg is not None:
            self._participants_pg.update_participant_role(session_id, user_email, role)
            return
        self._conn.execute(
            "UPDATE chat_session_participants SET role = ? "
            "WHERE session_id = ? AND user_email = ? AND left_at IS NULL",
            [role, session_id, user_email],
        )

    def list_sessions_for_participant(self, user_email: str) -> list[ChatSession]:
        """Co-sessions where this email is an active participant."""
        if self._participants_pg is not None:
            return self._participants_pg.list_sessions_for_participant(user_email)
        ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT session_id FROM chat_session_participants "
                "WHERE user_email = ? AND left_at IS NULL",
                [user_email],
            ).fetchall()
        ]
        out: list[ChatSession] = []
        for sid in ids:
            s = self.get_session(sid)
            if s is not None:
                out.append(s)
        return out

    def fork_session_as_co_session(
        self,
        source_id: str,
        *,
        owner_email: str,
        owner_user_id: str,
        invitee_email: str,
        invitee_user_id: str,
        seed_summary: Optional[str] = None,
    ) -> ChatSession:
        """Create a fresh co-session (is_co_session=TRUE, ephemeral=TRUE) with
        the owner + invitee as participants. Never blind-clones the source
        transcript (SR-8): seeds only an optional intersection-produced
        ``seed_summary`` as a system message. The source session is untouched.

        DuckDB has no multi-statement transaction guard here; steps are ordered
        so a partial failure leaves at most a harmless empty ephemeral session
        that the GC sweep (5b) reclaims.
        """
        if self._participants_pg is not None:
            return self._participants_pg.fork_session_as_co_session(
                source_id, owner_email=owner_email, owner_user_id=owner_user_id,
                invitee_email=invitee_email, invitee_user_id=invitee_user_id,
                seed_summary=seed_summary,
            )
        chat_id = _gen_id("chat")
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO chat_sessions "
            "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
            "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
            "VALUES (?, ?, 'web', NULL, NULL, NULL, ?, NULL, 0, FALSE, TRUE, TRUE)",
            [chat_id, owner_email, now],
        )
        self.add_session_participant(
            session_id=chat_id, user_email=owner_email, user_id=owner_user_id, role="owner",
        )
        self.add_session_participant(
            session_id=chat_id, user_email=invitee_email, user_id=invitee_user_id, role="collaborator",
        )
        if seed_summary:
            self.append_message(
                session_id=chat_id, role="system", content=seed_summary,
            )
        fetched = self.get_session(chat_id)
        assert fetched is not None
        return fetched
```
- [ ] Add `SessionParticipant` to the persistence imports. In `app/chat/persistence.py` (line 23 currently imports `ChatMessage, ChatSession, Surface, UserWorkdir`), change the import to:
```python
from app.chat.types import ChatMessage, ChatSession, SessionParticipant, Surface, UserWorkdir
```
- [ ] Run the DuckDB persistence tests, expect PASS: `.venv/bin/pytest tests/test_chat_persistence.py -k "v69_flags or participant_crud or fork_session or removes_participants" -v`.
- [ ] Run the full persistence test module to confirm no regression on existing reads: `.venv/bin/pytest tests/test_chat_persistence.py --tb=short -q`.
- [ ] Commit:
```
git add app/chat/persistence.py tests/test_chat_persistence.py
git commit -m "chat persistence (DuckDB): participant CRUD, fork, sender_email, flags, cascade"
```

---

## Task 5 — Postgres parity: `chat_sessions_pg`, `chat_messages_pg`, new participants repo

**Files:**
- Create: `src/repositories/chat_session_participants_pg.py`
- Modify: `src/repositories/chat_sessions_pg.py`, `src/repositories/chat_messages_pg.py`

(Tested via the cross-engine contract tests in Task 6 — the PG-only repos cannot run under DuckDB, so their gate is the parametrized contract suite.)

- [ ] Carry the flags in `chat_sessions_pg._row_to_session`. In `src/repositories/chat_sessions_pg.py` (lines 39–40 end the return with `message_count=` / `archived=`), replace those trailing lines:
```python
        message_count=int(row["message_count"]) if row["message_count"] is not None else 0,
        archived=bool(row["archived"]),
        is_co_session=bool(row["is_co_session"]),
        ephemeral=bool(row["ephemeral"]),
    )
```
(The `SELECT *` reads in this repo already surface the new columns; no SELECT edits needed.)
- [ ] Carry `sender_email` on `chat_messages_pg.append_message`. This file has **no** `_row_to_message` helper — `append_message` and `list_messages` each inline their own `ChatMessage(...)`. In `src/repositories/chat_messages_pg.py`, add `sender_email: Optional[str] = None` to the `append_message` signature (after the `model` param, line 54). Change the INSERT statement (lines 60–66) to add the column + bind param:
```python
                sa.text(
                    "INSERT INTO chat_messages "
                    "(id, session_id, role, content, tool_calls, tokens_in, "
                    "tokens_out, model, sender_email, created_at) "
                    "VALUES (:id, :session_id, :role, :content, "
                    "CAST(:tool_calls AS JSONB), :tokens_in, :tokens_out, "
                    ":model, :sender_email, :created_at)"
                ),
```
Add `"sender_email": sender_email,` to the bound-param dict (after the `"model": model,` line at 76):
```python
                    "model": model,
                    "sender_email": sender_email,
                    "created_at": now,
```
And add `sender_email=sender_email,` to the returned `ChatMessage(...)` (after `model=model,` near line 96):
```python
            model=model,
            sender_email=sender_email,
            created_at=now,
        )
```
- [ ] Carry `sender_email` on `chat_messages_pg.list_messages`. In `src/repositories/chat_messages_pg.py`, change the explicit SELECT column list (lines 111–115) to add `sender_email`:
```python
            sql = (
                "SELECT id, session_id, role, content, tool_calls, tokens_in, "
                "tokens_out, model, sender_email, created_at FROM chat_messages "
                "WHERE session_id = :session_id"
            )
```
And add `sender_email=r["sender_email"],` to the inline `ChatMessage(...)` construction (lines 124–134), after `model=r["model"],`:
```python
                model=r["model"],
                sender_email=r["sender_email"],
                created_at=r["created_at"],
            )
```
- [ ] Create `src/repositories/chat_session_participants_pg.py`:
```python
"""Postgres-backed chat-session-participant repository.

Mirrors the participant + fork operations of
``app/chat/persistence.py::ChatRepository``. Returns ``app.chat.types``
dataclasses so ChatRepository can delegate transparently.

Unlike DuckDB, the FK chat_session_participants.session_id → chat_sessions.id
carries ON DELETE CASCADE (migration 0016), so participant rows are removed
automatically when a session is hard-deleted; the explicit DuckDB pre-delete
makes the same intent visible on the in-process side.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import ChatSession, SessionParticipant, Surface


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _row_to_participant(row) -> SessionParticipant:
    return SessionParticipant(
        id=row["id"],
        session_id=row["session_id"],
        user_email=row["user_email"],
        user_id=row["user_id"],
        role=row["role"],
        joined_at=row["joined_at"],
        left_at=row["left_at"],
    )


def _row_to_session(row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        user_email=row["user_email"],
        surface=Surface(row["surface"]),
        slack_channel_id=row["slack_channel_id"],
        slack_thread_ts=row["slack_thread_ts"],
        title=row["title"],
        started_at=row["started_at"],
        last_message_at=row["last_message_at"],
        message_count=int(row["message_count"]) if row["message_count"] is not None else 0,
        archived=bool(row["archived"]),
        is_co_session=bool(row["is_co_session"]),
        ephemeral=bool(row["ephemeral"]),
    )


class ChatSessionParticipantPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def add_session_participant(
        self, *, session_id: str, user_email: str, user_id: str, role: str,
    ) -> SessionParticipant:
        pid = _gen_id("part")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_session_participants "
                    "(id, session_id, user_email, user_id, role, joined_at, left_at) "
                    "VALUES (:id, :sid, :ue, :uid, :role, :joined, NULL)"
                ),
                {"id": pid, "sid": session_id, "ue": user_email,
                 "uid": user_id, "role": role, "joined": now},
            )
        return SessionParticipant(
            id=pid, session_id=session_id, user_email=user_email,
            user_id=user_id, role=role, joined_at=now, left_at=None,
        )

    def get_session_participants(self, session_id: str) -> list[SessionParticipant]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM chat_session_participants "
                    "WHERE session_id = :sid AND left_at IS NULL "
                    "ORDER BY joined_at ASC"
                ),
                {"sid": session_id},
            ).mappings().all()
        return [_row_to_participant(r) for r in rows]

    def remove_participant(self, session_id: str, user_email: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_session_participants SET left_at = :now "
                    "WHERE session_id = :sid AND user_email = :ue AND left_at IS NULL"
                ),
                {"now": datetime.now(timezone.utc), "sid": session_id, "ue": user_email},
            )

    def update_participant_role(self, session_id: str, user_email: str, role: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chat_session_participants SET role = :role "
                    "WHERE session_id = :sid AND user_email = :ue AND left_at IS NULL"
                ),
                {"role": role, "sid": session_id, "ue": user_email},
            )

    def list_sessions_for_participant(self, user_email: str) -> list[ChatSession]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT s.* FROM chat_sessions s "
                    "JOIN chat_session_participants p ON p.session_id = s.id "
                    "WHERE p.user_email = :ue AND p.left_at IS NULL "
                    "ORDER BY s.last_message_at DESC NULLS LAST, s.started_at DESC"
                ),
                {"ue": user_email},
            ).mappings().all()
        return [_row_to_session(r) for r in rows]

    def fork_session_as_co_session(
        self,
        source_id: str,
        *,
        owner_email: str,
        owner_user_id: str,
        invitee_email: str,
        invitee_user_id: str,
        seed_summary: Optional[str] = None,
    ) -> ChatSession:
        """Atomic fork (single PG transaction): fresh co-session + two
        participant rows + optional seed summary message. Source untouched.
        Never blind-clones the transcript (SR-8). When a summary is seeded,
        the parent rollup columns (message_count / last_message_at) are
        maintained in the same transaction so the co-session row stays
        consistent with how chat_messages_pg.append_message rolls up every
        other message insert."""
        chat_id = _gen_id("chat")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chat_sessions "
                    "(id, user_email, surface, slack_channel_id, slack_thread_ts, title, "
                    "started_at, last_message_at, message_count, archived, is_co_session, ephemeral) "
                    "VALUES (:id, :ue, 'web', NULL, NULL, NULL, :now, NULL, 0, FALSE, TRUE, TRUE)"
                ),
                {"id": chat_id, "ue": owner_email, "now": now},
            )
            for email, uid, role in (
                (owner_email, owner_user_id, "owner"),
                (invitee_email, invitee_user_id, "collaborator"),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_session_participants "
                        "(id, session_id, user_email, user_id, role, joined_at, left_at) "
                        "VALUES (:id, :sid, :ue, :uid, :role, :now, NULL)"
                    ),
                    {"id": _gen_id("part"), "sid": chat_id, "ue": email,
                     "uid": uid, "role": role, "now": now},
                )
            if seed_summary:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat_messages "
                        "(id, session_id, role, content, created_at) "
                        "VALUES (:id, :sid, 'system', :content, :now)"
                    ),
                    {"id": _gen_id("msg"), "sid": chat_id,
                     "content": seed_summary, "now": now},
                )
                # Maintain the parent rollup the same way append_message does,
                # so message_count / last_message_at are not left stale at 0.
                conn.execute(
                    sa.text(
                        "UPDATE chat_sessions "
                        "SET message_count = message_count + 1, last_message_at = :now "
                        "WHERE id = :sid"
                    ),
                    {"now": now, "sid": chat_id},
                )
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM chat_sessions WHERE id = :id"), {"id": chat_id}
            ).mappings().first()
        assert row is not None
        return _row_to_session(row)
```
- [ ] Commit:
```
git add src/repositories/chat_sessions_pg.py src/repositories/chat_messages_pg.py src/repositories/chat_session_participants_pg.py
git commit -m "chat persistence (PG): participants repo, fork, sender_email, co-session flags"
```

---

## Task 6 — Cross-engine contract tests (`tests/db_pg/test_chat_pg.py`)

**Files:**
- Modify: `tests/db_pg/test_chat_pg.py`

- [ ] Add a participants fixture. After the `workdirs` fixture in `tests/db_pg/test_chat_pg.py`, add:
```python
@pytest.fixture
def participants(engine):
    from src.repositories.chat_session_participants_pg import (
        ChatSessionParticipantPgRepository,
    )
    return ChatSessionParticipantPgRepository(engine)
```
- [ ] Add the PG contract tests. Append to `tests/db_pg/test_chat_pg.py`:
```python
# --- v69 co-presence -------------------------------------------------------

def test_session_flags_default_false(sessions):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    assert s.is_co_session is False
    assert s.ephemeral is False
    assert sessions.get_session(s.id).is_co_session is False


def test_sender_email_round_trip(sessions, messages):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    messages.append_message(
        session_id=s.id, role="user", content="hi", sender_email="b@x.com"
    )
    got = messages.list_messages(s.id)[0]
    assert got.sender_email == "b@x.com"


def test_participant_add_list_role_remove(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(
        session_id=s.id, user_email="o@x.com", user_id="u-o", role="owner"
    )
    participants.add_session_participant(
        session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator"
    )
    active = participants.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com", "c@x.com"}
    participants.update_participant_role(s.id, "c@x.com", "owner")
    assert all(p.role == "owner" for p in participants.get_session_participants(s.id) if p.user_email == "c@x.com")
    participants.remove_participant(s.id, "c@x.com")
    assert {p.user_email for p in participants.get_session_participants(s.id)} == {"o@x.com"}


def test_list_sessions_for_participant(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(
        session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator"
    )
    found = participants.list_sessions_for_participant("c@x.com")
    assert s.id in {x.id for x in found}


def test_fork_session_as_co_session_pg(sessions, participants, messages):
    s0 = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        s0.id, owner_email="o@x.com", owner_user_id="u-o",
        invitee_email="c@x.com", invitee_user_id="u-c", seed_summary="prior context",
    )
    assert sessions.get_session(s0.id).is_co_session is False  # source untouched
    assert s1.is_co_session is True and s1.ephemeral is True
    parts = participants.get_session_participants(s1.id)
    assert {(p.user_email, p.role) for p in parts} == {
        ("o@x.com", "owner"), ("c@x.com", "collaborator")
    }
    seeded = messages.list_messages(s1.id)
    assert seeded and seeded[0].content == "prior context"  # summary, not raw clone
    # rollup maintained: the seeded system message bumped message_count.
    assert sessions.get_session(s1.id).message_count == 1


def test_hard_delete_cascades_participants(sessions, participants, engine):
    s = sessions.create_session(user_email="gone@x.com", surface=Surface.WEB)
    participants.add_session_participant(
        session_id=s.id, user_email="gone@x.com", user_id="u-g", role="owner"
    )
    sessions.hard_delete_user_sessions("gone@x.com")
    with engine.connect() as conn:
        remaining = conn.execute(
            sa.text("SELECT COUNT(*) FROM chat_session_participants WHERE session_id = :sid"),
            {"sid": s.id},
        ).scalar()
    assert remaining == 0  # ON DELETE CASCADE


def test_co_session_coexists_with_owner_other_surfaces(sessions, participants):
    """A co-session for an owner does not collide with that owner's existing
    web / slack_dm / slack_thread sessions."""
    web = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    dm = sessions.create_session(
        user_email="o@x.com", surface=Surface.SLACK_DM, slack_channel_id="D1"
    )
    co = participants.fork_session_as_co_session(
        web.id, owner_email="o@x.com", owner_user_id="u-o",
        invitee_email="c@x.com", invitee_user_id="u-c",
    )
    ids = {s.id for s in sessions.list_sessions("o@x.com")}
    assert {web.id, dm.id, co.id} <= ids
```
- [ ] Run the PG contract suite (requires the PG test backend; CI runs it): `.venv/bin/pytest tests/db_pg/test_chat_pg.py -v`. Expect PASS. If the local environment has no Postgres, note it in the PR body and rely on CI — do not skip the assertions.
- [ ] Commit:
```
git add tests/db_pg/test_chat_pg.py
git commit -m "tests(db_pg): cross-engine contract for participants, fork, sender_email, v69 flags"
```

---

## Task 7 — `ChatManager` multi-sink fan-out + `_stdin_lock` + `sender_email`

**Files:**
- Modify: `app/chat/manager.py`, `tests/test_chat_manager.py`, `tests/test_admin_chat.py`
- Test: `tests/test_chat_multisink.py` (Create)

> **Dataclass field-ordering constraint (blocker if ignored).** `LiveSession` declares `started_at: datetime` and `last_activity: datetime` (current lines 50–51) with **no defaults**, after the also-no-default `chat_id/user_email/state/handle` and the `ws: object` field. Every field with a default (`crash_count`, `cancel_event`, `tasks`, `current_pump`, `auto_title_started`) comes *after* `last_activity`. Therefore `sinks` and `_stdin_lock` (both defaulted) **must be appended after `auto_title_started`** — never inserted at `ws`'s position 5 ahead of the no-default `started_at`/`last_activity`, which would raise `TypeError: non-default argument 'started_at' follows default argument` at class-definition time. The `ws: object` field is **removed** (per spec §6.3); all 5 existing `LiveSession(ws=...)` constructor call sites are converted in this task.

- [ ] Write failing multi-sink tests. Create `tests/test_chat_multisink.py`:
```python
"""ChatManager multi-sink fan-out, stdin serialization, sender_email (Phase 5a).

Uses asyncio.run() per the project convention (see tests/test_chat_manager.py).
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager


class FakeSink:
    """Duck-typed sink: records frames and a participant_email."""
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed = True


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5),
    )


def _attach_fake_live(manager: ChatManager, chat_id: str, user_email: str, sink) -> LiveSession:
    """Insert a LiveSession with a fake handle + one sink, bypassing _spawn_runner."""
    from datetime import datetime, timezone
    handle = MagicMock()
    handle.stdin = MagicMock()
    handle.stdin.drain = AsyncMock()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
    )
    manager._live[chat_id] = live
    return live


def test_pump_broadcasts_to_all_sinks_persist_once(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        s1, s2 = FakeSink(), FakeSink()
        live = _attach_fake_live(manager, s.id, "o@x", s1)
        live.sinks.append(SinkEntry(participant_email="c@x", sink=s2))

        frames = [
            json.dumps({"type": "assistant_message", "content": "hello",
                        "tokens_in": 1, "tokens_out": 2}).encode() + b"\n",
            b"",  # EOF
        ]
        live.handle.stdout = MagicMock()
        live.handle.stdout.readline = AsyncMock(side_effect=frames)

        await manager._pump_subprocess_to_ws(live)

        # Both sinks received the assistant frame.
        assert any(f.get("type") == "assistant_message" for f in s1.frames)
        assert any(f.get("type") == "assistant_message" for f in s2.frames)
        # Persistence is singular: exactly one assistant row.
        msgs = manager._repo.list_messages(s.id)
        assert sum(1 for m in msgs if m.role == "assistant") == 1

    asyncio.run(_run())


def test_send_user_message_records_sender_email(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        _attach_fake_live(manager, s.id, "o@x", FakeSink())
        await manager.send_user_message(s.id, "hi from collaborator", sender_email="c@x")
        rows = manager._repo.list_messages(s.id)
        user_rows = [m for m in rows if m.role == "user"]
        assert user_rows[-1].sender_email == "c@x"

    asyncio.run(_run())


def test_send_user_message_defaults_sender_to_owner(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        _attach_fake_live(manager, s.id, "o@x", FakeSink())
        await manager.send_user_message(s.id, "hi")
        user_rows = [m for m in manager._repo.list_messages(s.id) if m.role == "user"]
        assert user_rows[-1].sender_email == "o@x"

    asyncio.run(_run())


def test_stdin_writes_are_serialized(manager: ChatManager):
    """Two concurrent sends must not interleave: each write+drain pair is
    atomic w.r.t. the event loop under _stdin_lock. We assert the bytes
    written are whole JSON lines (no partial-line interleaving)."""
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        live = _attach_fake_live(manager, s.id, "o@x", FakeSink())
        written: list[bytes] = []
        live.handle.stdin.write = lambda b: written.append(b)

        async def slow_drain():
            await asyncio.sleep(0)  # yield, inviting interleave if unlocked

        live.handle.stdin.drain = slow_drain
        await asyncio.gather(
            manager.send_user_message(s.id, "AAAA", sender_email="a@x"),
            manager.send_user_message(s.id, "BBBB", sender_email="b@x"),
        )
        # Each written chunk is exactly one complete JSON line.
        for chunk in written:
            line = chunk.decode().rstrip("\n")
            json.loads(line)  # raises if a chunk is a partial line
        assert len(written) == 2

    asyncio.run(_run())


def test_add_sink_replays_history_before_appending(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="o@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="user", content="q", sender_email="o@x")
        manager._repo.append_message(session_id=s.id, role="assistant", content="a")
        live = _attach_fake_live(manager, s.id, "o@x", FakeSink())

        late = FakeSink()
        await manager.add_sink(s.id, late, "c@x")

        # Late joiner saw the persisted history + a ready frame, and is now in sinks.
        assert "a" in [f.get("content") for f in late.frames if f.get("content")]
        assert any(f.get("type") == "ready" for f in late.frames)
        assert any(e.sink is late for e in live.sinks)

    asyncio.run(_run())
```
- [ ] Run them, expect FAIL: `.venv/bin/pytest tests/test_chat_multisink.py -v` → `ImportError: cannot import name 'SinkEntry'` and `LiveSession` has no `sinks`/`add_sink`.
- [ ] Add the `SinkEntry` dataclass. In `app/chat/manager.py`, after the `SessionNotFound` class (after line 40), add:
```python
@dataclass
class SinkEntry:
    """One output target for a live session's frames. Duck-typed sink:
    a web WebSocket or a SlackSinkBridge — both expose ``send_json`` and
    ``close``. ``participant_email`` attributes the sink to a principal so
    leave/teardown can drop exactly one sink (used by co-drive in 5b)."""
    participant_email: str
    sink: object
```
- [ ] Replace the `ws` field with appended `sinks`/`_stdin_lock`. In `app/chat/manager.py::LiveSession`, **delete** the line `ws: object  # WebSocket; typed loosely to avoid FastAPI import cycle` (line 49). Then **append** the two new fields after the last existing field `auto_title_started: bool = False` (line 63):
```python
    auto_title_started: bool = False
    # Output sinks the runner's frames fan out to. One SinkEntry per
    # attached principal (web WS or SlackSinkBridge). The primary sink is
    # seated by attach(); add_sink() appends late joiners (co-drive, 5b).
    sinks: list["SinkEntry"] = field(default_factory=list)
    # Serializes the stdin write+drain pair so two participants' concurrent
    # turns can never interleave partial JSON lines on the shared stdin
    # (spec §6.2).
    _stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```
- [ ] Update `attach` to take `*, is_primary=True` and seat the primary sink. In `app/chat/manager.py`, change the `attach` signature (line 179) to `async def attach(self, chat_id: str, ws, *, is_primary: bool = True) -> None:`. Change the `LiveSession(...)` construction (lines 188–196) to drop `ws=ws` and pass the sink list. Replace:
```python
        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            ws=ws,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
```
with:
```python
        # 5a seats the primary sink unconditionally. is_primary is part of
        # the spec §6.3 attach contract so 5b co-drive can attach a runner
        # without a primary seat (it seats collaborators via add_sink); 5a
        # never passes is_primary=False, so the primary is always present.
        live = LiveSession(
            chat_id=chat_id,
            user_email=session.user_email,
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=(
                [SinkEntry(participant_email=session.user_email, sink=ws)]
                if is_primary else []
            ),
        )
```
(The `await ws.send_json({"type": "ready"})` line directly below, line 198, is unchanged — `ws` is still the primary sink object in scope.)
- [ ] Rewrite `_pump_subprocess_to_ws` for multi-sink broadcast with singular persistence. In `app/chat/manager.py`, replace the entire body of `_pump_subprocess_to_ws` (lines 317–360) so the send section broadcasts via `_broadcast` while persistence/audit run exactly once, and add the `_broadcast` + `_safe_close` helpers immediately after it:
```python
    async def _pump_subprocess_to_ws(self, live: LiveSession) -> None:
        assert live.handle is not None
        while True:
            line = await live.handle.stdout.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            live.last_activity = datetime.now(timezone.utc)
            await self._broadcast(live, frame)
            if frame.get("type") == "assistant_message":
                self._repo.append_message(
                    session_id=live.chat_id,
                    role="assistant",
                    content=frame.get("content", ""),
                    tool_calls=frame.get("tool_calls"),
                    tokens_in=frame.get("tokens_in"),
                    tokens_out=frame.get("tokens_out"),
                    model=frame.get("model"),
                )
                if not live.auto_title_started:
                    self._maybe_start_auto_title(live)
            elif frame.get("type") == "tool_call":
                write_audit(
                    self._repo._conn,
                    user_email=live.user_email,
                    action="chat.tool_call",
                    details={
                        "session_id": live.chat_id,
                        "tool": frame.get("tool"),
                        "args_hash": hash_args(frame.get("args", {})),
                    },
                )

    async def _broadcast(self, live: LiveSession, frame: dict) -> None:
        """Send a frame to every sink, snapshotting the list first so a
        concurrent add/remove can't mutate it mid-iteration. Dead sinks are
        removed and closed after the loop. A failing sink never aborts the
        broadcast to the others."""
        dead: list[SinkEntry] = []
        for entry in list(live.sinks):
            try:
                await entry.sink.send_json(frame)
            except Exception:
                logger.warning("sink send failed for %s", live.chat_id)
                dead.append(entry)
        for entry in dead:
            if entry in live.sinks:
                live.sinks.remove(entry)
            asyncio.create_task(self._safe_close(entry.sink))

    @staticmethod
    async def _safe_close(sink) -> None:
        try:
            await sink.close()
        except Exception:
            pass
```
- [ ] Add `add_sink` with replay-before-append. In `app/chat/manager.py`, add immediately after `attach` (after line 208):
```python
    async def add_sink(self, chat_id: str, sink, participant_email: str) -> None:
        """Attach an additional output sink to an already-live session.

        Replays persisted history to the new sink BEFORE appending it to the
        broadcast list, so a late joiner never misses in-flight frames and
        never double-receives one (replay + append are serialized here; the
        pump only ever sees the sink once it's in live.sinks). Sends ``ready``
        last. Used by single-principal Slack cross-surface attach and (5b)
        co-drive join."""
        live = self._live.get(chat_id)
        if live is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        for msg in self._repo.list_messages(chat_id):
            await sink.send_json({
                "type": "assistant_message" if msg.role == "assistant" else "user_msg",
                "content": msg.content,
                "sender_email": msg.sender_email,
            })
        live.sinks.append(SinkEntry(participant_email=participant_email, sink=sink))
        await sink.send_json({"type": "ready"})
```
- [ ] Thread `sender_email` + `_stdin_lock` through `send_user_message`. In `app/chat/manager.py`, change the signature (line 410) to `async def send_user_message(self, chat_id: str, text: str, *, sender_email: Optional[str] = None) -> None:`. Replace the three `await live.ws.send_json({...})` error frames (the `daily_budget` at 421, `max_session_tokens` at 437, `rate_limit` at 457) with `await self._broadcast(live, {...})` (same frame dicts). Change the persist call (line 467) to:
```python
        self._repo.append_message(
            session_id=chat_id, role="user", content=text,
            sender_email=sender_email or live.user_email,
        )
```
And wrap the stdin write+drain pair (lines 468–470) in the lock:
```python
        payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        live.last_activity = datetime.now(timezone.utc)
        live.state = SessionState.ACTIVE
```
- [ ] Update `cancel` to broadcast and hold the lock on its stdin write. In `app/chat/manager.py::cancel` (lines 474–497), wrap the cancel-payload write+drain (lines 478–480) in `async with live._stdin_lock:`, and replace the two `await live.ws.send_json(...)` calls (the synthetic `tool_result` at 491 and the `cancelled` frame at 497) with `await self._broadcast(live, ...)` (same dicts):
```python
        payload = json.dumps({"type": "cancel"}) + "\n"
        async with live._stdin_lock:
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        synthetic = {
            "type": "tool_result",
            "tool": "_cancel",
            "result": {"cancelled": True},
        }
        await self._broadcast(live, synthetic)
        self._repo.append_message(
            session_id=chat_id, role="assistant",
            content="",
            tool_calls=[{"cancelled": True}],
        )
        await self._broadcast(live, {"type": "cancelled"})
```
- [ ] Update `_wait_for_exit_and_respawn` sends + replay stdin lock. In `app/chat/manager.py::_wait_for_exit_and_respawn` (lines 362–408), replace the two `await live.ws.send_json({...})` calls (the `error` frame at 370–374 and the `ready` frame at 384) with `await self._broadcast(live, {...})` (same dicts), and wrap the per-turn replay write+drain pair (lines 390–391) in `async with live._stdin_lock:`:
```python
                    payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
                    async with live._stdin_lock:
                        new_handle.stdin.write(payload.encode("utf-8"))
                        await new_handle.stdin.drain()
```
- [ ] Update `_run_auto_title`'s WS send to broadcast. In `app/chat/manager.py::_run_auto_title` (lines 562–567), replace the `await live.ws.send_json({"type": "session_renamed", ...})` (still inside its try/except) with `await self._broadcast(live, {"type": "session_renamed", "chat_id": live.chat_id, "title": title})`.
- [ ] Add the public `active_count_for_user` wrapper. In `app/chat/manager.py`, after `_active_count_for_user` (after line 164), add:
```python
    def active_count_for_user(self, user_email: str) -> int:
        """Public wrapper over the private cap predicate. Single source for
        the active-session count used by /agnes-status and per-sender caps."""
        return self._active_count_for_user(user_email)
```
- [ ] Convert the 4 `LiveSession(ws=ws)` call sites in `tests/test_chat_manager.py`. At each of lines ~367, ~421, ~472, ~583, the constructor passes `handle=..., ws=ws,`. Add `from app.chat.manager import SinkEntry` to that file's imports, then replace each `ws=ws,` constructor kwarg with `sinks=[SinkEntry(participant_email="u@x", sink=ws)],` (the user_email in all four is `"u@x"`). For example, the site at ~367 becomes:
```python
        mgr._live[s.id] = LiveSession(
            chat_id=s.id, user_email="u@x", state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
```
Apply the same `ws=ws,` → `sinks=[SinkEntry(participant_email="u@x", sink=ws)],` swap at the other three sites (preserving their existing `handle=`/`started_at=`/`last_activity=` values, e.g. the ~472 site keeps `handle=None` and its `started_at=now - timedelta(...)`).
- [ ] Convert the `LiveSession(ws=MagicMock())` call site in `tests/test_admin_chat.py`. At line ~151 the constructor passes `handle=None, ws=MagicMock(),` for `user_email="admin@test.com"`. Add `from app.chat.manager import SinkEntry` to that file's imports (or extend the existing manager import), then replace the `ws=MagicMock(),` kwarg with `sinks=[SinkEntry(participant_email="admin@test.com", sink=MagicMock())],`.
- [ ] Run the multi-sink tests, expect PASS: `.venv/bin/pytest tests/test_chat_multisink.py -v`.
- [ ] Run the manager + API + admin suites to confirm the constructor conversions and any other `live.ws` callers are clean: `.venv/bin/pytest tests/test_chat_manager.py tests/test_chat_api.py tests/test_chat_auto_title.py tests/test_admin_chat.py --tb=short -q`. If any remaining production or test code references `live.ws` directly, update it to `live.sinks[0].sink` (the primary) — fix before proceeding.
- [ ] Commit:
```
git add app/chat/manager.py tests/test_chat_multisink.py tests/test_chat_manager.py tests/test_admin_chat.py
git commit -m "chat manager: multi-sink fan-out, stdin lock, sender_email, active_count_for_user"
```

---

## Task 8 — Full suite, CHANGELOG, finalize

**Files:**
- Modify: `CHANGELOG.md`

- [ ] Run the full test suite (this is what CI runs): `.venv/bin/pytest tests/ --tb=short -n auto -q`. All tests touched by this phase must pass. For any failure unrelated to this diff, confirm with `git stash` that it reproduces on clean `main`, note it in the PR body, and do not block on it.
- [ ] Add the CHANGELOG bullet. In `CHANGELOG.md`, under the `## [Unreleased]` header, add (or extend) an `### Internal` subsection:
```markdown
### Internal
- **DuckDB schema → v69 + Postgres parity (co-drive foundation).** Additive
  migration `_v68_to_v69` in `src/db.py` (matching Alembic `0016_cloud_chat_v69`)
  adds `chat_sessions.is_co_session` / `ephemeral` (BOOLEAN DEFAULT FALSE),
  `chat_messages.sender_email` (nullable, backfilled to the session owner for
  existing user turns), and the `chat_session_participants` table. DuckDB
  `ChatRepository` deletes participant rows before sessions on hard-delete
  (no `ON DELETE CASCADE`); PG uses the FK cascade. New repo methods
  (`add_session_participant`, `get_session_participants`, `remove_participant`,
  `update_participant_role`, `list_sessions_for_participant`,
  `fork_session_as_co_session`) ship on both backends with cross-engine
  contract tests.
- **`ChatManager` multi-sink fan-out.** `LiveSession.ws` is now
  `sinks: list[SinkEntry]`; runner frames broadcast to every sink while
  persistence/audit stay singular. `attach` gains a `*, is_primary=True`
  parameter; `add_sink` replays persisted history before appending a
  late-joining sink. `send_user_message` accepts `sender_email` and serializes
  the stdin write+drain under a per-session `_stdin_lock` so concurrent turns
  can't interleave partial JSON lines. New `ChatManager.active_count_for_user`
  wrapper.
```
- [ ] Verify the CHANGELOG edit landed cleanly (no duplicate `[Unreleased]`): `.venv/bin/pytest tests/test_changelog_unreleased.py -q` if that guard exists; otherwise visually confirm via `git diff CHANGELOG.md`.
- [ ] Commit:
```
git add CHANGELOG.md
git commit -m "changelog: v69 schema + multi-sink fan-out (co-drive foundation)"
```

---

## Phase exit criteria

- `SCHEMA_VERSION == 69`; both ladder dispatch sites call `_v68_to_v69`; `_SYSTEM_SCHEMA` builds v69 directly; `tests/test_db_schema_version.py` and `tests/test_chat_v69_migration.py` green.
- Alembic head is `0016_cloud_chat_v69` reaching the same endpoint (cross-engine contract green in CI); the PG fork maintains the `message_count`/`last_message_at` rollup on a seeded summary.
- Every new repo method exists on both `app/chat/persistence.py` and the PG repos, exercised by parametrized contract tests in `tests/db_pg/test_chat_pg.py`.
- `ChatManager` broadcasts to N sinks with singular persistence, serializes stdin under `_stdin_lock`, threads `sender_email`, exposes `add_sink` + `active_count_for_user`, and keeps `attach`'s `is_primary` parameter aligned with spec §6.3 — the reusable surface 5b's co-drive routes build on. All 5 prior `LiveSession(ws=...)` constructor sites converted to `sinks=[...]`.
- GC-sweep half of §7's participant pre-delete requirement is **intentionally deferred to 5b** (the sweep itself does not yet exist); 5a covers only `hard_delete_user_sessions`.
- Vendor-agnostic throughout (placeholders only: `example.com`, `<your-host>`); no new endpoints introduced in this phase (RBAC gating lands with the 5b routes).
