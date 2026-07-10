"""Cross-engine contract tests for the usage repository.

Targets: UsageRepository (DuckDB) / UsagePgRepository (Postgres).
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the fixture pattern in test_rbac_contract.py: DuckDB uses
_ensure_schema; PG runs the alembic ladder to head. Seeding goes through
the repo's own write methods (upsert_summary / upsert_events).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.usage import UsageRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UsageRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.usage_pg import UsagePgRepository

    return UsagePgRepository(engine), None


@pytest.fixture(params=["duckdb", "pg"])
def usage_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_summary(
    repo,
    *,
    session_file,
    username,
    user_id=None,
    started_at,
    primary_model="claude-x",
    input_tokens=0,
    output_tokens=0,
    cache_read_tokens=0,
    cache_creation_tokens=0,
    tool_calls=0,
    tool_errors=0,
    user_messages=0,
):
    repo.upsert_summary(
        {
            "session_file": session_file,
            "session_id": session_file.rsplit("/", 1)[-1],
            "username": username,
            "user_id": user_id,
            "started_at": started_at,
            "ended_at": started_at,
            "active_seconds": 10,
            "wall_seconds": 20,
            "user_messages": user_messages,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "primary_model": primary_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
        processor_version=1,
    )


def _seed_event(repo, *, event_id, username, session_file, occurred_at, tool_name="Read", user_id=None):
    repo.upsert_events(
        [
            {
                "id": event_id,
                "session_id": session_file.rsplit("/", 1)[-1],
                "session_file": session_file,
                "username": username,
                "event_type": "tool",
                "tool_name": tool_name,
                "is_error": False,
                "source": "curated",
                "occurred_at": occurred_at,
                "user_id": user_id,
            }
        ],
        processor_version=1,
    )


def _seed_processor_state(repo, processor_name, session_file="ps/s.jsonl"):
    """Seed a session_processor_state checkpoint via that repo's own
    mark_processed UPSERT (it knows the full schema, incl. NOT NULL columns).
    reset_all's clear_processors path deletes these rows, so the contract test
    needs a real row to delete."""
    if hasattr(repo, "conn"):  # DuckDB
        from src.repositories.session_processor_state import (
            SessionProcessorStateRepository,
        )

        sps = SessionProcessorStateRepository(repo.conn)
    else:  # Postgres
        from src.repositories.session_processor_state_pg import (
            SessionProcessorStatePgRepository,
        )

        sps = SessionProcessorStatePgRepository(repo._engine)
    sps.mark_processed(
        processor_name=processor_name,
        session_file=session_file,
        username="seed",
        items_count=0,
        file_hash="h",
    )


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_count_events(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    assert repo.count_events() == 0
    _seed_event(repo, event_id="e1", username="alice", session_file="alice/s1.jsonl", occurred_at=now)
    _seed_event(repo, event_id="e2", username="alice", session_file="alice/s1.jsonl", occurred_at=now)
    assert repo.count_events() == 2


def test_reset_all_zeroes_tables_and_returns_counts(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="bob/s1.jsonl", username="bob", started_at=now)
    _seed_summary(repo, session_file="bob/s2.jsonl", username="bob", started_at=now)
    _seed_event(repo, event_id="r1", username="bob", session_file="bob/s1.jsonl", occurred_at=now)

    counts = repo.reset_all()
    # All five usage tables are reported.
    assert set(counts.keys()) == {
        "events",
        "session_summary",
        "tool_daily",
        "marketplace_item_daily",
        "marketplace_item_window",
    }
    assert counts["events"] == 1
    assert counts["session_summary"] == 2
    assert counts["tool_daily"] == 0
    assert counts["marketplace_item_daily"] == 0
    assert counts["marketplace_item_window"] == 0

    # Everything is gone.
    assert repo.count_events() == 0
    assert repo.list_sessions_for_user_self("bob") == []


def test_reset_all_clear_processors_clears_state_and_usage_together(usage_repo):
    """clear_processors deletes the matching session_processor_state rows in the
    SAME transaction as the usage tables (the reprocess_usage atomicity fix),
    scoped to the named processors only."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_event(repo, event_id="e1", username="bob", session_file="bob/s1.jsonl", occurred_at=now)
    _seed_processor_state(repo, "usage", "bob/s1.jsonl")
    _seed_processor_state(repo, "verification", "bob/s1.jsonl")  # must survive

    counts = repo.reset_all(clear_processors=["usage", "marketplace_rollup_30d"])
    # Only the 'usage' checkpoint matched (verification untouched, rollup absent).
    assert counts["state_rows"] == 1
    assert counts["events"] == 1
    assert repo.count_events() == 0


