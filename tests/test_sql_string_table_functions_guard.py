"""Guard: table functions that take SQL as a *string* must not reach the engine.

`/api/query`'s non-admin RBAC (``_enforce_non_admin_sql_rbac``) decides access by
matching **view names in the SQL text**. DuckDB's ``query('<sql>')`` /
``query_table('<name>')`` family takes its target as a string literal, so the
table name can be assembled at runtime and never appears as a matchable token:

    select * from query('select * from ' || 'ungranted')

The name-based denylist has nothing to match, and the inner SQL executes against
the analytics catalog where every view exists. ``query_table`` and
``bigquery_query`` were already on the keyword denylist; plain ``query(`` was
not, and a keyword denylist cannot cover the concatenated form anyway.

Detection is therefore on the parsed tree (the function *name* is visible even
when its argument is computed), mirroring ``_has_file_table_source``.
"""

import pytest
from fastapi import HTTPException

from app.api.query import _assert_select_only


BLOCKED = [
    # Plain query() — the gap this guard closes.
    "select * from query('select * from customers')",
    # Concatenated target: the name never appears as a token, so no
    # text-matching denylist can see it.
    "select * from query('select * from cust' || 'omers')",
    "select * from query('select * from ' || chr(105) || 'nvoices')",
    # Already covered by the keyword denylist — pinned here so the AST guard
    # keeps covering them if that list is ever trimmed.
    "select * from query_table('customers')",
    "select * from bigquery_query('proj', 'select 1')",
    # Other engines' SQL-string table functions.
    "select * from postgres_query('db', 'select 1')",
    "select * from sqlite_query('f.db', 'select 1')",
    "select * from mysql_query('db', 'select 1')",
    # Nested out of FROM position — still executes.
    "select (select count(*) from query('select 1')) as c",
    # Upper/mixed case reaches the guard lowercased, but the AST path must not
    # depend on the caller having lowercased it.
    "select * from QUERY('select 1')".lower(),
]

ALLOWED = [
    "select * from customers",
    # A column or table whose *name* merely contains "query" must still work —
    # a substring denylist on "query" would break these.
    "select query_id, query_text from job_history",
    "select * from saved_queries where id = 1",
    # A string literal that happens to contain the function name is data, not a
    # call. This is precisely what an AST check gets right and a substring
    # check gets wrong.
    "select * from customers where note = 'query(1)'",
]


@pytest.mark.parametrize("sql", BLOCKED)
def test_sql_string_table_function_is_rejected(sql):
    with pytest.raises(HTTPException) as exc:
        _assert_select_only(sql.strip().lower())
    assert exc.value.status_code == 400


@pytest.mark.parametrize("sql", ALLOWED)
def test_ordinary_sql_still_passes(sql):
    _assert_select_only(sql.strip().lower())


def test_sqlglot_models_sql_string_table_function_as_anonymous():
    """Tripwire for the sqlglot behavioral dependency in
    ``_has_sql_string_table_function``: the guard finds these calls by function
    *name* on an ``exp.Anonymous`` node, which is what makes the concatenated
    form detectable at all. If a sqlglot upgrade models them differently, fail
    here rather than silently stop rejecting them."""
    import sqlglot
    from sqlglot import exp

    for sql, expected in (
        ("select * from query('select 1')", "query"),
        ("select * from query('a' || 'b')", "query"),
        ("select * from postgres_query('db', 'select 1')", "postgres_query"),
    ):
        names = [
            node.this.lower()
            for node in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Anonymous)
            if isinstance(node.this, str)
        ]
        assert expected in names, f"sqlglot no longer exposes {expected!r} as Anonymous for: {sql}"


def test_unparseable_sql_carrying_the_call_is_still_rejected():
    """The AST path needs a parse. When sqlglot cannot parse the statement the
    guard must fall back to text matching rather than fail open — the same
    shape ``_has_file_table_source`` uses."""
    with pytest.raises(HTTPException):
        _assert_select_only("select * from query('select 1') where )( garbage".strip().lower())
