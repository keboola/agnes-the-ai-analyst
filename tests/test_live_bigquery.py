"""Live BigQuery tests — require real GCP credentials in environment variables.

Run with: pytest tests/test_live_bigquery.py -m live -v
Requires: BIGQUERY_PROJECT, GOOGLE_APPLICATION_CREDENTIALS environment variables.

All tests are read-only; no data is written or deleted.
"""

import os

import pytest

pytestmark = pytest.mark.live

BIGQUERY_PROJECT = os.environ.get("BIGQUERY_PROJECT", "")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")


@pytest.fixture(autouse=True)
def require_bigquery_env():
    """Skip all tests in this module if BigQuery credentials are missing."""
    if not BIGQUERY_PROJECT or not GOOGLE_APPLICATION_CREDENTIALS:
        pytest.skip(
            "BigQuery credentials not set. "
            "Export BIGQUERY_PROJECT and GOOGLE_APPLICATION_CREDENTIALS to run live tests."
        )


def test_simple_query():
    """BigQuery client can execute a trivial SELECT 1 query."""
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
    query_job = client.query("SELECT 1 as x")
    rows = list(query_job.result())
    assert len(rows) == 1
    assert rows[0]["x"] == 1
