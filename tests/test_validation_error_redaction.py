"""A 422 must not hand the rejected request body back to the caller.

FastAPI's default validation error carries the offending value in an ``input``
key. On the several admin endpoints whose body IS a credential, that echoes the
secret into the response — and from there into access logs, proxies and error
trackers.

Found live: `PUT /api/admin/source-connections/{id}/secret` sent with the wrong
field name (`secret` instead of `value`) answered 422 with a Keboola master
token verbatim in the body.
"""

from __future__ import annotations

import pytest

# Every admin endpoint whose request body is itself a credential. Each entry is
# (method, path template, wrong-shaped body carrying the secret).
SECRET = "12345-0000000-doNotEchoThisTokenBack"

SECRET_BEARING_ENDPOINTS = [
    ("put", "/api/admin/source-connections/{cid}/secret", {"secret": SECRET, "kind": "master"}),
    ("put", "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN", {"secret": SECRET}),
    ("put", "/api/admin/slack-secrets", {"secret": SECRET}),
]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("method,path,body", SECRET_BEARING_ENDPOINTS)
def test_validation_error_does_not_echo_the_secret(seeded_app, method, path, body):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    url = path.format(cid="does-not-exist")

    resp = getattr(c, method)(url, headers=_auth(token), json=body)

    # The point is the body, not the status: whatever the endpoint decides
    # (422 for the shape, 404 for the id, 403 for the gate), the submitted
    # credential must not come back.
    assert SECRET not in resp.text, f"{method.upper()} {url} echoed the submitted secret in a {resp.status_code}"


def test_validation_error_still_names_the_offending_field(seeded_app):
    """Redaction must not cost the caller the ability to fix the call — the
    wrong field name was the actual bug in the live case."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    resp = c.put(
        "/api/admin/source-connections/does-not-exist/secret",
        headers=_auth(token),
        json={"secret": SECRET, "kind": "master"},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any("value" in (e.get("loc") or []) for e in detail), detail
    assert all("input" not in e for e in detail), detail


def test_a_custom_validator_error_carries_no_ctx_back(seeded_app):
    """Devin Review: dropping `input` alone was still a guess.

    A Pydantic v2 error for a `value_error` also carries `ctx`, holding the
    validator's own exception — and ~37 validators in this app format the
    rejected value into their message (`got {v!r}`). One of those on a
    credential field would round-trip the secret through a key the drop-list
    never named. The echo is a keep-list now, so only `loc`/`msg`/`type`/`url`
    survive, whatever Pydantic adds next.
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    resp = c.post(
        "/api/admin/mcp-sources",
        headers=_auth(token),
        json={"name": "ctx-probe", "transport": SECRET, "url": "https://example.com/mcp"},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail, resp.text
    value_errors = [e for e in detail if e.get("type") == "value_error"]
    assert value_errors, f"expected a custom-validator error to exercise ctx: {detail}"
    for e in detail:
        assert set(e) <= {"loc", "msg", "type", "url"}, e
    assert SECRET not in resp.text
