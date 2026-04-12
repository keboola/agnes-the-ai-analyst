"""Tests for upload API endpoints — sessions, artifacts, local-md."""

import io
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestUploadSessions:
    def test_upload_session_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = b'{"type": "message", "text": "hello"}\n{"type": "message", "text": "world"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("session.jsonl", io.BytesIO(content), "application/jsonl")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["filename"] == "session.jsonl"
        assert data["size"] == len(content)

    def test_upload_session_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        content = b'{"type": "message"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("session.jsonl", io.BytesIO(content), "application/jsonl")},
        )
        assert resp.status_code == 401

    def test_upload_session_directory_traversal_rejected(self, seeded_app):
        """Filenames with ../ should be sanitized — only the basename is kept."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = b'{"type": "message"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("../../etc/passwd", io.BytesIO(content), "application/jsonl")},
            headers=_auth(token),
        )
        # The upload should succeed, but the path traversal should be stripped
        assert resp.status_code == 200
        data = resp.json()
        # filename must be just the basename — no slashes, no traversal
        assert "/" not in data["filename"]
        assert data["filename"] in ("passwd", "etc")

    def test_upload_session_empty_content(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("empty.jsonl", io.BytesIO(b""), "application/jsonl")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["size"] == 0

    def test_upload_session_analyst_allowed(self, seeded_app):
        """Analyst users can also upload sessions."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        content = b'{"type": "message"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("analyst_session.jsonl", io.BytesIO(content), "application/jsonl")},
            headers=_auth(token),
        )
        assert resp.status_code == 200


class TestUploadArtifacts:
    def test_upload_html_artifact(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = b"<html><body>Report</body></html>"
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("report.html", io.BytesIO(content), "text/html")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["filename"] == "report.html"
        assert data["size"] == len(content)

    def test_upload_png_artifact(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Minimal valid PNG header
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("chart.png", io.BytesIO(content), "image/png")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["filename"] == "chart.png"

    def test_upload_artifact_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        content = b"<html><body>Report</body></html>"
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("report.html", io.BytesIO(content), "text/html")},
        )
        assert resp.status_code == 401

    def test_upload_artifact_directory_traversal(self, seeded_app):
        """Path traversal in artifact filenames should be sanitized."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = b"<html></html>"
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("../../../tmp/evil.html", io.BytesIO(content), "text/html")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        # Must not contain slashes
        assert "/" not in resp.json()["filename"]

    def test_upload_artifact_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("empty.html", io.BytesIO(b""), "text/html")},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["size"] == 0


class TestUploadLocalMd:
    def test_upload_local_md_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = "# My Notes\n\nSome corporate memory content."
        resp = c.post(
            "/api/upload/local-md",
            json={"content": content},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["user"] == "admin@test.com"
        assert data["size"] == len(content)

    def test_upload_local_md_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/upload/local-md",
            json={"content": "some content"},
        )
        assert resp.status_code == 401

    def test_upload_local_md_missing_content_field(self, seeded_app):
        """Missing content field should return 422."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/upload/local-md",
            json={},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_upload_local_md_analyst(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/upload/local-md",
            json={"content": "analyst notes"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == "analyst@test.com"
