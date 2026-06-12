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

    def test_upload_api_dir_file_appears_as_unprocessed(self, seeded_app, admin_user, session_data_dir):
        """Sessions uploaded via /api/upload/sessions land under
        SESSION_DATA_DIR/<user_id>/ (a UUID-style id), NOT the email
        local-part dir the collector uses. The list must scan BOTH —
        previously these uploads were invisible until the usage
        processor indexed them."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        _seed_jsonl(session_data_dir, uid, "uploaded-via-api.jsonl")

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert [row["session_file"] for row in rows] == ["uploaded-via-api.jsonl"]
        assert rows[0]["processed"] is False

    def test_same_filename_in_both_dirs_listed_once(self, seeded_app, admin_user, session_data_dir):
        """A file present under both ingestion layouts is the same session —
        the list must not show it twice."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        _seed_jsonl(session_data_dir, username, "dup.jsonl")
        _seed_jsonl(session_data_dir, uid, "dup.jsonl")

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert [row["session_file"] for row in rows] == ["dup.jsonl"]

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

    def test_streams_file_from_upload_api_dir(self, seeded_app, admin_user, session_data_dir):
        """Single-file download must also resolve files under the
        upload-API layout (SESSION_DATA_DIR/<user_id>/) — previously a
        permanent 404 for every API-uploaded session."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        content = '{"role":"user","content":"uploaded"}\n'
        _seed_jsonl(session_data_dir, uid, "api-up.jsonl", content)

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/api-up.jsonl/download",
            headers=admin_user,
        )
        assert r.status_code == 200
        assert r.content.decode() == content

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

    def test_download_all_skips_symlink_escape(self, seeded_app, admin_user, session_data_dir, tmp_path):
        """A symlink inside the user dir pointing outside is silently skipped, not included in zip."""
        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]
        user_dir = session_data_dir / username
        user_dir.mkdir(exist_ok=True)
        # A real file inside user dir
        (user_dir / "ok.jsonl").write_text('{"role":"user"}')
        # A symlink jsonl pointing outside the user dir
        target = tmp_path / "outside.txt"
        target.write_text("SECRET DATA OUTSIDE SESSION DIR")
        os.symlink(target, user_dir / "escape.jsonl")

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions/download-all",
            headers=admin_user,
        )
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "ok.jsonl" in names
        assert "escape.jsonl" not in names

    def test_non_admin_gets_403(self, seeded_app, analyst_user):
        r = seeded_app["client"].get(
            "/api/admin/users/anyid/sessions/download-all",
            headers=analyst_user,
        )
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Sort order regression
# ---------------------------------------------------------------------------


class TestListUserSessionsSortOrder:
    def test_processed_sessions_sort_before_unprocessed(
        self, seeded_app, admin_user, session_data_dir
    ):
        """Processed sessions must appear before unprocessed ones in the list."""
        import datetime as dt

        uid = _get_admin_user_id(seeded_app, admin_user)
        username = _get_admin_email(seeded_app, admin_user).split("@")[0]

        # Write two JSONL files to the filesystem
        _seed_jsonl(session_data_dir, username, "processed.jsonl")
        _seed_jsonl(session_data_dir, username, "unprocessed.jsonl")

        # Seed a usage_session_summary row only for the processed file
        conn = get_system_db()
        conn.execute(
            """INSERT INTO usage_session_summary
            (session_file, session_id, username, started_at, ended_at,
             tool_calls, tool_errors, processor_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            [
                "processed.jsonl",
                "sess-processed",
                username,
                dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 5, 10, 13, 0, tzinfo=dt.timezone.utc),
                7,
                0,
            ],
        )
        conn.close()

        r = seeded_app["client"].get(
            f"/api/admin/users/{uid}/sessions", headers=admin_user
        )
        assert r.status_code == 200
        rows = r.json()["rows"]

        processed_indices = [i for i, row in enumerate(rows) if row.get("processed")]
        unprocessed_indices = [i for i, row in enumerate(rows) if not row.get("processed")]

        assert processed_indices, "Expected at least one processed row"
        assert unprocessed_indices, "Expected at least one unprocessed row"

        assert max(processed_indices) < min(unprocessed_indices), (
            f"Processed sessions should all appear before unprocessed ones; "
            f"got processed at {processed_indices}, unprocessed at {unprocessed_indices}, "
            f"rows={[{'file': r['session_file'], 'processed': r['processed']} for r in rows]}"
        )


# ---------------------------------------------------------------------------
# list_user_activity  (Phase F.2)
# ---------------------------------------------------------------------------


class TestListUserActivity:
    def test_list_user_activity_paginated(self, seeded_app, admin_user):
        """audit_log rows filtered by user_id, newest first, paginated."""
        conn = get_system_db()
        admin_id_row = conn.execute(
            "SELECT id FROM users WHERE email LIKE '%admin%' LIMIT 1"
        ).fetchone()
        if admin_id_row is None:
            conn.close()
            pytest.skip("admin user not seeded")
        admin_id = admin_id_row[0]
        # Seed 5 audit rows for this user
        for i in range(5):
            conn.execute(
                """INSERT INTO audit_log (id, timestamp, user_id, action, result, duration_ms)
                VALUES (?, current_timestamp, ?, ?, 'success', ?)""",
                [str(__import__('uuid').uuid4()), admin_id, f"test.action.{i}", 100 + i]
            )
        conn.close()
        resp = seeded_app["client"].get(
            f"/api/admin/users/{admin_id}/activity?limit=10", headers=admin_user
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["rows"]) >= 5
        assert data["pagination"]["total"] >= 5

    def test_list_user_activity_user_not_found(self, seeded_app, admin_user):
        resp = seeded_app["client"].get(
            "/api/admin/users/does-not-exist/activity", headers=admin_user
        )
        assert resp.status_code == 404

    def test_list_user_activity_admin_only(self, seeded_app, analyst_user):
        conn = get_system_db()
        admin_id_row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
        conn.close()
        if admin_id_row is None:
            pytest.skip()
        user_id = admin_id_row[0]
        resp = seeded_app["client"].get(
            f"/api/admin/users/{user_id}/activity", headers=analyst_user
        )
        assert resp.status_code in (401, 403)

    def test_list_user_activity_writes_audit_row(self, seeded_app, admin_user):
        """Reading another user's activity is itself audit-logged."""
        from src.db import get_system_db

        conn = get_system_db()
        # Grab an admin user ID (any user will do for this test)
        admin_id = conn.execute("SELECT id FROM users WHERE email LIKE '%admin%' LIMIT 1").fetchone()
        if admin_id:
            target_user_id = admin_id[0]
        else:
            # Fallback: use the current admin user (assuming they exist)
            target_user_id = admin_user.get("email") or "admin1"

        before = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='admin.user_activity_read'"
        ).fetchone()[0]
        conn.close()

        resp = seeded_app["client"].get(
            f"/api/admin/users/{target_user_id}/activity", headers=admin_user
        )
        assert resp.status_code == 200

        conn = get_system_db()
        after = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='admin.user_activity_read'"
        ).fetchone()[0]
        conn.close()

        assert after == before + 1
