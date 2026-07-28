"""J7 — Analyst upload journey tests.

Tests analyst file upload flows: session transcripts, artifacts,
and local markdown — verifying files are stored correctly.
"""

import io
import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestAnalystJourney:
    def test_upload_session_transcript(self, seeded_app):
        """Analyst can upload a session JSONL file and it is stored."""
        c = seeded_app["client"]
        analyst_h = _auth(seeded_app["analyst_token"])
        env = seeded_app["env"]

        content = b'{"role": "user", "content": "Show me total revenue"}\n{"role": "assistant", "content": "SELECT SUM(amount) FROM orders"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("session_20260101.jsonl", io.BytesIO(content), "application/jsonl")},
            headers=analyst_h,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["filename"] == "session_20260101.jsonl"
        assert body["size"] == len(content)

        # Verify file physically stored
        sessions_dir = env["data_dir"] / "user_sessions" / "analyst1"
        stored = sessions_dir / "session_20260101.jsonl"
        assert stored.exists()
        assert stored.read_bytes() == content

    def test_upload_artifact_html(self, seeded_app):
        """Analyst can upload an HTML artifact and it is stored."""
        c = seeded_app["client"]
        analyst_h = _auth(seeded_app["analyst_token"])
        env = seeded_app["env"]

        content = b"<html><body><h1>Revenue Report</h1></body></html>"
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("report.html", io.BytesIO(content), "text/html")},
            headers=analyst_h,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["filename"] == "report.html"

        # Verify on disk
        artifacts_dir = env["data_dir"] / "user_artifacts" / "analyst1"
        stored = artifacts_dir / "report.html"
        assert stored.exists()
        assert stored.read_bytes() == content

    def test_upload_artifact_png(self, seeded_app):
        """Analyst can upload a PNG chart file."""
        c = seeded_app["client"]
        analyst_h = _auth(seeded_app["analyst_token"])

        # Minimal valid-ish PNG header bytes
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("chart.png", io.BytesIO(fake_png), "image/png")},
            headers=analyst_h,
        )
        assert resp.status_code == 200
        assert resp.json()["filename"] == "chart.png"

    def test_upload_requires_auth(self, seeded_app):
        """Upload endpoints require authentication."""
        c = seeded_app["client"]

        for url in ["/api/upload/sessions", "/api/upload/artifacts"]:
            resp = c.post(
                url,
                files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
            )
            assert resp.status_code == 401, f"Expected 401 for {url}"

    def test_admin_can_also_upload(self, seeded_app):
        """Admin user can also upload files (not analyst-exclusive)."""
        c = seeded_app["client"]
        admin_h = _auth(seeded_app["admin_token"])
        env = seeded_app["env"]

        content = b"admin session data"
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("admin_session.jsonl", io.BytesIO(content), "application/jsonl")},
            headers=admin_h,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Stored under admin user dir
        admin_dir = env["data_dir"] / "user_sessions" / "admin1"
        assert (admin_dir / "admin_session.jsonl").exists()
