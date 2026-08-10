"""Table-name extraction for audit resource tagging (`_first_table_from_sql`).

The parsed name is not just an audit curiosity: it is the group-by key of the
query-telemetry top-tables ranking, so an identifier that is not a table shows
up in a user-facing dashboard as if it were real usage.

Each failure class below was observed in a production audit log before being
turned into a test.
"""

import pytest

from app.api.query import _first_table_from_sql


class TestPlainTableReferences:
    """The cases that already worked must keep working."""

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT a FROM orders", "orders"),
            ("select a from orders", "orders"),
            ("SELECT a FROM orders WHERE x = 1", "orders"),
            ("SELECT a FROM my_schema.orders", "my_schema.orders"),
            ('SELECT a FROM "orders"', "orders"),
            ("SELECT a FROM `orders`", "orders"),
            ("SELECT a FROM orders o JOIN items i ON o.id = i.order_id", "orders"),
            ("SELECT a FROM orders\nWHERE x = 1", "orders"),
        ],
    )
    def test_extracts_the_table(self, sql, expected):
        assert _first_table_from_sql(sql) == expected

    def test_join_when_no_from(self):
        assert _first_table_from_sql("UPDATE x JOIN orders ON 1=1") == "orders"

    def test_returns_none_without_a_table_reference(self):
        assert _first_table_from_sql("SELECT 1") is None

    def test_returns_none_for_empty_sql(self):
        assert _first_table_from_sql("") is None


class TestQualifiedPathsAreKeptWhole:
    """A qualified path is recorded in full, dashes and all.

    Two prior bugs, one after the other. First the character class excluded
    `-`, so a backticked BigQuery FQN terminated at the first dash and 105
    queries in a 30-day window were tagged `prj`. Then the path was collapsed
    to its tail segment, which merged physically different tables that share a
    name and mis-reported `registered` in both directions (Devin Review on
    #1121, thread "Fully-qualified paths collapse to the bare table name").
    Resolving a path to a registry id is the aggregation's job, and it needs
    the path to still be there.
    """

    def test_backticked_bq_fqn_keeps_project_and_dataset(self):
        sql = "SELECT a FROM `my-project-123.analytics.orders` WHERE x = 1"
        assert _first_table_from_sql(sql) == "my-project-123.analytics.orders"

    def test_unquoted_dashed_project_is_not_truncated(self):
        sql = "SELECT a FROM my-project-123.analytics.orders"
        assert _first_table_from_sql(sql) == "my-project-123.analytics.orders"

    def test_mixed_quoting_path_keeps_every_segment(self):
        """Observed as `bq.` — the quote terminated the match immediately."""
        sql = 'SELECT a FROM bq."finance"."ledger" WHERE x = 1'
        assert _first_table_from_sql(sql) == "bq.finance.ledger"

    def test_two_part_qualified_name_is_preserved(self):
        assert _first_table_from_sql("SELECT a FROM analytics.orders") == "analytics.orders"

    def test_same_table_name_in_two_projects_stays_distinct(self):
        """The whole point: these are different tables and must not share a
        telemetry row."""
        a = _first_table_from_sql("SELECT 1 FROM `proj_a.ds1.orders`")
        b = _first_table_from_sql("SELECT 1 FROM `proj_b.ds2.orders`")
        assert a == "proj_a.ds1.orders"
        assert b == "proj_b.ds2.orders"
        assert a != b


class TestExtractDoesNotLeakColumnNames:
    """`EXTRACT(<part> FROM <column>)` puts a column right after FROM.

    Observed: 37 queries tagged with column names (`event_date`,
    `order_created_ts`, `operational_view_date`, ...) or with the cast
    functions `DATE` / `TIMESTAMP`.
    """

    def test_extract_does_not_shadow_the_real_table(self):
        sql = "SELECT EXTRACT(YEAR FROM event_date) AS yr FROM orders"
        assert _first_table_from_sql(sql) == "orders"

    def test_extract_over_a_qualified_column(self):
        sql = "SELECT EXTRACT(MONTH FROM ue.view_date) AS mo FROM revenue ue"
        assert _first_table_from_sql(sql) == "revenue"

    def test_extract_wrapping_a_cast_function(self):
        """Observed as `DATE` and `TIMESTAMP`."""
        sql = "SELECT EXTRACT(YEAR FROM DATE(created_at)) AS yr FROM orders"
        assert _first_table_from_sql(sql) == "orders"

    def test_substring_from_does_not_shadow_the_table(self):
        sql = "SELECT SUBSTRING(name FROM 2) AS n FROM customers"
        assert _first_table_from_sql(sql) == "customers"

    def test_trim_from_does_not_shadow_the_table(self):
        sql = "SELECT TRIM(LEADING '0' FROM code) AS c FROM products"
        assert _first_table_from_sql(sql) == "products"

    def test_extract_only_query_has_no_table(self):
        assert _first_table_from_sql("SELECT EXTRACT(YEAR FROM event_date)") is None


class TestTableValuedFunctionsAreNotTables:
    """`FROM UNNEST([...])` is an inline literal, not a table.

    Observed: 46 queries tagged `UNNEST`.
    """

    def test_unnest_is_skipped_in_favour_of_the_real_table(self):
        sql = "WITH codes AS (SELECT c FROM UNNEST([STRUCT('A' AS c)])) SELECT * FROM orders JOIN codes USING (c)"
        assert _first_table_from_sql(sql) == "orders"

    def test_unnest_only_query_has_no_table(self):
        sql = "SELECT c FROM UNNEST([STRUCT('A' AS c)])"
        assert _first_table_from_sql(sql) is None

    def test_generate_series_is_not_a_table(self):
        assert _first_table_from_sql("SELECT * FROM generate_series(1, 10)") is None


