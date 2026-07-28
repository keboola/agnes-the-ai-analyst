"""Cross-engine contract tests for the reports repository.

Targets: ReportsRepository (DuckDB) / ReportsPgRepository (Postgres).
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must produce
identical outputs from both engines (the sync-map BLOCKING rule for any new
dual-backend repo). Mirrors the fixture pattern in test_usage_contract.py.

Seeding goes through direct backend-aware INSERTs because ReportsRepository is
read-only and its tables span several domains (usage_events, the marketplace
rollup, and the install ledgers) with no single write method.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

ANCHOR = date(2026, 6, 20)
PREV = ANCHOR - timedelta(days=1)


def _ts(d: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# repo construction — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.reports import ReportsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return ReportsRepository(conn), conn


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
    from src.repositories.reports_pg import ReportsPgRepository

    return ReportsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def reports_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, backend)`` for both backends with identical seed data."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        _seed(repo, backend)
        yield repo, backend
        conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        _seed(repo, backend)
        yield repo, backend


# ---------------------------------------------------------------------------
# backend-agnostic seeding
# ---------------------------------------------------------------------------


def _insert(repo, backend, table, cols, rows):
    collist = ", ".join(cols)
    if backend == "duckdb":
        ph = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        for r in rows:
            repo.conn.execute(sql, [r[c] for c in cols])
    else:
        ph = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        with repo._engine.begin() as c:
            for r in rows:
                c.execute(sa.text(sql), {k: r[k] for k in cols})


def _seed(repo, backend):
    # usage_events: anchor day = 3 events / 2 users / 1 error; one event late in
    # the UTC day (23:30) to pin day-bucketing. prev day = 1 event.
    ev_cols = [
        "id",
        "session_id",
        "session_file",
        "username",
        "event_type",
        "tool_name",
        "is_error",
        "source",
        "occurred_at",
        "processor_version",
    ]
    _insert(
        repo,
        backend,
        "usage_events",
        ev_cols,
        [
            {
                "id": "e1",
                "session_id": "s1",
                "session_file": "alice/a.jsonl",
                "username": "alice",
                "event_type": "tool_use",
                "tool_name": "Bash",
                "is_error": False,
                "source": "curated",
                "occurred_at": _ts(ANCHOR),
                "processor_version": 1,
            },
            {
                "id": "e2",
                "session_id": "s2",
                "session_file": "bob/b.jsonl",
                "username": "bob",
                "event_type": "tool_use",
                "tool_name": "Read",
                "is_error": False,
                "source": "curated",
                "occurred_at": _ts(ANCHOR),
                "processor_version": 1,
            },
            {
                "id": "e3",
                "session_id": "s1",
                "session_file": "alice/a.jsonl",
                "username": "alice",
                "event_type": "tool_use",
                "tool_name": "Edit",
                "is_error": True,
                "source": "flea",
                "occurred_at": _ts(ANCHOR, 23, 30),
                "processor_version": 1,
            },
            {
                "id": "e4",
                "session_id": "s3",
                "session_file": "alice/c.jsonl",
                "username": "alice",
                "event_type": "tool_use",
                "tool_name": "Bash",
                "is_error": False,
                "source": "curated",
                "occurred_at": _ts(PREV),
                "processor_version": 1,
            },
        ],
    )

    # usage_session_summary: 2 sessions on anchor, 1 on prev.
    ss_cols = ["session_file", "session_id", "username", "started_at", "processor_version"]
    _insert(
        repo,
        backend,
        "usage_session_summary",
        ss_cols,
        [
            {
                "session_file": "s1.jsonl",
                "session_id": "s1",
                "username": "alice",
                "started_at": _ts(ANCHOR),
                "processor_version": 1,
            },
            {
                "session_file": "s2.jsonl",
                "session_id": "s2",
                "username": "bob",
                "started_at": _ts(ANCHOR),
                "processor_version": 1,
            },
            {
                "session_file": "s3.jsonl",
                "session_id": "s3",
                "username": "alice",
                "started_at": _ts(PREV),
                "processor_version": 1,
            },
        ],
    )

    # marketplace-item rollup (anchor day).
    mi_cols = ["day", "source", "type", "parent_plugin", "name", "count", "distinct_users", "error_count"]
    _insert(
        repo,
        backend,
        "usage_marketplace_item_daily",
        mi_cols,
        [
            {
                "day": ANCHOR,
                "source": "curated",
                "type": "plugin",
                "parent_plugin": "",
                "name": "product-analyzer",
                "count": 8,
                "distinct_users": 3,
                "error_count": 1,
            },
            {
                "day": ANCHOR,
                "source": "flea",
                "type": "agent",
                "parent_plugin": "",
                "name": "data-bot",
                "count": 4,
                "distinct_users": 2,
                "error_count": 0,
            },
        ],
    )

    # installs (anchor day): 2 curated subscriptions + 1 flea install.
    _insert(
        repo,
        backend,
        "user_plugin_optouts",
        ["user_id", "marketplace_id", "plugin_name", "opted_out_at"],
        [
            {
                "user_id": "u1",
                "marketplace_id": "curated-product",
                "plugin_name": "product-analyzer",
                "opted_out_at": _ts(ANCHOR),
            },
            {
                "user_id": "u2",
                "marketplace_id": "curated-product",
                "plugin_name": "product-analyzer",
                "opted_out_at": _ts(ANCHOR),
            },
        ],
    )
    _insert(
        repo,
        backend,
        "store_entities",
        ["id", "owner_user_id", "owner_username", "type", "name", "version", "title", "synthetic_name"],
        [
            {
                "id": "ent-1",
                "owner_user_id": "owner",
                "owner_username": "owner",
                "type": "agent",
                "name": "data-bot",
                "version": "1.0.0",
                "title": "Data Bot",
                "synthetic_name": "data-bot-by-owner",
            },
        ],
    )
    _insert(
        repo,
        backend,
        "user_store_installs",
        ["user_id", "entity_id", "installed_at"],
        [
            {"user_id": "u1", "entity_id": "ent-1", "installed_at": _ts(ANCHOR)},
        ],
    )


# ---------------------------------------------------------------------------
# the contract — identical outputs from both engines
# ---------------------------------------------------------------------------

_P_START, _P_END = _ts(ANCHOR), _ts(ANCHOR + timedelta(days=1))
_TREND_START = _ts(PREV)


def test_event_window(reports_repo):
    repo, _ = reports_repo
    assert repo.event_window(_P_START, _P_END) == {"invocations": 3, "active_users": 2, "errors": 1}


def test_session_count(reports_repo):
    repo, _ = reports_repo
    assert repo.session_count(_P_START, _P_END) == 2


def test_events_daily_buckets_by_utc_day(reports_repo):
    repo, _ = reports_repo
    daily = repo.events_daily(_TREND_START, _P_END)
    # anchor day has e1, e2, e3 (the 23:30 UTC event stays on the anchor day on
    # both engines - the UTC-bucketing guarantee); prev day has e4.
    assert daily[ANCHOR] == {"invocations": 3, "active_users": 2, "errors": 1}
    assert daily[PREV] == {"invocations": 1, "active_users": 1, "errors": 0}


def test_by_source(reports_repo):
    repo, _ = reports_repo
    by_source = {r["source"]: r for r in repo.by_source(_P_START, _P_END)}
    assert by_source["curated"] == {"source": "curated", "invocations": 2, "distinct_users": 2, "error_count": 0}
    assert by_source["flea"] == {"source": "flea", "invocations": 1, "distinct_users": 1, "error_count": 1}


def test_items_window(reports_repo):
    repo, _ = reports_repo
    items = repo.items_window(ANCHOR, ANCHOR + timedelta(days=1))
    assert items[("curated", "plugin", "", "product-analyzer")] == {
        "invocations": 8,
        "distinct_users": 3,
        "error_count": 1,
    }
    assert items[("flea", "agent", "", "data-bot")] == {"invocations": 4, "distinct_users": 2, "error_count": 0}


def test_install_counts(reports_repo):
    repo, _ = reports_repo
    assert repo.install_counts(_P_START, _P_END) == {"curated": 2, "flea": 1}


def test_installs_daily_buckets_by_utc_day(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_daily(_TREND_START, _P_END) == {ANCHOR: 3}


def test_installs_curated_detail(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_curated_detail(_P_START, _P_END) == [
        {"ref_id": "curated-product/product-analyzer", "name": "product-analyzer", "installs": 2}
    ]


def test_installs_flea_detail(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_flea_detail(_P_START, _P_END) == [{"entity_id": "ent-1", "name": "data-bot", "installs": 1}]


# ---------------------------------------------------------------------------
# invocation_stats (#728) — the marketplace browse-panel telemetry read.
#
# Seeded via the REAL rollup producer (UsageRepository.rebuild_rollups /
# UsagePgRepository.rebuild_rollups), not hand-inserted rollup rows, so the
# test exercises the same production data flow the browse page relies on:
# usage_events -> rebuild_rollups -> usage_marketplace_item_{daily,window} ->
# ReportsRepository.invocation_stats. Moved off the inline SQL that used to
# run on the always-DuckDB ``_get_db`` connection in
# ``app/api/marketplace.py::_load_invocation_stats`` (#773 made the producer
# dual-backend; this closes the last read-side gap).
# ---------------------------------------------------------------------------


def _make_duckdb_pair(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.reports import ReportsRepository
    from src.repositories.usage import UsageRepository

    conn = _open_duckdb(str(tmp_path / "duck2.duckdb"))
    _ensure_schema(conn)
    return ReportsRepository(conn), UsageRepository(conn), conn


def _make_pg_pair(pg_engine, monkeypatch):
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

    from src.repositories.reports_pg import ReportsPgRepository
    from src.repositories.usage_pg import UsagePgRepository

    return ReportsPgRepository(engine), UsagePgRepository(engine), None


def _insert_pair(reports, conn, backend, table, cols, rows):
    """Backend-aware raw INSERT — mirrors the ``_insert`` helper above and
    the one in ``test_usage_contract.py``."""
    collist = ", ".join(cols)
    if backend == "duckdb":
        ph = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        for r in rows:
            conn.execute(sql, [r[c] for c in cols])
    else:
        ph = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        with reports._engine.begin() as c:
            for r in rows:
                c.execute(sa.text(sql), {k: r[k] for k in cols})


def _seed_invocation_events(reports, usage, conn, backend):
    """Seed a curated plugin + a standalone flea agent, with events split
    into a "recent" (today) and a "prior" (10 days ago) bucket so the
    window (30d/7d) AND the trend (recent-7-vs-prior-7) calculations both
    have real data to fold, then run the real producer."""
    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    prior_day = now - timedelta(days=10)

    _insert_pair(
        reports,
        conn,
        backend,
        "marketplace_registry",
        ["id", "name", "url"],
        [{"id": "mp", "name": "MP", "url": "https://example.test/repo.git"}],
    )
    _insert_pair(
        reports,
        conn,
        backend,
        "marketplace_plugins",
        ["marketplace_id", "name"],
        [{"marketplace_id": "mp", "name": "widget"}],
    )
    _insert_pair(
        reports,
        conn,
        backend,
        "store_entities",
        [
            "id",
            "owner_user_id",
            "owner_username",
            "type",
            "name",
            "version",
            "title",
            "synthetic_name",
            "visibility_status",
        ],
        [
            {
                "id": "ent-1",
                "owner_user_id": "owner",
                "owner_username": "owner",
                "type": "agent",
                "name": "helper",
                "version": "1.0.0",
                "title": "Helper",
                "synthetic_name": "helper-bot",
                "visibility_status": "approved",
            }
        ],
    )

    ev_cols = [
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

    def _event(event_id, occurred_at, skill_name, user_id):
        return {
            "id": event_id,
            "session_id": event_id,
            "session_file": f"{event_id}.jsonl",
            "username": user_id,
            "user_id": user_id,
            "event_type": "tool_use",
            "tool_name": "Skill",
            "skill_name": skill_name,
            "subagent_type": None,
            "command_name": None,
            "is_error": False,
            "source": "builtin",
            "occurred_at": occurred_at,
            "processor_version": 5,
        }

    rows = []
    # curated "widget:design" — recent: 5 events / 3 distinct users.
    for i, uid in enumerate(["u1", "u2", "u3", "u1", "u2"]):
        rows.append(_event(f"wr-{i}", now, "widget:design", uid))
    # curated "widget:design" — prior (10d ago): 8 events / 3 distinct users.
    for i, uid in enumerate(["u1", "u4", "u5", "u4", "u5", "u4", "u5", "u4"]):
        rows.append(_event(f"wp-{i}", prior_day, "widget:design", uid))
    # flea "helper-bot" — recent: 4 events / 2 distinct users.
    for i, uid in enumerate(["u10", "u11", "u10", "u11"]):
        rows.append(_event(f"fr-{i}", now, "flea:helper-bot", uid))
    # flea "helper-bot" — prior (10d ago): 6 events / 3 distinct users.
    for i, uid in enumerate(["u10", "u12", "u13", "u12", "u13", "u12"]):
        rows.append(_event(f"fp-{i}", prior_day, "flea:helper-bot", uid))
    _insert_pair(reports, conn, backend, "usage_events", ev_cols, rows)

    usage.rebuild_rollups(force_30d=True)


@pytest.fixture(params=["duckdb", "pg"])
def invocation_stats_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(reports_repo, backend)`` with rollups seeded through the
    real producer rather than hand-inserted rollup rows."""
    backend = request.param
    if backend == "duckdb":
        reports, usage, conn = _make_duckdb_pair(tmp_path)
        _seed_invocation_events(reports, usage, conn, backend)
        yield reports, backend
        conn.close()
    else:
        reports, usage, _ = _make_pg_pair(pg_engine, monkeypatch)
        _seed_invocation_events(reports, usage, None, backend)
        yield reports, backend


