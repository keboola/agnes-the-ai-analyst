"""sync_semantic_layer() orchestrator — mocked MetastoreClient/StorageClient,
real test DuckDB via the e2e_env fixture (same pattern as
tests/test_bq_metadata_refresh_endpoint.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _register_keboola_table(bucket: str, source_table: str, name: str):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name,
            name=name,
            source_type="keboola",
            bucket=bucket,
            source_table=source_table,
            query_mode="local",
        )
    finally:
        conn.close()


def _model_item(uuid="model-1", name="core"):
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _metric_item(name, sql, dataset, model_uuid="model-1"):
    return {
        "type": "semantic-metric",
        "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


class TestSyncSemanticLayer:
    def test_creates_metrics_from_metastore(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "ok"
        assert result["created_or_updated"] == 1
        assert result["skipped_unresolved_table"] == 0
        assert result["skipped_foreign_alias"] == 0

        row = metric_repo().get("keboola/model-1/total_revenue")
        assert row is not None
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert row["source"] == "keboola_semantic_layer"

    def test_prunes_metrics_removed_upstream(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()

        # First run: two metrics.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [
                _metric_item("a", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("b", "COUNT(*)", "in.c-example_source.orders"),
            ],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert metric_repo().get("keboola/model-1/a") is not None
        assert metric_repo().get("keboola/model-1/b") is not None

        # Second run: metric "b" removed upstream.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 1
        assert metric_repo().get("keboola/model-1/a") is not None
        assert metric_repo().get("keboola/model-1/b") is None

    def test_never_prunes_other_sources(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        metric_repo().create(
            id="manual/hand_authored",
            name="hand_authored",
            display_name="Hand Authored",
            category="manual",
            sql="SELECT 1",
            source="manual",
        )

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 0
        assert metric_repo().get("manual/hand_authored") is not None

    def test_metastore_fetch_error_returns_error_shape(self, e2e_env):
        """A Metastore 401/5xx/network failure aborts with a structured error
        instead of propagating an unhandled exception (500)."""
        from connectors.keboola.metastore_client import MetastoreApiError
        from connectors.keboola.semantic_layer import sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = MetastoreApiError("Metastore 503")

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "error"
        assert "Metastore fetch failed" in result["error"]

    def test_storage_preflight_error_returns_error_shape(self, e2e_env):
        """A Storage API outage during the master-token preflight aborts with a
        structured error, not an unhandled 500. MasterTokenRequiredError still
        propagates (config error → 400 at the endpoint)."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from connectors.keboola.storage_api import StorageApiError

        fake_storage = MagicMock()
        fake_storage.verify_token.side_effect = StorageApiError("Storage 503")
        fake_metastore = MagicMock()

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "error"
        assert "Storage API preflight failed" in result["error"]

    def test_empty_metrics_does_not_wipe_existing_rows(self, e2e_env):
        """A successful-but-empty metrics response (model still present) must
        NOT prune every previously-imported keboola_semantic_layer row — the
        safety valve mirrors the `if not models` guard."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()

        # First run: one metric imported.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert metric_repo().get("keboola/model-1/a") is not None

        # Second run: model still present, but zero metrics (upstream shape drift).
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 0
        assert metric_repo().get("keboola/model-1/a") is not None

    def test_skips_metric_with_unresolved_table(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [_metric_item("orphan", 'SUM("x")', "in.c-unregistered.table")],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_unresolved_table"] == 1
        assert metric_repo().get("keboola/model-1/orphan") is None

    def test_skips_metric_with_embedded_sql_comment(self, e2e_env):
        # Regression test for a bug found via live E2E verification
        # (2026-07-15): a real Keboola metric expression carried a trailing
        # `--` comment; naively composing `SELECT {expr} FROM ... AS t`
        # swallowed the FROM clause into the comment and broke the query.
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": [
                _metric_item(
                    "commented",
                    'ROUND("value" * 100, 2) -- FROM other_table (table not in this project)',
                    "in.c-example_source.orders",
                )
            ],
            "semantic-constraint": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_embedded_comment"] == 1
        assert result["created_or_updated"] == 0
        assert metric_repo().get("keboola/model-1/commented") is None

    def test_raises_master_token_required(self, e2e_env):
        from connectors.keboola.semantic_layer import MasterTokenRequiredError, sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": False}

        with patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage):
            with pytest.raises(MasterTokenRequiredError):
                sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="regular-tok")

    def test_missing_credentials_returns_error_status(self, e2e_env, monkeypatch):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

        result = sync_semantic_layer()

        assert result["status"] == "error"


def _glossary_item(term, definition, model_uuid="model-1"):
    return {
        "type": "semantic-glossary",
        "id": f"id-{term}",
        "attributes": {"term": term, "definition": definition, "seeAlso": [], "modelUUID": model_uuid},
    }


def _metastore_side_effect(glossary_items=None, metric_items=None):
    glossary_items = glossary_items or []
    metric_items = metric_items or []

    def _side_effect(item_type, model_uuid=None):
        return {
            "semantic-model": [_model_item()],
            "semantic-dataset": [],
            "semantic-metric": metric_items,
            "semantic-constraint": [],
            "semantic-glossary": glossary_items,
        }[item_type]

    return _side_effect


class TestSyncSemanticLayerGlossary:
    def test_creates_glossary_terms_from_metastore(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import glossary_repo

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            glossary_items=[_glossary_item("MRR", "Monthly recurring revenue.")]
        )

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "ok"
        assert result["glossary_created_or_updated"] == 1
        row = glossary_repo().get("keboola/model-1/mrr")
        assert row is not None
        assert row["definition"] == "Monthly recurring revenue."
        assert row["source"] == "keboola_semantic_layer"

    def test_prunes_glossary_terms_removed_upstream(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import glossary_repo

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()

        fake_metastore.list_items.side_effect = _metastore_side_effect(
            glossary_items=[_glossary_item("A", "def a"), _glossary_item("B", "def b")]
        )
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert glossary_repo().get("keboola/model-1/a") is not None
        assert glossary_repo().get("keboola/model-1/b") is not None

        fake_metastore.list_items.side_effect = _metastore_side_effect(glossary_items=[_glossary_item("A", "def a")])
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["glossary_pruned"] == 1
        assert glossary_repo().get("keboola/model-1/a") is not None
        assert glossary_repo().get("keboola/model-1/b") is None

    def test_never_prunes_manual_glossary_terms(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import glossary_repo

        glossary_repo().create(id="manual/hand_authored", term="Hand Authored", definition="d", source="manual")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(glossary_items=[])

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["glossary_pruned"] == 0
        assert glossary_repo().get("manual/hand_authored") is not None

    def test_skips_glossary_item_missing_term(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            glossary_items=[_glossary_item(None, "orphan definition")]
        )

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["glossary_created_or_updated"] == 0
        assert result["skipped_missing_term"] == 1

    def test_metric_import_behavior_unchanged_by_glossary_step(self, e2e_env):
        """Regression: adding the glossary step must not change a single
        existing metric-import assertion — same inputs, same metric outputs."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            metric_items=[_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            glossary_items=[_glossary_item("MRR", "def")],
        )

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["created_or_updated"] == 1
        assert result["glossary_created_or_updated"] == 1
        row = metric_repo().get("keboola/model-1/total_revenue")
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
