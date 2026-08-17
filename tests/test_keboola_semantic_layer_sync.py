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


def _dataset_item(table_id="in.c-example_source.orders", name="orders", model_uuid="model-1"):
    return {
        "type": "semantic-dataset",
        "id": f"ds-{table_id}",
        "attributes": {"name": name, "tableId": table_id, "modelUUID": model_uuid},
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
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
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

        row = metric_repo().get("keboola_metastore/_/core/total_revenue")
        assert row is not None
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert row["source"] == "keboola_metastore"

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
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [
                _metric_item("a", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("b", "COUNT(*)", "in.c-example_source.orders"),
            ],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert metric_repo().get("keboola_metastore/_/core/a") is not None
        assert metric_repo().get("keboola_metastore/_/core/b") is not None

        # Second run: metric "b" removed upstream.
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 1
        assert metric_repo().get("keboola_metastore/_/core/a") is not None
        assert metric_repo().get("keboola_metastore/_/core/b") is None

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
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-relationship": [],
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
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")
        assert metric_repo().get("keboola_metastore/_/core/a") is not None

        # Second run: model still present, but zero metrics (upstream shape drift).
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["pruned"] == 0
        assert metric_repo().get("keboola_metastore/_/core/a") is not None

    def test_skips_metric_with_unresolved_table(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("orphan", 'SUM("x")', "in.c-unregistered.table")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_unresolved_table"] == 1
        assert metric_repo().get("keboola_metastore/_/core/orphan") is None

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
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [
                _metric_item(
                    "commented",
                    'ROUND("value" * 100, 2) -- FROM other_table (table not in this project)',
                    "in.c-example_source.orders",
                )
            ],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        # The fine-grained skip counters are retired by the flat-table cutover
        # (always 0); the skip is now visible only as the row's absence.
        assert result["skipped_embedded_comment"] == 0
        assert result["created_or_updated"] == 0
        assert metric_repo().get("keboola_metastore/_/core/commented") is None

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


def _seed_column_metadata(table_id: str, column_names: list[str]):
    from src.db import get_system_db
    from src.repositories.column_metadata import ColumnMetadataRepository

    conn = get_system_db()
    try:
        repo = ColumnMetadataRepository(conn)
        for col in column_names:
            repo.save(table_id=table_id, column_name=col, basetype="VARCHAR")
    finally:
        conn.close()


def _relationship_item(name, from_id, to_id, on, rel_type="left", model_uuid="model-1"):
    return {
        "type": "semantic-relationship",
        "id": f"id-{name}",
        "attributes": {"name": name, "from": from_id, "to": to_id, "on": on, "type": rel_type, "modelUUID": model_uuid},
    }


def _glossary_item(term, definition, model_uuid="model-1"):
    return {
        "type": "semantic-glossary",
        "id": f"id-{term}",
        "attributes": {"term": term, "definition": definition, "seeAlso": [], "modelUUID": model_uuid},
    }


def _metastore_side_effect(glossary_items=None, metric_items=None, relationship_items=None):
    glossary_items = glossary_items or []
    metric_items = metric_items or []
    relationship_items = relationship_items or []

    def _side_effect(item_type, model_uuid=None):
        return {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": metric_items,
            "semantic-constraint": [],
            "semantic-relationship": relationship_items,
            "semantic-glossary": glossary_items,
        }[item_type]

    return _side_effect


class TestSyncSemanticLayerRelationships:
    def test_resolves_relationship_metric_end_to_end(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-a", "activities", "crm_activities")
        _register_keboola_table("in.c-a", "opportunities", "crm_opportunities")
        _seed_column_metadata("crm_activities", ["opportunity_id", "created_at"])
        _seed_column_metadata("crm_opportunities", ["id", "amount"])

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")],
            "semantic-constraint": [],
            "semantic-relationship": [
                _relationship_item("o_to_a", "in.c-a.opportunities", "in.c-a.activities", 'o."id" = a."opportunity_id"')
            ],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["created_or_updated"] == 1
        assert result["skipped_foreign_alias"] == 0
        row = metric_repo().get("keboola_metastore/_/core/linked_amount")
        assert row is not None
        assert row["tables"] == ["crm_activities", "crm_opportunities"]
        assert 'LEFT JOIN "crm_opportunities" AS j' in row["sql"]

    def test_ambiguous_relationship_is_skipped(self, e2e_env):
        """Since the flat-table cutover the fine-grained skip counters
        (skipped_ambiguous_relationship etc.) are no longer computed — always
        0 — so an ambiguously-related metric's skip is visible only as the
        row's absence and a flat created_or_updated count."""
        from connectors.keboola.semantic_layer import sync_semantic_layer

        _register_keboola_table("in.c-a", "activities", "crm_activities")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")],
            "semantic-constraint": [],
            "semantic-relationship": [],  # no relationship touches this dataset
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_ambiguous_relationship"] == 0
        assert result["skipped_foreign_alias"] == 0
        assert result["created_or_updated"] == 0
        from src.repositories import metric_repo

        assert metric_repo().get("keboola_metastore/_/core/linked_amount") is None

    def test_unverified_direction_is_skipped(self, e2e_env):
        """Same retirement as the ambiguous-relationship case above."""
        from connectors.keboola.semantic_layer import sync_semantic_layer

        _register_keboola_table("in.c-a", "activities", "crm_activities")
        _register_keboola_table("in.c-a", "opportunities", "crm_opportunities")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            # metric's own dataset (opportunities) is on the relationship's
            # "from" side — the unverified direction.
            "semantic-metric": [_metric_item("linked_amount", 'SUM(a."amount")', "in.c-a.opportunities")],
            "semantic-constraint": [],
            "semantic-relationship": [
                _relationship_item("o_to_a", "in.c-a.opportunities", "in.c-a.activities", 'o."id" = a."opportunity_id"')
            ],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["skipped_unverified_relationship_direction"] == 0
        assert result["created_or_updated"] == 0
        from src.repositories import metric_repo

        assert metric_repo().get("keboola_metastore/_/core/linked_amount") is None

    def test_single_table_metrics_unaffected_by_relationship_step(self, e2e_env):
        """Regression: adding the relationship step must not change a
        single existing single-table-metric assertion."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["created_or_updated"] == 1
        row = metric_repo().get("keboola_metastore/_/core/total_revenue")
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert "tables" not in row or row["tables"] is None


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
        row = glossary_repo().get("keboola_metastore/_/core/mrr")
        assert row is not None
        assert row["definition"] == "Monthly recurring revenue."
        assert row["source"] == "keboola_metastore"

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
        assert glossary_repo().get("keboola_metastore/_/core/a") is not None
        assert glossary_repo().get("keboola_metastore/_/core/b") is not None

        fake_metastore.list_items.side_effect = _metastore_side_effect(glossary_items=[_glossary_item("A", "def a")])
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["glossary_pruned"] == 1
        assert glossary_repo().get("keboola_metastore/_/core/a") is not None
        assert glossary_repo().get("keboola_metastore/_/core/b") is None

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
        # skipped_missing_term is a retired fine-grained skip counter (always
        # 0 since the cutover) — the skip is visible only as the zero count
        # above (composition drops the term before projection ever sees it).
        assert result["skipped_missing_term"] == 0

    def test_imports_multiple_terms_with_single_fts_rebuild(self, e2e_env):
        """Regression: importing N>1 glossary terms in one sync must rebuild
        the BM25 FTS index once (after the batch), not once per term — a
        per-row rebuild is an O(N^2) `PRAGMA create_fts_index` + CHECKPOINT
        storm against the shared system DB connection. Re-added post-cutover:
        `src.semantic.projection.project_document` is now the writer and must
        carry the same batching contract the retired flat composer had —
        `glossary_repo().create(..., refresh_fts=False)` per term, one
        `refresh_search_index()` after the whole batch (writes + prune)."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories.glossary import GlossaryRepository

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            glossary_items=[
                _glossary_item("A", "def a"),
                _glossary_item("B", "def b"),
                _glossary_item("C", "def c"),
            ]
        )

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
            patch.object(GlossaryRepository, "_refresh_fts_index") as mock_refresh,
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["glossary_created_or_updated"] == 3
        mock_refresh.assert_called_once()

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
        row = metric_repo().get("keboola_metastore/_/core/total_revenue")
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'


# ---------------------------------------------------------------------------
# Multi-source sync (per-connection master tokens, source_ref provenance)
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())


def _make_master_connection(conn_id: str, *, stack_url: str, token: str, is_default: bool = False) -> str:
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo, source_connections_repo

    source_connections_repo().create(
        id=conn_id,
        name=f"name-{conn_id}",
        source_type="keboola",
        config={"stack_url": stack_url},
        is_default=is_default,
        created_by="test",
    )
    connection_secrets_repo().upsert(master_secret_key(conn_id), token)
    return conn_id


def _fake_clients(projects: dict):
    """Build (storage_factory, metastore_factory) patch targets keyed by token.

    ``projects`` maps token -> {"owner_id", "model_uuid", "metrics", "glossary"}.
    """

    def storage_factory(url, token):
        client = MagicMock()
        client.verify_token.return_value = {
            "isMasterToken": True,
            "owner": {"id": projects[token]["owner_id"]},
        }
        return client

    def metastore_factory(url, token):
        project = projects[token]
        client = MagicMock()
        if project.get("explode"):
            # An unforeseen failure (client bug / unexpected upstream shape) —
            # NOT one of the MetastoreApiError/RequestException types the
            # per-source body converts into a structured error.
            client.list_items.side_effect = RuntimeError("upstream client exploded")
            return client
        client.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item(project["model_uuid"])],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": project.get("metrics", []),
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": project.get("glossary", []),
        }[item_type]
        return client

    return storage_factory, metastore_factory


