"""Live Jira tests — require real Jira credentials in environment variables.

Run with: pytest tests/test_live_jira.py -m live -v
Requires: JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN environment variables.

All tests are read-only; no data is written or deleted.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.live

JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")


@pytest.fixture(autouse=True)
def require_jira_env():
    """Skip all tests in this module if Jira credentials are missing."""
    if not JIRA_DOMAIN or not JIRA_EMAIL or not JIRA_API_TOKEN:
        pytest.skip(
            "Jira credentials not set. "
            "Export JIRA_DOMAIN, JIRA_EMAIL, and JIRA_API_TOKEN to run live tests."
        )


def test_jira_myself():
    """Jira /rest/api/3/myself returns 200 with valid credentials."""
    url = f"https://{JIRA_DOMAIN}/rest/api/3/myself"
    resp = httpx.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=15)
    assert resp.status_code == 200
    data = resp.json()
    assert "accountId" in data or "emailAddress" in data
