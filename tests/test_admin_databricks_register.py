"""Admin API contract for source_type='databricks' rows.

Two modes are registrable: 'materialized' (scheduler runs the SQL on the
warehouse and distributes a parquet, with the full-table SQL server-generated
when the admin supplies bucket+source_table only) and 'remote' (nothing syncs;
the analyst's statement ships to the warehouse per query). 'local' stays
rejected — no extractor would ever populate it.

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


def test_register_rejects_local_mode(seeded_app):
    """'local' would create a registry row nothing can ever populate: there is
    no Databricks extractor subprocess, only the materialize pass and the
    per-query warehouse path."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_payload(name="dbx_local", query_mode="local", source_query=None),
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    assert "materialized" in r.text


def test_register_remote_needs_bucket_and_source_table(seeded_app):
    """A remote row carries no SQL — bucket+source_table are what a bare
    reference to it gets rewritten into."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_payload(name="dbx_remote_bad", query_mode="remote", source_query=None, bucket=None),
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    assert "bucket" in r.text


def test_register_remote_row_stores_no_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_payload(
            name="dbx_remote",
            query_mode="remote",
            source_query=None,
            bucket="sales",
            source_table="orders_raw",
        ),
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), r.text
    listed = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in listed["tables"] if t["name"] == "dbx_remote")
    assert row["query_mode"] == "remote"
    assert not row.get("source_query")


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
