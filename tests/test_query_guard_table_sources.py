"""The /api/query name guards ask DuckDB which tables a query references.

Both guards used to scan the SQL text for every registered name as a word,
so a table named for a SQL keyword collided with ordinary syntax: a registry
row called `order` made every `ORDER BY` look like a reference to it.

They now ask the engine that will run the query (`json_serialize_sql`, which
parses without binding or executing) and fall back to the conservative text
scan only when DuckDB won't parse the SQL. The oracle matters as much for
what it does NOT miss: a third-party parser that disagrees with DuckDB about
a construct turns a deny check into a bypass, which is exactly what the
`(TABLE v)` cases below pin.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi import HTTPException

from app.api.query import (
    _enforce_non_admin_sql_rbac,
    _sql_referenced_names,
    _sql_text_references_name,
)

# ---------------------------------------------------------------------------
# _sql_referenced_names — DuckDB as the oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, expected",
    [
        # The reported bug: keyword syntax is not a table reference.
        ("select a from agnes_audit order by ts desc limit 2", {"agnes_audit"}),
        ("select a from t group by a limit 3", {"t"}),
        ("select count(*) filter (where a > 1) from issues", {"issues"}),
        ("select year(ts) from issues", {"issues"}),
        ("select cast(ts as date) from issues", {"issues"}),
        ("select list(x) from issues", {"issues"}),
        # Neither are literals or comments.
        ("select 'issues' as x from t -- issues\n/* issues */", {"t"}),
        # Real references, however spelled.
        ('select * from "order"', {"order"}),
        ("select * from issues i join other o on 1=1", {"issues", "other"}),
        ("select * from a, b", {"a", "b"}),
    ],
)
def test_oracle_reports_exactly_the_referenced_tables(sql, expected):
    assert _sql_referenced_names(sql) == expected


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("select * from main.orders", "orders"),
        ("select * from memory.main.orders", "orders"),
        ('select * from "a.b"', "a.b"),  # a name that really contains a dot
    ],
)
def test_oracle_reduces_a_qualified_reference_to_its_bare_name(sql, expected):
    """Callers compare unqualified identifiers — information_schema view
    names and registry ids, which `[a-z_][a-z0-9_]*` keeps dot-free — so the
    bare name is what has to come out. Catalog-qualified refs into un-granted
    catalogs are a separate layer's job."""
    assert _sql_referenced_names(sql) == {expected}


def test_oracle_covers_every_statement_of_a_multi_statement_string():
    """A reference hidden in a trailing statement must not be invisible."""
    assert _sql_referenced_names("select 1 from a; select 2 from secret_view") == {
        "a",
        "secret_view",
    }


@pytest.mark.parametrize(
    "sql",
    [
        "select * from (table secret_view) t",
        'select * from (table "secret_view") t',
        "with x as (table secret_view) select * from x",
        "select * from (select * from (table secret_view) a) b",
        "select * from (from secret_view) t",
        "select (select sentinel from secret_view) as v",
        "select * from other, lateral (select sentinel from secret_view) l",
    ],
)
def test_oracle_sees_the_constructs_a_third_party_parser_misreads(sql):
    """Each of these reads the view in DuckDB. sqlglot reports none of them —
    it models the `TABLE v` shorthand as a column named `table` — which is
    what made a parse-based deny check a bypass."""
    assert "secret_view" in (_sql_referenced_names(sql) or set())


@pytest.mark.parametrize(
    "sql",
    [
        "select * from (",  # malformed
        "select * from t where a = 'unclosed",  # unterminated literal
        "select * from (pivot orders on x using sum(y))",  # not serializable
        "select * from `proj.ds.tbl`",  # backtick BQ path — DuckDB rejects
        "select 1; insert into sentinel values (1)",  # non-SELECT statement
    ],
)
def test_oracle_returns_none_so_callers_fall_back(sql):
    assert _sql_referenced_names(sql) is None


def test_duckdb_still_reports_parse_failure_the_way_we_detect_it():
    """Tripwire on the contract `_sql_referenced_names` reads.

    It decides "DuckDB could not parse this" from the `error` field of the
    serialized document. `duckdb` is pinned open-ended, so an upgrade that
    changed that shape would make failures look like clean parses with no
    tables — a silent downgrade to "references nothing", which is the unsafe
    direction for a deny check. Fails loudly here instead.
    """
    import json as _json

    import duckdb as _duckdb

    conn = _duckdb.connect()
    for sql in ("select * from (", "select 1; insert into t values (1)"):
        document = _json.loads(conn.execute("select json_serialize_sql(?)", [sql]).fetchone()[0])
        assert document.get("error") is True, f"parse-failure shape changed for {sql!r}: {document}"
    ok = _json.loads(conn.execute("select json_serialize_sql(?)", ["select * from t"]).fetchone()[0])
    assert ok.get("error") is False and "statements" in ok, f"success shape changed: {ok}"