def test_invocation_stats_curated_plugin_rollup(invocation_stats_repo):
    repo, _ = invocation_stats_repo
    stats = repo.invocation_stats("curated")
    widget = stats["widget"]
    assert widget["invocations_30d"] == 13  # 5 recent + 8 prior
    assert widget["distinct_users_30d"] == 5  # {u1,u2,u3,u4,u5}
    assert widget["invocations_7d"] == 5  # only the recent bucket
    assert widget["distinct_users_7d"] == 3  # {u1,u2,u3}
    assert widget["trend_pct"] == pytest.approx((5 - 8) / 8 * 100.0)


def test_invocation_stats_flea_entity_rollup(invocation_stats_repo):
    repo, _ = invocation_stats_repo
    stats = repo.invocation_stats("flea")
    helper = stats["helper-bot"]
    assert helper["invocations_30d"] == 10  # 4 recent + 6 prior
    assert helper["distinct_users_30d"] == 4  # {u10,u11,u12,u13}
    assert helper["invocations_7d"] == 4
    assert helper["distinct_users_7d"] == 2  # {u10,u11}
    assert helper["trend_pct"] == pytest.approx((4 - 6) / 6 * 100.0)


def test_invocation_stats_identical_across_backends(pg_engine, monkeypatch, tmp_path):
    """Same seed via the same producer -> identical output on both engines."""
    reports_d, usage_d, conn_d = _make_duckdb_pair(tmp_path)
    _seed_invocation_events(reports_d, usage_d, conn_d, "duckdb")
    duck_curated = reports_d.invocation_stats("curated")
    duck_flea = reports_d.invocation_stats("flea")
    conn_d.close()

    reports_p, usage_p, _ = _make_pg_pair(pg_engine, monkeypatch)
    _seed_invocation_events(reports_p, usage_p, None, "pg")
    pg_curated = reports_p.invocation_stats("curated")
    pg_flea = reports_p.invocation_stats("flea")

    # The trend_pct division is pure Python (identical on both engines given
    # identical integer inputs), so plain equality holds — no float slop.
    assert duck_curated == pg_curated
    assert duck_flea == pg_flea


