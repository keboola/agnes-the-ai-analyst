"""Server-side tests for the browser-loopback CLI login (/cli/auth/*)."""

from urllib.parse import parse_qs, urlparse

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCliAuthStart:
    def test_unauthenticated_redirects_to_login(self, seeded_app):
        client = seeded_app["client"]
        r = client.get(
            "/cli/auth/start",
            params={"port": 54321, "state": "x" * 24},
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("/login?next=")
        # The loopback params survive the round-trip through sign-in.
        assert "cli%2Fauth%2Fstart" in loc or "cli/auth/start" in loc

    def test_authenticated_renders_confirm_page(self, seeded_app):
        client = seeded_app["client"]
        r = client.get(
            "/cli/auth/start",
            params={"port": 54321, "state": "s" * 24},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert "Authorize" in r.text

    @pytest.mark.parametrize("port,state", [
        (80, "s" * 24),          # privileged port
        (54321, "short"),        # state too short
        (999999, "s" * 24),      # port out of range
    ])
    def test_validation_rejects_bad_params(self, seeded_app, port, state):
        client = seeded_app["client"]
        r = client.get(
            "/cli/auth/start",
            params={"port": port, "state": state},
            headers=_auth(seeded_app["admin_token"]),
            follow_redirects=False,
        )
        assert r.status_code == 400


def _confirm_and_get_code(client, token, *, port=54321, state="s" * 24) -> str:
    """Drive POST /cli/auth/start and pull the code out of the loopback URL."""
    r = client.post(
        "/cli/auth/start",
        data={"port": port, "state": state},
        headers=_auth(token),
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    parsed = urlparse(loc)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == port
    assert parsed.path == "/callback"
    q = parse_qs(parsed.query)
    assert q["state"][0] == state
    return q["code"][0]


class TestCliAuthExchange:
    def test_full_happy_path_mints_pat(self, seeded_app):
        from app.auth.jwt import verify_token

        client = seeded_app["client"]
        code = _confirm_and_get_code(client, seeded_app["admin_token"])

        r = client.post("/cli/auth/exchange", json={"code": code, "name": "laptop"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["email"] == "admin@test.com"
        payload = verify_token(body["token"])
        assert payload is not None
        assert payload["typ"] == "pat"
        assert payload["email"] == "admin@test.com"
        assert payload.get("scope") == "cli-login"

    def test_code_is_single_use(self, seeded_app):
        client = seeded_app["client"]
        code = _confirm_and_get_code(client, seeded_app["admin_token"])

        first = client.post("/cli/auth/exchange", json={"code": code})
        assert first.status_code == 200
        second = client.post("/cli/auth/exchange", json={"code": code})
        assert second.status_code == 400

    def test_unknown_code_rejected(self, seeded_app):
        client = seeded_app["client"]
        r = client.post("/cli/auth/exchange", json={"code": "nope-not-a-real-code"})
        assert r.status_code == 400

    def test_confirm_requires_session(self, seeded_app):
        """POST /cli/auth/start with no auth must not mint a code."""
        client = seeded_app["client"]
        r = client.post(
            "/cli/auth/start",
            data={"port": 54321, "state": "s" * 24},
            follow_redirects=False,
        )
        assert r.status_code in (401, 403)
