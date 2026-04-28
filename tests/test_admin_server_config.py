"""Tests for the /admin/server-config UI page and its companion API.

Covers:
- Auth gate on both the HTML page and GET/POST /api/admin/server-config.
- The page-shell markers the JS hangs off (so we don't break the contract
  by renaming an ID/data-attribute without updating the script).
- GET response shape: editable_sections + per-section dict + secrets
  redacted.
- POST request shape: section patch validates, danger-zone gate enforced,
  unknown sections rejected.
- Audit log entry: written on every save with sanitized diff (secret
  fields masked, non-secret diff present).
- instance.yaml write happens at DATA_DIR/state/instance.yaml.
"""

import json
import uuid
from pathlib import Path

import pytest
import yaml


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- HTML page ----------------------------------------------------------------


class TestServerConfigPageAuth:
    def test_admin_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # JWT in cookie so the auth-redirect middleware doesn't bounce us
        # to /login (the same pattern the existing admin-UI tests use).
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/server-config", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Page-shell markers the JS targets.
        assert 'data-page="server-config"' in body
        assert "Server configuration" in body
        # Form skeleton: loader + per-section container.
        assert 'id="cfg-loading"' in body
        assert 'id="cfg-sections"' in body
        # Restart-required messaging on the page (issue #91 acceptance).
        assert "restart" in body.lower()
        # Danger-zone modal scaffolding.
        assert 'id="danger-modal"' in body
        assert 'id="danger-confirm-btn"' in body
        # Endpoint constant — guards against URL drift between UI and API.
        assert '/api/admin/server-config' in body

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get(
                "/admin/server-config",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
        finally:
            c.cookies.clear()
        # require_role(Role.ADMIN) → 403 for non-admins.
        assert resp.status_code in (302, 401, 403), resp.text

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/admin/server-config",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303, 401, 403), resp.text


class TestServerConfigPageRendersSections:
    """The page is shell-only — sections are rendered client-side from the
    GET response. The static HTML still ships every section's title in
    SECTION_META so the JS can label them; verify the labels are present."""

    def test_page_includes_all_section_titles(self, seeded_app):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            resp = c.get("/admin/server-config")
        finally:
            c.cookies.clear()
        assert resp.status_code == 200
        body = resp.text
        # The eight editable sections appear in the JS SECTION_META map.
        for section in (
            "instance", "data_source", "email", "telegram",
            "jira", "theme", "server", "auth",
        ):
            assert f'{section}:' in body, f"section meta missing: {section}"

    def test_page_marks_danger_sections_in_js(self, seeded_app):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            resp = c.get("/admin/server-config")
        finally:
            c.cookies.clear()
        body = resp.text
        # auth + server are flagged danger client-side too.
        assert 'DANGER_SECTIONS = new Set(["auth", "server"])' in body


# --- GET /api/admin/server-config --------------------------------------------


class TestGetServerConfigAPI:
    def test_get_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config")
        assert resp.status_code == 401

    def test_get_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 403

    def test_get_returns_section_envelope(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "sections" in data
        assert "editable_sections" in data
        assert "danger_sections" in data
        assert isinstance(data["sections"], dict)
        # Every editable section is surfaced (empty dict when unset) so the
        # UI can render headers without hitting "section missing" branches.
        for section in data["editable_sections"]:
            assert section in data["sections"]
        # Danger zone is the documented subset.
        assert set(data["danger_sections"]) == {"auth", "server"}

    def test_get_redacts_secret_fields(self, seeded_app, monkeypatch, tmp_path):
        """Pre-populate instance.yaml with a secret; GET must not leak it."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(yaml.dump({
            "instance": {"name": "Acme Analyst"},
            "email": {"smtp_host": "smtp.example.com", "smtp_password": "supersecret"},
            "auth": {"google_client_secret": "oauth-secret-value", "allowed_domain": "example.com"},
            "telegram": {"bot_token": "telegram-token-value"},
        }))
        # Bust the in-process cache so the next GET re-reads the file.
        import app.instance_config as ic
        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        body = json.dumps(data)
        # Cleartext values for any secret-looking key must NOT appear.
        for leak in ("supersecret", "oauth-secret-value", "telegram-token-value"):
            assert leak not in body, f"redaction failed: '{leak}' leaked in {body}"
        # Non-secret fields pass through.
        assert data["sections"]["instance"]["name"] == "Acme Analyst"
        assert data["sections"]["email"]["smtp_host"] == "smtp.example.com"
        assert data["sections"]["auth"]["allowed_domain"] == "example.com"


# --- POST /api/admin/server-config -------------------------------------------


class TestPostServerConfigAPI:
    def test_post_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "X"}}},
        )
        assert resp.status_code == 401

    def test_post_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "X"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_post_empty_sections_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_post_unknown_section_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"thmee": {"primary": "#000"}}},  # typo
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "thmee" in resp.json()["detail"]

    def test_post_danger_section_without_confirmation_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"auth": {"allowed_domain": "evil.example.com"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "confirm_danger" in resp.json()["detail"]

    def test_post_danger_section_with_confirmation_accepted(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"auth": {"allowed_domain": "example.com"}},
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["restart_required"] is True
        assert "auth" in data["sections_updated"]

    def test_post_writes_instance_yaml(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New Name", "subtitle": "Sub"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        written = tmp_path / "state" / "instance.yaml"
        assert written.exists(), "instance.yaml should be written to DATA_DIR/state/"
        loaded = yaml.safe_load(written.read_text())
        assert loaded["instance"]["name"] == "New Name"
        assert loaded["instance"]["subtitle"] == "Sub"

    def test_post_deep_merges_existing_sections(self, seeded_app, tmp_path, monkeypatch):
        """Saving one section must not wipe other sections that already
        exist in the file. This is the regression that made us pick
        deep-merge over wholesale replace."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(yaml.dump({
            "instance": {"name": "Original", "subtitle": "Keep me"},
            "email": {"smtp_host": "smtp.example.com", "smtp_port": 587},
        }))

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Patch ONLY instance.name — subtitle and the entire email section
        # must survive.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "Renamed"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200

        loaded = yaml.safe_load((state / "instance.yaml").read_text())
        assert loaded["instance"]["name"] == "Renamed"
        assert loaded["instance"]["subtitle"] == "Keep me"
        assert loaded["email"]["smtp_host"] == "smtp.example.com"
        assert loaded["email"]["smtp_port"] == 587

    def test_post_section_must_be_object(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Pydantic accepts the request but our handler also validates the
        # inner type — sending a string for a section value is a 422.
        resp = c.post(
            "/api/admin/server-config",
            # Use a list — Pydantic Dict[str, Dict[str, Any]] coerces some
            # types but a non-mapping fails validation outright.
            json={"sections": {"instance": "not a dict"}},
            headers=_auth(token),
        )
        # Either 422 (Pydantic validation) or 422-equivalent — both valid.
        assert resp.status_code in (400, 422)


class TestPostServerConfigAuditLog:
    def test_save_writes_audit_entry_with_sanitized_diff(self, seeded_app, tmp_path, monkeypatch):
        """Each save = one audit_log row tagged instance_config.update with
        a diff that mentions changed fields by path but masks any secret
        values."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Apply a patch that changes both a non-secret field and a secret.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"email": {
                "smtp_host": "smtp.example.com",
                "smtp_password": "fresh-secret-value-1234",
            }}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        # Pull the audit row directly from the DB — bypasses the API
        # surface so we're verifying the actual persisted shape.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT user_id, action, resource, params FROM audit_log "
                "WHERE action = 'instance_config.update' ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
        finally:
            conn.close()

        assert rows, "audit_log should contain at least one instance_config.update row"
        user_id, action, resource, params_raw = rows[0]
        assert action == "instance_config.update"
        assert resource == "instance.yaml"
        assert user_id == "admin1"  # seeded admin
        params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
        assert params["sections"] == ["email"]
        assert isinstance(params["diff"], list)
        # Diff includes the non-secret change verbatim.
        smtp_diff = next((d for d in params["diff"] if d["path"] == "email.smtp_host"), None)
        assert smtp_diff is not None
        assert smtp_diff["after"] == "smtp.example.com"
        # Secret diff path is present but value is MASKED — cleartext must
        # never land in audit_log.
        all_text = json.dumps(params)
        assert "fresh-secret-value-1234" not in all_text, \
            f"secret leaked into audit log params: {all_text}"
        pwd_diff = next((d for d in params["diff"] if d["path"] == "email.smtp_password"), None)
        assert pwd_diff is not None
        assert pwd_diff["after"] == "***"

    def test_danger_save_records_danger_sections(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"server": {"hostname": "data.example.com"}},
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        from src.db import get_system_db
        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT params FROM audit_log "
                "WHERE action = 'instance_config.update' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        params = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        assert "server" in params["danger_sections"]


class TestRedactionHelpers:
    """Unit-level tests on the masking helpers — these run without the API
    so a regression in the redaction logic is easy to spot."""

    def test_is_secret_key_matches_common_patterns(self):
        from app.api.admin import _is_secret_key
        assert _is_secret_key("smtp_password")
        assert _is_secret_key("google_client_secret")
        assert _is_secret_key("api_token")
        assert _is_secret_key("WEBAPP_SECRET_KEY")
        assert _is_secret_key("anthropic_api_key")
        assert not _is_secret_key("smtp_host")
        assert not _is_secret_key("name")
        assert not _is_secret_key("allowed_domain")

    def test_redact_masks_nested_secrets(self):
        from app.api.admin import _redact
        before = {
            "email": {"smtp_host": "h", "smtp_password": "leak"},
            "auth": {"google_client_secret": "leak2", "allowed_domain": "example.com"},
        }
        after = _redact(before)
        assert after["email"]["smtp_host"] == "h"
        assert after["email"]["smtp_password"] == "***"
        assert after["auth"]["google_client_secret"] == "***"
        assert after["auth"]["allowed_domain"] == "example.com"

    def test_diff_dicts_flat_paths(self):
        from app.api.admin import _diff_dicts
        before = {"email": {"smtp_host": "old", "smtp_port": 587}}
        after = {"email": {"smtp_host": "new", "smtp_port": 587}}
        diff = _diff_dicts(before, after)
        assert diff == [{"path": "email.smtp_host", "before": "old", "after": "new"}]

    def test_deep_merge_preserves_other_sections(self):
        from app.api.admin import _deep_merge
        base = {"a": {"x": 1, "y": 2}, "b": {"z": 3}}
        patch = {"a": {"y": 99}}
        out = _deep_merge(base, patch)
        assert out == {"a": {"x": 1, "y": 99}, "b": {"z": 3}}
