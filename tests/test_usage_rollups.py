"""Tests for UsageRepository.rebuild_rollups — usage_tool_daily (legacy) +
usage_marketplace_item_daily + usage_marketplace_item_window (v46).

#728: rebuild_rollups moved from a DuckDB-only free function
(services.session_processors.usage_lib) onto UsageRepository /
UsagePgRepository so the producer is backend-aware. This suite pins the
DuckDB-side behaviour on the raw connection; tests/db_pg/test_usage_contract.py
covers the cross-backend contract (identical output on both engines).

Seeds usage_events directly, then asserts the rollup output. Lookup tables
(marketplace_plugins, store_entities) are seeded explicitly per test so the
prefix-split attribution has something to match against.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import duckdb

from src.repositories.usage import UsageRepository


def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _seed_curated_plugin(conn, plugin_name: str, marketplace_id: str = "mp") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_registry (id, name, url) VALUES (?, ?, 'https://example.test/repo.git')",
        [marketplace_id, marketplace_id.upper()],
    )
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
        [marketplace_id, plugin_name],
    )


def _seed_flea_entity(conn, entity_id: str, name: str, type_: str = "skill") -> None:
    # v49 phase-1 added NOT NULL `title` + `synthetic_name` columns. Direct
    # INSERTs (bypassing repo.create's fallback) must supply both. Mirror
    # the formula the repo uses so per-test asserts still see the canonical
    # `<name>-by-<owner>` value.
    conn.execute(
        "INSERT OR IGNORE INTO store_entities "
        "(id, owner_user_id, owner_username, type, name, version, "
        " visibility_status, title, synthetic_name) "
        "VALUES (?, ?, 'alice', ?, ?, '1.0', 'approved', ?, ?)",
        [entity_id, "uid-" + entity_id, type_, name, name, f"{name}-by-alice"],
    )


def _seed_event(
    conn,
    *,
    occurred_at: datetime,
    tool_name: str | None = None,
    event_type: str = "tool_use",
    skill_name: str | None = None,
    subagent_type: str | None = None,
    command_name: str | None = None,
    is_error: bool = False,
    username: str = "alice",
    user_id: str = "uid-alice",
    session_id: str = "s1",
    session_file: str = "s1.jsonl",
    event_id: str | None = None,
):
    """Insert a minimal usage_event row with the relevant identifier."""
    import hashlib

    eid = (
        event_id
        or hashlib.sha256(
            f"{session_id}|{occurred_at}|{tool_name}|{skill_name}|{subagent_type}|{command_name}".encode()
        ).hexdigest()
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO usage_events
            (id, session_id, session_file, username, user_id, event_uuid, event_type,
             tool_name, skill_name, subagent_type, command_name, is_error,
             source, ref_id, occurred_at, processor_version)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'builtin', NULL, ?, 5)
        """,
        [
            eid,
            session_id,
            session_file,
            username,
            user_id,
            event_type,
            tool_name,
            skill_name,
            subagent_type,
            command_name,
            is_error,
            occurred_at,
        ],
    )


class TestUpsertSummary:
    """upsert_summary — the per-session_file PK upsert the usage session
    processor calls on every tick (~every 10 min) for every session it
    touches."""

    def test_repeated_upsert_same_key_updates_in_place(self, tmp_path, monkeypatch):
        """Regression 2026-07-17: `INSERT OR REPLACE` deletes-then-inserts
        the conflicting row internally on DuckDB 1.5.4, hitting the same
        PRIMARY KEY index assertion as the DELETE-then-bulk-INSERT rollup
        producers (#909) — it crashed the whole app process in production
        twice, on two different session_file keys, ~24h apart. Switched to
        INSERT ... ON CONFLICT DO UPDATE. Two upserts of the same
        session_file must not raise and must leave exactly one row with the
        latest values."""
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = UsageRepository(conn)
        base = {
            "session_file": "alice/s1.jsonl",
            "session_id": "s1",
            "username": "alice",
            "started_at": datetime.now(timezone.utc),
            "tool_calls": 3,
        }
        repo.upsert_summary(base, processor_version=1)
        repo.upsert_summary({**base, "tool_calls": 7}, processor_version=2)

        rows = conn.execute(
            "SELECT tool_calls, processor_version FROM usage_session_summary WHERE session_file = ?",
            [base["session_file"]],
        ).fetchall()
        assert rows == [(7, 2)]