def test_oracle_parses_without_executing():
    """A hostile statement must not run by virtue of being parsed.

    The canary is created through the PARSE connection, so a statement that
    escaped into execution would land in this very database and move the
    count. A canary on any other connection makes the assertion
    unfalsifiable — the parser would have nowhere to write even if it did
    execute — which is why this reaches for the singleton.
    """
    from app.api.query import _parse_connection

    conn = _parse_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS parse_canary (x INTEGER)")
    try:
        for hostile in (
            "insert into parse_canary values (1)",
            "select 1; insert into parse_canary values (2)",
            "drop table parse_canary",
            "update parse_canary set x = 9",
        ):
            assert _sql_referenced_names(hostile) is None, hostile
        assert conn.execute("select count(*) from parse_canary").fetchone()[0] == 0, "a parsed statement wrote rows"
        still_there = conn.execute("select count(*) from duckdb_tables() where table_name = 'parse_canary'").fetchone()[
            0
        ]
        assert still_there == 1, "a parsed statement dropped the table"
    finally:
        conn.execute("DROP TABLE IF EXISTS parse_canary")


def test_oracle_survives_deeply_nested_sql():
    """A deep statement parses fine in DuckDB; the walk over its tree must not
    hit Python's recursion limit and silently downgrade to the fallback."""
    sql = "select * from t" + " union all select * from t" * 400
    assert _sql_referenced_names(sql) == {"t"}


# ---------------------------------------------------------------------------
# _sql_text_references_name — the fallback, still conservative
# ---------------------------------------------------------------------------


def test_fallback_ignores_the_clause_keyword_position():
    assert not _sql_text_references_name("select a from t order by ts desc", "order")
    assert not _sql_text_references_name("select a from t group by a", "group")


def test_fallback_still_matches_real_references():
    assert _sql_text_references_name('select * from "order" order by c', "order")
    assert _sql_text_references_name("select * from (table values) t", "values")


# ---------------------------------------------------------------------------
# The non-admin view denylist, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Fresh system.duckdb so ``table_registry_repo()`` resolves per-test."""
    data_dir = tmp_path / "agnes_data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("STATE_DIR", raising=False)

    from src.db import _ensure_schema, close_system_db, get_system_db

    close_system_db()
    conn = get_system_db()
    _ensure_schema(conn)
    yield conn
    close_system_db()


@pytest.fixture
def analytics_with_denied_views(registry_env):
    """Caller is granted `issues` only; `order` and `values` exist as views
    but are not in their stack."""
    from src.repositories import table_registry_repo

    for name in ("order", "values", "issues"):
        table_registry_repo().register(id=name, name=name, source_type="keboola", query_mode="local")
    analytics = duckdb.connect()
    analytics.execute('CREATE VIEW "order" AS SELECT 1 AS id')
    analytics.execute('CREATE VIEW "values" AS SELECT 1 AS id')
    analytics.execute("CREATE VIEW issues AS SELECT 1 AS issue_key")
    yield analytics
    analytics.close()


@pytest.mark.parametrize(
    "sql",
    [
        "select issue_key from issues order by issue_key desc limit 1",
        "select issue_key from issues limit 5",
        "select count(*) filter (where issue_key > 1) from issues",
        "select cast(issue_key as date) from issues",
    ],
)
def test_ordinary_syntax_is_not_a_reference_to_a_keyword_named_view(analytics_with_denied_views, sql):
    """The reported symptom, denylist half: ordinary SQL against a granted
    table must not 403 because an ungranted view is named for the keyword,
    type or function it uses."""
    _enforce_non_admin_sql_rbac(analytics_with_denied_views, sql, ["issues"])  # must not raise


def test_referencing_a_denied_keyword_named_view_still_403(analytics_with_denied_views):
    with pytest.raises(HTTPException) as exc:
        _enforce_non_admin_sql_rbac(analytics_with_denied_views, 'select * from "order"', ["issues"])
    assert exc.value.status_code == 403
    assert "order" in str(exc.value.detail)


@pytest.mark.parametrize(
    "sql",
    [
        "select * from (table values) t",
        'select * from (table "values") t',
        "with x as (table values) select * from x",
        "select * from (from values) t",
        "select * from values",
        "select issue_key from issues; select * from (table values) t",
    ],
)
def test_shorthand_constructs_cannot_bypass_the_denylist(analytics_with_denied_views, sql):
    """Every one of these reads the ungranted `values` view in DuckDB, and a
    parse-based denylist backed by a third-party parser would let them
    through. Each must 403."""
    with pytest.raises(HTTPException) as exc:
        _enforce_non_admin_sql_rbac(analytics_with_denied_views, sql, ["issues"])
    assert exc.value.status_code == 403


def test_unparseable_sql_falls_back_to_the_conservative_scan(analytics_with_denied_views):
    """DuckDB won't parse this, so the text scan decides — and it denies."""
    with pytest.raises(HTTPException) as exc:
        _enforce_non_admin_sql_rbac(analytics_with_denied_views, "select * from ( values", ["issues"])
    assert exc.value.status_code == 403


def test_admin_bypasses_the_denylist_entirely(analytics_with_denied_views):
    _enforce_non_admin_sql_rbac(analytics_with_denied_views, 'select * from "order"', None)  # must not raise
