"""Snowflake table discovery — the browse half of "add a Snowflake table".

Snowflake was the only source type whose wizard step had no discovery: two
free-text inputs, nothing validated against the account, and the registry id
auto-composed as ``schema + "_" + table``. Paste a name that already carries
its schema prefix and you get ``gold_gold_bi_supply_demand`` pointing at a
table that does not exist — which then sits in the registry forever, because
nothing retries a remote-extract rebuild except a re-save.

These tests pin the primitive the picker is built on. The Snowflake round-trip
is stubbed: the live attach path is exercised by tests/test_snowflake_connector.py.
"""

from unittest.mock import MagicMock

import pytest

from connectors.snowflake import discovery

SF_SETTINGS = {
    "account": "acct-123",
    "user": "SVC",
    "database": "PROD",
    "warehouse": "WH",
    "role": "",
    "auth_type": "password",
    "password": "s3cret",
    "token_env": "SNOWFLAKE_PASSWORD",
}

_ROWS = [
    ("GOLD", "BI_DEMAND", "BASE TABLE"),
    ("GOLD", "BI_SUPPLY_AVAILABILITY", "BASE TABLE"),
    ("GOLD", "V_LATEST", "VIEW"),
    ("STAGING", "RAW_EVENTS", "BASE TABLE"),
]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(discovery, "resolve_snowflake_settings", lambda: SF_SETTINGS.copy())
    return SF_SETTINGS


@pytest.fixture
def fake_conn():
    """Stand-in for the DuckDB session the attach happens on."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = _ROWS
    return conn


def _patch_session(monkeypatch, conn, attach=None):
    monkeypatch.setattr(discovery, "_open_duckdb", lambda *a, **k: conn)
    monkeypatch.setattr(discovery, "attach_snowflake", attach or MagicMock())
    monkeypatch.setattr(discovery, "install_snowflake_adbc_driver", MagicMock())


def test_list_tables_groups_by_schema(configured, fake_conn, monkeypatch):
    _patch_session(monkeypatch, fake_conn)

    out = discovery.list_tables()

    assert out["database"] == "PROD"
    assert [s["name"] for s in out["schemas"]] == ["GOLD", "STAGING"]
    gold = out["schemas"][0]
    assert gold["tables"] == [
        {"name": "BI_DEMAND", "table_type": "BASE TABLE"},
        {"name": "BI_SUPPLY_AVAILABILITY", "table_type": "BASE TABLE"},
        {"name": "V_LATEST", "table_type": "VIEW"},
    ]


def test_list_tables_excludes_information_schema(configured, fake_conn, monkeypatch):
    """The account's own metadata schema is noise in a picker — and offering it
    for registration would produce rows nobody wants."""
    _patch_session(monkeypatch, fake_conn)

    discovery.list_tables()

    sql = " ".join(str(c) for c in fake_conn.execute.call_args_list)
    assert "INFORMATION_SCHEMA" in sql.upper()


def test_list_tables_returns_none_when_not_configured(monkeypatch):
    """No Snowflake block / no credential is a "nothing to browse" answer, not a
    crash — the caller turns it into a 400 with the setup hint."""
    monkeypatch.setattr(discovery, "resolve_snowflake_settings", lambda: None)

    assert discovery.list_tables() is None


def test_list_tables_refuses_host_outside_allowlist(configured, fake_conn, monkeypatch):
    """Same egress gate the extract build applies: the credential must not be
    shipped to a host the operator did not allow."""
    _patch_session(monkeypatch, fake_conn)
    monkeypatch.setattr(discovery, "is_attach_host_allowed", lambda url: False)

    with pytest.raises(ValueError, match="ALLOWLIST"):
        discovery.list_tables()


def test_list_tables_closes_session_on_failure(configured, monkeypatch):
    """A failed catalog query must not leak the DuckDB handle (and with it the
    attached Snowflake session)."""
    conn = MagicMock()
    conn.execute.side_effect = RuntimeError("boom")
    _patch_session(monkeypatch, conn)

    with pytest.raises(RuntimeError):
        discovery.list_tables()
    conn.close.assert_called_once()


# ---- the endpoint the picker calls ----------------------------------------

_LISTING = {
    "database": "PROD",
    "schemas": [{"name": "GOLD", "tables": [{"name": "BI_DEMAND", "table_type": "BASE TABLE"}]}],
}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_endpoint_returns_grouped_listing(seeded_app, monkeypatch):
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: _LISTING)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_type"] == "snowflake"
    assert body["database"] == "PROD"
    assert body["schemas"][0]["tables"][0]["name"] == "BI_DEMAND"


def test_endpoint_passes_schema_filter_through(seeded_app, monkeypatch):
    seen = {}

    def _listing(schema=None):
        seen["schema"] = schema
        return _LISTING

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _listing)
    c = seeded_app["client"]

    resp = c.get(
        "/api/admin/data-sources/snowflake/tables?schema=gold",
        headers=_auth(seeded_app["admin_token"]),
    )

    assert resp.status_code == 200, resp.text
    assert seen["schema"] == "gold"


def test_endpoint_requires_admin(seeded_app, monkeypatch):
    """Browsing an account's catalog is reconnaissance; it stays admin-only."""
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: _LISTING)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables")

    assert resp.status_code in (401, 403), resp.text


def test_endpoint_rejects_unbrowsable_source_type(seeded_app):
    """Keboola lists per connection — this route must not pretend to serve it."""
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/keboola/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "source-connections" in resp.json()["detail"]


def test_endpoint_explains_unconfigured_snowflake(seeded_app, monkeypatch):
    """`None` from the connector is a setup problem — say what to set, rather
    than answering 200 with an empty account."""
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: None)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "not configured" in resp.json()["detail"]


def test_endpoint_maps_driver_failure_to_502_not_empty(seeded_app, monkeypatch):
    """An empty listing would read as "the account has no tables" — the worse lie."""

    def _boom(schema=None):
        raise RuntimeError("ADBC driver not found")

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _boom)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 502, resp.text
    assert "ADBC driver not found" in resp.json()["detail"]


def test_endpoint_maps_allowlist_refusal_to_400(seeded_app, monkeypatch):
    """A host outside the egress allowlist is operator misconfiguration, not an
    upstream fault — 400, so the wizard does not offer a pointless retry."""

    def _refuse(schema=None):
        raise ValueError("host is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST")

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _refuse)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "ALLOWLIST" in resp.json()["detail"]
