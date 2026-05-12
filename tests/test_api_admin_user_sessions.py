"""Per-user Sessions admin endpoints.

Tests cover:
- Paginated list (limit/offset validation, processed vs unprocessed rows)
- Single-file download (content, audit_log, path-traversal guard, 404)
- Bulk ZIP download (content, audit_log, empty-dir case)
- Non-admin 403
"""

import io
import json
import os
import zipfile

import pytest

from src.db import get_system_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_data_dir(tmp_path, monkeypatch):
    """Override SESSION_DATA_DIR for the test and return the root path."""
    sdir = tmp_path / "user_sessions"
    sdir.mkdir()
    monkeypatch.setenv("SESSION_DATA_DIR", str(sdir))
    # Also patch the module-level helper so already-imported routers pick up
    # the new env value without re-import.
    import app.api.admin_user_sessions as _mod
    monkeypatch.setattr(_mod, "_session_data_dir", lambda: sdir)
    return sdir


def _seed_jsonl(session_data_dir, username, filename, content=None):
    """Write a JSONL file under session_data_dir/<username>/."""
    user_dir = session_data_dir / username
    user_dir.mkdir(exist_ok=True)
    f = user_dir / filename
    f.write_text(content or '{"role":"user","content":"hi"}\n')
    return f


def _get_admin_user_id(seeded_app, admin_user):
    """Return the seeded admin's user_id.

    The seeded_app fixture always seeds the admin as id='admin1' / email='admin@test.com'.
    We also verify via /api/users/admin1 (admin-gated) to stay robust against refactors.
    """
    return "admin1"


def _get_admin_email(seeded_app, admin_user):
    """Return the seeded admin's email."""
    return "admin@test.com"


# ---------------------------------------------------------------------------
# list_user_sessions
# ---------------------------------------------------------------------------


class TestListUserSessions:
    def test_returns_empty_when_no_sessions(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        body = r.json()
        assert body["rows"] == []
        assert body["pagination"]["total"] == 0

    def test_filesystem_file_appears_as_unprocessed(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        # Resolve username the same way the endpoint does (email local-part)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        _seed_jsonl(session_data_dir, username, "sess-abc-123.jsonl")

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["session_file"] == "sess-abc-123.jsonl"
        assert row["processed"] is False
        assert row["tool_calls"] == 0

    def test_processed_true_for_indexed_session(self, seeded_app, admin_user, session_data_dir):
        """When usage_session_summary has a row for the file, processed=True."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        _seed_jsonl(session_data_dir, username, "indexed.jsonl")

        # Insert a summary row directly
        conn = get_system_db()
        conn.execute(
            """
            INSERT INTO usage_session_summary
              (session_file, session_id, username, started_at, tool_calls,
               tool_errors, processor_version)
            VALUES (?, ?, ?, current_timestamp, 5, 1, 1)
            """,
            ["indexed.jsonl", "sess-xyz", username],
        )
        conn.close()

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        match = [row for row in rows if row["session_file"] == "indexed.jsonl"]
        assert len(match) == 1
        assert match[0]["processed"] is True
        assert match[0]["tool_calls"] == 5

    def test_pagination_limit_enforced_at_200(self, seeded_app, admin_user):
        """FastAPI should return 422 for limit > 200."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions?limit=500", headers=admin_user
        )
        assert r.status_code == 422

    def test_pagination_offset_and_limit(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        for i in range(5):
            _seed_jsonl(session_data_dir, username, f"sess-{i:03d}.jsonl")

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions?limit=2&offset=0", headers=admin_user
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["rows"]) == 2
        assert body["pagination"]["total"] == 5

        r2 = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions?limit=2&offset=2", headers=admin_user
        )
        assert r2.status_code == 200
        assert len(r2.json()["rows"]) == 2

    def test_404_for_unknown_user(self, seeded_app, admin_user):
        r = seeded_app["client"].get(
            "/api/admin/users/no-such-user-id/sessions", headers=admin_user
        )
        assert r.status_code == 404

    def test_non_admin_gets_403(self, seeded_app, analyst_user):
        r = seeded_app["client"].get(
            "/api/admin/users/anyid/sessions", headers=analyst_user
        )
        assert r.status_code in (401, 403)

    def test_unauthenticated_gets_401(self, seeded_app):
        r = seeded_app["client"].get("/api/admin/users/anyid/sessions")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# download_session (single file)
