"""Live Keboola tests — require real credentials in environment variables.

Run with: pytest tests/test_live_keboola.py -m live -v
Requires: KBC_STORAGE_TOKEN, KBC_STACK_URL environment variables.

All tests are read-only; no data is written or deleted.
"""

import os

import pytest

pytestmark = pytest.mark.live

KBC_STORAGE_TOKEN = os.environ.get("KBC_STORAGE_TOKEN", "")
KBC_STACK_URL = os.environ.get("KBC_STACK_URL", "")


@pytest.fixture(autouse=True)
def require_keboola_env():
    """Skip all tests in this module if Keboola credentials are missing."""
    if not KBC_STORAGE_TOKEN or not KBC_STACK_URL:
        pytest.skip(
            "Keboola credentials not set. "
            "Export KBC_STORAGE_TOKEN and KBC_STACK_URL to run live tests."
        )


def test_connection():
    """KeboolaClient.test_connection() returns True with valid credentials."""
    from connectors.keboola.client import KeboolaClient

    client = KeboolaClient(token=KBC_STORAGE_TOKEN, url=KBC_STACK_URL)
    assert client.test_connection() is True


def test_discover_tables():
    """KeboolaClient.discover_all_tables() returns a non-empty list of tables."""
    from connectors.keboola.client import KeboolaClient

    client = KeboolaClient(token=KBC_STORAGE_TOKEN, url=KBC_STACK_URL)
    tables = client.discover_all_tables()
    assert isinstance(tables, list)
    assert len(tables) > 0, "Expected at least one table in the Keboola project"
    # Verify structure of first table entry
    first = tables[0]
    assert "id" in first
    assert "name" in first