# ---------------------------------------------------------------------------
# plugin_daily_series / inner_item_stats / inner_items_stats_by_parent (#728)
# — the detail-page telemetry reads (plugin daily-series chart, single-item
# hero chip, bulk inner-card grid). Same seed as invocation_stats above (the
# "widget" curated plugin's "design" skill has both a recent (today) and a
# prior (10d ago) event bucket), so the same widget/design fixture data
# exercises all three.
# ---------------------------------------------------------------------------


def test_plugin_daily_series_curated(invocation_stats_repo):
    repo, _ = invocation_stats_repo
    series = repo.plugin_daily_series("curated", "widget")
    assert len(series) == 30
    by_day = {e["day"]: e["invocations"] for e in series}
    # UTC, matching the seed clock in _seed_invocation_events and the UTC
    # day buckets of the rollup fact — date.today() breaks when the local
    # date differs from the UTC date.
    utc_today = datetime.now(timezone.utc).date()
    today = utc_today.isoformat()
    prior_day = (utc_today - timedelta(days=10)).isoformat()
    assert by_day[today] == 5
    assert by_day[prior_day] == 8


def test_plugin_daily_series_flea_standalone_entity(invocation_stats_repo):
    """A standalone flea entity (type='agent', parent_plugin='') must get a
    non-zero daily series — the filter mirrors invocation_stats, not a
    hard-coded type='plugin' (Devin finding on #777)."""
    repo, _ = invocation_stats_repo
    series = repo.plugin_daily_series("flea", "helper-bot")
    assert len(series) == 30
    assert sum(e["invocations"] for e in series) == 10  # 4 recent + 6 prior


