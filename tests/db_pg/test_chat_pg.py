"""Integration tests for the cloud-chat PG repositories.

PG-side smoke covering ChatSessionPgRepository, ChatMessagePgRepository, and
UserWorkdirPgRepository — the CRUD surface plus the two Postgres-only
constraints the DuckDB schema cannot express:

  - chat_messages.session_id FK ON DELETE CASCADE (hard_delete removes
    child rows automatically).
  - per-surface partial unique indexes (slack_dm channel uniqueness,
    slack_thread (channel, ts) uniqueness).

Mirrors the alembic-head fixture idiom from ``test_data_packages_pg.py``.

Also contains the dual-backend contract tests for sandbox-ref repo methods
(Task 3 of the pause/resume plan). Those tests use ``state_backend`` from
conftest.py so the same assertions run against both DuckDB and Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.chat.types import Surface

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return pg_engine


@pytest.fixture
def sessions(engine):
    from src.repositories.chat_sessions_pg import ChatSessionPgRepository

    return ChatSessionPgRepository(engine)


@pytest.fixture
def messages(engine):
    from src.repositories.chat_messages_pg import ChatMessagePgRepository

    return ChatMessagePgRepository(engine)


@pytest.fixture
def workdirs(engine):
    from src.repositories.user_workdirs_pg import UserWorkdirPgRepository

    return UserWorkdirPgRepository(engine)


@pytest.fixture
def participants(engine):
    from src.repositories.chat_session_participants_pg import (
        ChatSessionParticipantPgRepository,
    )

    return ChatSessionParticipantPgRepository(engine)


# --- sessions --------------------------------------------------------------


def test_create_and_get_session(sessions):
    s = sessions.create_session(user_email="a@x.com", surface=Surface.WEB)
    assert s.id.startswith("chat_")
    assert s.message_count == 0
    assert s.archived is False
    fetched = sessions.get_session(s.id)
    assert fetched is not None
    assert fetched.user_email == "a@x.com"
    assert fetched.surface == Surface.WEB


def test_list_sessions_excludes_archived_by_default(sessions):
    a = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.archive_session(a.id)
    visible = sessions.list_sessions("u@x.com")
    assert a.id not in {s.id for s in visible}
    assert len(visible) == 1
    assert len(sessions.list_sessions("u@x.com", include_archived=True)) == 2


def test_set_title(sessions):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.set_title(s.id, "Renamed")
    assert sessions.get_session(s.id).title == "Renamed"


def test_slack_dm_partial_unique_index(sessions, engine):
    sessions.create_session(user_email="u@x.com", surface=Surface.SLACK_DM, slack_channel_id="C1")
    found = sessions.get_slack_dm_session("C1")
    assert found is not None
    # Second slack_dm for the same channel violates the partial unique index.
    with pytest.raises(Exception):
        sessions.create_session(user_email="other@x.com", surface=Surface.SLACK_DM, slack_channel_id="C1")


def test_slack_thread_partial_unique_index(sessions):
    sessions.create_session(
        user_email="u@x.com",
        surface=Surface.SLACK_THREAD,
        slack_channel_id="C1",
        slack_thread_ts="100.1",
    )
    found = sessions.get_slack_thread_session("C1", "100.1")
    assert found is not None
    # Different ts in same channel is allowed.
    sessions.create_session(
        user_email="u@x.com",
        surface=Surface.SLACK_THREAD,
        slack_channel_id="C1",
        slack_thread_ts="200.2",
    )
    with pytest.raises(Exception):
        sessions.create_session(
            user_email="u@x.com",
            surface=Surface.SLACK_THREAD,
            slack_channel_id="C1",
            slack_thread_ts="100.1",
        )


# --- messages --------------------------------------------------------------


def test_append_message_updates_session_rollup(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="hi")
    messages.append_message(
        session_id=s.id,
        role="assistant",
        content="hello",
        tokens_in=10,
        tokens_out=20,
    )
    refreshed = sessions.get_session(s.id)
    # PG keeps the rollup current (no DuckDB FK+index bug).
    assert refreshed.message_count == 2
    assert refreshed.last_message_at is not None


def test_list_messages_and_after_id(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    m1 = messages.append_message(session_id=s.id, role="user", content="one")
    messages.append_message(session_id=s.id, role="assistant", content="two")
    all_msgs = messages.list_messages(s.id)
    assert [m.content for m in all_msgs] == ["one", "two"]
    after = messages.list_messages(s.id, after_id=m1.id)
    assert [m.content for m in after] == ["two"]


def test_tool_calls_round_trip(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    payload = [{"name": "query", "args": {"sql": "SELECT 1"}}]
    messages.append_message(session_id=s.id, role="assistant", content="x", tool_calls=payload)
    got = messages.list_messages(s.id)[0]
    assert got.tool_calls == payload


def test_get_first_user_message(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="assistant", content="greeting")
    messages.append_message(session_id=s.id, role="user", content="first ask")
    messages.append_message(session_id=s.id, role="user", content="follow up")
    assert messages.get_first_user_message(s.id) == "first ask"


def test_session_total_and_daily_tokens(sessions, messages):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="a", tokens_in=5, tokens_out=7)
    messages.append_message(session_id=s.id, role="assistant", content="b", tokens_in=3, tokens_out=4)
    assert messages.session_total_tokens(s.id) == 19
    tin, tout = messages.daily_anthropic_tokens("u@x.com")
    assert (tin, tout) == (8, 11)


# --- archive / delete ------------------------------------------------------


def test_archive_empty_user_sessions(sessions, messages):
    empty = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    full = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    messages.append_message(session_id=full.id, role="user", content="hi")
    n = sessions.archive_empty_user_sessions("u@x.com")
    assert n == 1
    assert sessions.get_session(empty.id).archived is True
    assert sessions.get_session(full.id).archived is False


def test_archive_empty_respects_exclude_and_surface(sessions):
    keep = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    n = sessions.archive_empty_user_sessions("u@x.com", surface=Surface.WEB, exclude_id=keep.id)
    assert n == 1
    assert sessions.get_session(keep.id).archived is False


def test_hard_delete_cascades_messages(sessions, messages, engine):
    s = sessions.create_session(user_email="gone@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="bye")
    deleted = sessions.hard_delete_user_sessions("gone@x.com")
    assert deleted == 1
    assert sessions.get_session(s.id) is None
    with engine.connect() as conn:
        remaining = conn.execute(
            sa.text("SELECT COUNT(*) FROM chat_messages WHERE session_id = :sid"),
            {"sid": s.id},
        ).scalar()
    assert remaining == 0  # ON DELETE CASCADE removed children


# --- workdirs --------------------------------------------------------------


def test_workdir_upsert_get_delete(workdirs):
    assert workdirs.get_workdir("u@x.com") is None
    workdirs.upsert_workdir(
        user_email="u@x.com",
        marketplace_sha="abc",
        initial_workspace_sha="def",
        agnes_version="1.0.0",
    )
    w = workdirs.get_workdir("u@x.com")
    assert w is not None
    assert w.marketplace_sha == "abc"
    assert w.agnes_version_at_init == "1.0.0"
    # upsert again updates in place
    workdirs.upsert_workdir(
        user_email="u@x.com",
        marketplace_sha="zzz",
        initial_workspace_sha=None,
        agnes_version="2.0.0",
    )
    w2 = workdirs.get_workdir("u@x.com")
    assert w2.marketplace_sha == "zzz"
    assert w2.initial_workspace_sha is None
    workdirs.delete_workdir_row("u@x.com")
    assert workdirs.get_workdir("u@x.com") is None


# --- v69 co-presence -------------------------------------------------------


def test_session_flags_default_false(sessions):
    s = sessions.create_session(user_email="u@x.com", surface=Surface.WEB)
    assert s.is_co_session is False
    assert s.ephemeral is False
    assert sessions.get_session(s.id).is_co_session is False


def test_sender_email_round_trip(sessions, messages):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    messages.append_message(session_id=s.id, role="user", content="hi", sender_email="b@x.com")
    got = messages.list_messages(s.id)[0]
    assert got.sender_email == "b@x.com"


def test_participant_add_list_role_remove(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="o@x.com", user_id="u-o", role="owner")
    participants.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    active = participants.get_session_participants(s.id)
    assert {p.user_email for p in active} == {"o@x.com", "c@x.com"}
    participants.update_participant_role(s.id, "c@x.com", "owner")
    assert all(p.role == "owner" for p in participants.get_session_participants(s.id) if p.user_email == "c@x.com")
    participants.remove_participant(s.id, "c@x.com")
    assert {p.user_email for p in participants.get_session_participants(s.id)} == {"o@x.com"}


def test_list_sessions_for_participant(sessions, participants):
    s = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="c@x.com", user_id="u-c", role="collaborator")
    found = participants.list_sessions_for_participant("c@x.com")
    assert s.id in {x.id for x in found}


def test_fork_session_as_co_session_pg(sessions, participants, messages):
    s0 = sessions.create_session(user_email="o@x.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
        seed_summary="prior context",
    )
    assert sessions.get_session(s0.id).is_co_session is False  # source untouched
    assert s1.is_co_session is True and s1.ephemeral is True
    parts = participants.get_session_participants(s1.id)
    assert {(p.user_email, p.role) for p in parts} == {("o@x.com", "owner"), ("c@x.com", "collaborator")}
    seeded = messages.list_messages(s1.id)
    assert seeded and seeded[0].content == "prior context"  # summary, not raw clone
    # rollup maintained: the seeded system message bumped message_count.
    assert sessions.get_session(s1.id).message_count == 1


def test_hard_delete_cascades_participants(sessions, participants, engine):
    s = sessions.create_session(user_email="gone@x.com", surface=Surface.WEB)
    participants.add_session_participant(session_id=s.id, user_email="gone@x.com", user_id="u-g", role="owner")
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
    dm = sessions.create_session(user_email="o@x.com", surface=Surface.SLACK_DM, slack_channel_id="D1")
    co = participants.fork_session_as_co_session(
        web.id,
        owner_email="o@x.com",
        owner_user_id="u-o",
        invitee_email="c@x.com",
        invitee_user_id="u-c",
    )
    ids = {s.id for s in sessions.list_sessions("o@x.com")}
    assert {web.id, dm.id, co.id} <= ids


# --- Task 9: fork_session_as_co_session contract + fork_co_session_to_private ---


def test_fork_session_as_co_session_no_messages_copied(sessions, participants, messages):
    """SR-8: fork_session_as_co_session must NOT copy transcript messages."""
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    messages.append_message(session_id=s0.id, role="user", content="secret data")
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    assert s1.is_co_session is True and s1.ephemeral is True
    # SR-8: no transcript blind-clone
    assert messages.list_messages(s1.id) == []
    again = sessions.get_session(s0.id)
    assert again.is_co_session is False and again.ephemeral is False
    rows = participants.get_session_participants(s1.id)
    by_role = {r.role: r for r in rows}
    assert by_role["owner"].user_id == "ua"
    assert by_role["collaborator"].user_id == "ub"


def test_fork_co_session_to_private_copies_transcript(sessions, participants, messages):
    """fork_co_session_to_private: fresh private session with co-session transcript."""
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com",
        owner_user_id="ua",
        invitee_email="b@example.com",
        invitee_user_id="ub",
    )
    messages.append_message(session_id=s1.id, role="assistant", content="hi from co")
    priv_id = participants.fork_co_session_to_private(
        source_session_id=s1.id,
        owner_email="b@example.com",
    )
    priv = sessions.get_session(priv_id)
    assert priv.is_co_session is False and priv.ephemeral is False
    assert priv.user_email == "b@example.com"
    assert any(m.content == "hi from co" for m in messages.list_messages(priv_id))


# ---------------------------------------------------------------------------
# Dual-backend contract: sandbox-ref repo methods (Task 3, pause/resume plan)
#
# Uses ``state_backend`` from conftest so each test runs twice: once against
# DuckDB (ChatRepository directly) and once against Postgres (ChatRepository
# delegates to ChatSessionPgRepository when use_pg() is True). The PG leg
# will skip if no PG server is available.
# ---------------------------------------------------------------------------


@pytest.fixture
def _chat_env(state_backend, tmp_path, monkeypatch):
    """Boot the right backend and return a ready ChatRepository."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        conn = get_system_db()
        from app.chat.persistence import ChatRepository

        return ChatRepository(conn)
    else:
        # PG path: ChatRepository's __init__ detects use_pg() and delegates.
        import duckdb

        from src.db import _ensure_schema

        conn = duckdb.connect(":memory:")
        _ensure_schema(conn)
        from app.chat.persistence import ChatRepository

        return ChatRepository(conn)


