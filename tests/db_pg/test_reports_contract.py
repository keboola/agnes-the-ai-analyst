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
    ev_cols = ["id", "session_id", "session_file", "username", "event_type",
               "tool_name", "is_error", "source", "occurred_at", "processor_version"]
    _insert(repo, backend, "usage_events", ev_cols, [
        {"id": "e1", "session_id": "s1", "session_file": "alice/a.jsonl",
         "username": "alice", "event_type": "tool_use", "tool_name": "Bash",
         "is_error": False, "source": "curated", "occurred_at": _ts(ANCHOR), "processor_version": 1},
        {"id": "e2", "session_id": "s2", "session_file": "bob/b.jsonl",
         "username": "bob", "event_type": "tool_use", "tool_name": "Read",
         "is_error": False, "source": "curated", "occurred_at": _ts(ANCHOR), "processor_version": 1},
        {"id": "e3", "session_id": "s1", "session_file": "alice/a.jsonl",
         "username": "alice", "event_type": "tool_use", "tool_name": "Edit",
         "is_error": True, "source": "flea", "occurred_at": _ts(ANCHOR, 23, 30), "processor_version": 1},
        {"id": "e4", "session_id": "s3", "session_file": "alice/c.jsonl",
         "username": "alice", "event_type": "tool_use", "tool_name": "Bash",
         "is_error": False, "source": "curated", "occurred_at": _ts(PREV), "processor_version": 1},
    ])

    # usage_session_summary: 2 sessions on anchor, 1 on prev.
    ss_cols = ["session_file", "session_id", "username", "started_at", "processor_version"]
    _insert(repo, backend, "usage_session_summary", ss_cols, [
        {"session_file": "s1.jsonl", "session_id": "s1", "username": "alice",
         "started_at": _ts(ANCHOR), "processor_version": 1},
        {"session_file": "s2.jsonl", "session_id": "s2", "username": "bob",
         "started_at": _ts(ANCHOR), "processor_version": 1},
        {"session_file": "s3.jsonl", "session_id": "s3", "username": "alice",
         "started_at": _ts(PREV), "processor_version": 1},
    ])

    # marketplace-item rollup (anchor day).
    mi_cols = ["day", "source", "type", "parent_plugin", "name", "count",
               "distinct_users", "error_count"]
    _insert(repo, backend, "usage_marketplace_item_daily", mi_cols, [
        {"day": ANCHOR, "source": "curated", "type": "plugin", "parent_plugin": "",
         "name": "product-analyzer", "count": 8, "distinct_users": 3, "error_count": 1},
        {"day": ANCHOR, "source": "flea", "type": "agent", "parent_plugin": "",
         "name": "data-bot", "count": 4, "distinct_users": 2, "error_count": 0},
    ])

    # installs (anchor day): 2 curated subscriptions + 1 flea install.
    _insert(repo, backend, "user_plugin_optouts",
            ["user_id", "marketplace_id", "plugin_name", "opted_out_at"], [
                {"user_id": "u1", "marketplace_id": "curated-product",
                 "plugin_name": "product-analyzer", "opted_out_at": _ts(ANCHOR)},
                {"user_id": "u2", "marketplace_id": "curated-product",
                 "plugin_name": "product-analyzer", "opted_out_at": _ts(ANCHOR)},
            ])
    _insert(repo, backend, "store_entities",
            ["id", "owner_user_id", "owner_username", "type", "name", "version",
             "title", "synthetic_name"], [
                {"id": "ent-1", "owner_user_id": "owner", "owner_username": "owner",
                 "type": "agent", "name": "data-bot", "version": "1.0.0",
                 "title": "Data Bot", "synthetic_name": "data-bot-by-owner"},
            ])
    _insert(repo, backend, "user_store_installs",
            ["user_id", "entity_id", "installed_at"], [
                {"user_id": "u1", "entity_id": "ent-1", "installed_at": _ts(ANCHOR)},
            ])


# ---------------------------------------------------------------------------
# the contract — identical outputs from both engines
# ---------------------------------------------------------------------------

_P_START, _P_END = _ts(ANCHOR), _ts(ANCHOR + timedelta(days=1))
_TREND_START = _ts(PREV)


def test_event_window(reports_repo):
    repo, _ = reports_repo
    assert repo.event_window(_P_START, _P_END) == {
        "invocations": 3, "active_users": 2, "errors": 1}


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
    assert by_source["curated"] == {
        "source": "curated", "invocations": 2, "distinct_users": 2, "error_count": 0}
    assert by_source["flea"] == {
        "source": "flea", "invocations": 1, "distinct_users": 1, "error_count": 1}


def test_items_window(reports_repo):
    repo, _ = reports_repo
    items = repo.items_window(ANCHOR, ANCHOR + timedelta(days=1))
    assert items[("curated", "plugin", "", "product-analyzer")] == {
        "invocations": 8, "distinct_users": 3, "error_count": 1}
    assert items[("flea", "agent", "", "data-bot")] == {
        "invocations": 4, "distinct_users": 2, "error_count": 0}


def test_install_counts(reports_repo):
    repo, _ = reports_repo
    assert repo.install_counts(_P_START, _P_END) == {"curated": 2, "flea": 1}


def test_installs_daily_buckets_by_utc_day(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_daily(_TREND_START, _P_END) == {ANCHOR: 3}


def test_installs_curated_detail(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_curated_detail(_P_START, _P_END) == [
        {"ref_id": "curated-product/product-analyzer",
         "name": "product-analyzer", "installs": 2}]


def test_installs_flea_detail(reports_repo):
    repo, _ = reports_repo
    assert repo.installs_flea_detail(_P_START, _P_END) == [
        {"entity_id": "ent-1", "name": "data-bot", "installs": 1}]