def test_inner_item_stats_curated_skill(invocation_stats_repo):
    repo, _ = invocation_stats_repo
    stat = repo.inner_item_stats("curated", parent_plugin="widget", name="design", item_type="skill")
    assert stat["invocations_30d"] == 13  # 5 recent + 8 prior
    assert stat["distinct_users_30d"] == 5  # {u1,u2,u3,u4,u5}
    assert stat["trend_pct"] == pytest.approx((5 - 8) / 8 * 100.0)
    assert len(stat["daily_series"]) == 30


def test_inner_items_stats_by_parent_curated(invocation_stats_repo):
    repo, _ = invocation_stats_repo
    stats = repo.inner_items_stats_by_parent("curated", "widget")
    assert stats[("design", "skill")] == pytest.approx(
        {
            "invocations_30d": 13,
            "distinct_users_30d": 5,
            "trend_pct": (5 - 8) / 8 * 100.0,
        }
    )


def test_new_detail_helpers_identical_across_backends(pg_engine, monkeypatch, tmp_path):
    """plugin_daily_series / inner_item_stats / inner_items_stats_by_parent —
    same seed via the same producer -> identical output on both engines."""
    reports_d, usage_d, conn_d = _make_duckdb_pair(tmp_path)
    _seed_invocation_events(reports_d, usage_d, conn_d, "duckdb")
    duck_series = reports_d.plugin_daily_series("curated", "widget")
    duck_item = reports_d.inner_item_stats("curated", parent_plugin="widget", name="design", item_type="skill")
    duck_by_parent = reports_d.inner_items_stats_by_parent("curated", "widget")
    conn_d.close()

    reports_p, usage_p, _ = _make_pg_pair(pg_engine, monkeypatch)
    _seed_invocation_events(reports_p, usage_p, None, "pg")
    pg_series = reports_p.plugin_daily_series("curated", "widget")
    pg_item = reports_p.inner_item_stats("curated", parent_plugin="widget", name="design", item_type="skill")
    pg_by_parent = reports_p.inner_items_stats_by_parent("curated", "widget")

    assert duck_series == pg_series
    assert duck_item == pg_item
    assert duck_by_parent == pg_by_parent