class TestOutputContract:
    """Callers write the result into `resource=table:<id>`, capped at 256 chars."""

    def test_result_is_length_capped(self):
        long_name = "a" * 500
        out = _first_table_from_sql(f"SELECT 1 FROM {long_name}")
        assert out is not None
        assert len(out) <= 200

    def test_no_surrounding_quotes_survive(self):
        for sql in ['SELECT a FROM "orders"', "SELECT a FROM `orders`"]:
            assert _first_table_from_sql(sql) == "orders"


def test_paren_inside_a_string_literal_does_not_hide_the_table():
    """Walking left counting brackets used to read a `(` inside a quoted
    value as a function-argument list, dropping the genuine FROM and
    recording the query as untargeted (Devin Review on #1121)."""
    from app.api.query import _first_table_from_sql

    assert _first_table_from_sql("SELECT 'extract(' AS tag, x FROM orders") == "orders"
    assert _first_table_from_sql("SELECT 'a(b(c' AS t FROM sales JOIN x ON 1=1") == "sales"
    # ...and the real EXTRACT(... FROM col) case still skips the column.
    assert _first_table_from_sql("SELECT EXTRACT(YEAR FROM ts) FROM events") == "events"


def test_from_inside_a_comment_or_literal_is_not_a_match():
    from app.api.query import _first_table_from_sql

    assert _first_table_from_sql("SELECT 1 -- FROM commented_out\nFROM real_table") == "real_table"
    assert _first_table_from_sql("SELECT /* FROM blocked */ 1 FROM real_table") == "real_table"
    assert _first_table_from_sql("SELECT 'FROM literal_table' AS s FROM real_table") == "real_table"


def test_quoted_identifier_containing_a_quote_char_is_not_treated_as_a_literal():
    from app.api.query import _first_table_from_sql

    assert _first_table_from_sql('SELECT * FROM "odd\'name"') == "odd'name"


def test_labelling_a_long_query_stays_linear():
    """The per-match left-walk plus tail slice made cost grow with the square
    of the query length, so one oversized query could tie up a worker."""
    import time

    from app.api.query import _first_table_from_sql

    def elapsed(joins: int) -> float:
        """Best of N. A shared CI runner descheduling this thread, or a GC
        pause landing inside one timed call, only ever ADDS time — so the
        minimum is the closest estimate of the work itself, and taking it on
        both sides keeps the ratio meaningful. A single sample of each is
        what made this flaky: shard 6 once measured small=1.4ms against
        large=912ms — a 656x ratio on 8x the input, which reads as
        catastrophically superlinear but was one stalled call. Locally the
        same input scales 0.5 / 1.1 / 2.2 / 4.6 / 9.2 ms across 200 -> 3200
        joins, i.e. exactly linear.
        """
        sql = "SELECT * FROM t0 " + " ".join(f"JOIN t{i} ON t{i}.a = t0.a" for i in range(1, joins))
        best = float("inf")
        for _ in range(5):
            start = time.perf_counter()
            _first_table_from_sql(sql)
            best = min(best, time.perf_counter() - start)
        return best

    small = elapsed(400)
    large = elapsed(3200)  # 8x the input
    # Linear is ~8x, quadratic ~64x — 20x sits between them, so the RATIO is
    # the real check. Measured here: linear 8.3x, a quadratic stand-in 57x.
    #
    # The floor is the escape hatch for timer noise, so it has to sit ABOVE
    # honest cost to do anything at all, and BELOW quadratic cost to not
    # hide the bug. Measured: linear ~9ms, quadratic ~59ms, so 25ms is
    # inside that window. It used to be 0.5s — above both, which silently
    # disabled the entire test: the ratio was never reached and a quadratic
    # regression could have landed green.
    #
    # The input sizes moved up (200/1600 -> 400/3200) for the same reason
    # the samples are best-of-N: a `small` of ~1ms is far enough above
    # timer resolution that the ratio means something, and the linear/
    # quadratic gap is wide enough to survive a loaded runner without
    # either masking the bug or inventing one.
    assert large < max(small * 20, 0.025), f"{small=} {large=}"


def test_quoted_remote_catalog_path_keeps_its_table():
    """`/api/query` executes DuckDB SQL even for BigQuery — remote access is
    the ATTACH-catalog form `bq."dataset"."table"` — so double quotes are
    identifiers on every path. Blanking them as BigQuery literals tagged the
    row `table:bq` instead (Devin Review on #1121)."""
    from app.api.query import _first_table_from_sql

    assert _first_table_from_sql('SELECT * FROM bq."finance"."ledger"') == "bq.finance.ledger"
    assert _first_table_from_sql('SELECT * FROM "orders"') == "orders"


def test_tagged_id_is_lowercased_to_match_registry_ids():
    """Registry ids are lowercased at registration; a tag that preserved the
    SQL's casing never matched and grouped as its own row."""
    from app.api.query import _first_table_from_sql

    assert _first_table_from_sql("select * from Orders") == "orders"
    assert _first_table_from_sql("SELECT * FROM MySchema.MyTable") == "myschema.mytable"
