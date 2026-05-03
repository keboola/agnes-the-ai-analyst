"""BQ_PATH regex used by /api/query to detect direct `bq.X.Y` references in
user SQL. The regex powers both the cost guardrail (matches contribute
dry-run targets) and the RBAC patch (matches must point at a registered row).

Adversarial cases verified empirically before commit; see #160 spec §4.3.1.
"""
import pytest


@pytest.fixture
def regex():
    from app.api.query import BQ_PATH
    return BQ_PATH


# --- positive: must match (3-part `bq.<dataset>.<source_table>`) -------------

@pytest.mark.parametrize("sql,expected_groups", [
    # Fully quoted
    ('SELECT * FROM bq."finance"."ue"', ('"finance"', '"ue"')),
    # Unquoted
    ('SELECT * FROM bq.finance.ue', ('finance', 'ue')),
    # Mixed quoting
    ('SELECT * FROM bq."finance".ue', ('"finance"', 'ue')),
    ('SELECT * FROM bq.finance."ue"', ('finance', '"ue"')),
    # Case-insensitive
    ('select * from BQ.Finance.UE', ('Finance', 'UE')),
    # With WHERE
    ('SELECT a FROM bq.ds.tbl WHERE x=1', ('ds', 'tbl')),
    # Inside CTE body
    ('WITH x AS (SELECT * FROM bq.ds.tbl) SELECT * FROM x', ('ds', 'tbl')),
    # Trailing punctuation
    ('SELECT * FROM bq.ds.tbl;', ('ds', 'tbl')),
])
def test_regex_matches_direct_bq_paths(regex, sql, expected_groups):
    matches = list(regex.finditer(sql))
    assert len(matches) >= 1, f"expected at least one match in {sql!r}"
    assert matches[0].groups() == expected_groups


def test_regex_finds_multiple_paths_in_one_statement(regex):
    """Two-path query: SELECT FROM bq.a.b JOIN bq.c.d — must match both."""
    sql = "SELECT * FROM bq.ds.tbl, bq.ds2.tbl2"
    matches = list(regex.finditer(sql))
    assert len(matches) == 2
    assert matches[0].groups() == ('ds', 'tbl')
    assert matches[1].groups() == ('ds2', 'tbl2')


# --- negative: must NOT match -------------------------------------------------

@pytest.mark.parametrize("sql,reason", [
    ('SELECT * FROM unit_economics', 'bare registered name (no bq prefix)'),
    ('SELECT * FROM "unit_economics"', 'quoted bare name'),
    ('SELECT bq.col FROM tbl', '2-part bq.col is column qualifier, not catalog'),
    ('SELECT count(*) FROM unit_economics', 'aggregate on bare name'),
    ('SELECT * FROM other_bq.ds.tbl', 'prefix that contains bq'),
    ('SELECT * FROM x.bq.ds.tbl', 'bq is middle component, not catalog'),
])
def test_regex_rejects_non_bq_paths(regex, sql, reason):
    matches = list(regex.finditer(sql))
    assert matches == [], \
        f"regex should not match {sql!r} ({reason}); matched {[m.group(0) for m in matches]}"


# --- accepted false-positives (documented in §4.3.1) -------------------------

def test_regex_matches_string_literal_containing_bq_path(regex):
    """Known false-positive: `WHERE c = 'bq.foo.bar'` matches. Cost guardrail
    runs a wasted dry-run (cheap), RBAC fires 403 if the path isn't
    registered — strict-deny is the right choice on a security boundary."""
    sql = "SELECT * FROM x WHERE c = 'bq.foo.bar'"
    matches = list(regex.finditer(sql))
    assert len(matches) == 1
    assert matches[0].groups() == ('foo', 'bar')
