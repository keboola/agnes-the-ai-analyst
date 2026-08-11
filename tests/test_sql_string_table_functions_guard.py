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
    # QUOTED function name. DuckDB resolves it the same way, but sqlglot models
    # the callee as an `exp.Identifier` rather than a `str`, so a guard that
    # tested `isinstance(name, str)` read it as "not one of ours" — and since
    # the statement parses, the text fallback never ran either. Quoting was a
    # one-character bypass. (Devin Review on #1264.)
    "select * from \"query\"('select * from customers')",
    "select * from `query`('select * from customers')",
    "select * from \"query_table\"('customers')",
    # Catalog-qualified call — the name is still the callee.
    "select * from system.main.query('select 1')",
    # The `_execute` siblings: same string-argument shape, and they WRITE.
    "select postgres_execute('db', 'drop table customers')",
    "select sqlite_execute('f.db', 'delete from t')",
    "select mysql_execute('db', 'update t set x = 1')",
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
    # A quoted IDENTIFIER that is not a call must still be fine — the guard
    # keys on the call, not on the quoting.
    'select * from "saved_queries" where id = 1',
    'select "query_id" from job_history',
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


def test_sqlglot_models_a_quoted_call_as_an_identifier():
    """Tripwire for the shape the bypass depended on.

    If sqlglot ever normalizes a quoted callee to a plain `str`, this test
    fails and `_anonymous_name` can be simplified — but until then the guard
    must unwrap the identifier, and this pins WHY.
    """
    import sqlglot
    from sqlglot import exp

    statement = sqlglot.parse("""select * from "query"('select 1')""", read="duckdb")[0]
    anonymous = list(statement.find_all(exp.Anonymous))
    assert anonymous, "quoted call is not an Anonymous node any more — revisit the guard"
    assert isinstance(anonymous[0].this, exp.Identifier), type(anonymous[0].this)

    from app.api.query import _anonymous_name

    assert _anonymous_name(anonymous[0]) == "query"


def test_unparseable_quoted_call_is_still_rejected():
    """The text fallback must see the same call the parser does."""
    with pytest.raises(HTTPException) as exc:
        _assert_select_only("""select * from "query"('select 1'""")
    assert exc.value.status_code == 400