# ---------------------------------------------------------------------------
# PG session-TimeZone independence (#840)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_reports_repo_skewed_tz(pg_engine, monkeypatch):
    """PG reports repo whose pooled connections run in a session TimeZone
    whose local date is guaranteed to differ from the UTC date right now —
    mirrors ``test_usage_contract.pg_repo_skewed_tz``."""
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    # UTC hour >= 12 -> UTC+14 is already tomorrow; else UTC-12 is yesterday.
    # (POSIX Etc/GMT signs are inverted: Etc/GMT-14 == UTC+14.)
    tz = "Etc/GMT-14" if datetime.now(timezone.utc).hour >= 12 else "Etc/GMT+12"

    def _set_tz(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute(f"SET TIME ZONE '{tz}'")
        cur.close()
        # PG's SET is transactional — commit it, or the pool's
        # reset-on-return rollback reverts the GUC on check-in.
        dbapi_conn.commit()

    sa.event.listen(repo._engine, "connect", _set_tz)
    repo._engine.dispose()  # drop pre-listener pooled connections
    yield repo
    sa.event.remove(repo._engine, "connect", _set_tz)
    repo._engine.dispose()


def test_pg_trend_windows_are_utc_regardless_of_session_timezone(pg_reports_repo_skewed_tz):
    """The 7/14-day trend split in ``invocation_stats`` must anchor on the
    UTC "today". A revert to bare ``CURRENT_DATE`` shifts both boundaries by
    a day under a skewed session TimeZone: with the local date ahead of UTC
    the day-7 row falls out of the recent window (recent=0, prior=8 ->
    trend -100.0); with it behind, the day-8 row leaks into the recent
    window (prior=0 < 3 -> trend None). Either direction breaks the
    assertion below, so a CURRENT_DATE revert cannot pass CI."""
    repo = pg_reports_repo_skewed_tz
    utc_day = datetime.now(timezone.utc).date()

    # Sanity: the session clock really is on the other side of midnight.
    with repo._engine.connect() as c:
        assert c.execute(sa.text("SELECT CURRENT_DATE")).scalar_one() != utc_day

    base = {"source": "curated", "type": "plugin", "parent_plugin": "", "name": "tzplug", "distinct_users": 1, "error_count": 0}
    _insert(
        repo,
        "pg",
        "usage_marketplace_item_daily",
        ["day", "source", "type", "parent_plugin", "name", "count", "distinct_users", "error_count"],
        [
            {**base, "day": utc_day - timedelta(days=7), "count": 5},
            {**base, "day": utc_day - timedelta(days=8), "count": 3},
        ],
    )

    stats = repo.invocation_stats("curated")
    assert stats["tzplug"]["trend_pct"] == pytest.approx((5 - 3) / 3 * 100.0)
