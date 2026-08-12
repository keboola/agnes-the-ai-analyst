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
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # A second row registered against the SAME physical source.
        # Registration itself (POST) carries no policy of its own yet, so
        # it is not gated — the very next PUT re-validates the merged shape
        # against every policied table's physical source.
        twin_id = _register(
            c,
            token,
            name="twin_src",
            bucket="in.c-main",
            source_table="invoices",
        )

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
