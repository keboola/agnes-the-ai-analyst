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


class TestDisableLeavesNothingBehind(TestChatToolsEndpoint):
    """Turning chat tools off must revoke, not park.

    Devin Review on this PR. `_remove_chat_tools` deleted the `mcp_sources`
    row and the vault secret, but the tools discovered under that source live
    in `tool_registry` and the per-group permissions live in `tool_grants` —
    neither is keyed on the source row, so neither went. And because
    `derived_source_id()` is a pure function of the connection id, a later
    re-enable lands on the *same* id and adopts whatever was left: an admin
    who disabled chat tools to revoke access and later re-enabled them got
    the revoked grants back, silently.
    """

    def _seed_tool(self, source_id: str, *, group_id: str | None = None) -> str:
        from src.repositories import tool_registry_repo

        repo = tool_registry_repo()
        tool_id = f"{source_id}__kbc_query"
        repo.upsert(
            tool_id=tool_id,
            source_id=source_id,
            original_name="kbc_query",
            exposed_name="kbc_query",
            mode="passthrough",
            description="run a query",
            input_schema={},
        )
        if group_id:
            repo.add_grant(tool_id, group_id)
        return tool_id

    def test_disable_removes_the_discovered_tools(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-orphans")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import tool_registry_repo

        source_id = derived_source_id(conn_id)
        self._seed_tool(source_id)
        assert tool_registry_repo().list_for_source(source_id), "fixture seeded nothing"

        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204
        assert tool_registry_repo().list_for_source(source_id) == [], (
            "tools survived the disable and will be adopted by the next enable"
        )

    def test_re_enabling_does_not_restore_revoked_grants(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-regrant")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import tool_registry_repo, user_groups_repo

        source_id = derived_source_id(conn_id)
        group = user_groups_repo().get_by_name("Everyone")
        tool_id = self._seed_tool(source_id, group_id=group["id"])
        assert tool_registry_repo().grants_for_tool(tool_id), "fixture granted nothing"

        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        surviving = tool_registry_repo().list_for_source(source_id)
        assert surviving == [], f"re-enable adopted orphaned tools: {surviving}"


class TestAFailedResyncKeepsTheLiveSetupWorking(TestChatToolsEndpoint):
    """Devin Review on this PR: the rollback was unconditional.

    Re-running enable is how a rotated token is propagated, so on a re-sync
    the vault slot being overwritten holds the credential the existing,
    still-live setup authenticates with. Deleting it when the config write
    failed left every tool call of a previously working project failing auth
    — worse than the failed re-sync the admin came to fix.

    The patching uses `pytest.MonkeyPatch.context()` rather than the
    `monkeypatch` fixture: that fixture is ONE instance per test, shared with
    the autouse `_stable_vault_key`, so an `undo()` here also unsets
    `AGNES_VAULT_KEY` — the vault then fails to decrypt and every secret reads
    back as `None`, which looks exactly like the bug under test and would have
    made this test pass against the fixed code for the wrong reason.
    """

    @staticmethod
    def _failing_sources_repo(mp):
        """Make `mcp_sources_repo().upsert` raise; leave every other call real."""
        import app.api.admin_source_connections as mod

        real = mod.mcp_sources_repo

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("config write failed")

                return _fail if name == "upsert" else getattr(real(), name)

        mp.setattr(mod, "mcp_sources_repo", lambda: _Boom())

    def test_failed_resync_restores_the_previous_credential(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-resync")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        source_id = derived_source_id(conn_id)
        working = shared_secrets_repo().get(source_id)
        assert working, "fixture stored no credential"

        # Rotate the connection's token, then make the config write fail.
        assert (
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "rotated-token-value"},
                headers=_auth(token),
            ).status_code
            == 204
        )
        with pytest.MonkeyPatch.context() as mp:
            self._failing_sources_repo(mp)
            # TestClient re-raises server exceptions rather than rendering a
            # 500; what is under test is what the handler leaves behind.
            with pytest.raises(RuntimeError, match="config write failed"):
                c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))

        assert shared_secrets_repo().get(source_id) == working, (
            "a failed re-sync left the live setup unable to authenticate"
        )

    def test_failed_first_enable_still_leaves_no_orphaned_secret(self, seeded_app):
        """The original guarantee must survive the fix."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-firstfail")

        with pytest.MonkeyPatch.context() as mp:
            self._failing_sources_repo(mp)
            with pytest.raises(RuntimeError, match="config write failed"):
                c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))

        from src.repositories import shared_secrets_repo

        assert shared_secrets_repo().get(derived_source_id(conn_id)) is None


class TestDisableDoesNotClaimSuccessItCannotVouchFor(TestChatToolsEndpoint):
    """Devin Review on this PR: every removal step swallowed its own failure.

    `try/except Exception: logger.debug(…)` cannot tell "there was nothing to
    delete" (normal — the endpoint is idempotent) from "the delete did not
    work". An admin turning chat tools off to cut access could therefore be
    answered `204` while the tools, the grants and the copied credential were
    all still live: the one outcome this endpoint exists to prevent.
    """

    @staticmethod
    def _failing_tool_registry(mp):
        import app.api.admin_source_connections as mod

        real = mod.tool_registry_repo

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("registry unavailable")

                return _fail if name == "delete_for_source" else getattr(real(), name)

        mp.setattr(mod, "tool_registry_repo", lambda: _Boom())

    def test_a_failed_removal_is_not_answered_204(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-failremove")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        with pytest.MonkeyPatch.context() as mp:
            self._failing_tool_registry(mp)
            resp = c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))

        assert resp.status_code == 500, "a failed revoke was reported as success"
        detail = resp.json()["detail"]
        assert detail["error"] == "chat_tools_not_fully_removed"
        assert "tools and their grants" in detail["still_present"]

    def test_the_other_steps_still_run_when_one_fails(self, seeded_app):
        """A partial teardown beats stopping at the first failure."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-partial")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        source_id = derived_source_id(conn_id)
        with pytest.MonkeyPatch.context() as mp:
            self._failing_tool_registry(mp)
            assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 500

        assert mcp_sources_repo().get(source_id) is None, "the source survived an unrelated failure"
        assert shared_secrets_repo().get(source_id) is None, "the credential survived an unrelated failure"

    def test_a_clean_disable_is_still_204_and_idempotent(self, seeded_app):
        """The broad catch was load-bearing for nothing — deletes are no-ops."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-idem")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204
        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204


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
