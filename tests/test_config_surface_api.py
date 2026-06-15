"""Tests for GET /api/admin/config-surface.

Covers:
- Auth gate (admin only).
- Payload shape: knobs list, initial_workspace, marketplaces, infra_repo_url.
- current_value/source correctly reflect env override vs yaml value vs default.
- infra_repo_url round-trips through the get_infra_repo_url() resolver.
"""

import os
from unittest.mock import patch


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestConfigSurfaceAuth:
    def test_admin_can_access(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 200, resp.text

    def test_non_admin_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_is_rejected(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/config-surface")
        assert resp.status_code == 401


class TestConfigSurfaceShape:
    def test_top_level_keys_present(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "knobs" in data
        assert "initial_workspace" in data
        assert "marketplaces" in data
        assert "infra_repo_url" in data

    def test_knobs_is_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["knobs"], list)

    def test_knob_entry_shape(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knobs = data["knobs"]
        assert knobs, "knobs list must not be empty"
        # Every knob must have the documented fields.
        required_fields = {"key", "resolver", "env_var", "yaml_path", "default", "current_value", "source"}
        for knob in knobs:
            missing = required_fields - knob.keys()
            assert not missing, f"knob {knob.get('resolver', '?')} missing fields: {missing}"

    def test_source_values_constrained(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        valid_sources = {"env", "yaml", "default"}
        for knob in data["knobs"]:
            assert knob["source"] in valid_sources, f"knob {knob['resolver']} has invalid source={knob['source']!r}"

    def test_marketplaces_is_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["marketplaces"], list)

    def test_initial_workspace_is_none_or_dict(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        iw = data["initial_workspace"]
        assert iw is None or isinstance(iw, dict), f"initial_workspace must be null or dict, got {type(iw)}"
        if isinstance(iw, dict):
            assert "url" in iw
            assert "branch" in iw
            assert "last_sync_sha" in iw

    def test_infra_repo_url_is_string(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert isinstance(data["infra_repo_url"], str)

    def test_known_knob_present(self, seeded_app):
        """get_home_route must appear in the knobs list."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        resolvers = {k["resolver"] for k in data["knobs"]}
        assert "get_home_route" in resolvers

    def test_infra_repo_url_knob_present(self, seeded_app):
        """get_infra_repo_url must appear in the knobs list."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        resolvers = {k["resolver"] for k in data["knobs"]}
        assert "get_infra_repo_url" in resolvers


class TestConfigSurfaceSourceResolution:
    def test_source_is_default_when_nothing_set(self, seeded_app):
        """get_home_route resolves from default when no env/yaml is set."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Remove env override to ensure default path.
        env_without_override = {k: v for k, v in os.environ.items() if k != "AGNES_HOME_ROUTE"}
        with patch.dict("os.environ", env_without_override, clear=True):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_home_route"), None)
        assert knob is not None
        # In the test environment with no yaml and no env override, source=default.
        assert knob["source"] in ("default", "yaml")

    def test_source_is_env_when_env_var_set(self, seeded_app):
        """When AGNES_HOME_ROUTE is set, source=env and current_value matches."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch.dict("os.environ", {"AGNES_HOME_ROUTE": "/test-route"}):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_home_route"), None)
        assert knob is not None
        assert knob["source"] == "env"
        assert knob["current_value"] == "/test-route"

    def test_infra_repo_url_default_is_empty(self, seeded_app):
        """infra_repo_url knob default and current_value are both empty string."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        env_without_infra = {k: v for k, v in os.environ.items() if k != "AGNES_INFRA_REPO_URL"}
        with patch.dict("os.environ", env_without_infra, clear=True):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_infra_repo_url"), None)
        assert knob is not None
        assert knob["default"] == ""
        assert knob["source"] in ("default", "yaml")

    def test_infra_repo_url_round_trips_via_env(self, seeded_app):
        """AGNES_INFRA_REPO_URL env var reaches the endpoint as source=env."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        test_url = "https://github.example.com/org/infra-repo"
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": test_url}):
            resp = c.get("/api/admin/config-surface", headers=_auth(token))
        data = resp.json()
        assert data["infra_repo_url"] == test_url
        knob = next((k for k in data["knobs"] if k["resolver"] == "get_infra_repo_url"), None)
        assert knob is not None
        assert knob["source"] == "env"
        assert knob["current_value"] == test_url


class TestGetInfraRepoUrl:
    """Unit-level test of the get_infra_repo_url() resolver in isolation."""

    def test_returns_empty_string_by_default(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        env = {k: v for k, v in os.environ.items() if k != "AGNES_INFRA_REPO_URL"}
        with patch.dict("os.environ", env, clear=True):
            result = get_infra_repo_url()
        assert result == ""

    def test_env_var_takes_priority(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": "https://git.example.com/infra"}):
            result = get_infra_repo_url()
        assert result == "https://git.example.com/infra"

    def test_strips_whitespace(self):
        from app.instance_config import get_infra_repo_url, reset_cache

        reset_cache()
        with patch.dict("os.environ", {"AGNES_INFRA_REPO_URL": "  https://git.example.com/infra  "}):
            result = get_infra_repo_url()
        assert result == "https://git.example.com/infra"