def test_reset_all_without_clear_processors_omits_state_rows(usage_repo):
    """Default reset_all (no clear_processors) keeps its original 5-key shape —
    no state_rows key — so existing callers/tests are unaffected."""
    repo, _, _ = usage_repo
    counts = repo.reset_all()
    assert "state_rows" not in counts


def test_list_sessions_for_user_admin_filters_on_user_id_or_username(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    # Matches via user_id.
    _seed_summary(repo, session_file="u/s1.jsonl", username="legacyname", user_id="uid-1", started_at=now)
    # Matches via username.
    _seed_summary(repo, session_file="u/s2.jsonl", username="carol", user_id="other", started_at=now)
    # No match.
    _seed_summary(repo, session_file="u/s3.jsonl", username="nobody", user_id="zzz", started_at=now)

    rows = repo.list_sessions_for_user_admin(user_id="uid-1", username="carol")
    files = {r["session_file"] for r in rows}
    assert files == {"u/s1.jsonl", "u/s2.jsonl"}
    # 9-column shape.
    assert set(rows[0].keys()) == {
        "session_file",
        "session_id",
        "started_at",
        "ended_at",
        "active_seconds",
        "wall_seconds",
        "tool_calls",
        "tool_errors",
        "primary_model",
    }


def test_list_sessions_for_user_self_filters_on_username_only(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="d/s1.jsonl", username="dave", user_id="uid-x", started_at=now)
    # Same user_id but different username — must NOT appear (self filters on username).
    _seed_summary(repo, session_file="d/s2.jsonl", username="someone-else", user_id="uid-x", started_at=now)

    rows = repo.list_sessions_for_user_self("dave")
    files = {r["session_file"] for r in rows}
    assert files == {"d/s1.jsonl"}
    # 14-column shape.
    assert set(rows[0].keys()) == {
        "session_file",
        "session_id",
        "started_at",
        "ended_at",
        "active_seconds",
        "wall_seconds",
        "user_messages",
        "tool_calls",
        "tool_errors",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "primary_model",
    }


def test_tokens_totals_and_by_model(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo,
        session_file="t/s1.jsonl",
        username="erin",
        started_at=now,
        primary_model="claude-a",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=3,
        cache_creation_tokens=2,
    )
    _seed_summary(
        repo,
        session_file="t/s2.jsonl",
        username="erin",
        started_at=now,
        primary_model="claude-b",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )

    totals = repo.tokens_totals("erin")
    assert totals["input"] == 110
    assert totals["output"] == 220
    assert totals["cache_read"] == 3
    assert totals["cache_creation"] == 2
    assert totals["total"] == 335
    assert totals["sessions"] == 2

    by_model = repo.tokens_by_model("erin")
    # Ordered by total desc → claude-b (300) first, claude-a (35) second.
    assert [m["model"] for m in by_model] == ["claude-b", "claude-a"]
    assert by_model[0]["total"] == 300
    assert by_model[1]["total"] == 35


def test_tokens_top_sessions_orders_by_total(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo, session_file="ts/small.jsonl", username="frank", started_at=now, input_tokens=1, output_tokens=1
    )
    _seed_summary(
        repo, session_file="ts/big.jsonl", username="frank", started_at=now, input_tokens=1000, output_tokens=1000
    )

    top = repo.tokens_top_sessions("frank", limit=10)
    assert top[0]["session_file"] == "ts/big.jsonl"
    assert top[0]["total"] == 2000
    assert top[1]["session_file"] == "ts/small.jsonl"
    # limit is honored.
    assert len(repo.tokens_top_sessions("frank", limit=1)) == 1


def test_tokens_daily_series_window(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="ds/s1.jsonl", username="grace", started_at=now, input_tokens=5, output_tokens=5)

    series = repo.tokens_daily_series("grace", days=30)
    assert len(series) == 1
    assert series[0]["total"] == 10
    assert series[0]["sessions"] == 1


def test_tokens_daily_series_filters_username_and_days(usage_repo):
    """Pins the username + days predicates: an old same-user row (outside the
    window) and a current other-user row must both be excluded, so the series
    reflects only the requested user's in-window totals."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo,
        session_file="ds/current.jsonl",
        username="grace",
        started_at=now - timedelta(days=5),
        input_tokens=5,
        output_tokens=5,
    )
    _seed_summary(
        repo,
        session_file="ds/old.jsonl",
        username="grace",
        started_at=now - timedelta(days=45),
        input_tokens=500,
        output_tokens=500,
    )
    _seed_summary(
        repo,
        session_file="ds/other.jsonl",
        username="heidi",
        started_at=now - timedelta(days=5),
        input_tokens=50,
        output_tokens=50,
    )

    series = repo.tokens_daily_series("grace", days=30)
    assert len(series) == 1
    assert series[0]["total"] == 10
    assert series[0]["sessions"] == 1


def test_delete_older_than_present_on_both_backends(usage_repo):
    repo, _, _ = usage_repo
    # Sanity: method exists + is callable (parity reused by prune_usage).
    assert repo.delete_older_than(3650) == 0


def test_delete_older_than_removes_only_events_before_cutoff(usage_repo):
    """Pins the retention prune semantics behind POST /api/admin/telemetry/prune:
    rows older than the cutoff are deleted, recent rows survive, and the
    returned count is the number actually removed — on both backends (guards the
    dialect-specific interval arithmetic)."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_event(
        repo, event_id="old", username="alice", session_file="retention/old.jsonl", occurred_at=now - timedelta(days=40)
    )
    _seed_event(
        repo,
        event_id="recent",
        username="alice",
        session_file="retention/recent.jsonl",
        occurred_at=now - timedelta(days=5),
    )

    assert repo.delete_older_than(30) == 1
    assert repo.count_events() == 1
    # Idempotent: nothing left older than the cutoff.
    assert repo.delete_older_than(30) == 0