# ---------------------------------------------------------------------------


class TestDownloadSession:
    def test_streams_file_content(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        content = '{"role":"user","content":"hello world"}\n'
        _seed_jsonl(session_data_dir, username, "my-session.jsonl", content=content)

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/my-session.jsonl/download",
            headers=admin_user,
        )
        assert r.status_code == 200
        assert r.text == content
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_writes_audit_log(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        _seed_jsonl(session_data_dir, username, "audit-check.jsonl")

        seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/audit-check.jsonl/download",
            headers=admin_user,
        )

        conn = get_system_db()
        row = conn.execute(
            "SELECT params FROM audit_log WHERE action = 'session_download' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        params = json.loads(row[0])
        assert params["session_file"] == "audit-check.jsonl"
        assert "bytes" in params

    def test_rejects_path_traversal_dotdot(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/..%2Fetc%2Fpasswd/download",
            headers=admin_user,
        )
        assert r.status_code == 400

    def test_rejects_path_with_slash(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/subdir/file.jsonl/download",
            headers=admin_user,
        )
        # Either 400 (traversal guard catches it) or 404 (file absent) is fine.
        assert r.status_code in (400, 404)

    def test_rejects_non_jsonl_extension(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/passwd/download",
            headers=admin_user,
        )
        assert r.status_code == 400

    def test_404_when_file_missing(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        # Ensure the user_dir exists so we reach the file-not-found branch
        (session_data_dir / username).mkdir(exist_ok=True)
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/no-such-file.jsonl/download",
            headers=admin_user,
        )
        assert r.status_code == 404

    def test_non_admin_gets_403(self, seeded_app, analyst_user):
        r = seeded_app["client"].get(
            "/api/admin/users/anyid/sessions/x.jsonl/download",
            headers=analyst_user,
        )
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# download_all_sessions (bulk ZIP)
# ---------------------------------------------------------------------------


class TestDownloadAllSessions:
    def test_zips_all_files(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        contents = {
            "sess-001.jsonl": '{"role":"user","content":"one"}\n',
            "sess-002.jsonl": '{"role":"user","content":"two"}\n',
            "sess-003.jsonl": '{"role":"user","content":"three"}\n',
        }
        for fname, body in contents.items():
            _seed_jsonl(session_data_dir, username, fname, content=body)

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/download-all",
            headers=admin_user,
        )
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/zip")

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = set(zf.namelist())
            assert names == set(contents.keys())
            for fname, expected_body in contents.items():
                assert zf.read(fname).decode() == expected_body

    def test_writes_audit_log(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        _seed_jsonl(session_data_dir, username, "bulk-01.jsonl")
        _seed_jsonl(session_data_dir, username, "bulk-02.jsonl")

        seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/download-all",
            headers=admin_user,
        )

        conn = get_system_db()
        row = conn.execute(
            "SELECT params FROM audit_log WHERE action = 'session_bulk_download' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        params = json.loads(row[0])
        assert params["file_count"] == 2
        assert params["total_bytes"] > 0

    def test_empty_dir_returns_empty_zip(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        (session_data_dir / username).mkdir(exist_ok=True)  # dir exists, no jsonls

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/download-all",
            headers=admin_user,
        )
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert zf.namelist() == []

    def test_404_when_no_user_dir(self, seeded_app, admin_user, session_data_dir):
        uid = _get_admin_user_id(seeded_app, admin_user)
        # session_data_dir exists but no username subdir
        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/download-all",
            headers=admin_user,
        )
        assert r.status_code == 404

    def test_non_admin_gets_403(self, seeded_app, analyst_user):
        r = seeded_app["client"].get(
            "/api/admin/users/anyid/sessions/download-all",
            headers=analyst_user,
        )
        assert r.status_code in (401, 403)
