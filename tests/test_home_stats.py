"""Homepage status frame — schema v44, endpoint shapes, manifest stamp,
operator visibility flag.

Covers:
- v44 ALTERs land users.last_pull_at + 4 token columns idempotently on
  both fresh installs and upgrades from a v43-shaped DB.
- compute_home_stats returns the right counters for 24h vs 7d windows
  and clamps unknown windows to 24h.
- GET /api/sync/manifest bumps users.last_pull_at as a side effect.
- get_home_status_frame_visibility honors the env var + yaml override
  and defaults true.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import duckdb
import pytest

from src.db import (
    _SYSTEM_SCHEMA,
    _ensure_schema,
    _v43_to_v44,
)


# ---------------------------------------------------------------------------
# Schema v44
# ---------------------------------------------------------------------------


def test_v44_fresh_install_has_token_columns_and_last_pull(tmp_path):
    """Fresh install reaches v44 with all new columns declared in
    _SYSTEM_SCHEMA (the migration function is a no-op on fresh install
    but the columns must exist regardless)."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert "last_pull_at" in user_cols

    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_session_summary)").fetchall()}
    for col in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        assert col in sess_cols, f"missing {col}"


def test_v43_to_v44_upgrade_is_idempotent(tmp_path):
    """Running _v43_to_v44 on a hand-rolled pre-v44 DB lands the four
    new columns; a second call is a no-op (IF NOT EXISTS guards)."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    # Hand-roll v43-shaped tables (no last_pull_at, no token cols).
    conn.execute("CREATE TABLE users (id VARCHAR PRIMARY KEY, email VARCHAR, onboarded BOOLEAN DEFAULT FALSE)")
    conn.execute(
        """
        CREATE TABLE usage_session_summary (
            session_file VARCHAR PRIMARY KEY,
            session_id   VARCHAR NOT NULL,
            username     VARCHAR NOT NULL,
            started_at   TIMESTAMP,
            ended_at     TIMESTAMP,
            user_messages INTEGER DEFAULT 0,
            processor_version INTEGER NOT NULL
        )
        """
    )

    _v43_to_v44(conn)
    _v43_to_v44(conn)  # idempotent

    assert "last_pull_at" in {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    tok_cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_session_summary)").fetchall() if "token" in r[1]}
    assert tok_cols == {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    }


# ---------------------------------------------------------------------------
# compute_home_stats / GET /api/me/home-stats
# ---------------------------------------------------------------------------


@pytest.fixture
def stats_conn(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_SYSTEM_SCHEMA)
    return conn


def _seed_user(conn, *, uid="u1", email="alice@example.com"):
    conn.execute(
        "INSERT INTO users (id, email, active, onboarded, last_pull_at) VALUES (?, ?, TRUE, TRUE, current_timestamp)",
        [uid, email],
    )


def _seed_session(
    conn,
    *,
    session_file,
    username,
    started_sql,
    prompts=0,
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    cache_creation=0,
):
    conn.execute(
        f"""
        INSERT INTO usage_session_summary
          (session_file, session_id, username, started_at, ended_at,
           user_messages, input_tokens, output_tokens, cache_read_tokens,
           cache_creation_tokens, processor_version)
        VALUES (?, ?, ?, {started_sql}, current_timestamp,
                ?, ?, ?, ?, ?, 2)
        """,
        [session_file, session_file, username, prompts, input_tokens, output_tokens, cache_read, cache_creation],
    )


def _seed_event(conn, *, ev_id, session_file, username, cwd, occurred_sql):
    conn.execute(
        f"""
        INSERT INTO usage_events
          (id, session_id, session_file, username, event_type, source,
           cwd, occurred_at, processor_version)
        VALUES (?, ?, ?, ?, 'tool_use', 'builtin', ?, {occurred_sql}, 2)
        """,
        [ev_id, session_file, session_file, username, cwd],
    )


def test_compute_home_stats_24h_vs_7d_windowing(stats_conn):
    """A session 1h ago shows in both windows; a session 3 days ago
    only in 7d; a session 30 days ago in neither."""
    from app.api.me import compute_home_stats

    _seed_user(stats_conn)
    _seed_session(
        stats_conn,
        session_file="a.jsonl",
        username="alice",
        started_sql="current_timestamp - INTERVAL 1 HOUR",
        prompts=5,
        input_tokens=100,
        output_tokens=50,
        cache_read=800,
        cache_creation=25,
    )
    _seed_session(
        stats_conn,
        session_file="b.jsonl",
        username="alice",
        started_sql="current_timestamp - INTERVAL 3 DAY",
        prompts=5,
        input_tokens=100,
        output_tokens=50,
        cache_read=800,
        cache_creation=25,
    )
    _seed_session(
        stats_conn,
        session_file="c.jsonl",
        username="alice",
        started_sql="current_timestamp - INTERVAL 30 DAY",
        prompts=99,
    )

    _seed_event(
        stats_conn,
        ev_id="e1",
        session_file="a.jsonl",
        username="alice",
        cwd="/proj/alpha",
        occurred_sql="current_timestamp - INTERVAL 1 HOUR",
    )
    _seed_event(
        stats_conn,
        ev_id="e2",
        session_file="a.jsonl",
        username="alice",
        cwd="/proj/beta",
        occurred_sql="current_timestamp - INTERVAL 2 HOUR",
    )
    _seed_event(
        stats_conn,
        ev_id="e3",
        session_file="b.jsonl",
        username="alice",
        cwd="/proj/gamma",
        occurred_sql="current_timestamp - INTERVAL 3 DAY",
    )

    user = {"id": "u1", "email": "alice@example.com"}

    s24 = compute_home_stats(stats_conn, user, "24h")
    assert s24["window"] == "24h"
    assert s24["sessions"] == 1
    assert s24["prompts"] == 5
    assert s24["projects"] == 2
    assert s24["tokens"]["total"] == 100 + 50 + 800 + 25
    assert s24["last_pull_at"] is not None

    s7 = compute_home_stats(stats_conn, user, "7d")
    assert s7["window"] == "7d"
    assert s7["sessions"] == 2
    assert s7["prompts"] == 10
    assert s7["projects"] == 3
    assert s7["tokens"]["total"] == 2 * (100 + 50 + 800 + 25)


def test_compute_home_stats_unknown_window_clamps_to_24h(stats_conn):
    """Out-of-band window values clamp to 24h rather than 400-ing."""
    from app.api.me import compute_home_stats

    _seed_user(stats_conn)
    s = compute_home_stats(stats_conn, {"id": "u1", "email": "alice@example.com"}, "bogus")
    assert s["window"] == "24h"


def test_compute_home_stats_empty_user_returns_zeros(stats_conn):
    """Brand-new user with no sessions / events surfaces zeros, not 500."""
    from app.api.me import compute_home_stats

    _seed_user(stats_conn, uid="u_empty", email="nobody@example.com")
    s = compute_home_stats(
        stats_conn,
        {"id": "u_empty", "email": "nobody@example.com"},
        "24h",
    )
    assert s["sessions"] == 0
    assert s["prompts"] == 0
    assert s["projects"] == 0
    assert s["tokens"]["total"] == 0
    # Seeded user row carries a last_pull_at from the helper, so this
    # asserts the column travels through the join correctly.
    assert s["last_pull_at"] is not None


def test_compute_home_stats_missing_users_row_returns_zeros(stats_conn):
    """If the users row is missing entirely (race during deletion), the
    helper returns a zeroed payload instead of crashing."""
    from app.api.me import compute_home_stats

    s = compute_home_stats(
        stats_conn,
        {"id": "ghost", "email": "ghost@example.com"},
        "24h",
    )
    assert s == {
        "window": "24h",
        "last_pull_at": None,
        "sessions": 0,
        "prompts": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "total": 0,
        },
        "projects": 0,
    }


# ---------------------------------------------------------------------------
# GET /api/sync/manifest bumps users.last_pull_at
# ---------------------------------------------------------------------------


def test_sync_manifest_bumps_last_pull_at(stats_conn, monkeypatch, tmp_path):
    """The manifest endpoint records the user's pull timestamp so the
    /home status frame's 'Last sync' card stays current."""
    from app.api.sync import sync_manifest

    # data_dir for asset hashing; we don't seed docs/profiles so the
    # assets dict will be empty (manifest still returns ok).
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    _seed_user(stats_conn, uid="u_pull", email="puller@example.com")
    # Wipe seeded last_pull_at so we can detect the bump.
    stats_conn.execute("UPDATE users SET last_pull_at = NULL WHERE id = ?", ["u_pull"])

    asyncio.run(
        sync_manifest(
            user={"id": "u_pull", "email": "puller@example.com"},
            conn=stats_conn,
        )
    )
    row = stats_conn.execute("SELECT last_pull_at FROM users WHERE id = ?", ["u_pull"]).fetchone()
    # Don't compare against `datetime.now(utc)` — DuckDB's
    # ``current_timestamp`` returns the session's wall-clock time which
    # may be naive-local-or-utc depending on the environment, so a
    # delta-based assertion would tz-skew. The semantic the test cares
    # about is "the column flipped from NULL", which is what the home
    # status card reads.
    assert row[0] is not None