def test_sandbox_ref_roundtrip(_chat_env):
    """set_sandbox_ref / get_session / set_sandbox_paused_at / list_paused_sessions
    / clear_sandbox_ref all round-trip correctly on both backends.

    Covers the DuckDB 1.5.3 FK+index guard: the same operations are repeated
    AFTER append_message() has inserted chat_messages rows, which is the
    production order (pause always happens after messages exist) and is exactly
    what the bug would break if the new columns were indexed.
    """
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)
    assert repo.get_session(s.id).sandbox_id is None

    # --- basic set/get roundtrip ---
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_1", runner_pid=413)
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid, got.sandbox_paused_at) == ("sbx_1", 413, None)

    # --- set_sandbox_paused_at marks the session as paused ---
    ts = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    repo.set_sandbox_paused_at(s.id, ts)
    got2 = repo.get_session(s.id)
    assert got2.sandbox_paused_at is not None
    paused = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id in {x.id for x in paused}

    # --- set_sandbox_paused_at(None) clears the marker (resume path) ---
    repo.set_sandbox_paused_at(s.id, None)
    assert repo.get_session(s.id).sandbox_paused_at is None
    paused_after_clear = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id not in {x.id for x in paused_after_clear}

    # --- clear_sandbox_ref wipes all three columns ---
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_2", runner_pid=999)
    repo.clear_sandbox_ref(s.id)
    got3 = repo.get_session(s.id)
    assert (got3.sandbox_id, got3.runner_pid, got3.sandbox_paused_at) == (None, None, None)


