from src.semantic.dialect import resolve_expression


def _expr(*pairs):
    return {"dialects": [{"dialect": d, "expression": e} for d, e in pairs]}


def test_duckdb_dialect_wins_when_present():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("DUCKDB", "sum(a)")))
    assert (sql, reason) == ("sum(a)", None)


def test_ansi_sql_is_the_fallback():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("SNOWFLAKE", "SUM(a)")))
    assert (sql, reason) == ("SUM(a)", None)


def test_warehouse_only_expression_is_unusable_not_spliced():
    sql, reason = resolve_expression(_expr(("SNOWFLAKE", "TRY_CAST(a AS NUMBER)")))
    assert sql is None
    assert "SNOWFLAKE" in reason


def test_empty_expression_is_unusable():
    sql, reason = resolve_expression({"dialects": []})
    assert sql is None
    assert reason


def test_dialect_entry_without_a_name_is_ignored_not_a_crash():
    sql, reason = resolve_expression({"dialects": [{"dialect": None, "expression": "SUM(a)"}]})
    assert sql is None
    assert reason
