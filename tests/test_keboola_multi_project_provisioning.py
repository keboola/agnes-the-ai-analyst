"""Login-time multi-project provisioning: connections, vault slots, groups,
membership diffing, grant policy, and the select-mode stash — against real
repositories (DuckDB backend), with the upstream PAT mint + verify faked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.auth import keboola_provisioning as kprov
from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv
from connectors.keboola.storage_api import KeboolaStorageClient

STACK = "https://connection.example.com"

# Projects whose minted PAT verifies as a MASTER token in the fakes below.
MASTER_PROJECTS = {"516"}


def P(pid: str, name: str = "", role: str = "admin") -> kp.DiscoveredProject:
    return kp.DiscoveredProject(id=pid, name=name or f"Project {pid}", role=role)


@pytest.fixture
def env(e2e_env, monkeypatch):
    """Schema-initialized system DB + a signed-in user + a working vault."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    _reset_ephemeral_key_for_tests()
    monkeypatch.setattr(kv, "stack_url", lambda: STACK)
    from src.db import get_system_db

    get_system_db().close()  # runs schema init + system-group seeding
    from src.repositories import users_repo

    users_repo().create(id="u1", email="jane@example.com", name="Jane")
    return {"user": users_repo().get_by_email("jane@example.com")}


@pytest.fixture
def pat_mocks(monkeypatch):
    """Fake the two upstream calls: PAT mint returns ``pat-{project_id}``,
    and any ``pat-*`` token's verify reports that project as owner (master
    token only for MASTER_PROJECTS). ``pat_mocks`` records every mint as
    ``(project_id, read_only)`` — order and COUNT matter (a re-login must
    reuse, not re-mint)."""
    minted: list[tuple[str, bool]] = []

    def fake_exchange(access_token, project_id, *, read_only):
        minted.append((project_id, read_only))
        return f"pat-{project_id}"

    monkeypatch.setattr(kp, "exchange_project_pat", fake_exchange)

    def fake_verify(self):
        pid = self.token.split("-", 1)[1]
        return {
            "isMasterToken": pid in MASTER_PROJECTS,
            "owner": {"id": int(pid), "name": f"Project {pid}"},
        }

    monkeypatch.setattr(KeboolaStorageClient, "verify_token", fake_verify)
    return minted


def _connection_for(pid: str):
    from src.repositories import source_connections_repo

    for row in source_connections_repo().list(source_type="keboola"):
        if str((row.get("config") or {}).get("project_id")) == str(pid):
            return row
    return None


def _kbc_memberships(user_id: str):
    from src.repositories import user_group_members_repo

    return {
        row["name"]
        for row in user_group_members_repo().list_groups_with_meta_for_user(user_id)
        if row.get("source") == kprov.MEMBERSHIP_SOURCE
    }


