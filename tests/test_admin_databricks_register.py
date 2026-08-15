"""Admin API contract for source_type='databricks' rows (phase 1:
query_mode='materialized' only, server-generated full-table SQL).

Shares the seeded_app fixture; the freshly-bootstrapped test instance has
data_source.type='local', so _validate_source_type_configured stays
permissive (bootstrap-friendliness path) and no databricks config block is
needed to exercise the register-time shape rules."""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides):
    p = {
        "name": "dbx_orders",
        "source_type": "databricks",
        "query_mode": "materialized",
        "source_query": "SELECT o_date, MEASURE(`Total Revenue`) FROM `main`.`sales`.`kpis` GROUP BY o_date",
        "sync_schedule": "every 6h",
    }
    p.update(overrides)
    return p


def test_register_materialized_with_custom_sql(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json=_payload(), headers=_auth(token))
    assert r.status_code in (200, 201), r.text

    listed = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in listed["tables"] if t["name"] == "dbx_orders")
    assert row["source_type"] == "databricks"
    assert row["query_mode"] == "materialized"
    assert "MEASURE" in row["source_query"]


def test_register_rejects_non_materialized_modes(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    for mode in ("local", "remote"):
        r = c.post(
            "/api/admin/register-table",
            json=_payload(name=f"dbx_{mode}", query_mode=mode, source_query=None),
            headers=_auth(token),
        )
        assert r.status_code == 422, f"{mode}: {r.text}"
        assert "materialized" in r.text


def test_register_server_generates_full_table_sql_from_dotted_bucket(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_payload(name="dbx_full", source_query=None, bucket="main.sales", source_table="orders"),
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), r.text
    listed = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in listed["tables"] if t["name"] == "dbx_full")
    assert row["source_query"] == "SELECT * FROM `main`.`sales`.`orders`"


def test_register_without_sql_or_table_is_422_with_hint(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_payload(name="dbx_nothing", source_query=None),
        headers=_auth(token),
    )
    assert r.status_code == 422
    assert "bucket+source_table" in r.text


def test_register_plain_bucket_without_catalog_is_422_with_hint(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    # No data_source.databricks.catalog configured on the test instance and
    # the bucket is undotted — the route must say how to fix it.
    r = c.post(
        "/api/admin/register-table",
        json=_payload(name="dbx_nocat", source_query=None, bucket="sales", source_table="orders"),
        headers=_auth(token),
    )
    assert r.status_code == 422
    assert "catalog" in r.text


def test_update_cannot_flip_databricks_row_out_of_materialized(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json=_payload(name="dbx_upd"), headers=_auth(token))
    assert r.status_code in (200, 201), r.text
    row_id = r.json().get("id") or "dbx_upd"

    r = c.put(
        f"/api/admin/registry/{row_id}",
        json={"query_mode": "local"},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    assert "materialized" in r.text