class TestRebuildRollupsToolDaily:
    """Legacy rollup still ticks — must keep its behaviour after v46."""

    def test_three_events_same_tool_same_day(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(3):
            _seed_event(conn, occurred_at=today, tool_name="Bash", event_id=f"eid-bash-{i}")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        rows = conn.execute("SELECT * FROM usage_tool_daily").fetchall()
        assert len(rows) == 1
        desc = [d[0] for d in conn.description]
        row = dict(zip(desc, rows[0]))
        assert row["invocations"] == 3
        assert row["tool_name"] == "Bash"

    def test_repeated_rebuild_same_key_does_not_raise(self, tmp_path, monkeypatch):
        """Regression: two rebuild_rollups calls whose since_day window both
        cover the same (day, tool_name, source) key must not raise. The prior
        DELETE-then-bulk-INSERT implementation hit a DuckDB 1.5.4 PRIMARY KEY
        index assertion in production whenever a scheduler tick re-rebuilt a
        window containing a key it had already written on a previous tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Edit", event_id="e-1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        _seed_event(conn, occurred_at=today, tool_name="Edit", event_id="e-2")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        rows = conn.execute("SELECT invocations FROM usage_tool_daily WHERE tool_name = 'Edit'").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2

    def test_stale_key_removed_when_source_event_deleted(self, tmp_path, monkeypatch):
        """Regression (agnes-reviewer-parity finding on PR #909): switching to
        ON CONFLICT DO UPDATE must not silently retain a rollup row forever
        once its only source event is gone (e.g. a corrected/deleted usage
        event). The anti-join delete only removes keys ABSENT from the fresh
        set, so it can coexist with the ON CONFLICT upsert without
        delete-then-reinserting the same key."""
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", event_id="e-stale")
        _seed_event(conn, occurred_at=today, tool_name="Edit", event_id="e-keep")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        tools = {r[0] for r in conn.execute("SELECT tool_name FROM usage_tool_daily").fetchall()}
        assert tools == {"Bash", "Edit"}

        conn.execute("DELETE FROM usage_events WHERE id = ?", ["e-stale"])
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        tools = {r[0] for r in conn.execute("SELECT tool_name FROM usage_tool_daily").fetchall()}
        assert tools == {"Edit"}, "stale Bash row must be removed once its source event is deleted"

    def test_distinct_users(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", username="alice", event_id="e-a")
        _seed_event(
            conn,
            occurred_at=today,
            tool_name="Bash",
            username="bob",
            user_id="uid-bob",
            session_id="s2",
            session_file="s2.jsonl",
            event_id="e-b",
        )
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        row = conn.execute("SELECT distinct_users FROM usage_tool_daily").fetchone()
        assert row[0] == 2


class TestMarketplaceItemDaily:
    """Daily fact table — v46 replacement for usage_plugin_daily."""

    def test_curated_skill_attributed(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(3):
            _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id=f"ep-{i}")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        rows = conn.execute(
            "SELECT source, type, parent_plugin, name, count "
            "FROM usage_marketplace_item_daily "
            "WHERE type='skill' ORDER BY name"
        ).fetchall()
        assert rows == [("curated", "skill", "myplug", "design", 3)]

    def test_stale_key_removed_when_source_event_deleted(self, tmp_path, monkeypatch):
        """Same anti-join-delete regression as usage_tool_daily (PR #909
        parity finding), for the marketplace daily fact table. Uses two
        distinct curated plugins (not a builtin tool — see
        test_builtin_excluded — which never enters this table at all and
        would make the "stale" side a no-op)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        _seed_curated_plugin(conn, "otherplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e-keep")
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="otherplug:gone", event_id="e-stale")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        names = {r[0] for r in conn.execute("SELECT name FROM usage_marketplace_item_daily").fetchall()}
        assert names == {"design", "myplug", "gone", "otherplug"}

        conn.execute("DELETE FROM usage_events WHERE id = ?", ["e-stale"])
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        names = {r[0] for r in conn.execute("SELECT name FROM usage_marketplace_item_daily").fetchall()}
        assert names == {"design", "myplug"}, "stale entries must be removed once their source events are gone"

    def test_curated_plugin_row_aggregates_children(self, tmp_path, monkeypatch):
        """Plugin-level row sums child invocations (skills + agents) for the
        same parent_plugin on the same day."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e-s1")
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e-s2")
        _seed_event(
            conn,
            occurred_at=today,
            tool_name="Task",
            event_type="subagent",
            subagent_type="myplug:helper",
            event_id="e-a1",
        )
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        row = conn.execute(
            "SELECT count FROM usage_marketplace_item_daily WHERE type='plugin' AND name='myplug'"
        ).fetchone()
        assert row is not None
        assert row[0] == 3

    def test_flea_skill_attributed_with_empty_parent(self, tmp_path, monkeypatch):
        """v49 phase-5: rollup `name` carries the entity's synthetic_name
        (`<name>-by-<owner>`), matching what Claude Code writes after the
        bundle plugin's `flea:` prefix."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_flea_entity(conn, "ent-1", "flea-skill", type_="skill")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="flea:flea-skill-by-alice", event_id="ef-1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        row = conn.execute(
            "SELECT source, type, parent_plugin, name FROM usage_marketplace_item_daily WHERE source='flea'"
        ).fetchone()
        assert row == ("flea", "skill", "", "flea-skill-by-alice")

    def test_flea_plugin_row_aggregates_children(self, tmp_path, monkeypatch):
        """v49 phase-6: nested skill/agent invocations under a flea plugin
        entity get rolled up into a synthetic plugin-level row mirroring
        the curated path. Without this, `_load_invocation_stats('flea')`
        (filters `parent_plugin = ''`) returned no row for plugin entity
        cards / detail telemetry chips even though nested children were
        attributed correctly. `distinct_users` is the union across
        children (one user invoking two skills counts once)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        # Plugin entity with synthetic_name="my-plug-by-alice" — the
        # frontmatter-baked name Claude Code writes as the JSONL prefix
        # when invoking nested skills inside this flea plugin.
        _seed_flea_entity(conn, "ent-plug", "my-plug", type_="plugin")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        # Two nested skills, one of them invoked by two distinct users —
        # plugin-level distinct_users should be 2 (union of children),
        # not 3 (sum of per-child counts).
        _seed_event(
            conn,
            occurred_at=today,
            tool_name="Skill",
            skill_name="my-plug-by-alice:setup",
            user_id="uid-alice",
            username="alice",
            event_id="p1",
        )
        _seed_event(
            conn,
            occurred_at=today,
            tool_name="Skill",
            skill_name="my-plug-by-alice:setup",
            user_id="uid-bob",
            username="bob",
            event_id="p2",
        )
        _seed_event(
            conn,
            occurred_at=today,
            tool_name="Skill",
            skill_name="my-plug-by-alice:review",
            user_id="uid-alice",
            username="alice",
            event_id="p3",
        )
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        # Plugin-level aggregated row.
        row = conn.execute(
            "SELECT source, type, parent_plugin, name, count, distinct_users "
            "FROM usage_marketplace_item_daily "
            "WHERE source='flea' AND type='plugin'"
        ).fetchone()
        assert row is not None, "flea plugin-level aggregated row missing"
        assert row == ("flea", "plugin", "", "my-plug-by-alice", 3, 2)
        # Child rows still present alongside the aggregate.
        children = conn.execute(
            "SELECT name, count FROM usage_marketplace_item_daily "
            "WHERE source='flea' AND type='skill' "
            "AND parent_plugin='my-plug-by-alice' ORDER BY name"
        ).fetchall()
        assert children == [("review", 1), ("setup", 2)]

    def test_unknown_plugin_excluded(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        # No marketplace plugin seeded — prefix `ghost` is unknown.
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="ghost:foo", event_id="e-ghost")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
        assert n == 0

    def test_builtin_excluded(self, tmp_path, monkeypatch):
        """Bash et al. carry no plugin prefix → never enter the marketplace
        rollup, even though they appear in usage_tool_daily."""
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", event_id="eb-1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
        assert n == 0

    def test_flat_slash_command_excluded(self, tmp_path, monkeypatch):
        """A slash command without a `:` prefix is ignored (per product rule
        — built-in commands like /exit don't get marketplace attribution)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, event_type="slash_command", command_name="exit", event_id="e-exit")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
        assert n == 0

    def test_slash_command_with_prefix_counts_as_skill(self, tmp_path, monkeypatch):
        """`compound:debug` slash command attributes to `compound` plugin as
        type='skill' (commands counted under skill rollup per product rule)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "compound")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, event_type="slash_command", command_name="compound:debug", event_id="e-cd")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        rows = conn.execute(
            "SELECT source, type, parent_plugin, name, count FROM usage_marketplace_item_daily WHERE type='skill'"
        ).fetchall()
        assert rows == [("curated", "skill", "compound", "debug", 1)]


class TestMarketplaceItemWindow:
    """Sliding-window snapshot — distinct_users is TRUE distinct across the
    window (not sum-of-daily-distincts)."""

    def test_refreshed_at_updates_on_conflict(self, tmp_path, monkeypatch):
        """Devin Review finding on PR #909: switching to ON CONFLICT DO
        UPDATE must still bump `refreshed_at` for an existing key — the
        prior DELETE-then-INSERT path always got a fresh
        DEFAULT current_timestamp on every rebuild, so an existing key that's
        merely upserted (not freshly inserted) must not keep its original
        timestamp forever."""
        import time

        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        first = conn.execute("SELECT refreshed_at FROM usage_marketplace_item_window WHERE name='design'").fetchone()[0]

        time.sleep(0.01)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e2")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        second = conn.execute("SELECT refreshed_at FROM usage_marketplace_item_window WHERE name='design'").fetchone()[
            0
        ]

        assert second > first, "refreshed_at must advance when an existing key is upserted, not just inserted"

    def test_stale_key_removed_when_source_event_deleted(self, tmp_path, monkeypatch):
        """Same anti-join-delete regression as usage_tool_daily (PR #909
        parity finding), for the sliding-window snapshot table. Uses two
        distinct curated plugins (not a builtin tool — see
        test_builtin_excluded — which never enters this table at all and
        would make the "stale" side a no-op)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        _seed_curated_plugin(conn, "otherplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e-keep")
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="otherplug:gone", event_id="e-stale")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM usage_marketplace_item_window WHERE period_label='last_7d'"
            ).fetchall()
        }
        assert names == {"design", "myplug", "gone", "otherplug"}

        conn.execute("DELETE FROM usage_events WHERE id = ?", ["e-stale"])
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM usage_marketplace_item_window WHERE period_label='last_7d'"
            ).fetchall()
        }
        assert names == {"design", "myplug"}, "stale entries must be removed once their source events are gone"

    def test_true_distinct_users_across_days(self, tmp_path, monkeypatch):
        """Alice invokes the same skill on day 1 and day 5 — daily fact rows
        each show distinct_users=1, but the window snapshot must show 1
        (true distinct), not 2 (sum)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        day1 = now - timedelta(days=5)
        day2 = now - timedelta(days=1)
        _seed_event(
            conn,
            occurred_at=day1,
            tool_name="Skill",
            skill_name="myplug:design",
            username="alice",
            user_id="uid-alice",
            event_id="ed1",
        )
        _seed_event(
            conn,
            occurred_at=day2,
            tool_name="Skill",
            skill_name="myplug:design",
            username="alice",
            user_id="uid-alice",
            event_id="ed2",
        )
        UsageRepository(conn).rebuild_rollups(since_day=day1.date())

        # Daily fact: two rows, each distinct_users=1
        daily = conn.execute(
            "SELECT day, distinct_users FROM usage_marketplace_item_daily "
            "WHERE type='skill' AND name='design' ORDER BY day"
        ).fetchall()
        assert len(daily) == 2
        assert all(r[1] == 1 for r in daily)

        # Window 7d: 1 row, distinct_users=1 (true distinct)
        row = conn.execute(
            "SELECT invocations, distinct_users "
            "FROM usage_marketplace_item_window "
            "WHERE period_label='last_7d' AND type='skill' AND name='design'"
        ).fetchone()
        assert row == (2, 1)

    def test_30d_window_built_on_first_call(self, tmp_path, monkeypatch):
        """No prior tracker row → 30d window populates on first rebuild call."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_window WHERE period_label='last_30d'").fetchone()[
            0
        ]
        assert n >= 1  # plugin row + skill row

    def test_30d_window_throttled_until_force(self, tmp_path, monkeypatch):
        """Second tick within the hour skips 30d refresh; force_30d=True
        forces it."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_curated_plugin(conn, "myplug")
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e1")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        # tracker timestamp recorded — capture it
        t1 = conn.execute(
            "SELECT processed_at FROM session_processor_state WHERE processor_name='marketplace_rollup_30d'"
        ).fetchone()[0]
        # Add a second event and rebuild without force — tracker shouldn't move
        _seed_event(conn, occurred_at=today, tool_name="Skill", skill_name="myplug:design", event_id="e2")
        UsageRepository(conn).rebuild_rollups(since_day=today.date())
        t2 = conn.execute(
            "SELECT processed_at FROM session_processor_state WHERE processor_name='marketplace_rollup_30d'"
        ).fetchone()[0]
        assert t1 == t2, "30d tracker should not advance within throttle window"
        # force=True bumps it
        UsageRepository(conn).rebuild_rollups(since_day=today.date(), force_30d=True)
        t3 = conn.execute(
            "SELECT processed_at FROM session_processor_state WHERE processor_name='marketplace_rollup_30d'"
        ).fetchone()[0]
        assert t3 > t1