def _run_sync(projects: dict):
    from connectors.keboola.semantic_layer import sync_semantic_layer

    storage_factory, metastore_factory = _fake_clients(projects)
    with (
        patch("connectors.keboola.storage_api.KeboolaStorageClient", side_effect=storage_factory),
        patch("connectors.keboola.metastore_client.MetastoreClient", side_effect=metastore_factory),
    ):
        return sync_semantic_layer()


class TestSyncSemanticLayerMultiSource:
    @pytest.fixture(autouse=True)
    def _no_legacy_env(self, monkeypatch):
        """These tests resolve credentials from connections, never the legacy
        env pair — clear it so an ambient value can't shadow the source loop."""
        monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
        monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)

    def test_multi_source_syncs_all_master_connections(self, e2e_env, vault_key):
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        conn_a = _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        conn_b = _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        result = _run_sync(
            {
                "tok-a": {
                    "owner_id": 111,
                    "model_uuid": "model-a",
                    "metrics": [_metric_item("a1", 'SUM("amount")', "in.c-example_source.orders")],
                },
                "tok-b": {
                    "owner_id": 222,
                    "model_uuid": "model-b",
                    "metrics": [_metric_item("b1", "COUNT(*)", "in.c-example_source.orders")],
                },
            }
        )

        assert result["status"] == "ok"
        assert result["created_or_updated"] == 2
        assert [s["connection_id"] for s in result["sources"]] == [conn_a, conn_b]
        assert all(s["status"] == "ok" for s in result["sources"])

        row_a = metric_repo().get("keboola_metastore/conn-a/core/a1")
        row_b = metric_repo().get("keboola_metastore/conn-b/core/b1")
        assert row_a["source_ref"] == conn_a
        assert row_b["source_ref"] == conn_b

    def test_prune_is_scoped_per_source(self, e2e_env, vault_key):
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        first = {
            "tok-a": {
                "owner_id": 111,
                "model_uuid": "model-a",
                "metrics": [
                    _metric_item("a1", 'SUM("amount")', "in.c-example_source.orders"),
                    _metric_item("a2", "COUNT(*)", "in.c-example_source.orders"),
                ],
            },
            "tok-b": {
                "owner_id": 222,
                "model_uuid": "model-b",
                "metrics": [_metric_item("b1", "COUNT(*)", "in.c-example_source.orders")],
            },
        }
        _run_sync(first)
        assert metric_repo().get("keboola_metastore/conn-a/core/a2") is not None

        # Source A drops one metric upstream; source B is unchanged.
        second = {**first, "tok-a": {**first["tok-a"], "metrics": first["tok-a"]["metrics"][:1]}}
        result = _run_sync(second)

        assert result["pruned"] == 1
        assert metric_repo().get("keboola_metastore/conn-a/core/a1") is not None
        assert metric_repo().get("keboola_metastore/conn-a/core/a2") is None
        assert metric_repo().get("keboola_metastore/conn-b/core/b1") is not None

    def test_null_ref_rows_are_purged_by_the_default_connection(self, e2e_env, vault_key):
        """NULL-ref rows predate per-connection provenance and are the
        retired `keboola_semantic_layer` source's — the default connection's
        one-time purge (gated on a successful sync) covers them, same as any
        other in-scope row of that source. There is no more "adoption": the
        Keboola metric with the same name lands fresh under the projector's
        own scoped id, coexisting until the purge removes the legacy row."""
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        conn_a = _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)

        # Legacy rows from a pre-multi-source sync: source_ref IS NULL.
        metric_repo().create(
            id="keboola/model-a/legacy",
            name="legacy",
            display_name="legacy",
            category="keboola",
            sql='SELECT 1 FROM "crm_orders" AS t',
            source="keboola_semantic_layer",
        )
        metric_repo().create(
            id="keboola/model-a/gone_upstream",
            name="gone_upstream",
            display_name="gone_upstream",
            category="keboola",
            sql='SELECT 1 FROM "crm_orders" AS t',
            source="keboola_semantic_layer",
        )

        result = _run_sync(
            {
                "tok-a": {
                    "owner_id": 111,
                    "model_uuid": "model-a",
                    "metrics": [_metric_item("legacy", 'SUM("amount")', "in.c-example_source.orders")],
                }
            }
        )

        fresh = metric_repo().get("keboola_metastore/conn-a/core/legacy")
        assert fresh is not None
        assert fresh["source_ref"] == conn_a
        assert fresh["source"] == "keboola_metastore"
        # Both NULL-ref legacy rows are inside the default connection's purge
        # scope and are removed unconditionally once the sync writes rows —
        # regardless of whether their own name still exists upstream.
        assert result["pruned"] == 2
        assert metric_repo().get("keboola/model-a/legacy") is None
        assert metric_repo().get("keboola/model-a/gone_upstream") is None

    def test_null_ref_glossary_rows_are_also_purged_by_the_default_connection(self, e2e_env, vault_key):
        """Symmetric to `test_null_ref_rows_are_purged_by_the_default_connection`
        on the metric side: a legacy `source="keboola_semantic_layer"`
        glossary row must not survive as a permanent duplicate of its
        freshly-projected `keboola_metastore` twin. Gated on
        `glossary_written` (not `metrics_written`): this sync's project
        publishes a glossary term, so the purge is allowed to run."""
        from src.repositories import glossary_repo

        _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)

        glossary_repo().create(
            id="keboola/model-a/legacy_term",
            term="legacy_term",
            definition="a pre-multi-source glossary row",
            source="keboola_semantic_layer",
        )

        result = _run_sync(
            {
                "tok-a": {
                    "owner_id": 111,
                    "model_uuid": "model-a",
                    "metrics": [],
                    "glossary": [_glossary_item("mrr", "Monthly recurring revenue.")],
                }
            }
        )

        assert result["glossary_created_or_updated"] == 1
        assert result["glossary_pruned"] == 1
        assert glossary_repo().get("keboola_metastore/conn-a/core/mrr") is not None
        assert glossary_repo().get("keboola/model-a/legacy_term") is None

    def test_safety_valve_scoped_per_source(self, e2e_env, vault_key):
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        first = {
            "tok-a": {
                "owner_id": 111,
                "model_uuid": "model-a",
                "metrics": [_metric_item("a1", 'SUM("amount")', "in.c-example_source.orders")],
            },
            "tok-b": {
                "owner_id": 222,
                "model_uuid": "model-b",
                "metrics": [
                    _metric_item("b1", "COUNT(*)", "in.c-example_source.orders"),
                    _metric_item("b2", "COUNT(*)", "in.c-example_source.orders"),
                ],
            },
        }
        _run_sync(first)

        # A returns zero usable metrics (its prune must be skipped) while B
        # legitimately drops one metric (its prune must still run).
        second = {
            "tok-a": {**first["tok-a"], "metrics": []},
            "tok-b": {**first["tok-b"], "metrics": first["tok-b"]["metrics"][:1]},
        }
        result = _run_sync(second)

        assert metric_repo().get("keboola_metastore/conn-a/core/a1") is not None
        assert metric_repo().get("keboola_metastore/conn-b/core/b2") is None
        assert result["pruned"] == 1

    def test_name_shared_by_two_connections_coexists(self, e2e_env, vault_key, monkeypatch):
        """Since the flat-table cutover there is no name-ownership gate: two
        connections publishing a metric of the same name each land their own
        row under a scoped id — coexistence, not a sticky first claim."""
        from connectors.keboola import semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        conn_a = _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        conn_b = _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        projects = {
            "tok-a": {
                "owner_id": 111,
                "model_uuid": "model-a",
                "metrics": [_metric_item("shared", 'SUM("amount")', "in.c-example_source.orders")],
            },
            "tok-b": {
                "owner_id": 222,
                "model_uuid": "model-b",
                "metrics": [_metric_item("shared", "COUNT(*)", "in.c-example_source.orders")],
            },
        }
        result = _run_sync(projects)

        assert result["created_or_updated"] == 2
        assert result["skipped_conflict"] == 0
        assert metric_repo().get("keboola_metastore/conn-a/core/shared")["source_ref"] == conn_a
        assert metric_repo().get("keboola_metastore/conn-b/core/shared")["source_ref"] == conn_b

        # Discovery order no longer matters — there is no first-claim gate to flip.
        original = semantic_layer._enumerate_master_sources
        monkeypatch.setattr(
            semantic_layer,
            "_enumerate_master_sources",
            lambda: list(reversed(original())),
        )
        result = _run_sync(projects)

        assert result["skipped_conflict"] == 0
        assert metric_repo().get("keboola_metastore/conn-a/core/shared")["source_ref"] == conn_a
        assert metric_repo().get("keboola_metastore/conn-b/core/shared")["source_ref"] == conn_b

    def test_glossary_term_shared_by_two_connections_coexists(self, e2e_env, vault_key, monkeypatch):
        """Glossary mirror of the metric coexistence case above: two projects
        defining "MRR" both land their own scoped row."""
        from connectors.keboola import semantic_layer
        from src.repositories import glossary_repo

        conn_a = _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        conn_b = _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        projects = {
            "tok-a": {
                "owner_id": 111,
                "model_uuid": "model-a",
                "glossary": [_glossary_item("MRR", "Monthly recurring revenue (project A).")],
            },
            "tok-b": {
                "owner_id": 222,
                "model_uuid": "model-b",
                "glossary": [_glossary_item("MRR", "Monthly recurring revenue (project B).")],
            },
        }
        result = _run_sync(projects)

        assert result["glossary_created_or_updated"] == 2
        assert result["skipped_conflict"] == 0
        assert glossary_repo().get("keboola_metastore/conn-a/core/mrr")["source_ref"] == conn_a
        assert glossary_repo().get("keboola_metastore/conn-b/core/mrr")["source_ref"] == conn_b

        # Discovery order no longer matters.
        original = semantic_layer._enumerate_master_sources
        monkeypatch.setattr(
            semantic_layer,
            "_enumerate_master_sources",
            lambda: list(reversed(original())),
        )
        result = _run_sync(projects)

        assert result["skipped_conflict"] == 0
        assert result["glossary_pruned"] == 0
        row_a = glossary_repo().get("keboola_metastore/conn-a/core/mrr")
        assert row_a["source_ref"] == conn_a
        assert row_a["definition"] == "Monthly recurring revenue (project A)."

    # (removed: test_conflict_skip_does_not_prune_the_sources_own_row — tested
    # the retired name-ownership gate's `find_by_name` interaction with the
    # sticky-claim/skip path. There is no more conflict-skip to retain a row
    # against: every source's row lives at its own scoped id and is pruned
    # only within its own (source, source_ref) scope, so a same-named foreign
    # row can no longer affect it. Covered by test_prune_is_scoped_per_source.)

    # (removed: test_conflict_skip_does_not_prune_the_sources_own_glossary_row
    # — glossary mirror of the metric-side test above, retired for the same
    # reason: no more conflict-skip / find_by_name interaction to protect a
    # row against. Covered by test_glossary_term_shared_by_two_connections_coexists.)

    def test_unexpected_source_error_is_isolated(self, e2e_env, vault_key):
        """An exception that is not MasterTokenRequiredError (and not one of
        the API error types the per-source body handles) must be recorded
        against that source, not abort the sources after it."""
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _make_master_connection("conn-a", stack_url="https://a.keboola.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://b.keboola.com", token="tok-b")

        result = _run_sync(
            {
                "tok-a": {"owner_id": 111, "model_uuid": "model-a", "explode": True},
                "tok-b": {
                    "owner_id": 222,
                    "model_uuid": "model-b",
                    "metrics": [_metric_item("b1", "COUNT(*)", "in.c-example_source.orders")],
                },
            }
        )

        assert result["status"] == "ok"
        assert result["created_or_updated"] == 1
        by_conn = {s["connection_id"]: s for s in result["sources"]}
        assert by_conn["conn-a"]["status"] == "error"
        assert "exploded" in by_conn["conn-a"]["error"]
        assert by_conn["conn-b"]["status"] == "ok"
        assert metric_repo().get("keboola_metastore/conn-b/core/b1") is not None

    def test_duplicate_project_deduped(self, e2e_env, vault_key):
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        conn_a = _make_master_connection("conn-a", stack_url="https://dup.keboola.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://dup.keboola.com", token="tok-b")

        # Both connections point at the SAME upstream project (same host, same
        # token owner id) — the second must be skipped, not re-stamp the rows.
        projects = {
            "tok-a": {
                "owner_id": 777,
                "model_uuid": "model-dup",
                "metrics": [_metric_item("dup_metric", 'SUM("amount")', "in.c-example_source.orders")],
            },
            "tok-b": {
                "owner_id": 777,
                "model_uuid": "model-dup",
                "metrics": [_metric_item("dup_metric", 'SUM("amount")', "in.c-example_source.orders")],
            },
        }
        result = _run_sync(projects)

        assert result["skipped_duplicate_project"] == 1
        assert result["created_or_updated"] == 1
        assert metric_repo().get("keboola_metastore/conn-a/core/dup_metric")["source_ref"] == conn_a
        by_conn = {s["connection_id"]: s for s in result["sources"]}
        assert by_conn["conn-b"]["status"] == "skipped"
        assert by_conn["conn-b"]["skipped_duplicate_project"] == 1

        # Second run: still no flip-flop, and nothing gets pruned.
        result = _run_sync(projects)
        assert result["pruned"] == 0
        assert metric_repo().get("keboola_metastore/conn-a/core/dup_metric")["source_ref"] == conn_a

    def test_missing_owner_id_is_not_deduped(self, e2e_env, vault_key):
        """Two distinct upstream projects on the same host whose verify_token
        response carries no owner id must NOT collide on a shared (host, None)
        identity — that would silently skip the second one as a spurious
        "duplicate project" even though it's an unrelated project. Both
        sources must sync."""
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        _make_master_connection("conn-a", stack_url="https://dup.keboola.com", token="tok-a", is_default=True)
        _make_master_connection("conn-b", stack_url="https://dup.keboola.com", token="tok-b")

        # Same host as test_duplicate_project_deduped, but NEITHER verify_token
        # response carries an owner id — no reliable identity to dedupe on.
        projects = {
            "tok-a": {
                "owner_id": None,
                "model_uuid": "model-a",
                "metrics": [
                    _metric_item("a_metric", 'SUM("amount")', "in.c-example_source.orders", model_uuid="model-a")
                ],
            },
            "tok-b": {
                "owner_id": None,
                "model_uuid": "model-b",
                "metrics": [
                    _metric_item("b_metric", 'SUM("amount")', "in.c-example_source.orders", model_uuid="model-b")
                ],
            },
        }
        result = _run_sync(projects)

        assert result["skipped_duplicate_project"] == 0
        by_conn = {s["connection_id"]: s for s in result["sources"]}
        assert by_conn["conn-a"]["status"] == "ok"
        assert by_conn["conn-b"]["status"] == "ok"
        assert by_conn["conn-a"]["skipped_duplicate_project"] == 0
        assert by_conn["conn-b"]["skipped_duplicate_project"] == 0
        assert metric_repo().get("keboola_metastore/conn-a/core/a_metric") is not None
        assert metric_repo().get("keboola_metastore/conn-b/core/b_metric") is not None

    def test_fallback_no_master_tokens_preserves_foreign_rows(self, e2e_env, monkeypatch):
        """No master tokens at all: the legacy single-source path runs, and
        its prune scope covers only NULL-ref / default-connection rows —
        rows stamped by another connection are left orphaned-but-intact."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.keboola.com")
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "legacy-tok")

        metric_repo().create(
            id="keboola/model-x/foreign",
            name="foreign",
            display_name="foreign",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="other-conn",
        )
        metric_repo().create(
            id="keboola/model-1/stale",
            name="stale",
            display_name="stale",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
        )

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True, "owner": {"id": 1}}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            metric_items=[_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")]
        )

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer()

        assert result["status"] == "ok"
        assert result["pruned"] == 1
        assert metric_repo().get("keboola/model-1/stale") is None
        assert metric_repo().get("keboola/model-x/foreign") is not None
        assert metric_repo().get("keboola_metastore/_/core/a") is not None

    def _run_fallback_sync(self, metric_name: str = "a"):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True, "owner": {"id": 1}}
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = _metastore_side_effect(
            metric_items=[_metric_item(metric_name, 'SUM("amount")', "in.c-example_source.orders")]
        )
        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            return sync_semantic_layer()

    def test_fallback_env_credentials_do_not_claim_the_default_connection(self, e2e_env, vault_key, monkeypatch):
        """Legacy env pair + a default connection present: the env pair has no
        connection identity, so rows are stamped NULL (spec §3 — the default
        connection id is stamped only when the credentials came FROM that
        connection). The prune scope still covers NULL *or* the default id."""
        from src.repositories import connection_secrets_repo, metric_repo, source_connections_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source_connections_repo().create(
            id="conn-default",
            name="default-conn",
            source_type="keboola",
            config={"stack_url": "https://named.keboola.com"},
            is_default=True,
            created_by="test",
        )
        connection_secrets_repo().upsert("conn-default", "plain-token")  # regular slot, NOT master
        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.keboola.com")
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "legacy-tok")

        # Stale row previously owned by the default connection, plus another
        # connection's row that must survive.
        metric_repo().create(
            id="keboola/model-1/stale_default",
            name="stale_default",
            display_name="stale_default",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-default",
        )
        metric_repo().create(
            id="keboola/model-x/foreign",
            name="foreign",
            display_name="foreign",
            category="keboola",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="other-conn",
        )

        result = self._run_fallback_sync()

        assert result["status"] == "ok"
        assert metric_repo().get("keboola_metastore/_/core/a")["source_ref"] is None
        # Prune scope = NULL or default connection id.
        assert result["pruned"] == 1
        assert metric_repo().get("keboola/model-1/stale_default") is None
        assert metric_repo().get("keboola/model-x/foreign") is not None

    def test_fallback_connection_credentials_stamp_the_default_connection(self, e2e_env, vault_key, monkeypatch):
        """Same fallback path, but the credentials came from the default
        connection itself — those rows DO carry its id."""
        from src.repositories import connection_secrets_repo, metric_repo, source_connections_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source_connections_repo().create(
            id="conn-default",
            name="default-conn",
            source_type="keboola",
            config={"stack_url": "https://named.keboola.com"},
            is_default=True,
            created_by="test",
        )
        connection_secrets_repo().upsert("conn-default", "plain-token")  # regular slot, NOT master

        result = self._run_fallback_sync()

        assert result["status"] == "ok"
        assert metric_repo().get("keboola_metastore/conn-default/core/a")["source_ref"] == "conn-default"
        assert [s["connection_id"] for s in result["sources"]] == ["conn-default"]


def test_metric_definitions_has_source_ref_column(tmp_path):
    """v107: metric_definitions + glossary_terms grow a nullable
    ``source_ref`` column — per-connection provenance for the multi-project
    semantic-layer sync (2026-07-28 spec)."""
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('metric_definitions')").fetchall()}
    gcols = {r[1] for r in conn.execute("PRAGMA table_info('glossary_terms')").fetchall()}
    conn.close()
    assert "source_ref" in cols
    assert "source_ref" in gcols


def _glossary_item(term, definition="A definition."):
    return {
        "type": "semantic-glossary",
        "id": f"g-{term}",
        "attributes": {"term": term, "definition": definition},
    }


def _multi_model_metastore(models, per_model):
    """Fake MetastoreClient whose per-type lists are keyed by model uuid.

    ``per_model`` is ``{model_uuid: {item_type: [...]}}``; anything absent
    reads as an empty list, so a test spells out only the types it cares
    about. The single-model fakes elsewhere in this file ignore the
    ``model_uuid`` argument entirely — which is precisely what could not
    catch a sync that only ever looks at one model.
    """
    fake = MagicMock()

    def _list_items(item_type, model_uuid=None):
        if item_type == "semantic-model":
            return models
        return (per_model.get(model_uuid) or {}).get(item_type, [])

    fake.list_items.side_effect = _list_items
    return fake


class TestMultiModelSync:
    """A project may expose more than one semantic model — the normal case
    once a model shared from another project is linked into the consumer's
    Metastore (keboola/ui#7739 + the `targeted` scope in
    keboola/go-monorepo#571). Every model must be imported, and one model's
    prune pass must never reach another model's rows."""

    def _run(self, models, per_model):
        from connectors.keboola.semantic_layer import sync_semantic_layer

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = _multi_model_metastore(models, per_model)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            return sync_semantic_layer(
                keboola_url="https://connection.keboola.com",
                keboola_token="master-tok",
            )

    def test_imports_metrics_from_every_model(self, e2e_env):
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        result = self._run(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                    "semantic-metric": [
                        _metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                    "semantic-metric": [
                        _metric_item("orders_count", "COUNT(*)", "in.c-example_source.orders", "model-2")
                    ],
                },
            },
        )

        assert result["status"] == "ok"
        assert result["created_or_updated"] == 2
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None

    def test_prune_spares_the_other_models_rows(self, e2e_env):
        """The regression that a per-model prune would cause: model-2's pass
        must not delete model-1's rows just because they are absent from
        model-2's metric list."""
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        models = [_model_item("model-1", "core"), _model_item("model-2", "shared")]
        self._run(
            models,
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                    "semantic-metric": [
                        _metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                    "semantic-metric": [
                        _metric_item("orders_count", "COUNT(*)", "in.c-example_source.orders", "model-2"),
                        _metric_item("aov", 'AVG("amount")', "in.c-example_source.orders", "model-2"),
                    ],
                },
            },
        )
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/aov") is not None

        # Second run: model-2 dropped "aov". Only that row may go.
        result = self._run(
            models,
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                    "semantic-metric": [
                        _metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                    "semantic-metric": [
                        _metric_item("orders_count", "COUNT(*)", "in.c-example_source.orders", "model-2")
                    ],
                },
            },
        )

        assert result["pruned"] == 1
        assert metric_repo().get("keboola_metastore/_/shared/aov") is None
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None

    def test_partial_composition_does_not_prune_the_dropped_models_rows(self, e2e_env):
        """`_store_ossie_documents` logs-and-drops any single composed
        document that fails Ossie schema validation. When that happens the
        merged model list handed to `project_document` this pass is a PARTIAL
        view of what upstream actually publishes — pruning against it would
        delete the dropped model's OWN previously-written rows, which
        upstream never asked to have removed. Simulates the drop by forcing
        `validate_document` to reject the "shared" model's composed document
        on the second sync, while "core"'s stays real and unaffected."""
        from src.repositories import metric_repo
        from src.semantic import document_validation

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        models = [_model_item("model-1", "core"), _model_item("model-2", "shared")]
        per_model = {
            "model-1": {
                "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                "semantic-metric": [_metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")],
            },
            "model-2": {
                "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                "semantic-metric": [_metric_item("orders_count", "COUNT(*)", "in.c-example_source.orders", "model-2")],
            },
        }
        self._run(models, per_model)
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None

        real_validate = document_validation.validate_document

        def _fail_shared_model(text):
            if "name: shared" in text:
                return document_validation.ValidationResult(ok=False, errors=["forced failure for test"])
            return real_validate(text)

        with patch("src.semantic.document_validation.validate_document", side_effect=_fail_shared_model):
            result = self._run(models, per_model)

        assert result["status"] == "ok"
        # "core" still composes and projects fine.
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        # "shared"'s document failed validation and was dropped — its
        # PREVIOUSLY-WRITTEN row must survive this pass, not be pruned as if
        # upstream had genuinely removed it.
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None

    def test_imports_glossary_from_every_model(self, e2e_env):
        from src.repositories import glossary_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        result = self._run(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                    "semantic-glossary": [_glossary_item("churn")],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                    "semantic-glossary": [_glossary_item("cohort")],
                },
            },
        )

        assert result["glossary_created_or_updated"] == 2
        terms = {g["term"] for g in glossary_repo().list(limit=100)}
        assert {"churn", "cohort"} <= terms

    def test_same_metric_name_in_two_models_coexists(self, e2e_env):
        """Since the flat-table cutover there is no name-ownership gate: two
        models in the same project can legitimately publish `revenue`, and
        both land — scoped by their own model name segment in the id — rather
        than the second being counted as a conflict and shadowed."""
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        result = self._run(
            [_model_item("model-1", "core"), _model_item("model-2", "shared")],
            {
                "model-1": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                    "semantic-metric": [
                        _metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")
                    ],
                },
                "model-2": {
                    "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                    "semantic-metric": [_metric_item("revenue", "COUNT(*)", "in.c-example_source.orders", "model-2")],
                },
            },
        )

        assert result["created_or_updated"] == 2
        assert result["skipped_conflict"] == 0
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/revenue") is not None

    def test_fetch_failure_on_a_later_model_aborts_without_pruning(self, e2e_env):
        """A partial fetch must never reach the prune loop — the models that
        did load would prune the rows of the model that did not."""
        from connectors.keboola.metastore_client import MetastoreApiError
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        models = [_model_item("model-1", "core"), _model_item("model-2", "shared")]
        per_model = {
            "model-1": {
                "semantic-dataset": [_dataset_item(model_uuid="model-1")],
                "semantic-metric": [_metric_item("revenue", 'SUM("amount")', "in.c-example_source.orders", "model-1")],
            },
            "model-2": {
                "semantic-dataset": [_dataset_item(model_uuid="model-2")],
                "semantic-metric": [_metric_item("orders_count", "COUNT(*)", "in.c-example_source.orders", "model-2")],
            },
        }
        self._run(models, per_model)
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}
        fake_metastore = MagicMock()

        def _list_items(item_type, model_uuid=None):
            if item_type == "semantic-model":
                return models
            if model_uuid == "model-2":
                raise MetastoreApiError("Metastore 503")
            return (per_model.get(model_uuid) or {}).get(item_type, [])

        fake_metastore.list_items.side_effect = _list_items

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(
                keboola_url="https://connection.keboola.com",
                keboola_token="master-tok",
            )

        assert result["status"] == "error"
        assert metric_repo().get("keboola_metastore/_/core/revenue") is not None
        assert metric_repo().get("keboola_metastore/_/shared/orders_count") is not None


