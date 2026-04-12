"""Tests for /api/telegram/* endpoints — verify, unlink, status."""

import pytest


class TestTelegramStatus:
    """GET /api/telegram/status"""

    def test_status_unlinked(self, seeded_app):
        client = seeded_app["client"]
        headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}

        resp = client.get("/api/telegram/status", headers=headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["linked"] is False

    def test_status_requires_auth(self, seeded_app):
        resp = seeded_app["client"].get("/api/telegram/status")
        assert resp.status_code == 401


class TestTelegramVerify:
    """POST /api/telegram/verify"""

    def test_verify_invalid_code(self, seeded_app):
        client = seeded_app["client"]
        headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}

        resp = client.post(
            "/api/telegram/verify",
            json={"code": "000000"},
            headers=headers,
        )

        assert resp.status_code == 400
        assert "invalid" in resp.json()["detail"].lower() or "expired" in resp.json()["detail"].lower()

    def test_verify_requires_auth(self, seeded_app):
        resp = seeded_app["client"].post("/api/telegram/verify", json={"code": "123"})
        assert resp.status_code == 401

    def test_verify_missing_code(self, seeded_app):
        client = seeded_app["client"]
        headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}

        resp = client.post("/api/telegram/verify", json={}, headers=headers)

        assert resp.status_code == 422


class TestTelegramUnlink:
    """POST /api/telegram/unlink"""

    def test_unlink_when_not_linked(self, seeded_app):
        client = seeded_app["client"]
        headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}

        resp = client.post("/api/telegram/unlink", headers=headers)

        # Should succeed even if not linked (idempotent)
        assert resp.status_code == 200
        assert resp.json()["status"] == "unlinked"

    def test_unlink_requires_auth(self, seeded_app):
        resp = seeded_app["client"].post("/api/telegram/unlink")
        assert resp.status_code == 401