# ---------------------------------------------------------------------------
# rebuild_rollups (#728 — dual-backend marketplace usage rollup producer)
# ---------------------------------------------------------------------------


def _insert(repo, conn, backend, table, cols, rows):
    """Backend-aware raw INSERT — mirrors test_reports_contract.py's ``_insert``.

    Used to seed the marketplace_plugins / store_entities lookup tables and
    full-shape usage_events rows (skill_name / subagent_type / command_name)
    that the plain ``_seed_event`` helper above doesn't carry.
    """
    collist = ", ".join(cols)
    if backend == "duckdb":
        ph = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        for r in rows:
            conn.execute(sql, [r[c] for c in cols])
    else:
        ph = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        with repo._engine.begin() as c:
            for r in rows:
                c.execute(sa.text(sql), {k: r[k] for k in cols})


def _seed_curated_plugin(repo, conn, backend, plugin_name, marketplace_id="mp"):
    _insert(
        repo,
        conn,
        backend,
        "marketplace_registry",
        ["id", "name", "url"],
        [{"id": marketplace_id, "name": marketplace_id.upper(), "url": "https://example.test/repo.git"}],
    )
    _insert(
        repo,
        conn,
        backend,
        "marketplace_plugins",
        ["marketplace_id", "name"],
        [{"marketplace_id": marketplace_id, "name": plugin_name}],
    )


