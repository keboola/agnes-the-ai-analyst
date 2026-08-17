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
        pytest.skip("Jira credentials not set. Export JIRA_DOMAIN, JIRA_EMAIL, and JIRA_API_TOKEN to run live tests.")


def test_jira_myself():
    """Jira /rest/api/3/myself returns 200 with valid credentials."""
    url = f"https://{JIRA_DOMAIN}/rest/api/3/myself"
    resp = httpx.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), timeout=15)
    assert resp.status_code == 200
    data = resp.json()
    assert "accountId" in data or "emailAddress" in data


JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "SUPPORT")


def _search(jql: str, fields: str, max_results: int) -> dict:
    resp = httpx.get(
        f"https://{JIRA_DOMAIN}/rest/api/3/search/jql",
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        params={"jql": jql, "fields": fields, "maxResults": max_results},
        headers={"Accept": "application/json"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_jsd_public_rides_on_the_plain_issue_payload():
    """``jsdPublic`` needs no ``expand`` and no second request.

    This is the assumption ``_comment_public_visibility`` is built on, and the
    reason the connector adds no per-issue API call for the column. If Atlassian
    ever stops embedding the flag, this test is what says so.
    """
    issues = _search(
        f"project = {JIRA_PROJECT} ORDER BY created DESC",
        fields="comment",
        max_results=25,
    ).get("issues", [])
    comments = [c for i in issues for c in ((i["fields"].get("comment") or {}).get("comments") or [])]
    assert comments, "no comments found to check"

    missing = [c["id"] for c in comments if "jsdPublic" not in c]
    assert not missing, f"{len(missing)}/{len(comments)} comments lack jsdPublic: {missing[:10]}"


def test_transform_matches_live_jira_across_years():
    """Every comment's transformed ``public_visibility`` equals live ``jsdPublic``.

    Spread across years so a regression that only bites older payloads — the
    exact shape of the defaulting bug this column was added to avoid — cannot
    hide behind recent data.
    """
    from connectors.jira.transform import transform_comments

    checked = 0
    mismatches: list[str] = []
    for year in range(2022, 2027):
        issues = _search(
            f'project = {JIRA_PROJECT} AND created >= "{year}-01-01" '
            f'AND created <= "{year}-12-31" ORDER BY created ASC',
            fields="comment,created",
            max_results=15,
        ).get("issues", [])
        for issue in issues:
            live = {
                str(c["id"]): c.get("jsdPublic") for c in ((issue["fields"].get("comment") or {}).get("comments") or [])
            }
            records = transform_comments(issue, preserve_on_incomplete=False) or []
            for record in records:
                expected = live.get(str(record["comment_id"]))
                if expected is None:
                    continue
                checked += 1
                if record["public_visibility"] is not bool(expected):
                    mismatches.append(
                        f"{issue['key']}/{record['comment_id']} ({year}): "
                        f"stored={record['public_visibility']} jira={expected}"
                    )

    assert checked >= 10, f"only {checked} comments checked — sample too small to mean anything"
    assert not mismatches, f"{len(mismatches)}/{checked} mismatches: {mismatches[:10]}"