# ---------------------------------------------------------------------------
# Operator visibility flag — get_home_status_frame_visibility
# ---------------------------------------------------------------------------


def test_status_frame_default_is_visible(monkeypatch):
    """Absent both env var and yaml entry, the flag returns True."""
    monkeypatch.delenv("AGNES_HOME_SHOW_STATUS_FRAME", raising=False)
    from app.instance_config import get_home_status_frame_visibility

    assert get_home_status_frame_visibility() is True


def test_status_frame_env_var_off(monkeypatch):
    """AGNES_HOME_SHOW_STATUS_FRAME=0 hides the frame."""
    monkeypatch.setenv("AGNES_HOME_SHOW_STATUS_FRAME", "0")
    from app.instance_config import get_home_status_frame_visibility

    assert get_home_status_frame_visibility() is False


def test_status_frame_env_var_falsey_values(monkeypatch):
    """Each of {0, false, no, off, ''} hides the frame; anything else shows."""
    from app.instance_config import get_home_status_frame_visibility

    for val in ("0", "false", "False", "FALSE", "no", "off", ""):
        monkeypatch.setenv("AGNES_HOME_SHOW_STATUS_FRAME", val)
        assert get_home_status_frame_visibility() is False, f"{val!r} should hide"
    for val in ("1", "true", "yes", "on", "anything"):
        monkeypatch.setenv("AGNES_HOME_SHOW_STATUS_FRAME", val)
        assert get_home_status_frame_visibility() is True, f"{val!r} should show"
