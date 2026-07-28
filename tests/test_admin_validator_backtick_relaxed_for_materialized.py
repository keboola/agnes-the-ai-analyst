"""Backtick-quoted identifiers are required for materialized BQ source_query
(when the dataset/table/project name contains a dash). The validator must
allow them on materialized rows but still reject on remote/local."""
from __future__ import annotations
import pytest
from pydantic import ValidationError

from app.api.admin import RegisterTableRequest


def test_materialized_accepts_backticks():
    req = RegisterTableRequest(
        name="b1",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT * FROM `my-project.ds.tbl`",
    )
    assert req.source_query == "SELECT * FROM `my-project.ds.tbl`"


def test_remote_rejects_backticks():
    with pytest.raises(ValidationError):
        RegisterTableRequest(
            name="r1",
            source_type="bigquery",
            query_mode="remote",
            bucket="ds", source_table="tbl",
            source_query="SELECT * FROM `prj.ds.tbl`",
        )


def test_local_rejects_backticks():
    with pytest.raises(ValidationError):
        RegisterTableRequest(
            name="l1",
            source_type="keboola",
            query_mode="local",
            source_query="SELECT * FROM `kbc.ds.tbl`",
        )
