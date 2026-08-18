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


def _fetch_issue(key: str) -> dict:
    """The exact production fetch shape (``JiraService.fetch_issue`` / backfill)."""
    resp = httpx.get(
        f"https://{JIRA_DOMAIN}/rest/api/3/issue/{key}",
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        params={"expand": "renderedFields,changelog", "fields": "*all"},
        headers={"Accept": "application/json"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_jsd_public_rides_on_the_production_issue_payload():
    """``jsdPublic`` needs no ``expand`` and no second request — verified on the
    payloads production actually fetches.

    ``search/jql`` is used only to DISCOVER issue keys: its comment embed is
    serialized independently of ``GET /issue`` (a newest-20 window vs an
    oldest-first block of up to 100), so asserting on the search payload would
    not guard the production path. Each issue is refetched with the exact
    shape ``JiraService.fetch_issue`` and the backfill use, and one page of
    ``GET /issue/{key}/comment`` — the endpoint ``complete_issue_comments``
    pages when a thread exceeds the embed cap — is checked as well. If
    Atlassian ever stops embedding the flag where production reads it, this
    test is what says so. The type check is strict: a string-typed flag is as
    much a regression as an absent one, since only JSON booleans are trusted.
    """
    found = _search(
        f"project = {JIRA_PROJECT} ORDER BY created DESC",
        fields="comment",
        max_results=25,
    ).get("issues", [])
    keys = [i["key"] for i in found if ((i["fields"].get("comment") or {}).get("comments"))][:8]
    assert keys, "no issues with comments found"

    checked = 0
    problems: list[str] = []
    for key in keys:
        issue = _fetch_issue(key)
        for c in (issue["fields"].get("comment") or {}).get("comments") or []:
            checked += 1
            if not isinstance(c.get("jsdPublic"), bool):
                problems.append(f"{key}/{c.get('id')}: jsdPublic={c.get('jsdPublic')!r}")
    assert checked, "no comments on the refetched issues"
    assert not problems, f"{len(problems)}/{checked} comments lack a boolean jsdPublic: {problems[:10]}"

    resp = httpx.get(
        f"https://{JIRA_DOMAIN}/rest/api/3/issue/{keys[0]}/comment",
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        params={"startAt": 0, "maxResults": 100},
        headers={"Accept": "application/json"},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    paged = resp.json().get("comments") or []
    assert paged, f"no comments returned by the paged endpoint for {keys[0]}"
    bad = [c.get("id") for c in paged if not isinstance(c.get("jsdPublic"), bool)]
    assert not bad, f"paged-endpoint comments lack a boolean jsdPublic: {bad[:10]}"


def test_transform_matches_live_jira_across_years():
    """Every comment's transformed ``public_visibility`` equals live ``jsdPublic``.

    Spread across years so a regression that only bites older payloads — the
    exact shape of the defaulting bug this column was added to avoid — cannot
    hide behind recent data. Issues are discovered per year via search, then
    REFETCHED with the production fetch shape so the transform runs on the
    payload it sees in production. Two deliberate strictness choices: a
    non-boolean live flag counts as a mismatch (the transform must resolve it
    to NULL, never invert it via ``bool()``), and flag-less comments are
    counted and asserted absent rather than silently skipped — production
    would write NULL for those, which is exactly the regression to surface.
    """
    from connectors.jira.transform import transform_comments

    checked = 0
    flagless = 0
    mismatches: list[str] = []
    for year in range(2022, 2027):
        found = _search(
            f'project = {JIRA_PROJECT} AND created >= "{year}-01-01" '
            f'AND created <= "{year}-12-31" ORDER BY created ASC',
            fields="created",
            max_results=6,
        ).get("issues", [])
        for stub in found:
            issue = _fetch_issue(stub["key"])
            live = {
                str(c["id"]): c.get("jsdPublic") for c in ((issue["fields"].get("comment") or {}).get("comments") or [])
            }
            records = transform_comments(issue, preserve_on_incomplete=False) or []
            for record in records:
                expected = live.get(str(record["comment_id"]))
                if expected is None:
                    flagless += 1
                    continue
                checked += 1
                if not isinstance(expected, bool) or record["public_visibility"] is not expected:
                    mismatches.append(
                        f"{issue['key']}/{record['comment_id']} ({year}): "
                        f"stored={record['public_visibility']} jira={expected!r}"
                    )

    assert checked >= 10, f"only {checked} comments checked — sample too small to mean anything"
    assert not mismatches, f"{len(mismatches)}/{checked} mismatches: {mismatches[:10]}"
    assert flagless == 0, f"{flagless} comments carry no jsdPublic — production would write NULL for these"
