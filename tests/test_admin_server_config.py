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

    def test_corrupt_overlay_refused_with_500_not_silently_overwritten(
        self, seeded_app, tmp_path, monkeypatch
    ):
        """A malformed overlay used to be silently replaced — that masked
        disk corruption / partial writes / hand-edits and dropped every
        previously-saved section on the next save. The endpoint must now
        refuse with 500 so the operator can investigate."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        # Plant a deliberately broken YAML — unclosed brace + tab indent —
        # so PyYAML raises rather than yielding a dict.
        overlay_path = state / "instance.yaml"
        overlay_path.write_text("instance: {name: 'good'\nauth:\n\tallowed_domain: bad")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New Name"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 500, resp.text
        assert "corrupt overlay" in resp.json()["detail"]
        # Critical: the corrupt file is NOT replaced. Operator gets a chance
        # to back it up.
        assert overlay_path.read_text().startswith("instance: {name: 'good'")

    def test_round_tripped_redacted_secret_does_not_corrupt_overlay(
        self, seeded_app, tmp_path, monkeypatch
    ):
        """Regression: GET redacts secret-keyed children in nested objects to
        ``"***"`` so the form never displays cleartext. The data_source
        section renders as a JSON textarea, so a no-op save would otherwise
        send the masked JSON back verbatim. Without scrub, the deep-merge
        would persist ``token_env: "***"`` on top of the real
        ``"KEBOOLA_STORAGE_TOKEN"``, silently breaking the next sync. The
        server-side scrub must drop the sentinel so the overlay value
        survives a round-trip."""
        import yaml as _yaml
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        # Plant a real overlay with the actual env-var name in token_env.
        (state / "instance.yaml").write_text(_yaml.dump({
            "data_source": {
                "type": "keboola",
                "keboola": {
                    "stack_url": "https://connection.keboola.com",
                    "token_env": "KEBOOLA_STORAGE_TOKEN",
                },
            },
        }))
        # Bust the in-process cache so the next read sees the planted overlay.
        import app.instance_config as ic
        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Simulate a UI round-trip: send back the redacted payload verbatim.
        # The sentinel matches the string `_mask` produces.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"keboola": {
                "stack_url": "https://connection.keboola.com",
                "token_env": "***",  # redaction sentinel — must not persist
            }}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        # Real env-var name must survive on disk.
        overlay = _yaml.safe_load((state / "instance.yaml").read_text())
        assert overlay["data_source"]["keboola"]["token_env"] == "KEBOOLA_STORAGE_TOKEN", \
            f"overlay corrupted by round-tripped sentinel: {overlay}"

    @pytest.mark.parametrize("private_url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/",
        "http://10.0.0.1/",
        "http://localhost/",
    ])
    def test_post_rejects_private_url_in_keboola_stack_url(
        self, seeded_app, tmp_path, monkeypatch, private_url
    ):
        """SSRF gate symmetric with /configure: data_source.keboola.stack_url
        must not point to private/loopback/link-local networks. Without this
        check, the editor would let an admin sneak the GCE/EC2 metadata
        endpoint or an internal service into the overlay and trigger SSRF on
        the next sync."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"keboola": {"stack_url": private_url}}}},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert "private or reserved network" in resp.json()["detail"] \
            or "could not resolve hostname" in resp.json()["detail"]
        # Overlay must not have been written.
        assert not (tmp_path / "state" / "instance.yaml").exists()


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

    def test_diff_records_dict_to_scalar_replacement(self):
        """Regression: when one side is a dict and the other is a non-None
        scalar, _diff_dicts used to recurse with the missing side coerced
        to {} — losing the scalar entirely. Now the shape change is
        recorded as a single replacement at the parent path."""
        from app.api.admin import _diff_dicts
        # dict → scalar
        diff = _diff_dicts(
            {"keboola": {"stack_url": "https://x", "token_env": "Y"}},
            {"keboola": "disabled"},
        )
        # The whole 'keboola' entry should appear as a single shape change,
        # not as per-field removals.
        keboola_rows = [d for d in diff if d["path"] == "keboola"]
        assert len(keboola_rows) == 1, f"missing dict→scalar row in {diff}"
        assert keboola_rows[0]["after"] == "disabled"

    def test_diff_records_scalar_to_dict_replacement(self):
        from app.api.admin import _diff_dicts
        diff = _diff_dicts(
            {"keboola": "disabled"},
            {"keboola": {"stack_url": "https://x"}},
        )
        keboola_rows = [d for d in diff if d["path"] == "keboola"]
        assert len(keboola_rows) == 1
        assert keboola_rows[0]["before"] == "disabled"
        assert keboola_rows[0]["after"] == {"stack_url": "https://x"}

    def test_diff_shape_change_redacts_secret_keyed_children(self):
        """Regression: when a section's shape flips between dict and scalar,
        the dict side may carry secret-keyed children (token_env →
        env-resolved cleartext). The shape-change branch used to log the
        raw dict verbatim because the parent key wasn't itself secret-named,
        so a wholesale replacement of `data_source.keboola: {...}` ->
        `data_source.keboola: "disabled"` would dump every cleartext token
        / password the dict contained into audit_log.params. The branch now
        runs `_redact` over the dict side so secret-keyed children are
        masked even when the parent isn't."""
        from app.api.admin import _diff_dicts
        # dict-with-secret-child → scalar
        diff = _diff_dicts(
            {"keboola": {"stack_url": "https://x", "token_env": "hunter2-cleartext"}},
            {"keboola": "disabled"},
        )
        row = next(d for d in diff if d["path"] == "keboola")
        assert row["before"]["stack_url"] == "https://x"  # non-secret stays
        assert row["before"]["token_env"] == "***"        # secret masked
        assert "hunter2-cleartext" not in str(row), \
            f"cleartext leaked into audit row: {row}"

        # scalar → dict-with-secret-child (symmetric)
        diff = _diff_dicts(
            {"email": "disabled"},
            {"email": {"smtp_host": "smtp.example.com", "smtp_password": "supersecret"}},
        )
        row = next(d for d in diff if d["path"] == "email")
        assert row["after"]["smtp_host"] == "smtp.example.com"
        assert row["after"]["smtp_password"] == "***"
        assert "supersecret" not in str(row)

    def test_overlay_does_not_resolve_env_var_placeholders(self, seeded_app, tmp_path, monkeypatch):
        """Regression: prior fix wrote every editable section from the merged
        (env-resolved) config to the overlay. Static `smtp_password: ${SMTP_PASSWORD}`
        would get persisted as the actual cleartext password — turning a
        config-leak into a disk-leak. Now we write only the sections the
        user explicitly patched, deep-merged onto whatever was already in
        the overlay; static env-var placeholders never touch the overlay
        unless the admin types a literal value to replace them."""
        import yaml as _yaml
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "instance.yaml").write_text(_yaml.dump({
            "instance": {"name": "Old"},
            "auth": {"allowed_domain": "example.com", "webapp_secret_key": "x"},
            "server": {"host": "1.2.3.4", "hostname": "example.com"},
            "email": {
                "smtp_host": "smtp.example.com",
                "smtp_password": "${SMTP_PASSWORD}",
            },
        }))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CONFIG_DIR", str(static_dir))
        monkeypatch.setenv("SMTP_PASSWORD", "hunter2-cleartext-secret")
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        from pathlib import Path as _Path
        import config.loader as _loader_mod
        monkeypatch.setattr(_loader_mod, "CONFIG_DIR", _Path(static_dir))
        from app.instance_config import reset_cache
        reset_cache()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Admin patches `instance.name` only — does NOT touch `email`.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        overlay_text = (tmp_path / "state" / "instance.yaml").read_text()
        # The cleartext secret value MUST NOT appear in the overlay because
        # the user didn't touch `email`. Only the patched section lands.
        assert "hunter2-cleartext-secret" not in overlay_text, \
            f"env-resolved secret leaked into overlay:\n{overlay_text}"
        # And the email section shouldn't be there at all (user didn't patch it).
        overlay = _yaml.safe_load(overlay_text)
        assert "email" not in overlay, \
            f"untouched section persisted to overlay: {overlay}"
        # And `instance` IS persisted with the user's value.
        assert overlay["instance"]["name"] == "New"

    def test_load_instance_config_still_returns_static_sections_after_editor_save(self, seeded_app, tmp_path, monkeypatch):
        """End-to-end regression for the load_instance_config × narrow-overlay
        bug: pre-fix, load_instance_config() returned the overlay verbatim
        when it existed (no merge). Combined with the editor's narrow-write
        strategy, the first save deleted static-only sections (datasets,
        corporate_memory, openmetadata) from every runtime get_value() /
        get_datasets() call. This test exercises the actual call path the
        rest of the app uses, not just the overlay's on-disk shape."""
        import yaml as _yaml
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        # Required fields per config.loader's strict validation, plus the
        # static-only sections we want to assert flow through after a save.
        (static_dir / "instance.yaml").write_text(_yaml.dump({
            "instance": {"name": "Old"},
            "auth": {"allowed_domain": "example.com", "webapp_secret_key": "x"},
            "server": {"host": "1.2.3.4", "hostname": "example.com"},
            "datasets": {"sales": {"primary_key": ["id"]}},
            "corporate_memory": {"enabled": True, "retention_days": 90},
        }))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CONFIG_DIR", str(static_dir))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        # config.loader caches CONFIG_DIR at module import time, so the
        # env-var override above doesn't propagate. Patch the module
        # attribute directly so the static base resolves to our fixture.
        from pathlib import Path as _Path
        import config.loader as _loader_mod
        monkeypatch.setattr(_loader_mod, "CONFIG_DIR", _Path(static_dir))

        from app.instance_config import reset_cache
        reset_cache()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        # Now use the SAME function the rest of the app uses.
        from app.instance_config import load_instance_config, get_value
        reset_cache()  # make sure we're reading fresh post-save
        cfg = load_instance_config()
        assert cfg.get("instance", {}).get("name") == "New", "edit didn't land"
        assert "corporate_memory" in cfg, \
            f"static-only section vanished after save: keys={list(cfg.keys())}"
        assert cfg["corporate_memory"]["enabled"] is True
        assert cfg["corporate_memory"]["retention_days"] == 90
        assert "datasets" in cfg
        assert cfg["datasets"]["sales"]["primary_key"] == ["id"]
        # And via get_value (the path admin.py / sync.py / catalog actually use):
        assert get_value("corporate_memory", "enabled") is True
        assert get_value("datasets", "sales", "primary_key") == ["id"]

    def test_overlay_does_not_shadow_static_non_editable_sections(self, seeded_app, tmp_path, monkeypatch):
        """Regression: writing the full merged config to the overlay would
        snapshot non-editable sections (e.g. `corporate_memory`, `datasets`)
        and silently shadow later updates to those sections in the static
        file. Overlay must persist ONLY the editable sections so the static
        file stays the source of truth for everything else."""
        import yaml as _yaml
        # Static config has both editable + non-editable sections.
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        static_path = static_dir / "instance.yaml"
        static_path.write_text(_yaml.dump({
            "instance": {"name": "Old"},
            "datasets": {"sales": {"primary_key": ["id"]}},
            "corporate_memory": {"enabled": True, "retention_days": 90},
        }))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CONFIG_DIR", str(static_dir))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Edit only an editable section.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        # Overlay should contain ONLY editable sections, not corporate_memory
        # or datasets — those stay in the static file.
        overlay = _yaml.safe_load((tmp_path / "state" / "instance.yaml").read_text())
        assert "instance" in overlay
        assert overlay["instance"]["name"] == "New"
        assert "corporate_memory" not in overlay, \
            f"non-editable section leaked into overlay: {overlay}"
        assert "datasets" not in overlay

    def test_secret_rotation_is_visible_in_audit_diff(self, seeded_app, tmp_path, monkeypatch):
        """Regression: pre-masking inputs to _diff_dicts collapses a
        secret-rotation (password A → password B) into 'no diff' because
        both sides redact to '***'. Compute diff on RAW values, redact
        per-row at emit time, so a rotation row IS recorded (with both
        sides masked but the path present)."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # First save: set initial secret.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"email": {"smtp_password": "OLD_secret_value"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        # Second save: rotate to a different secret.
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"email": {"smtp_password": "NEW_rotated_secret"}}},
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
        # The rotation MUST appear as a diff row even though both sides
        # are secrets — pre-fix would have produced an empty diff.
        pwd_diffs = [d for d in params["diff"] if d["path"] == "email.smtp_password"]
        assert len(pwd_diffs) == 1, f"rotation lost from audit diff: {params['diff']}"
        assert pwd_diffs[0]["before"] == "***"
        assert pwd_diffs[0]["after"] == "***"
        # Neither cleartext leaks.
        all_text = json.dumps(params)
        assert "OLD_secret_value" not in all_text
        assert "NEW_rotated_secret" not in all_text

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


# --- Phase J: BQ fields exposure in /admin/server-config ---------------------


class TestServerConfigBigQueryFields:
    """Phase J — billing_project, legacy_wrap_views, and
    max_bytes_per_materialize are surfaced in the UI/API so an operator can
    set them without SSH'ing to the VM. The first two were previously only
    addressable via direct YAML edits; max_bytes_per_materialize had no UI
    hint at all."""

    def test_get_surfaces_bq_fields_even_when_unset(self, seeded_app, tmp_path, monkeypatch):
        """GET response always includes the three BQ fields under
        data_source.bigquery so the UI's JSON-textarea rendering shows them
        as editable keys even when YAML omits them. Without this, the
        operator has no UI hint that the knobs exist."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        # Plant a minimal instance.yaml that has data_source.bigquery but
        # NONE of the three fields set.
        (state / "instance.yaml").write_text(yaml.dump({
            "data_source": {
                "type": "bigquery",
                "bigquery": {"project": "my-data-prj", "location": "US"},
            },
        }))
        import app.instance_config as ic
        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        bq = resp.json()["sections"]["data_source"]["bigquery"]
        assert "billing_project" in bq, f"billing_project missing from GET: {bq}"
        assert "legacy_wrap_views" in bq, f"legacy_wrap_views missing from GET: {bq}"
        assert "max_bytes_per_materialize" in bq, \
            f"max_bytes_per_materialize missing from GET: {bq}"

    def test_get_preserves_existing_bq_field_values(self, seeded_app, tmp_path, monkeypatch):
        """When the operator HAS set the fields, GET must surface their actual
        values, not the unset defaults."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(yaml.dump({
            "data_source": {
                "type": "bigquery",
                "bigquery": {
                    "project": "my-data-prj",
                    "billing_project": "my-billing-prj",
                    "legacy_wrap_views": True,
                    "max_bytes_per_materialize": 5368709120,  # 5 GiB
                },
            },
        }))
        import app.instance_config as ic
        ic._instance_config = None

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        bq = resp.json()["sections"]["data_source"]["bigquery"]
        assert bq["billing_project"] == "my-billing-prj"
        assert bq["legacy_wrap_views"] is True
        assert bq["max_bytes_per_materialize"] == 5368709120

    def test_post_persists_billing_project(self, seeded_app, tmp_path, monkeypatch):
        """POST through the existing section-patch flow persists
        data_source.bigquery.billing_project to the overlay."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"bigquery": {
                "billing_project": "my-billing-prj",
            }}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        # Round-trip: GET should now reflect it.
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        bq = resp.json()["sections"]["data_source"]["bigquery"]
        assert bq["billing_project"] == "my-billing-prj"

    def test_post_persists_legacy_wrap_views(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"bigquery": {
                "legacy_wrap_views": True,
            }}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        bq = resp.json()["sections"]["data_source"]["bigquery"]
        assert bq["legacy_wrap_views"] is True

    def test_post_persists_max_bytes_per_materialize(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"bigquery": {
                "max_bytes_per_materialize": 21474836480,  # 20 GiB
            }}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        bq = resp.json()["sections"]["data_source"]["bigquery"]
        assert bq["max_bytes_per_materialize"] == 21474836480

    def test_template_documents_three_new_fields(self, seeded_app):
        """The three BQ optional fields are now surfaced through the
        known-fields registry (GET /api/admin/server-config carries them
        in `known_fields.data_source.bigquery.fields`), not hardcoded into
        the template text. The renderer reads the registry at runtime and
        creates a structured form with hints for each leaf — so the test
        verifies operator-discoverability through the API channel rather
        than via static HTML inspection.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/server-config", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        bq_fields = resp.json()["known_fields"]["data_source"]["bigquery"]["fields"]
        assert "billing_project" in bq_fields, \
            "registry must expose billing_project as a known field"
        assert "legacy_wrap_views" in bq_fields, \
            "registry must expose legacy_wrap_views as a known field"
        assert "max_bytes_per_materialize" in bq_fields, \
            "registry must expose max_bytes_per_materialize as a known field"
        # Each field must carry a hint so the renderer can show operator-
        # facing help text — no anonymous knobs.
        for k in ("billing_project", "legacy_wrap_views", "max_bytes_per_materialize"):
            assert "hint" in bq_fields[k] and bq_fields[k]["hint"], \
                f"{k} must carry a non-empty hint"
