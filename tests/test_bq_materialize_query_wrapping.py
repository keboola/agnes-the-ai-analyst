"""materialize_query must always wrap admin source_query in
bigquery_query('<billing>', '<admin>') so the COPY uses BQ jobs API,
which works for base tables AND views — Storage Read API does not."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from connectors.bigquery.extractor import (
    _wrap_admin_sql_for_jobs_api,
    _escape_sql_string_literal,
)


def test_wrap_simple_select():
    out = _wrap_admin_sql_for_jobs_api(
        billing_project="prj-billing",
        inner_sql="SELECT * FROM `ds.tbl`",
    )
    assert out == (
        "SELECT * FROM bigquery_query('prj-billing', "
        "'SELECT * FROM `ds.tbl`')"
    )


def test_escape_single_quotes_in_inner_sql():
    inner = "SELECT name FROM `ds.tbl` WHERE country = 'CZ'"
    escaped = _escape_sql_string_literal(inner)
    assert escaped == "SELECT name FROM `ds.tbl` WHERE country = ''CZ''"


def test_wrap_with_inner_quotes_round_trips():
    inner = "SELECT * FROM `ds.tbl` WHERE col = 'foo''bar'"
    out = _wrap_admin_sql_for_jobs_api("myproject", inner)
    # Outer string-literal envelope must double the inner single quotes
    # so DuckDB's parser sees a balanced literal.
    assert out.count("'") % 2 == 0
    # Round-trip: stripping the wrapper gives back the original inner exactly.
    prefix = "SELECT * FROM bigquery_query('myproject', '"
    assert out.startswith(prefix)
    assert out.endswith("')")
    middle = out[len(prefix):-2]
    # DuckDB string literal escape: '' → '. Reverse it.
    decoded = middle.replace("''", "'")
    assert decoded == inner


def test_billing_project_validates_format():
    with pytest.raises(ValueError, match="billing_project"):
        _wrap_admin_sql_for_jobs_api(
            billing_project="bad project'; DROP",
            inner_sql="SELECT 1",
        )
