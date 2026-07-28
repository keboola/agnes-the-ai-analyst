"""Verify /api/* responses carry X-Agnes-Latest-Version + X-Agnes-Min-Version."""

from fastapi.testclient import TestClient


def test_api_response_carries_version_headers():
    from app.main import app
    from app.version import APP_VERSION, MIN_COMPAT_CLI_VERSION
    client = TestClient(app)
    # /api/version is unauthenticated and cheap.
    resp = client.get("/api/version")
    assert resp.status_code == 200
    # Headers must equal the constants in app.version, not just be parseable.
    # When MIN_COMPAT_CLI_VERSION is deliberately bumped in a future PR, this
    # test is updated in the same PR — the review-discipline guardrail.
    assert resp.headers["X-Agnes-Latest-Version"] == APP_VERSION
    assert resp.headers["X-Agnes-Min-Version"] == MIN_COMPAT_CLI_VERSION
    # Day-one floor pin: drop or update this assertion when the floor moves.
    assert resp.headers["X-Agnes-Min-Version"] == "0.0.0"


def test_non_api_response_does_not_carry_version_headers():
    from app.main import app
    client = TestClient(app)
    # /cli/latest is under /cli, not /api — should NOT carry the headers.
    resp = client.get("/cli/latest")
    assert resp.status_code == 200
    assert "X-Agnes-Latest-Version" not in resp.headers
    assert "X-Agnes-Min-Version" not in resp.headers