class TestUpstreamErrorClassification:
    """Which upstream failures are the admin's to fix, and which are outages.

    Everything used to collapse into 502 Bad Gateway, so "you pasted the wrong
    token" and "Keboola is down" were indistinguishable — operators read Bad
    Gateway and went looking for an infrastructure problem.
    """

    def test_4xx_is_a_client_error(self):
        from connectors.keboola.storage_api import StorageApiError, is_upstream_client_error

        assert is_upstream_client_error(StorageApiError("nope", status=401))
        assert is_upstream_client_error(StorageApiError("nope", status=403))

    def test_5xx_is_not_a_client_error(self):
        from connectors.keboola.storage_api import StorageApiError, is_upstream_client_error

        assert not is_upstream_client_error(StorageApiError("boom", status=500))
        assert not is_upstream_client_error(StorageApiError("boom", status=503))

    def test_statusless_transport_failure_is_not_a_client_error(self):
        """A ConnectionError/Timeout carries no status. Absence of a 4xx must
        never be read as "not the upstream's fault"."""
        import requests

        from connectors.keboola.storage_api import StorageApiError, is_upstream_client_error

        assert not is_upstream_client_error(requests.ConnectionError("dns"))
        assert not is_upstream_client_error(StorageApiError("no status at all"))

    def test_metastore_error_classifies_without_a_cross_import(self):
        """The classifier is duck-typed on `.status`, so it covers the
        Metastore client's error type too."""
        from connectors.keboola.metastore_client import MetastoreApiError
        from connectors.keboola.storage_api import is_upstream_client_error

        assert is_upstream_client_error(MetastoreApiError("nope", status=401))
        assert not is_upstream_client_error(MetastoreApiError("boom", status=502))


