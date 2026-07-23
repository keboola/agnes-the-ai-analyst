"""Tests for upload API endpoints — sessions, artifacts, local-md."""

import gzip
import io
import os


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
        """Filenames with ../ are rejected outright by the strict filename regex."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        content = b'{"type": "message"}\n'
        resp = c.post(
            "/api/upload/sessions",
            files={"file": ("../../etc/passwd", io.BytesIO(content), "application/jsonl")},
            headers=_auth(token),
        )
        # Strict regex rejects any filename containing characters outside [A-Za-z0-9._-]
        assert resp.status_code == 400
        assert "filename" in resp.text.lower()

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


class TestSessionGzipCapability:
    def test_api_responses_advertise_session_gzip(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/health")
        caps = resp.headers.get("X-Agnes-Accepts", "")
        assert "session-gzip" in [t.strip() for t in caps.split(",")]


class TestSessionGzipUpload:
    def _post(self, seeded_app, name, body):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        return c.post(
            "/api/upload/sessions",
            files={"file": (name, io.BytesIO(body), "application/gzip")},
            headers=_auth(token),
        )

    def test_gzip_roundtrip_stores_decompressed_jsonl(self, seeded_app):
        content = b'{"type": "message", "text": "hello"}\n' * 50
        resp = self._post(seeded_app, "sess_gz1.jsonl.gz", gzip.compress(content))
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "sess_gz1.jsonl"  # .gz stripped
        assert data["size"] == len(content)  # decompressed size
        # Stored file is byte-identical plain JSONL (admin user id is "admin1").
        from app.utils import get_data_dir

        stored = get_data_dir() / "user_sessions" / "admin1" / "sess_gz1.jsonl"
        assert stored.read_bytes() == content

    def test_corrupt_gzip_rejected_400(self, seeded_app):
        resp = self._post(seeded_app, "sess_gz2.jsonl.gz", b"not gzip at all")
        assert resp.status_code == 400
        assert "invalid_gzip" in resp.text

    def test_truncated_gzip_rejected_400(self, seeded_app):
        full = gzip.compress(b'{"type": "message"}\n' * 100)
        resp = self._post(seeded_app, "sess_gz3.jsonl.gz", full[: len(full) // 2])
        assert resp.status_code == 400
        assert "invalid_gzip" in resp.text

    def test_zip_bomb_rejected_413(self, seeded_app):
        # ~55 MB of zeros compresses to ~55 KB — decompressed cap must fire.
        bomb = gzip.compress(b"\x00" * (55 * 1024 * 1024))
        assert len(bomb) < 1024 * 1024  # sanity: transfer size is tiny
        resp = self._post(seeded_app, "sess_gz4.jsonl.gz", bomb)
        assert resp.status_code == 413
        from app.utils import get_data_dir

        assert not (get_data_dir() / "user_sessions" / "admin1" / "sess_gz4.jsonl").exists()

    def test_gz_only_filename_rejected_400(self, seeded_app):
        resp = self._post(seeded_app, ".gz", gzip.compress(b"x"))
        assert resp.status_code == 400

    def test_plain_jsonl_path_unchanged(self, seeded_app):
        content = b'{"type": "message"}\n'
        resp = self._post(seeded_app, "sess_plain.jsonl", content)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "filename": "sess_plain.jsonl", "size": len(content)}

    def test_large_valid_roundtrip_exercises_drain_loop(self, seeded_app):
        # >1 MB decompressed forces the bounded drain loop to iterate several times.
        content = b'{"type": "message", "text": "x"}\n' * 60000  # ~2 MB
        assert len(content) > 1024 * 1024
        resp = self._post(seeded_app, "sess_big.jsonl.gz", gzip.compress(content))
        assert resp.status_code == 200
        assert resp.json()["size"] == len(content)
        from app.utils import get_data_dir

        stored = get_data_dir() / "user_sessions" / "admin1" / "sess_big.jsonl"
        assert stored.read_bytes() == content

    def test_raw_transfer_bound_rejects_413(self, seeded_app, monkeypatch):
        import app.api.upload as _upl

        # Shrink the cap so a small INCOMPRESSIBLE payload trips the raw-transfer
        # (compressed-bytes) bound BEFORE decompression — exercises the secondary guard.
        monkeypatch.setattr(_upl, "MAX_UPLOAD_SIZE", 4096)
        payload = gzip.compress(os.urandom(16384))  # incompressible → stays > 4096
        assert len(payload) > 4096
        resp = self._post(seeded_app, "sess_rawbig.jsonl.gz", payload)
        assert resp.status_code == 413
        assert "File too large" in resp.text  # the raw-bound message, not "Decompressed"

    def test_temp_file_cleaned_up_on_oversize(self, seeded_app, monkeypatch, tmp_path):
        import tempfile

        import app.api.upload as _upl

        monkeypatch.setattr(_upl, "MAX_UPLOAD_SIZE", 4096)
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        # Compressible payload whose DECOMPRESSED size exceeds the patched cap → 413 mid-stream.
        payload = gzip.compress(b"\x00" * (64 * 1024))
        resp = self._post(seeded_app, "sess_bombtmp.jsonl.gz", payload)
        assert resp.status_code == 413
        # The intermediate NamedTemporaryFile (suffix ".tmp") must have been unlinked.
        assert list(tmp_path.glob("*.tmp")) == []