def test_sandbox_ref_roundtrip_after_messages(_chat_env):
    """Repeat the full sandbox-ref roundtrip AFTER append_message() has inserted
    chat_messages rows.

    This is the production order — pause always happens after at least one
    message exists. It specifically guards against the DuckDB 1.5.3 FK+index
    bug: if any of the three new columns were indexed, UPDATE on chat_sessions
    after a child chat_messages INSERT would raise a false FK violation.
    """
    repo = _chat_env
    s = repo.create_session(user_email="u@example.com", surface=Surface.WEB)

    # Insert messages first — this is the state that would trigger the FK bug.
    repo.append_message(session_id=s.id, role="user", content="hello")
    repo.append_message(session_id=s.id, role="assistant", content="hi")

    # Now run the full sandbox-ref lifecycle; none of these must raise.
    repo.set_sandbox_ref(s.id, sandbox_id="sbx_post_msg", runner_pid=77)
    got = repo.get_session(s.id)
    assert (got.sandbox_id, got.runner_pid) == ("sbx_post_msg", 77)

    ts = datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc)
    repo.set_sandbox_paused_at(s.id, ts)
    assert repo.get_session(s.id).sandbox_paused_at is not None
    paused = repo.list_paused_sessions(paused_before=ts + timedelta(seconds=1))
    assert s.id in {x.id for x in paused}

    repo.set_sandbox_paused_at(s.id, None)
    repo.clear_sandbox_ref(s.id)
    got2 = repo.get_session(s.id)
    assert (got2.sandbox_id, got2.runner_pid, got2.sandbox_paused_at) == (None, None, None)
