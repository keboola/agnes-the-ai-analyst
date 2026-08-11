"""Deriving a Keboola MCP source from a registered source connection.

The launch-command assertions are regression tests for a silent failure found
by running the real thing: without ``--prerelease=allow`` uv resolves
``keboola-mcp-server`` backwards to 1.32.0 (its newest release with no
pre-release dependency) and the agent gets a working server with a quietly
truncated toolset — 33 tools instead of 37, no semantic-layer tools.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.keboola_chat_tools import (
    KEBOOLA_MCP_VERSION,
    build_stdio_spec,
    derived_source_id,
    runner_args,
)

BASE = "/api/admin/source-connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestSpecBuilder:
    def test_runner_args_allow_prereleases(self):
        # Without this flag uv silently resolves an ancient release.
        assert "--prerelease=allow" in runner_args()

    def test_runner_args_pin_the_version(self):
        args = runner_args()
        assert f"keboola-mcp-server=={KEBOOLA_MCP_VERSION}" in args
        # ...and the pin must be what --from receives, not a stray argv entry.
        assert args[args.index("--from") + 1] == f"keboola-mcp-server=={KEBOOLA_MCP_VERSION}"

    def test_spec_is_stdio_with_token_via_env_not_argv(self):
        spec = build_stdio_spec(
            connection_id="c1",
            connection_name="Demo",
            stack_url="https://connection.example.com/",
            version="9.9.9",
        )
        assert spec["transport"] == "stdio"
        assert spec["auth_secret_env"] == "KBC_STORAGE_TOKEN"
        # The token must never appear on argv — only its env-var *name* does.
        assert not any("token" in str(a).lower() for a in spec["args"])
        assert spec["env"]["KBC_STORAGE_API_URL"] == "https://connection.example.com"

    def test_derived_id_is_stable_for_a_connection(self):
        assert derived_source_id("abc") == derived_source_id("abc")
        assert derived_source_id("abc") != derived_source_id("abd")


class TestChatToolsEndpoint:
    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _create_keboola(self, c, token, *, name, with_secret=True):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        if with_secret:
            assert (
                c.put(
                    f"{BASE}/{conn_id}/secret",
                    json={"value": "kbc-token-value"},
                    headers=_auth(token),
                ).status_code
                == 204
            )
        return conn_id

    def test_enable_creates_derived_mcp_source(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-enable")

        resp = c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))
        assert resp.status_code == 201, resp.text

        from src.repositories import mcp_sources_repo

        row = mcp_sources_repo().get(derived_source_id(conn_id))
        assert row is not None
        assert row["transport"] == "stdio"
        assert row["command"] == "uv"

    def test_enable_copies_the_connection_token_into_the_mcp_vault(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-secret")

        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        assert shared_secrets_repo().get(derived_source_id(conn_id)) == "kbc-token-value"

    def test_enable_without_a_token_fails_closed(self, seeded_app):
        """A source that would connect anonymously is worse than no source:
        every tool call fails at the far end with an opaque upstream error."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-notoken", with_secret=False)

        resp = c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))
        assert resp.status_code == 400
        assert "token" in resp.text.lower()

        from src.repositories import mcp_sources_repo

        assert mcp_sources_repo().get(derived_source_id(conn_id)) is None

    def test_enable_is_idempotent_and_resyncs_the_token(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-idem")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        # Rotate the connection's token, then re-enable.
        assert (
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "rotated-token"},
                headers=_auth(token),
            ).status_code
            == 204
        )
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        assert shared_secrets_repo().get(derived_source_id(conn_id)) == "rotated-token"

    def test_enable_rejects_a_non_keboola_connection(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "kbc-chat-bq",
                "source_type": "bigquery",
                "config": {"project": "some-project"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        resp2 = c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))
        assert resp2.status_code == 400

    def test_enable_missing_connection_returns_404(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        assert c.post(f"{BASE}/nope/chat-tools", headers=_auth(token)).status_code == 404

    def test_disable_removes_source_and_secret(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-disable")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        assert mcp_sources_repo().get(derived_source_id(conn_id)) is None
        assert shared_secrets_repo().get(derived_source_id(conn_id)) is None

    def test_deleting_the_connection_removes_its_chat_tools(self, seeded_app):
        """Otherwise the derived source outlives the connection, still holding
        a live Keboola token and still offering the project to the agent."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-cascade")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        assert c.delete(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 204

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        assert mcp_sources_repo().get(derived_source_id(conn_id)) is None
        assert shared_secrets_repo().get(derived_source_id(conn_id)) is None

    def test_disable_is_idempotent(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-disable-idem")
        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204

    def test_connection_detail_reports_chat_tools_state(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-state")

        before = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert before["has_chat_tools"] is False

        c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))
        after = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert after["has_chat_tools"] is True

    def test_enable_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        conn_id = self._create_keboola(c, seeded_app["admin_token"], name="kbc-chat-rbac")
        resp = c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_enable_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        conn_id = self._create_keboola(c, seeded_app["admin_token"], name="kbc-chat-anon")
        assert c.post(f"{BASE}/{conn_id}/chat-tools").status_code == 401


class TestAuthMethodOptionsAreImplemented:
    """Every auth method the admin UI offers must be one the outbound client
    can actually build credentials for.

    ``auth_method='header'`` ("custom header") was offered by both MCP admin
    templates while ``connectors/mcp/client.py::_build_http_headers`` had no
    branch for it — picking it produced a source that connected with **no**
    credential at all, and said nothing.
    """

    TEMPLATES = (
        "app/web/templates/admin_mcp_sources.html",
        "app/web/templates/admin_mcp_source_detail.html",
    )

    def test_no_template_offers_an_unimplemented_auth_method(self):
        import pathlib
        import re

        implemented = {"", "none", "bearer", "basic", "oauth"}
        offered: set[str] = set()
        for rel in self.TEMPLATES:
            text = pathlib.Path(rel).read_text(encoding="utf-8")
            for match in re.finditer(
                r"""id=["'](?:new|edit)-auth-method["'].*?</select>""", text, re.S
            ):
                offered.update(re.findall(r"""<option value=["']([^"']*)["']""", match.group(0)))

        assert offered, "auth-method selects not found — did the templates move?"
        assert offered <= implemented, (
            f"admin UI offers auth methods with no branch in "
            f"connectors/mcp/client.py::_build_http_headers: {sorted(offered - implemented)}"
        )


class TestUvCacheLocation:
    """The runtime image sets no ``HOME`` and its filesystem is replaced on
    every upgrade, so uv's default cache path is both underivable and
    ephemeral. Pin it onto the data volume instead."""

    def test_cache_dir_follows_data_dir(self, monkeypatch):
        from src.keboola_chat_tools import uv_cache_dir

        monkeypatch.setenv("DATA_DIR", "/data")
        assert uv_cache_dir() == "/data/cache/uv"

    def test_spec_pins_the_cache_dir(self, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/data")
        spec = build_stdio_spec(
            connection_id="c1",
            connection_name="Demo",
            stack_url="https://connection.example.com",
        )
        # Without this the first tool call after every auto-upgrade re-downloads
        # the package, and a HOME-less container may not resolve a cache at all.
        assert spec["env"]["UV_CACHE_DIR"] == "/data/cache/uv"
