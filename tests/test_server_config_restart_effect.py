"""Tests for three defects found in the 2026-08 admin audit:

1. `restart_required` on ``POST /api/admin/server-config`` was a hardcoded
   ``True`` regardless of which section was saved, even though most
   sections are resolved live per request (`app/switches.py::Switch.effect`
   already tracks this per flag, and ``GET /api/admin/server-config``
   already surfaces it). The response must compute the effect from the
   sections actually touched and expose both an honest ``restart_required``
   and a per-section ``sections_effect`` map.
2. ``POST /api/admin/configure`` replaced the entire ``data_source`` block
   with ``{"type": ...}``, silently wiping sibling connection blocks
   (``snowflake``/``databricks``/other-source coordinates) an instance had
   already configured through ``/admin/server-config``.
3. ``POST /api/admin/keboola/test-connection`` probes only instance-level
   Keboola credentials, but its ``not_configured`` response reads as
   "Keboola is broken" even on an instance whose real Keboola projects live
   entirely in the ``source_connections`` registry. The response must name
   the layer it probed and, only when a registry row for a keboola
   connection actually exists, point the operator at the per-project card.
"""

from __future__ import annotations

import yaml


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Defect 1: honest restart_required + sections_effect --------------------


class TestServerConfigRestartRequiredHonesty:
    def test_data_source_only_patch_requires_a_restart_for_the_other_processes(self, seeded_app, tmp_path, monkeypatch):
        """`reset_cache()` makes a data_source save live in THIS process only.

        Under a role-split deployment (api/gateway/worker as separate
        processes) the scheduler and workers keep extracting against the
        pre-save connection coordinates until they are bounced, so the
        section's baseline is `restart` -- the same cross-process reasoning
        that classifies `telegram`. Reporting it as `live` also made the
        Snowflake wizard's own restart warning
        (`_sfRestartRequired` in admin_data_sources.html) unreachable.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"bigquery": {"project": "acme-prod"}}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restart_required"] is True, data
        assert data["sections_effect"] == {"data_source": "restart"}

    def test_instance_and_theme_patches_are_both_live(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "X"}, "theme": {"primary": "#123456"}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restart_required"] is False
        assert data["sections_effect"] == {"instance": "live", "theme": "live"}

    def test_chat_patch_requires_restart(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"chat": {"enabled": True}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restart_required"] is True
        assert data["sections_effect"] == {"chat": "restart"}

    def test_auth_patch_stays_restart_even_though_its_switches_are_live(self, seeded_app, tmp_path, monkeypatch):
        """auth.keboola.allow_token_header is itself effect="live" in the
        switch registry, but the section as a whole builds OAuth provider
        client objects once at startup -- a save must not under-claim just
        because the one touched leaf happens to belong to a live switch."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"auth": {"keboola": {"allow_token_header": True}}},
                "confirm_danger": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restart_required"] is True
        assert data["sections_effect"] == {"auth": "restart"}

    def test_mixed_sections_restart_required_true_if_any_section_needs_it(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"instance": {"name": "New Name"}, "chat": {"enabled": True}}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["restart_required"] is True
        assert data["sections_effect"] == {"instance": "live", "chat": "restart"}


# --- Defect 2: /api/admin/configure preserves data_source siblings ----------


class TestConfigurePreservesDataSourceSiblings:
    def test_configure_preserves_other_data_source_connections(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "data_source": {
                        "type": "keboola",
                        "keboola": {
                            "stack_url": "https://connection.example.com",
                            "token_env": "KEBOOLA_STORAGE_TOKEN",
                        },
                        "snowflake": {"account": "acme-account", "user": "svc_agnes"},
                        "databricks": {"host": "adb-123.azuredatabricks.net"},
                    }
                }
            )
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        written = yaml.safe_load((state / "instance.yaml").read_text())
        assert written["data_source"]["type"] == "local"
        # Sibling connection blocks this call never touches must survive.
        assert written["data_source"]["snowflake"] == {"account": "acme-account", "user": "svc_agnes"}
        assert written["data_source"]["databricks"] == {"host": "adb-123.azuredatabricks.net"}
        assert written["data_source"]["keboola"]["stack_url"] == "https://connection.example.com"


# --- Defect 3: keboola test-connection names the layer it probed -----------


class TestKeboolaTestConnectionScope:
    def test_scope_field_present_when_not_configured(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/keboola/test-connection", headers=_auth(token))
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["kind"] == "not_configured"
        assert detail["scope"] == "instance"

    def test_hint_stays_plain_when_no_registry_connections_exist(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/keboola/test-connection", headers=_auth(token))
        assert resp.status_code == 400, resp.text
        hint = resp.json()["detail"]["hint"]
        assert "/admin/data-sources" not in hint

    def test_hint_points_to_data_sources_card_when_a_keboola_registry_row_exists(
        self, seeded_app, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "test-scope-keboola",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = c.post("/api/admin/keboola/test-connection", headers=_auth(token))
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["kind"] == "not_configured"
        assert "/admin/data-sources" in detail["hint"]

    def test_missing_token_branch_also_extends_hint_when_registry_row_exists(self, seeded_app, tmp_path, monkeypatch):
        """Same as above but for the OTHER not_configured raise site (stack_url
        set, token missing) -- both early-return branches need the fix."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump({"data_source": {"keboola": {"stack_url": "https://connection.example.com"}}})
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        create_resp = c.post(
            "/api/admin/source-connections",
            json={
                "name": "test-scope-keboola-2",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection2.example.com"},
            },
            headers=_auth(token),
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = c.post("/api/admin/keboola/test-connection", headers=_auth(token))
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["kind"] == "not_configured"
        assert detail["scope"] == "instance"
        assert "/admin/data-sources" in detail["hint"]
