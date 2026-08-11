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

    @pytest.fixture(autouse=True)
    def _storage_api_is_reachable(self):
        """`PUT /{id}/secret` runs a live `verify_token` preflight (#1242) and
        records the project it opens, so storing a token now needs the stack
        to answer. These tests are about the chat-tools pair, not about the
        Storage API — one stable project for every store."""
        from unittest.mock import patch

        # `_validate_url_not_private` too: `/secret` re-validates the stack URL
        # at use, DNS included, and `connection.example.com` does not resolve.
        # That is validate-at-use doing its job, not something to weaken in the
        # handler — so the fixture stands in for a reachable stack.
        with (
            patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify,
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            verify.return_value = {"isMasterToken": False, "owner": {"id": 4242, "name": "Test Project"}}
            yield verify

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
        assert shared_secrets_repo().get(source_id), "fixture stored no credential"

        # Rotate the connection's token, then make the config write fail.
        assert (
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "rotated-token-value"},
                headers=_auth(token),
            ).status_code
            == 204
        )
        # Read AFTER the rotation: storing a token now propagates to the
        # agent's copy, so this — not the pre-rotation value — is what the
        # live setup authenticates with and what a failed re-sync must keep.
        working = shared_secrets_repo().get(source_id)
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

    def test_a_failure_past_the_tools_step_does_not_block_the_operation(self, seeded_app):
        """Only the TOOLS step is fatal — it is the one that is access.

        Past it the grants are already gone, so a failure leaves orphaned
        material that reaches nobody. Raising there made `delete_connection`
        (which runs this before dropping the row) permanently unfinishable on
        a persistent vault fault: the retry re-ran the same failing step
        forever and the connection could never be deleted.
        """
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-partial")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        import app.api.admin_source_connections as mod
        from src.repositories import mcp_sources_repo, tool_registry_repo

        real = mod.shared_secrets_repo
        source_id = derived_source_id(conn_id)

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("vault unavailable")

                return _fail if name == "delete" else getattr(real(), name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "shared_secrets_repo", lambda: _Boom())
            assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204

        # The access IS gone — which is what the operation is for.
        assert tool_registry_repo().list_for_source(source_id) == []
        assert mcp_sources_repo().get(source_id) is None

    def test_a_persistently_failing_vault_does_not_make_the_connection_undeletable(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-undeletable")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        import app.api.admin_source_connections as mod

        real = mod.shared_secrets_repo

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("vault unavailable")

                return _fail if name == "delete" else getattr(real(), name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "shared_secrets_repo", lambda: _Boom())
            assert c.delete(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 204

        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 404

    def test_a_clean_disable_is_still_204_and_idempotent(self, seeded_app):
        """The broad catch was load-bearing for nothing — deletes are no-ops."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-idem")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204
        assert c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 204


class TestDeletingTheConnectionStaysRetryable(TestChatToolsEndpoint):
    """Devin Review on this PR: the teardown ran after the row was gone.

    `_remove_chat_tools` now raises on a genuine failure instead of swallowing
    it, so ordering started to matter: run after `repo.delete`, a hiccup
    answered "delete failed" for a connection that no longer existed — the
    retry 404s, the leftover tools have no obvious route to removal, and the
    list still shows the row until a reload.
    """

    def test_a_failed_teardown_leaves_the_connection_deletable(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-chat-delorder")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        import app.api.admin_source_connections as mod

        real = mod.tool_registry_repo

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("registry unavailable")

                return _fail if name == "delete_for_source" else getattr(real(), name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "tool_registry_repo", lambda: _Boom())
            assert c.delete(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 500

        # The connection must still be there, so the admin can simply retry.
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 200
        assert c.delete(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 204

    def test_the_teardown_runs_before_the_row_is_deleted(self):
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "admin_source_connections.py").read_text(
            encoding="utf-8"
        )
        block = src[src.index("def delete_connection") :][:3000]
        assert block.index("_remove_chat_tools(connection_id)") < block.index("repo.delete(connection_id)")


def test_the_admin_page_renders_a_structured_error_detail():
    """A FastAPI `detail` is a string on some paths and an object on others.

    Pasting it into a template literal renders "[object Object]" and throws
    away the one thing the admin needs — which is what the chat-tools toast
    did with the partial-teardown report. (Devin Review on this PR.)
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    assert "function detailMessage(" in src
    assert "still_present" in src, "the partial-teardown list is never shown"
    assert 'showToast("Failed: " + (body.detail' not in src, "the chat-tools toast still stringifies an object"


class TestTheStoredStackUrlIsValidatedOnTheWayIn:
    """Devin Review on this PR: the enable path cited a guard with no caller.

    Enabling chat tools deliberately skips the DNS-resolving validation — it
    makes no outbound request, and re-resolving would only make enabling fail
    whenever DNS is down. Its justification was that the URL "is the
    connection's own, already SSRF-validated on create/update". That was not
    true: `_validate_stack_url`'s `required=False` branch, written for exactly
    those two handlers, had no caller anywhere, so an admin-supplied
    `stack_url` was stored unchecked. Validate-at-use on `/test`, `/tables`
    and `/secret` is unchanged — it closes the DNS-rebind window and this does
    not replace it.
    """

    def test_create_rejects_a_plain_http_stack_url(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        r = c.post(
            BASE,
            json={
                "name": "kbc-ssrf-http",
                "source_type": "keboola",
                "config": {"stack_url": "http://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text

    def test_update_rejects_a_plain_http_stack_url(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        created = c.post(
            BASE,
            json={
                "name": "kbc-ssrf-update",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = c.put(
            f"{BASE}/{created.json()['id']}",
            json={"config": {"stack_url": "http://connection.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text

    def test_an_unresolvable_stack_is_still_storable(self, seeded_app):
        """The private-range half needs DNS and deliberately stays at use.

        A stack that does not resolve from the Agnes host yet — a fresh
        deployment, split-horizon DNS, a momentary outage — is a legitimate
        thing to configure, and `/test` is where the operator finds out.
        """
        c, token = seeded_app["client"], seeded_app["admin_token"]
        r = c.post(
            BASE,
            json={
                "name": "kbc-unresolvable",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text

    def test_a_partial_config_from_the_wizard_still_saves(self, seeded_app):
        """`required=False` exists so the add-data-source wizard can save a
        config that has no stack_url yet — that must keep working."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        r = c.post(
            BASE,
            json={"name": "kbc-partial", "source_type": "keboola", "config": {}},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text


class TestClearingTheTokenCutsTheAgentOffToo(TestChatToolsEndpoint):
    """Devin Review on this PR: the agent kept a working copy.

    Enabling chat tools copies the connection's storage token into the MCP
    vault. Clearing the connection's token is how an admin cuts a project off
    — and the copy survived it, so the agent went on querying that project
    with a credential the admin believed they had removed.
    """

    def test_clearing_the_storage_token_clears_the_agent_copy(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clear-token")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert shared_secrets_repo().get(source_id), "fixture stored no copy"

        assert c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token)).status_code == 204

        assert shared_secrets_repo().get(source_id) is None, (
            "the agent still holds a credential the admin cleared"
        )

    def test_clearing_the_master_token_leaves_the_agent_copy(self, seeded_app):
        """The copy is of the STORAGE token; the master one is a different slot."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clear-master")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        assert c.delete(f"{BASE}/{conn_id}/secret?kind=master", headers=_auth(token)).status_code == 204
        assert shared_secrets_repo().get(derived_source_id(conn_id))


class TestADerivedNameClashIsExplained(TestChatToolsEndpoint):
    """Devin Review on this PR: `mcp_sources.name` is unique.

    A hand-registered source already holding the derived name made the upsert
    die with an opaque 500, with nothing naming the clash.
    """

    def test_a_taken_name_answers_409_and_says_what_clashed(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clash")

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        spec = build_stdio_spec(
            connection_id=conn_id, connection_name="kbc-clash", stack_url="https://connection.example.com"
        )
        squatter = {**spec, "id": "someone-elses-source"}
        mcp_sources_repo().upsert(**squatter)

        r = c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))
        assert r.status_code == 409, f"expected an explained clash, got {r.status_code}: {r.text}"
        detail = r.json()["detail"]
        assert detail["error"] == "mcp_source_name_taken"
        assert detail["name"] == spec["name"]
        assert "someone-elses-source" in detail["message"]

    def test_a_failed_clash_check_leaves_no_orphaned_credential(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clash-secret")

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo, shared_secrets_repo

        spec = build_stdio_spec(
            connection_id=conn_id, connection_name="kbc-clash-secret", stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**spec, "id": "squatter-2"})

        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 409
        assert shared_secrets_repo().get(spec["id"]) is None


def test_the_cli_points_at_the_step_that_actually_creates_tools():
    """Dual-surface: the web hint was fixed; the CLI printed the same wrong
    next step, naming a command that cannot grant MCP tools either."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "cli" / "commands" / "admin_connection.py").read_text(
        encoding="utf-8"
    )
    assert "Introspect" in src
    assert "agnes admin grant --help" not in src, "still points at a command that cannot grant tool access"


class TestAFailedToolRemovalStopsTheTeardown(TestChatToolsEndpoint):
    """Devin Review on this PR: continuing left the access live.

    `list_passthrough_for_groups` joins `tool_registry` to `tool_grants` and
    never to `mcp_sources`, so a tool whose parent source is gone is still
    served to any granted group. Deleting the source after failing to remove
    the tools therefore left the access working AND removed the row an admin
    would use to clean it up from /admin/mcp.
    """

    def test_the_source_survives_a_failed_tool_removal(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-teardown-stop")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        import app.api.admin_source_connections as mod
        from src.repositories import mcp_sources_repo, shared_secrets_repo

        real = mod.tool_registry_repo

        class _Boom:
            def __getattr__(self, name):
                def _fail(*a, **kw):
                    raise RuntimeError("registry unavailable")

                return _fail if name == "delete_for_source" else getattr(real(), name)

        source_id = derived_source_id(conn_id)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "tool_registry_repo", lambda: _Boom())
            r = c.delete(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token))

        assert r.status_code == 500
        assert r.json()["detail"]["still_present"] == ["tools and their grants"]
        assert mcp_sources_repo().get(source_id) is not None, (
            "the source an admin would clean up from was deleted while the tools stayed live"
        )
        assert shared_secrets_repo().get(source_id) is not None


class TestRotatingTheTokenPropagatesToo(TestChatToolsEndpoint):
    """Devin Review on this PR: clearing propagated, rotating did not.

    Copy-not-reference is deliberate, but the asymmetry meant the two halves
    of one admin intent behaved differently: rotating a leaked token left the
    leaked value live in the MCP vault, and the agent went on authenticating
    with a credential that may already have been revoked upstream.
    """

    def test_a_rotated_token_reaches_the_agent_copy(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-rotate")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert shared_secrets_repo().get(source_id) == "kbc-token-value"

        assert (
            c.put(f"{BASE}/{conn_id}/secret", json={"value": "rotated"}, headers=_auth(token)).status_code == 204
        )

        assert shared_secrets_repo().get(source_id) == "rotated", (
            "the agent still holds the previous token after a rotation"
        )

    def test_storing_a_token_does_not_enable_chat_tools(self, seeded_app):
        """Only an EXISTING copy is updated — this is not a back door to on."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-rotate-off", with_secret=False)

        assert (
            c.put(f"{BASE}/{conn_id}/secret", json={"value": "fresh"}, headers=_auth(token)).status_code == 204
        )

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert shared_secrets_repo().get(source_id) is None
        assert mcp_sources_repo().get(source_id) is None

    def test_the_master_token_slot_is_untouched(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-rotate-master")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        source_id = derived_source_id(conn_id)
        # `kind` rides in the BODY on PUT (it is a query param only on DELETE);
        # sending it as a query string silently stored a STORAGE token, which
        # is how this test first "failed" against correct code.
        r = c.put(
            f"{BASE}/{conn_id}/secret",
            json={"value": "master-v2", "kind": "master"},
            headers=_auth(token),
        )
        # Whether the master token verifies against the live stack is not the
        # point — either way the storage-token copy must be untouched.
        assert r.status_code in (204, 400, 502), r.text
        assert shared_secrets_repo().get(source_id) == "kbc-token-value"


class TestMovingTheConnectionMovesTheAgentToo(TestChatToolsEndpoint):
    """Devin Review on this PR: the derived spec embeds the address.

    `build_stdio_spec` bakes the connection's name and `stack_url` into the
    MCP source, so an edit that moved the project to a new stack left the
    agent talking to the old one — and, now that storing a token propagates
    to the derived copy, a freshly rotated credential was being copied to a
    source still pointed at the previous address.
    """

    def test_changing_the_stack_url_moves_the_derived_source(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-moved")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import mcp_sources_repo

        source_id = derived_source_id(conn_id)
        before = mcp_sources_repo().get(source_id)
        assert "connection.example.com" in str(before), before

        r = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://moved.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        after = mcp_sources_repo().get(source_id)
        assert "moved.example.com" in str(after), "the agent still points at the old address"
        assert "connection.example.com" not in str(after)

    def test_editing_a_connection_without_chat_tools_creates_nothing(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-notools", with_secret=False)

        assert (
            c.put(
                f"{BASE}/{conn_id}",
                json={"config": {"stack_url": "https://moved.example.com"}},
                headers=_auth(token),
            ).status_code
            == 200
        )

        from src.repositories import mcp_sources_repo

        assert mcp_sources_repo().get(derived_source_id(conn_id)) is None


class TestTheTwoPropagationFixesDoNotCancelEachOther(TestChatToolsEndpoint):
    """Devin Review on this PR: clear-then-store was a dead end.

    Clearing deletes the agent's copy; storing propagated only when a copy
    already existed. Together that left the agent with NO credential while
    the switch still read "on" — each fix correct alone, broken as a pair.
    """

    def test_clearing_then_storing_restores_the_agent_copy(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clear-then-store")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token)).status_code == 204
        assert shared_secrets_repo().get(source_id) is None

        assert (
            c.put(f"{BASE}/{conn_id}/secret", json={"value": "re-added"}, headers=_auth(token)).status_code
            == 204
        )
        assert shared_secrets_repo().get(source_id) == "re-added", (
            "the switch reads 'on' but the agent has no credential"
        )


class TestAnEditDoesNotResurrectADisabledServer(TestChatToolsEndpoint):
    """Devin Review on this PR: `build_stdio_spec` always says enabled.

    It is written for the enable path. Upserting it wholesale on an unrelated
    edit switched a server the admin had deliberately disabled back on.
    """

    def test_a_disabled_derived_source_stays_disabled_across_an_edit(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-disabled")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        source_id = derived_source_id(conn_id)
        # Disable it the way an admin would — re-upserting the spec with the
        # flag off. (A fetched row carries `created_at`/`updated_at`, which
        # are not upsert kwargs.)
        spec = build_stdio_spec(
            connection_id=conn_id, connection_name="kbc-disabled", stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**spec, "enabled": False})
        assert mcp_sources_repo().get(source_id)["enabled"] is False

        assert (
            c.put(f"{BASE}/{conn_id}", json={"name": "kbc-disabled-renamed"}, headers=_auth(token)).status_code
            == 200
        )

        assert mcp_sources_repo().get(source_id)["enabled"] is False, (
            "an unrelated edit switched a deliberately-disabled server back on"
        )

    def test_a_rename_onto_a_taken_name_leaves_the_source_alone(self, seeded_app):
        """Skipping loudly beats raising into the broad handler, which would
        log a clash as an unexpected failure and tell the admin nothing."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-rename-clash")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        taken = build_stdio_spec(
            connection_id="other", connection_name="occupied", stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**taken, "id": "squatter"})

        source_id = derived_source_id(conn_id)
        before = mcp_sources_repo().get(source_id)
        assert c.put(f"{BASE}/{conn_id}", json={"name": "occupied"}, headers=_auth(token)).status_code == 200
        assert mcp_sources_repo().get(source_id)["name"] == before["name"]


class TestEnableAlwaysEnables:
    """Devin Review on this PR, from both sides.

    An earlier revision carried a stored `enabled=False` over so that a
    re-run to propagate a rotated token could not silently re-enable a
    switched-off server. The cost was the opposite bug: the page's switch
    could no longer turn chat tools back ON — it reported success and left
    the server off. Both are gone now that a rotation propagates on its own
    path (`set_connection_secret`), so this endpoint can mean what its name
    says. The unrelated-edit path still preserves the flag.
    """

class TestTheSwitchReflectsWhatTheSourceDoes(TestChatToolsEndpoint):
    """Devin Review on this PR: `has_chat_tools` was row existence alone.

    Both enable and the edit re-sync deliberately carry a previously-set
    `enabled=False` over, so a disabled derived source kept reading "on" —
    and toggling the switch off and on, the obvious remedy, does nothing
    because the row was there the whole time.
    """

    def _disable(self, conn_id: str, name: str) -> None:
        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        spec = build_stdio_spec(
            connection_id=conn_id, connection_name=name, stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**spec, "enabled": False})

    def test_a_disabled_source_reads_as_off(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-switch")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["has_chat_tools"] is True

        self._disable(conn_id, "kbc-switch")
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["has_chat_tools"] is False

    def test_a_source_with_no_enabled_key_still_reads_as_on(self, seeded_app):
        """Absent means enabled — an older row must not read as off."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-switch-legacy")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["has_chat_tools"] is True


class TestEnableTurnsADisabledServerBackOn(TestChatToolsEndpoint):
    """The switch must be able to undo itself.

    With the flag carried over unconditionally, an admin who switched the
    derived server off could never turn chat tools back on from the
    data-sources page: the request succeeded and the server stayed off.
    """

    def test_enabling_a_disabled_source_enables_it(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-reenable")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        source_id = derived_source_id(conn_id)
        spec = build_stdio_spec(
            connection_id=conn_id, connection_name="kbc-reenable", stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**spec, "enabled": False})
        assert mcp_sources_repo().get(source_id)["enabled"] is False

        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201
        assert mcp_sources_repo().get(source_id)["enabled"] is True, (
            "the switch reported success and left the server off"
        )
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["has_chat_tools"] is True

    def test_an_unrelated_edit_still_preserves_the_flag(self, seeded_app):
        """An edit is not a request to enable — that path keeps the flag."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-edit-keeps-off")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.keboola_chat_tools import build_stdio_spec
        from src.repositories import mcp_sources_repo

        source_id = derived_source_id(conn_id)
        spec = build_stdio_spec(
            connection_id=conn_id, connection_name="kbc-edit-keeps-off", stack_url="https://connection.example.com"
        )
        mcp_sources_repo().upsert(**{**spec, "enabled": False})

        assert c.put(f"{BASE}/{conn_id}", json={"name": "renamed-still-off"}, headers=_auth(token)).status_code == 200
        assert mcp_sources_repo().get(source_id)["enabled"] is False


class TestAnEditKeepsWhatTheAdminAdjusted(TestChatToolsEndpoint):
    """Devin Review on this PR: the resync rebuilt the whole row.

    `build_stdio_spec` describes a freshly enabled source, so upserting it
    wholesale on an unrelated save — flipping "set as default", a rename —
    discarded every setting an admin had adjusted on that server entry. Only
    the connection's own name and stack URL go stale, so only those are
    re-derived.
    """

    def test_customisations_survive_an_unrelated_edit(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-custom")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import mcp_sources_repo

        source_id = derived_source_id(conn_id)
        before = mcp_sources_repo().get(source_id)
        keep = {
            k: before[k]
            for k in ("id", "name", "transport", "command", "url", "auth_method", "auth_secret_env", "enabled")
            if k in before
        }
        mcp_sources_repo().upsert(
            **keep,
            scope="per_user",
            connect_hint="Ask the data team before using this.",
            args=["--pinned", "1.2.3"],
            env={**(before.get("env") or {}), "KBC_EXTRA": "keep-me"},
        )

        assert (
            c.put(f"{BASE}/{conn_id}", json={"name": "kbc-custom-renamed"}, headers=_auth(token)).status_code == 200
        )

        after = mcp_sources_repo().get(source_id)
        assert after["scope"] == "per_user"
        assert after["connect_hint"] == "Ask the data team before using this."
        assert list(after["args"]) == ["--pinned", "1.2.3"]
        assert (after.get("env") or {}).get("KBC_EXTRA") == "keep-me"

    def test_the_two_derived_fields_are_still_refreshed(self, seeded_app):
        """…and the point of the resync survives: name and stack URL follow
        the connection, or the agent talks to the wrong project."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-moves")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.keboola_chat_tools import STACK_URL_ENV, derived_source_name
        from src.repositories import mcp_sources_repo

        assert (
            c.put(
                f"{BASE}/{conn_id}",
                json={"name": "kbc-moved", "config": {"stack_url": "https://other.example.com"}},
                headers=_auth(token),
            ).status_code
            == 200
        )

        after = mcp_sources_repo().get(derived_source_id(conn_id))
        assert after["name"] == derived_source_name("kbc-moved")
        assert (after.get("env") or {})[STACK_URL_ENV] == "https://other.example.com"


class TestClearingTheTokenSwitchesTheAgentOff:
    """Devin Review on this PR, across three rounds.

    Deleting the vault copy alone did not cut the agent off:
    `connectors/mcp/client.py` falls back to `os.environ[auth_secret_env]`,
    and the derived source names `KBC_STORAGE_TOKEN`, which a Keboola
    deployment plausibly has set. Clearing that field instead was worse — for
    a stdio source it is also the name the vault value is injected under, so
    it broke the working path and left re-adding a token useless. Disabling
    the source is the honest expression of "cut this project off", and the
    switch then reads off because it is.
    """


class TestClearingTheTokenDisablesTheDerivedSource(TestChatToolsEndpoint):
    """See the module note above: disabling is what actually cuts it off."""

    def test_clearing_switches_it_off(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clear-off")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token)).status_code == 204

        assert shared_secrets_repo().get(source_id) is None
        assert mcp_sources_repo().get(source_id)["enabled"] is False
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["has_chat_tools"] is False

    def test_the_injection_name_is_left_intact(self):
        """`auth_secret_env` is how the vault value reaches a stdio subprocess
        — clearing it breaks the working path, not the fallback."""
        import inspect

        from app.api import admin_source_connections as mod

        src = inspect.getsource(mod.delete_connection_secret)
        assert '"auth_secret_env": None' not in src
        assert '"enabled": False' in src

    def test_re_adding_a_token_and_enabling_restores_service(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="kbc-clear-restore")
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201
        assert c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token)).status_code == 204

        assert (
            c.put(f"{BASE}/{conn_id}/secret", json={"value": "fresh"}, headers=_auth(token)).status_code == 204
        )
        assert c.post(f"{BASE}/{conn_id}/chat-tools", headers=_auth(token)).status_code == 201

        from src.repositories import mcp_sources_repo, shared_secrets_repo

        source_id = derived_source_id(conn_id)
        assert mcp_sources_repo().get(source_id)["enabled"] is True
        assert mcp_sources_repo().get(source_id)["auth_secret_env"], "the injection name must survive"
        assert shared_secrets_repo().get(source_id) == "fresh"