class TestAutoProvision:
    def test_fresh_login_provisions_everything(self, env, pat_mocks):
        from src.repositories import connection_secrets_repo, user_groups_repo

        projects = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")

        assert [o.error for o in summary.outcomes] == [None, None]
        for pid in ("516", "7"):
            row = _connection_for(pid)
            assert row is not None, pid
            config = row["config"]
            assert config["stack_url"] == STACK
            assert config["user_email"] == "jane@example.com"
            assert connection_secrets_repo().get(row["id"]) == f"pat-{pid}"

        # Admin's PAT is writable; everyone else's read-only.
        assert pat_mocks == [("516", False), ("7", True)]

        # Master slot only where the minted token verified as master.
        from app.api.admin_source_connections import master_secret_key

        assert connection_secrets_repo().has(master_secret_key(_connection_for("516")["id"]))
        assert not connection_secrets_repo().has(master_secret_key(_connection_for("7")["id"]))
        assert summary.semantic_sync_needed is True

        # Role groups exist and the user's keboola_sync memberships match.
        assert user_groups_repo().get_by_name("kbc-516-admin") is not None
        assert user_groups_repo().get_by_name("kbc-7-readonly") is not None
        assert _kbc_memberships("u1") == {"kbc-516-admin", "kbc-7-readonly"}
        assert summary.memberships_added == 2 and summary.memberships_removed == 0

        # No derived MCP source yet → chat tools deferred to the background.
        created_ids = {o.connection_id for o in summary.outcomes}
        assert set(summary.connections_needing_chat_tools) == created_ids

    def test_relogin_is_idempotent_and_reuses_tokens(self, env, pat_mocks):
        """A re-login must not create duplicates — and must not mint fresh
        PATs either: every mint leaves the superseded, still-valid token
        orphaned upstream, where nothing can revoke it (Devin Review on
        this PR). A stored token that still verifies is reused."""
        from src.repositories import connection_secrets_repo, source_connections_repo

        projects = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        first = kprov.provision_projects(env["user"], projects, projects, "at-1")
        second = kprov.provision_projects(env["user"], projects, projects, "at-2")

        assert len(source_connections_repo().list(source_type="keboola")) == 2
        assert {o.connection_id for o in first.outcomes} == {o.connection_id for o in second.outcomes}
        assert second.memberships_added == 0 and second.memberships_removed == 0
        assert not any(o.connection_created for o in second.outcomes)
        # Only the FIRST login minted; the second reused the stored tokens.
        assert pat_mocks == [("516", False), ("7", True)]
        assert all(o.token_reused for o in second.outcomes)
        for outcome in second.outcomes:
            assert connection_secrets_repo().get(outcome.connection_id) == f"pat-{outcome.project_id}"

    def test_stale_stored_token_is_replaced_by_a_fresh_mint(self, env, pat_mocks, monkeypatch):
        """When the stored token no longer verifies (revoked upstream), the
        re-login mints a replacement instead of reusing the dead one."""
        from src.repositories import connection_secrets_repo

        projects = [P("516", "Agnes - test", "admin")]
        kprov.provision_projects(env["user"], projects, projects, "at-1")

        real_verify = KeboolaStorageClient.verify_token

        def dead_stored(self):
            if self.token == "pat-516":
                from connectors.keboola.storage_api import StorageApiError

                raise StorageApiError("token was revoked", status=401)
            return real_verify(self)

        # The replacement mint must produce a distinguishable token.
        monkeypatch.setattr(kp, "exchange_project_pat", lambda tok, pid, *, read_only: f"pat-{pid}-fresh")

        def fresh_verify(self):
            if self.token == "pat-516":
                from connectors.keboola.storage_api import StorageApiError

                raise StorageApiError("token was revoked", status=401)
            pid = self.token.split("-")[1]
            return {"isMasterToken": pid in MASTER_PROJECTS, "owner": {"id": int(pid), "name": "P"}}

        monkeypatch.setattr(KeboolaStorageClient, "verify_token", fresh_verify)
        summary = kprov.provision_projects(env["user"], projects, projects, "at-2")
        assert summary.outcomes[0].token_stored is True
        assert summary.outcomes[0].token_reused is False
        assert connection_secrets_repo().get(summary.outcomes[0].connection_id) == "pat-516-fresh"

    def test_lost_project_removes_membership_but_keeps_the_connection(self, env, pat_mocks):
        both = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        kprov.provision_projects(env["user"], both, both, "at-1")
        only_a = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], only_a, only_a, "at-2")

        assert _kbc_memberships("u1") == {"kbc-516-admin"}
        assert summary.memberships_removed == 1
        # The connection (and its credential) survives — other users may
        # still hold the project; membership is the per-user revocation.
        assert _connection_for("7") is not None

    def test_role_change_swaps_the_group(self, env, pat_mocks):
        admin = [P("516", "Agnes - test", "admin")]
        kprov.provision_projects(env["user"], admin, admin, "at-1")
        demoted = [P("516", "Agnes - test", "readOnly")]
        kprov.provision_projects(env["user"], demoted, demoted, "at-2")
        assert _kbc_memberships("u1") == {"kbc-516-readonly"}

    def test_admin_placed_credential_is_never_overwritten(self, env, pat_mocks):
        from src.repositories import connection_secrets_repo, source_connections_repo

        source_connections_repo().create(
            id="admin-conn",
            name="Admin managed",
            source_type="keboola",
            config={"stack_url": STACK, "project_id": "99", "project_name": "Admin project"},
        )
        connection_secrets_repo().upsert("admin-conn", "admin-token")

        projects = [P("99", "Admin project", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")

        assert connection_secrets_repo().get("admin-conn") == "admin-token"
        outcome = summary.outcomes[0]
        assert outcome.connection_id == "admin-conn"
        assert outcome.connection_created is False
        assert outcome.token_stored is False
        # The admin row keeps its identity — no user_email adopted.
        assert "user_email" not in (source_connections_repo().get("admin-conn")["config"] or {})

    def test_empty_slot_on_admin_row_is_filled(self, env, pat_mocks):
        from src.repositories import connection_secrets_repo, source_connections_repo

        source_connections_repo().create(
            id="bare-conn",
            name="Admin managed bare",
            source_type="keboola",
            config={"stack_url": STACK, "project_id": "99"},
        )
        projects = [P("99", "Admin project", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        assert connection_secrets_repo().get("bare-conn") == "pat-99"
        assert summary.outcomes[0].token_stored is True

    def test_mismatched_pat_is_refused(self, env, pat_mocks, monkeypatch):
        from src.repositories import connection_secrets_repo

        def wrong_owner(self):
            return {"isMasterToken": True, "owner": {"id": 999, "name": "Somebody else"}}

        monkeypatch.setattr(KeboolaStorageClient, "verify_token", wrong_owner)
        projects = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        outcome = summary.outcomes[0]
        assert outcome.error == "pat_project_mismatch"
        assert outcome.token_stored is False
        assert not connection_secrets_repo().has(outcome.connection_id)

    def test_one_project_failing_does_not_block_the_rest(self, env, pat_mocks, monkeypatch):
        def flaky_exchange(access_token, project_id, *, read_only):
            if project_id == "7":
                raise kp.KeboolaProjectApiError("pat_exchange_denied")
            return f"pat-{project_id}"

        monkeypatch.setattr(kp, "exchange_project_pat", flaky_exchange)
        projects = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        by_id = {o.project_id: o for o in summary.outcomes}
        assert by_id["516"].token_stored is True
        assert by_id["7"].error == "pat_exchange: pat_exchange_denied"
        # Membership still reflects BOTH projects — upstream access exists.
        assert _kbc_memberships("u1") == {"kbc-516-admin", "kbc-7-readonly"}

    def test_group_ensure_hiccup_never_strips_membership(self, env, pat_mocks, monkeypatch):
        """A transient failure while ensuring a role group leaves the desired
        set incomplete — the sync may add, but must NOT remove memberships
        the user still holds upstream (Devin Review on this PR)."""
        both = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        kprov.provision_projects(env["user"], both, both, "at-1")
        assert _kbc_memberships("u1") == {"kbc-516-admin", "kbc-7-readonly"}

        def flaky_ensure(project):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(kprov, "_ensure_group", flaky_ensure)
        summary = kprov.provision_projects(env["user"], both, both, "at-2")
        assert summary.membership_removals_safe is False
        assert summary.memberships_removed == 0
        assert _kbc_memberships("u1") == {"kbc-516-admin", "kbc-7-readonly"}

    def test_concurrent_create_race_reconciles_to_one_connection(self, env, pat_mocks, monkeypatch):
        """Two logins can both miss the find and both insert (no unique
        constraint on the project identity — Devin Review on this PR): the
        loser deletes its own fresh row and adopts the canonical one."""
        from src.repositories import connection_secrets_repo, source_connections_repo

        # The rival row that "the other login" inserted inside our race
        # window. Id sorts before any uuid4 hex, so it is the canonical row.
        source_connections_repo().create(
            id="!rival",
            name="Rival copy",
            source_type="keboola",
            config={"stack_url": STACK, "project_id": "99", "project_name": "Raced project"},
        )
        # Simulate the window: the initial find sees nothing…
        monkeypatch.setattr(kprov, "_find_connection", lambda stack, pid: None)
        projects = [P("99", "Raced project", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")

        # …but the post-create reconcile collapses back to the rival row.
        outcome = summary.outcomes[0]
        assert outcome.connection_id == "!rival"
        assert outcome.connection_created is False
        rows = [
            row
            for row in source_connections_repo().list(source_type="keboola")
            if str((row.get("config") or {}).get("project_id")) == "99"
        ]
        assert len(rows) == 1 and rows[0]["id"] == "!rival"
        # The empty-slot rule then lands the token on the canonical row.
        assert connection_secrets_repo().get("!rival") == "pat-99"

    def test_vault_unconfigured_skips_connections_but_syncs_membership(self, env, pat_mocks, monkeypatch):
        monkeypatch.setattr("app.secrets_vault.can_store_secrets", lambda: False)
        projects = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        assert summary.outcomes[0].error == "vault_key_not_configured"
        assert _connection_for("516") is None
        assert _kbc_memberships("u1") == {"kbc-516-admin"}


class TestGrantPolicy:
    def _register_tools(self, connection_id: str):
        from src.keboola_chat_tools import build_stdio_spec, derived_source_id, derived_tool_id
        from src.repositories import mcp_sources_repo, tool_registry_repo
        from src.repositories.tool_registry import PASSTHROUGH

        spec = build_stdio_spec(connection_id=connection_id, connection_name="X", stack_url=STACK)
        mcp_sources_repo().upsert(**spec)
        registry = tool_registry_repo()
        for name, mutating in (("query_data", False), ("create_bucket", True)):
            registry.upsert(
                tool_id=derived_tool_id(connection_id, name),
                source_id=derived_source_id(connection_id),
                original_name=name,
                exposed_name=f"kbc_x_{name}",
                mode=PASSTHROUGH,
                mutating=mutating,
            )
        return registry, derived_tool_id(connection_id, "query_data"), derived_tool_id(connection_id, "create_bucket")

    def test_admin_gets_all_tools_readonly_only_nonmutating(self, env, pat_mocks):
        from src.repositories import user_groups_repo, users_repo

        projects = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        connection_id = summary.outcomes[0].connection_id
        registry, read_tool, write_tool = self._register_tools(connection_id)

        # Re-login now that tools exist: the admin group is granted inline.
        kprov.provision_projects(env["user"], projects, projects, "at-2")
        admin_gid = user_groups_repo().get_by_name("kbc-516-admin")["id"]
        assert admin_gid in registry.grants_for_tool(read_tool)
        assert admin_gid in registry.grants_for_tool(write_tool)

        # A second, read-only user of the same project gets only read tools.
        users_repo().create(id="u2", email="bob@example.com", name="Bob")
        bob = users_repo().get_by_email("bob@example.com")
        ro = [P("516", "Agnes - test", "readOnly")]
        kprov.provision_projects(bob, ro, ro, "at-3")
        ro_gid = user_groups_repo().get_by_name("kbc-516-readonly")["id"]
        assert ro_gid in registry.grants_for_tool(read_tool)
        assert ro_gid not in registry.grants_for_tool(write_tool)
        assert _kbc_memberships("u2") == {"kbc-516-readonly"}

    def test_connection_with_registered_tools_is_not_re_enabled(self, env, pat_mocks):
        projects = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        self._register_tools(summary.outcomes[0].connection_id)
        again = kprov.provision_projects(env["user"], projects, projects, "at-2")
        assert again.connections_needing_chat_tools == []

    def test_admin_disabled_source_is_left_alone(self, env, pat_mocks):
        """A derived source the admin switched OFF is neither re-enabled nor
        granted from a login — the off-switch is an admin decision."""
        from src.keboola_chat_tools import derived_source_id
        from src.repositories import mcp_sources_repo, user_groups_repo

        projects = [P("516", "Agnes - test", "admin")]
        summary = kprov.provision_projects(env["user"], projects, projects, "at-1")
        connection_id = summary.outcomes[0].connection_id
        registry, read_tool, _ = self._register_tools(connection_id)
        source = mcp_sources_repo().get(derived_source_id(connection_id))
        mcp_sources_repo().upsert(
            **{k: v for k, v in {**source, "enabled": False}.items() if k not in ("created_at", "updated_at")}
        )

        again = kprov.provision_projects(env["user"], projects, projects, "at-2")
        assert again.connections_needing_chat_tools == []
        assert again.deferred_grants == []
        admin_gid = user_groups_repo().get_by_name("kbc-516-admin")["id"]
        assert admin_gid not in registry.grants_for_tool(read_tool)


class TestFinishLoginProvisioning:
    """The background tail: chat-tools enable per connection (failures
    isolated), deferred grants only after a successful enable, and the
    semantic-layer refresh under the shared guard."""

    def _run(self, summary):
        import asyncio

        asyncio.run(kprov.finish_login_provisioning(summary))

    def test_enables_grants_and_syncs(self, monkeypatch):
        import app.api.admin_source_connections as asc
        import app.api.keboola_semantic_layer_refresh as kslr

        enabled, granted, synced = [], [], []

        async def fake_enable(connection_id, _user=None):
            enabled.append(connection_id)
            return {"tools_registered": 2}

        monkeypatch.setattr(asc, "enable_chat_tools", fake_enable)
        monkeypatch.setattr(kprov, "apply_tool_grants", lambda cid, gid, role: granted.append((cid, gid, role)))

        async def fake_sync(*, trigger):
            synced.append(trigger)

        monkeypatch.setattr(kslr, "run_semantic_layer_refresh_background", fake_sync)

        summary = kprov.ProvisionSummary(
            connections_needing_chat_tools=["c1"],
            deferred_grants=[{"connection_id": "c1", "group_id": "g1", "role": "admin"}],
            semantic_sync_needed=True,
        )
        self._run(summary)
        assert enabled == ["c1"]
        assert granted == [("c1", "g1", "admin")]
        assert synced == ["keboola-login"]

    def test_one_enable_failing_does_not_stop_the_rest(self, monkeypatch):
        import app.api.admin_source_connections as asc
        import app.api.keboola_semantic_layer_refresh as kslr
        from fastapi import HTTPException

        enabled, granted = [], []

        async def flaky_enable(connection_id, _user=None):
            if connection_id == "c1":
                raise HTTPException(status_code=502, detail="upstream download failed")
            enabled.append(connection_id)
            return {"tools_registered": 1}

        monkeypatch.setattr(asc, "enable_chat_tools", flaky_enable)
        monkeypatch.setattr(kprov, "apply_tool_grants", lambda cid, gid, role: granted.append(cid))

        async def fake_sync(*, trigger):
            pass

        monkeypatch.setattr(kslr, "run_semantic_layer_refresh_background", fake_sync)

        summary = kprov.ProvisionSummary(
            connections_needing_chat_tools=["c1", "c2"],
            deferred_grants=[
                {"connection_id": "c1", "group_id": "g1", "role": "admin"},
                {"connection_id": "c2", "group_id": "g2", "role": "readOnly"},
            ],
        )
        self._run(summary)
        assert enabled == ["c2"]
        # No grants for the connection whose tools never registered.
        assert granted == ["c2"]


class TestSelectModeStash:
    def test_store_load_roundtrip_and_import(self, env, pat_mocks):
        projects = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        assert kprov.store_pending_discovery(env["user"], projects, "at-1") is True
        blob = kprov.load_pending_discovery("u1")
        assert blob is not None
        assert {p["id"] for p in blob["projects"]} == {"516", "7"}

        summary = kprov.provision_selected(env["user"], ["516"])
        assert [o.project_id for o in summary.outcomes] == ["516"]
        assert _connection_for("516") is not None
        assert _connection_for("7") is None
        assert _kbc_memberships("u1") == {"kbc-516-admin"}
        # The stash survives an import — the user may import more later.
        assert kprov.load_pending_discovery("u1") is not None

    def test_import_unknown_project_is_refused(self, env, pat_mocks):
        kprov.store_pending_discovery(env["user"], [P("516")], "at-1")
        with pytest.raises(kprov.DiscoveryStateError) as err:
            kprov.provision_selected(env["user"], ["999"])
        assert err.value.reason == "unknown_project"

    def test_import_without_discovery_is_expired(self, env):
        with pytest.raises(kprov.DiscoveryStateError) as err:
            kprov.provision_selected(env["user"], ["516"])
        assert err.value.reason == "discovery_expired"

    def test_expired_stash_is_deleted_on_load(self, env):
        from src.repositories import per_user_secrets_repo

        stale = {
            "v": 1,
            "access_token": "at-old",
            "stack_url": STACK,
            "stored_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "projects": [{"id": "516", "name": "A", "role": "admin"}],
        }
        per_user_secrets_repo().upsert(kprov.PENDING_DISCOVERY_SOURCE_ID, "u1", json.dumps(stale))
        assert kprov.load_pending_discovery("u1") is None
        assert per_user_secrets_repo().get(kprov.PENDING_DISCOVERY_SOURCE_ID, "u1") is None

    def test_relogin_membership_sync_covers_connected_projects_only(self, env, pat_mocks):
        # Import one of two, then a later select-mode login (provision
        # nothing) keeps the imported project's membership and drops nothing.
        projects = [P("516", "Agnes - test", "admin"), P("7", "Beta", "readOnly")]
        kprov.store_pending_discovery(env["user"], projects, "at-1")
        kprov.provision_selected(env["user"], ["516"])

        summary = kprov.provision_projects(env["user"], [], projects, "at-2")
        assert summary.outcomes == []
        assert _kbc_memberships("u1") == {"kbc-516-admin"}