class TestSyncErrorCodes:
    def test_missing_credentials_reports_a_config_code(self, e2e_env):
        """The endpoint maps this to 400: nothing is configured yet, which is
        a setup step, not a gateway failure."""
        from connectors.keboola.semantic_layer import sync_semantic_layer

        with patch("connectors.keboola.semantic_layer._enumerate_master_sources", return_value=[]):
            with patch(
                "connectors.keboola.semantic_layer._resolve_keboola_credentials_slot",
                return_value=("", "", "none"),
            ):
                result = sync_semantic_layer()

        assert result["status"] == "error"
        assert result["code"] == "credentials_not_configured"


class TestProjectMismatchAtSyncTime:
    """A connection bound to one project must never import another's layer.

    The preflight already fetches the token's owner, so this costs nothing —
    and the alternative is silent: the wrong project's metrics land under this
    connection's `source_ref`, inside its prune scope, so the next sync of the
    correct project deletes them again. Nothing on the page would say why.
    """

    def _fakes(self, owner_id):
        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {
            "isMasterToken": True,
            "owner": {"id": owner_id, "name": f"Project {owner_id}"},
        }
        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("a", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]
        return fake_storage, fake_metastore

    def test_mismatched_project_fails_the_source_without_writing(self, e2e_env):
        from connectors.keboola.semantic_layer import _sync_one_source
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        fake_storage, fake_metastore = self._fakes(owner_id=9999)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = _sync_one_source(
                "https://connection.keboola.com",
                "master-tok",
                "conn-a",
                adopt_null=False,
                expected_project=(1234, "Acme Analytics"),
            )

        assert result["status"] == "error"
        assert result["code"] == "project_mismatch"
        assert "9999" in result["error"] and "1234" in result["error"]
        # Refused BEFORE the Metastore was touched — nothing imported.
        assert metric_repo().get("keboola_metastore/conn-a/core/a") is None
        fake_metastore.list_items.assert_not_called()

    def test_matching_project_survives_an_id_stored_as_text(self, e2e_env):
        """The stored id round-trips through a JSON config column on two
        backends. A 1234 coming back as "1234" must not fail a correctly
        configured project — and here the only escape is re-pointing the
        connection to another stack. Devin Review on #1242.
        """
        from connectors.keboola.semantic_layer import _sync_one_source
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        fake_storage, fake_metastore = self._fakes(owner_id=1234)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = _sync_one_source(
                "https://connection.keboola.com",
                "master-tok",
                "conn-a",
                adopt_null=False,
                expected_project=("1234", "Acme Analytics"),
            )

        assert result["status"] == "ok", result
        assert metric_repo().get("keboola_metastore/conn-a/core/a") is not None

    def test_matching_project_syncs_normally(self, e2e_env):
        from connectors.keboola.semantic_layer import _sync_one_source
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        fake_storage, fake_metastore = self._fakes(owner_id=1234)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = _sync_one_source(
                "https://connection.keboola.com",
                "master-tok",
                "conn-a",
                adopt_null=False,
                expected_project=(1234, "Acme Analytics"),
            )

        assert result["status"] == "ok"
        assert metric_repo().get("keboola_metastore/conn-a/core/a") is not None

    def test_unbound_connection_still_syncs(self, e2e_env):
        """expected_project=None (identity never recorded) must not block a
        sync that worked before this check existed."""
        from connectors.keboola.semantic_layer import _sync_one_source
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        fake_storage, fake_metastore = self._fakes(owner_id=777)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = _sync_one_source(
                "https://connection.keboola.com",
                "master-tok",
                "conn-a",
                adopt_null=False,
                expected_project=None,
            )

        assert result["status"] == "ok"
        assert metric_repo().get("keboola_metastore/conn-a/core/a") is not None