def _seed_full_event(
    repo,
    conn,
    backend,
    *,
    event_id,
    occurred_at,
    skill_name=None,
    subagent_type=None,
    command_name=None,
    event_type="tool_use",
    tool_name=None,
    username="alice",
    user_id="uid-alice",
    session_id="s1",
    session_file="s1.jsonl",
):
    cols = [
        "id",
        "session_id",
        "session_file",
        "username",
        "user_id",
        "event_type",
        "tool_name",
        "skill_name",
        "subagent_type",
        "command_name",
        "is_error",
        "source",
        "occurred_at",
        "processor_version",
    ]
    _insert(
        repo,
        conn,
        backend,
        "usage_events",
        cols,
        [
            {
                "id": event_id,
                "session_id": session_id,
                "session_file": session_file,
                "username": username,
                "user_id": user_id,
                "event_type": event_type,
                "tool_name": tool_name,
                "skill_name": skill_name,
                "subagent_type": subagent_type,
                "command_name": command_name,
                "is_error": False,
                "source": "builtin",
                "occurred_at": occurred_at,
                "processor_version": 5,
            }
        ],
    )


def test_rebuild_rollups_daily_fact_identical_across_backends(usage_repo):
    """Same seed -> identical usage_marketplace_item_daily row on both engines."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(3):
        _seed_full_event(
            repo,
            conn,
            backend,
            event_id=f"ep-{i}",
            occurred_at=today,
            tool_name="Skill",
            skill_name="myplug:design",
        )

    repo.rebuild_rollups(since_day=today.date())

    if backend == "duckdb":
        rows = conn.execute(
            "SELECT source, type, parent_plugin, name, count FROM usage_marketplace_item_daily "
            "WHERE type='skill' ORDER BY name"
        ).fetchall()
    else:
        with repo._engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT source, type, parent_plugin, name, count FROM usage_marketplace_item_daily "
                    "WHERE type='skill' ORDER BY name"
                )
            ).fetchall()
    assert [tuple(r) for r in rows] == [("curated", "skill", "myplug", "design", 3)]


def test_rebuild_rollups_since_day_none_is_full_rebuild(usage_repo):
    """since_day=None must cover ALL history, not just the last 7 days — the
    #728 semantics fix (docstring already promised this; code defaulted to
    today-7). Seeds a 20-day-old event and asserts it lands in the daily
    fact table when since_day is omitted."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    old_day = datetime.now(timezone.utc) - timedelta(days=20)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="old-1",
        occurred_at=old_day,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups()  # since_day=None -> full rebuild

    if backend == "duckdb":
        n = conn.execute(
            "SELECT COUNT(*) FROM usage_marketplace_item_daily WHERE type='skill' AND name='design'"
        ).fetchone()[0]
    else:
        with repo._engine.connect() as c:
            n = c.execute(
                sa.text("SELECT COUNT(*) FROM usage_marketplace_item_daily WHERE type='skill' AND name='design'")
            ).scalar()
    assert n == 1


def test_rebuild_rollups_explicit_cutoff_is_incremental(usage_repo):
    """An explicit since_day only rebuilds days >= cutoff — the steady-state
    scheduler-tick behaviour. A 20-day-old event must NOT appear when the
    cutoff is 'today'."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    old_day = today - timedelta(days=20)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="old-2",
        occurred_at=old_day,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date())

    if backend == "duckdb":
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
    else:
        with repo._engine.connect() as c:
            n = c.execute(sa.text("SELECT COUNT(*) FROM usage_marketplace_item_daily")).scalar()
    assert n == 0


def test_rebuild_rollups_force_30d_populates_window(usage_repo):
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="e1",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date(), force_30d=True)

    if backend == "duckdb":
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_window WHERE period_label='last_30d'").fetchone()[
            0
        ]
    else:
        with repo._engine.connect() as c:
            n = c.execute(
                sa.text("SELECT COUNT(*) FROM usage_marketplace_item_window WHERE period_label='last_30d'")
            ).scalar()
    assert n >= 1


def _tracker_ts(repo, conn, backend):
    sql = "SELECT processed_at FROM session_processor_state WHERE processor_name='marketplace_rollup_30d'"
    if backend == "duckdb":
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    with repo._engine.connect() as c:
        return c.execute(sa.text(sql)).scalar()


def test_rebuild_rollups_30d_throttled_until_force(usage_repo):
    """The hourly 30d-window throttle behaves identically on both backends:
    a second rebuild within the window leaves the tracker untouched;
    force_30d=True advances it."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="thr-1",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date())
    t1 = _tracker_ts(repo, conn, backend)
    assert t1 is not None

    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="thr-2",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )
    repo.rebuild_rollups(since_day=today.date())
    assert _tracker_ts(repo, conn, backend) == t1, "tracker must not advance within the throttle window"

    repo.rebuild_rollups(since_day=today.date(), force_30d=True)
    assert _tracker_ts(repo, conn, backend) > t1


