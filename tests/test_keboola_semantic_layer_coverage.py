"""compute_semantic_coverage() — what reaches Agnes, and which gaps deserve a warning.

The distinction under test is the whole point of the function: a metric whose
table nobody registered is the ordinary steady state and must stay quiet, while
a project where NOTHING binds, or a metric that cannot be composed from its own
definition, is a real finding.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


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


def _dataset_item(table_id, model_uuid="model-1"):
    return {
        "type": "semantic-dataset",
        "id": f"ds-{table_id}",
        "attributes": {"name": table_id.rpartition(".")[2], "tableId": table_id, "modelUUID": model_uuid},
    }


def _run(metrics, datasets=(), glossary=(), *, storage_project=("5947", "Demo"), master_project=("5947", "Demo")):
    """Run the coverage computation against a fake Metastore and fake token
    identities. Returns the single source entry."""
    from connectors.keboola import semantic_layer

    fake_metastore = MagicMock()
    fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: {
        "semantic-model": [_model_item()],
        "semantic-dataset": list(datasets),
        "semantic-metric": list(metrics),
        "semantic-relationship": [],
        "semantic-glossary": list(glossary),
    }[item_type]

    def fake_identity(url, token):
        pair = master_project if token == "master-tok" else storage_project
        return None if pair is None else {"id": pair[0], "name": pair[1]}

    with (
        patch.object(
            semantic_layer,
            "_enumerate_master_sources",
            return_value=[
                {
                    "connection_id": "conn-1",
                    "name": "Demo project",
                    "stack_url": "https://connection.keboola.com",
                    "token": "master-tok",
                }
            ],
        ),
        patch.object(semantic_layer, "_connection_storage_token", return_value="storage-tok"),
        patch.object(semantic_layer, "_project_identity", side_effect=fake_identity),
        patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
    ):
        result = semantic_layer.compute_semantic_coverage()

    assert len(result["sources"]) == 1
    return result["sources"][0]


def _warning_codes(source) -> set[str]:
    return {w["code"] for w in source["warnings"]}


class TestCoverageCounts:
    def test_metric_on_a_registered_table_is_importable(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [_metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders")],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        assert source["metrics"] == {"upstream": 1, "importable": 1}
        assert source["unregistered_tables"] == []
        assert _warning_codes(source) == set()

    def test_glossary_is_counted_independently_of_any_table(self, e2e_env):
        source = _run([], glossary=[{"id": "g1", "attributes": {"term": "MRR", "definition": "…"}}])

        assert source["glossary"]["upstream"] == 1


class TestUnregisteredTablesAreNotAProblem:
    """The steady state: a semantic layer describes more of a project than the
    instance registers. Those metrics are MEANT to stay out."""

    def test_partial_coverage_raises_no_warning(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [
                _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders"),
                _metric_item("headcount", "COUNT(*)", "in.c-hr.employees"),
            ],
            datasets=[_dataset_item("in.c-example_source.orders"), _dataset_item("in.c-hr.employees")],
        )

        assert source["metrics"] == {"upstream": 2, "importable": 1}
        # Reported as fact...
        assert source["unregistered_tables"] == ["in.c-hr.employees"]
        # ...and NOT as something anybody has to act on.
        assert _warning_codes(source) == set()

    def test_unregistered_metrics_never_land_in_blocked(self, e2e_env):
        source = _run(
            [_metric_item("headcount", "COUNT(*)", "in.c-hr.employees")],
            datasets=[_dataset_item("in.c-hr.employees")],
        )

        assert source["blocked"] == []
        assert source["unregistered_tables"] == ["in.c-hr.employees"]


class TestWarnings:
    def test_nothing_binding_at_all_is_a_warning(self, e2e_env):
        source = _run(
            [_metric_item("headcount", "COUNT(*)", "in.c-hr.employees")],
            datasets=[_dataset_item("in.c-hr.employees")],
        )

        assert source["metrics"] == {"upstream": 1, "importable": 0}
        assert "no_metrics_bound" in _warning_codes(source)

    def test_a_project_publishing_no_metrics_is_not_a_warning(self, e2e_env):
        source = _run([])

        assert source["metrics"] == {"upstream": 0, "importable": 0}
        assert "no_metrics_bound" not in _warning_codes(source)

    def test_metric_blocked_by_its_own_definition_is_reported_separately(self, e2e_env):
        _register_keboola_table("in.c-example_source", "orders", "crm_orders")
        source = _run(
            [
                _metric_item("total_revenue", 'SUM("amount")', "in.c-example_source.orders"),
                # A trailing comment swallows the composed FROM clause — no
                # amount of registering fixes this one.
                _metric_item("broken", 'SUM("amount") -- fixme', "in.c-example_source.orders"),
            ],
            datasets=[_dataset_item("in.c-example_source.orders")],
        )

        assert source["metrics"] == {"upstream": 2, "importable": 1}
        assert [b["metric"] for b in source["blocked"]] == ["broken"]
        assert source["blocked"][0]["reason"] == "embedded_sql_comment"
        assert "metrics_blocked" in _warning_codes(source)
        assert source["unregistered_tables"] == []

    def test_tokens_pointing_at_different_projects_is_a_warning(self, e2e_env):
        source = _run([], storage_project=("4451", "Other"), master_project=("5947", "Demo"))

        assert source["token_project_mismatch"] is True
        assert "token_project_mismatch" in _warning_codes(source)
        message = next(w["message"] for w in source["warnings"] if w["code"] == "token_project_mismatch")
        assert "4451" in message and "5947" in message

    def test_same_project_on_both_tokens_is_not_a_warning(self, e2e_env):
        source = _run([], storage_project=("5947", "Demo"), master_project=("5947", "Demo"))

        assert source["token_project_mismatch"] is False
        assert "token_project_mismatch" not in _warning_codes(source)

    def test_unknown_storage_identity_does_not_claim_a_mismatch(self, e2e_env):
        """A token we cannot resolve is unknown, not different — claiming a
        mismatch there would send an admin chasing a config error that isn't."""
        source = _run([], storage_project=None, master_project=("5947", "Demo"))

        assert source["token_project_mismatch"] is False
        assert "token_project_mismatch" not in _warning_codes(source)


class TestUpstreamFailure:
    def test_metastore_failure_is_captured_not_raised(self, e2e_env):
        from connectors.keboola import semantic_layer
        from connectors.keboola.metastore_client import MetastoreApiError

        fake_metastore = MagicMock()
        fake_metastore.list_items.side_effect = MetastoreApiError("boom", status=502)

        with (
            patch.object(
                semantic_layer,
                "_enumerate_master_sources",
                return_value=[
                    {
                        "connection_id": "conn-1",
                        "name": "Demo project",
                        "stack_url": "https://connection.keboola.com",
                        "token": "master-tok",
                    }
                ],
            ),
            patch.object(semantic_layer, "_connection_storage_token", return_value=""),
            patch.object(semantic_layer, "_project_identity", return_value=None),
            patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
        ):
            result = semantic_layer.compute_semantic_coverage()

        source = result["sources"][0]
        assert source["error"] is not None
        assert "fetch_failed" in _warning_codes(source)