def test_legacy_connection_slot_path_also_gets_the_mismatch_guard(e2e_env):
    """Mode 3 stamps rows with the default connection's id, so the same
    cross-attribution the master-token loop refuses is reachable there
    whenever that connection's STORAGE token opens a different project than
    the one it is bound to. Devin Review on #1242.
    """
    from connectors.keboola.semantic_layer import sync_semantic_layer

    conn = {
        "id": "conn-default",
        "name": "Bound Project",
        "config": {
            "stack_url": "https://connection.keboola.com",
            "project_id": 1234,
            "project_name": "Acme Analytics",
        },
    }
    fake_storage = MagicMock()
    fake_storage.verify_token.return_value = {
        "isMasterToken": True,
        "owner": {"id": 9999, "name": "Some Other Project"},
    }

    with (
        patch("connectors.keboola.semantic_layer._enumerate_master_sources", return_value=[]),
        patch(
            "connectors.keboola.semantic_layer._resolve_keboola_credentials_slot",
            return_value=("https://connection.keboola.com", "storage-tok", "connection"),
        ),
        patch("connectors.keboola.semantic_layer._default_keboola_connection", return_value=conn),
        patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
    ):
        result = sync_semantic_layer()

    assert result["status"] == "error"
    assert result["code"] == "project_mismatch"
    assert "9999" in result["error"] and "1234" in result["error"]