# ---------------------------------------------------------------------------
# home_stats — backs the /home status frame (app.api.me.compute_home_stats).
# Pins identical aggregation semantics across DuckDB and Postgres, including
# the transitional user_id-OR-username match for pre-v45 legacy rows.
# ---------------------------------------------------------------------------


def _seed_session_summary(repo, conn, backend, **row):
    cols = [
        "session_file",
        "session_id",
        "username",
        "user_id",
        "started_at",
        "user_messages",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "processor_version",
    ]
    defaults = {
        "user_messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "processor_version": 1,
    }
    _insert(repo, conn, backend, "usage_session_summary", cols, [{**defaults, **row}])


def test_home_stats_counters_match_across_backends(usage_repo):
    """Sessions/prompts/token counters + distinct-project count aggregate
    identically on both backends: user_id rows and legacy username-only rows
    both count, out-of-window and foreign-user rows don't, NULL cwd is
    excluded from projects."""
    repo, conn, backend = usage_repo
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    inside = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    before = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)

    # counted: matched by user_id
    _seed_session_summary(
        repo, conn, backend,
        session_file="hs-1.jsonl", session_id="hs-1", username="other-name",
        user_id="uid-alice", started_at=inside,
        user_messages=3, input_tokens=100, output_tokens=20,
        cache_read_tokens=7, cache_creation_tokens=5,
    )
    # counted: legacy pre-v45 row matched by username, user_id NULL
    _seed_session_summary(
        repo, conn, backend,
        session_file="hs-2.jsonl", session_id="hs-2", username="alice",
        user_id=None, started_at=inside, user_messages=2, input_tokens=50,
    )
    # not counted: outside the window
    _seed_session_summary(
        repo, conn, backend,
        session_file="hs-3.jsonl", session_id="hs-3", username="alice",
        user_id="uid-alice", started_at=before, user_messages=9, input_tokens=999,
    )
    # not counted: different user entirely
    _seed_session_summary(
        repo, conn, backend,
        session_file="hs-4.jsonl", session_id="hs-4", username="bob",
        user_id="uid-bob", started_at=inside, user_messages=4,
    )

    ev_cols = ["id", "session_id", "session_file", "username", "user_id",
               "event_type", "source", "cwd", "occurred_at", "processor_version"]

    def ev(eid, cwd, occurred_at, user_id="uid-alice", username="alice"):
        return {
            "id": eid, "session_id": "hs-1", "session_file": "hs-1.jsonl",
            "username": username, "user_id": user_id, "event_type": "tool_use",
            "source": "builtin", "cwd": cwd, "occurred_at": occurred_at,
            "processor_version": 1,
        }

    _insert(repo, conn, backend, "usage_events", ev_cols, [
        ev("hs-e1", "/w/projA", inside),
        ev("hs-e2", "/w/projA", inside),                      # same project, deduped
        ev("hs-e3", "/w/projB", inside, user_id=None),        # legacy row, counts via username
        ev("hs-e4", None, inside),                            # NULL cwd excluded
        ev("hs-e5", "/w/projC", before),                      # out of window
        ev("hs-e6", "/w/projD", inside, user_id="uid-bob", username="bob"),
    ])

    stats = repo.home_stats("uid-alice", "alice", since)
    assert stats == {
        "sessions": 2,
        "prompts": 5,
        "input_tokens": 150,
        "output_tokens": 20,
        "cache_read": 7,
        "cache_creation": 5,
        "projects": 2,
    }
