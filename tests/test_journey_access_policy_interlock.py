"""Task 4 — the distribution interlock on ``PUT /registry/{table_id}``
(table access policies design doc §3.1/§3.2).

Mirrors ``tests/test_journey_server_only.py``: HTTP-level, admin token,
``seeded_app``. A policy may only be attached to a table that is not
distributed (``query_mode='remote'`` or ``server_only=true``); attaching it
is itself gated behind the ``access_policies.enabled`` feature flag.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    resp = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _policy_sql(table_name: str) -> str:
    return f"SELECT * FROM {table_name}"


@pytest.mark.journey
class TestFeatureFlagGate:
    def test_attach_rejected_when_flag_disabled(self, seeded_app, monkeypatch):
        """The whole policy-write path is dark until an operator opts in."""
        monkeypatch.delenv("AGNES_ACCESS_POLICIES_ENABLED", raising=False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="flag_off_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("flag_off_tbl"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policies_disabled" in resp.text

        # And the write never landed.
        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is None


@pytest.mark.journey
class TestInterlockCaseA:
    """§3.1 — attaching a policy to a table that is neither remote nor
    server_only."""

    def test_attach_rejected_on_a_distributed_table(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # query_mode='local', server_only defaults False -> distributed.
        table_id = _register(c, token, name="distributed_tbl")

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("distributed_tbl"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "set server_only=true first" in resp.text


@pytest.mark.journey
class TestInterlockCaseB:
    """§3.1 — clearing server_only, or moving query_mode to 'local', on a
    table that currently has a policy. Same validator as case A: the
    interlock is one shared check on the merged record, so it catches both
    directions."""

    def test_clearing_server_only_on_a_policied_table_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="attach_then_flip", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("attach_then_flip"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # A SEPARATE PUT that never mentions access_policy_sql at all —
        # exactly the "one toggle away from publishing the raw table" shape
        # the design doc calls out.
        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"server_only": False},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_requires_undistributed" in resp.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["server_only"] is True, "the refused write must not have partially landed"
        assert row["access_policy_sql"] is not None

    def test_moving_a_policied_remote_table_to_local_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="remote_policied", query_mode="remote")

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("remote_policied"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"query_mode": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_requires_undistributed" in resp.text


@pytest.mark.journey
class TestInterlockCaseC:
    """§3.2 — the physical-source twin: a different, distributable row
    resolving to the same physical source as a policied table."""

    def test_a_distributable_twin_of_a_policied_source_is_rejected(self, seeded_app, monkeypatch):
        """PUT-path defense-in-depth: ``TestRegisterTimeInterlock`` covers
        the twin being caught the moment IT is registered. This test
        instead registers the twin BEFORE its sibling has a policy
        attached — so register-time sees no conflict yet and the twin
        lands (as it must: two unpolicied rows sharing a source is not yet
        a leak) — and shows the very next write to that twin still gets
        caught. The interlock re-validates the merged shape on every write
        to a distributable row, independent of which fields that write
        touches, so a twin that predates its sibling's policy is not a
        permanent blind spot."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="invoices",
        )
        # Registered while orig_src carries no policy yet.
        twin_id = _register(
            c,
            token,
            name="twin_src",
            bucket="in.c-main",
            source_table="invoices",
        )

        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{twin_id}",
            json={"description": "an unrelated edit"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

    def test_a_twin_that_stays_server_only_is_not_rejected(self, seeded_app, monkeypatch):
        """The conflict is about DISTRIBUTABILITY, not mere physical-source
        overlap — two undistributed rows sharing a source is not a leak."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="orig_src2",
            server_only=True,
            bucket="in.c-main",
            source_table="orders",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("orig_src2"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        twin_id = _register(
            c,
            token,
            name="twin_src2",
            server_only=True,
            bucket="in.c-main",
            source_table="orders",
        )

        resp = c.put(
            f"/api/admin/registry/{twin_id}",
            json={"description": "an unrelated edit"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text


@pytest.mark.journey
class TestRegisterTimeInterlock:
    """§3.2 at register time — ``POST /api/admin/register-table`` must
    reject a brand-new distributable twin of an already-policied table's
    physical source by itself; it cannot rely on a follow-up PUT to catch
    it. Before this fix, ``register_table`` ran no physical-source check
    at all: a fresh registry row sharing a policied table's physical
    source landed unrejected whenever it was distributable (query_mode
    'local' or 'materialized', server_only left False) — and for a
    materialized row the next sync tick writes the raw, unfiltered rows to
    parquet with no follow-up PUT ever required.
    """

    def test_a_distributable_twin_is_rejected_at_register(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="reg_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="invoices",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("reg_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # The twin: SAME physical source, distributable (query_mode='local',
        # server_only omitted -> False). Must be rejected at the POST itself.
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "reg_twin_src",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-main",
                "source_table": "invoices",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        # And the row never landed in the registry.
        from src.repositories import table_registry_repo

        assert table_registry_repo().get("reg_twin_src") is None

    def test_a_materialized_twin_is_rejected_at_register(self, seeded_app, monkeypatch):
        """Same interlock via query_mode='materialized' — the mode the
        finding singled out, since a materialized row's next sync tick
        writes the raw rows to parquet with no further admin action."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="mat_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="shipments",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("mat_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "mat_twin_src",
                "source_type": "keboola",
                "query_mode": "materialized",
                "bucket": "in.c-main",
                "source_table": "shipments",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("mat_twin_src") is None

    def test_a_server_only_twin_is_allowed_at_register(self, seeded_app, monkeypatch):
        """The register-time check only blocks the DISTRIBUTABLE case —
        two undistributed rows sharing a physical source is not a leak."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="so_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="payments",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("so_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "so_twin_src",
                "source_type": "keboola",
                "query_mode": "local",
                "server_only": True,
                "bucket": "in.c-main",
                "source_table": "payments",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text


@pytest.mark.journey
class TestHappyPath:
    def test_policy_attaches_cleanly_to_a_server_only_table(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="clean_attach", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("clean_attach"),
                "access_policy_note": "restrict to the caller's cost center",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert "access_policy_sql" in resp.json()["updated"]

        listing = c.get("/api/admin/registry", headers=_auth(token))
        assert listing.status_code == 200, listing.text
        persisted = next(t for t in listing.json()["tables"] if t["id"] == table_id)
        assert persisted["access_policy_sql"] == _policy_sql("clean_attach")
        assert persisted["access_policy_note"] == "restrict to the caller's cost center"
        assert persisted["access_policy_updated_by"] == "admin@test.com"
        assert persisted["access_policy_updated_at"] is not None

        # Clearing works too, and needs no flag (safety valve).
        monkeypatch.delenv("AGNES_ACCESS_POLICIES_ENABLED", raising=False)
        clear = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert clear.status_code == 200, clear.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] is None
        assert row["access_policy_note"] is None
        assert row["access_policy_updated_by"] is None