class TestSyncComposesOssieDocuments:
    """`_sync_one_source` composes an Ossie document per model
    (connectors/keboola/semantic_ossie.py::KeboolaMetastoreAdapter), stores it
    under `source='keboola_metastore'`, and PROJECTS it into the flat tables —
    since the flat-table cutover this document pipeline is the only writer of
    `metric_definitions`/`glossary_terms`, so composition succeeding or
    failing directly gates whether anything lands in the flat tables at all.
    """

    def test_populates_semantic_models_alongside_the_flat_tables(self, e2e_env):
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo, semantic_model_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "ok"
        assert metric_repo().get("keboola_metastore/_/core/total_revenue") is not None

        model = semantic_model_repo().get_by_slug("core")
        assert model is not None
        assert model["source"] == "keboola_metastore"
        assert model["status"] == "valid"
        assert model["document_json"]["semantic_model"][0]["datasets"][0]["name"] == "orders"

    def test_a_broken_ossie_composition_is_a_hard_error(self, e2e_env, monkeypatch):
        """Since the flat-table cutover, composition IS the write — there is
        no separate flat sync left to fall back to (the legacy flat composer
        that used to write independently of the Ossie document is gone). A
        composition failure must therefore abort the source with a
        structured error, not silently produce an empty-but-successful sync."""
        from connectors.keboola.semantic_layer import sync_semantic_layer
        from src.repositories import metric_repo

        _register_keboola_table("in.c-example_source", "orders", "crm_orders")

        fake_storage = MagicMock()
        fake_storage.verify_token.return_value = {"isMasterToken": True}

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
            "semantic-model": [_model_item()],
            "semantic-dataset": [_dataset_item()],
            "semantic-metric": [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }[item_type]

        def _boom(self, config):
            raise RuntimeError("boom")

        monkeypatch.setattr("connectors.keboola.semantic_ossie.KeboolaMetastoreAdapter.extract", _boom)

        with (
            patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

        assert result["status"] == "error"
        assert "Ossie document composition failed" in result["error"]
        assert metric_repo().get("keboola_metastore/_/core/total_revenue") is None
