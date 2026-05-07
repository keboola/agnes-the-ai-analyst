"""Dispatch + identifier-validation gate for the source-agnostic
metadata providers."""

from app.api._metadata_models import MetadataRequest


def test_dispatcher_returns_bq_provider_for_bigquery():
    from app.api.v2_catalog import _metadata_provider_for
    from connectors.bigquery import metadata as bq_meta
    fn = _metadata_provider_for("bigquery")
    assert fn is bq_meta.fetch


def test_dispatcher_returns_keboola_provider_for_keboola():
    from app.api.v2_catalog import _metadata_provider_for
    from connectors.keboola import metadata as kb_meta
    fn = _metadata_provider_for("keboola")
    assert fn is kb_meta.fetch


def test_dispatcher_returns_none_for_unknown_source():
    from app.api.v2_catalog import _metadata_provider_for
    assert _metadata_provider_for("jira") is None
    assert _metadata_provider_for("") is None
    assert _metadata_provider_for("snowflake") is None


def test_build_metadata_request_for_valid_row():
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "orders",
        "bucket": "dwh_base",
        "source_table": "orders_2024",
    })
    assert isinstance(req, MetadataRequest)
    assert req.table_id == "orders"
    assert req.bucket == "dwh_base"
    assert req.source_table == "orders_2024"


def test_build_metadata_request_rejects_unsafe_bucket():
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "x",
        "bucket": "evil`; DROP--",
        "source_table": "t",
    })
    assert req is None


def test_build_metadata_request_falls_back_to_id_when_source_table_missing():
    """Some legacy Keboola registry rows have empty source_table; the row id
    is the table name in that case (mirrors v2_schema:168 behavior)."""
    from app.api.v2_catalog import _build_metadata_request
    req = _build_metadata_request({
        "id": "orders",
        "bucket": "in.c-crm",
        "source_table": "",
    })
    assert req is not None
    assert req.source_table == "orders"


def test_stub_providers_return_none():
    """Providers don't have their real bodies yet — stubs return None
    so the catalog endpoint stays 200 while we wire the rest."""
    from connectors.bigquery import metadata as bq_meta
    from connectors.keboola import metadata as kb_meta
    req = MetadataRequest(table_id="x", bucket="b", source_table="t")
    assert bq_meta.fetch(req) is None
    assert kb_meta.fetch(req) is None
