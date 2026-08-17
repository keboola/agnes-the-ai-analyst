"""Golden regression: the projector (`src/semantic/projection.py`) produces the
same load-bearing metric rows as the legacy Keboola flat composer
(`connectors/keboola/semantic_layer.py::build_metric_row` + the sync loop).

This is the gate for the flat-table cutover (wave 2 of
`docs/superpowers/plans/2026-08-16-semantic-layer-parity-sequencing.md`): the
cutover deletes the legacy composer and lets `project_document` become the sole
writer of Keboola `metric_definitions` rows. It is safe to do that only once
the projector matches the composer on everything a query depends on. This test
proves that, and it stays in the repo as a regression after the composer is
gone (the expected values are pinned here, not derived from the composer).

Both paths run against the SAME mocked Metastore fixture:
  - legacy: `sync_semantic_layer` writes rows under `source='keboola_semantic_layer'`;
  - projector: the documents that same sync stored in `semantic_models`
    (`source='keboola_metastore'`) are projected via `project_document`.

Metrics are compared BY NAME, not id — the id shape differs by design
(`keboola/{uuid}/{name}` vs `{source}/{source_ref}/{model}/{name}`), and that
difference is asserted explicitly below so the cutover cannot change it
unnoticed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _register_keboola_table(bucket: str, source_table: str, name: str) -> None:
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


def _model_item(uuid: str, name: str) -> dict:
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _dataset_item(table_id: str, model_uuid: str, *, grain: str | None = None, primary_key=None) -> dict:
    attrs: dict = {"name": table_id.rpartition(".")[2], "tableId": table_id, "modelUUID": model_uuid}
    if grain:
        attrs["grain"] = grain
    if primary_key:
        attrs["primaryKey"] = list(primary_key)
    return {"type": "semantic-dataset", "id": f"ds-{table_id}", "attributes": attrs}


def _metric_item(name: str, sql: str, dataset: str, model_uuid: str) -> dict:
    return {
        "type": "semantic-metric",
        "id": f"m-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


def _constraint_item(name: str, rule: str, metrics, severity: str = "error") -> dict:
    return {
        "type": "semantic-constraint",
        "id": f"c-{name}",
        "attributes": {
            "name": name,
            "constraintType": "range",
            "rule": rule,
            "metrics": list(metrics),
            "severity": severity,
        },
    }


# One fixture, exercising the cases the plan (§ wave 2) calls out: a plain
# bound metric, a metric carrying a constraint, and a metric on a table the
# instance never registered (must be skipped by BOTH paths).
_ITEMS = {
    "semantic-model": [_model_item("model-1", "core")],
    "semantic-dataset": [
        _dataset_item("in.c-shop.orders", "model-1", grain="monthly", primary_key=["order_id"]),
        _dataset_item("in.c-nowhere.ghosts", "model-1"),
    ],
    "semantic-metric": [
        _metric_item("total_revenue", 'SUM("amount")', "in.c-shop.orders", "model-1"),
        _metric_item("order_count", "COUNT(*)", "in.c-shop.orders", "model-1"),
        _metric_item("ghost_metric", "COUNT(*)", "in.c-nowhere.ghosts", "model-1"),
        # References a foreign alias with no relationship to resolve it: legacy
        # skips it (`foreign_alias_reference`), and the projector skips it too
        # (`references_foreign_alias`). The relationship-resolved JOIN case,
        # which legacy can compose and the projector cannot, is the one known
        # remaining parity gap before the cutover — see the module docstring /
        # PR — and is deliberately NOT exercised here.
        _metric_item("cross_metric", 'SUM(other."x")', "in.c-shop.orders", "model-1"),
    ],
    "semantic-constraint": [_constraint_item("non_negative", "value >= 0", ["total_revenue"])],
    "semantic-relationship": [],
    "semantic-glossary": [],
}


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def _norm_validation(v):
    return json.loads(v) if isinstance(v, str) else v


@pytest.fixture
def synced(e2e_env):
    """Register the shop table, run the sync against the fixture, and return
    (legacy_rows, projector_rows) — both keyed by metric name."""
    from connectors.keboola.semantic_layer import sync_semantic_layer
    from src.repositories import metric_repo, semantic_model_repo
    from src.semantic.projection import project_document

    _register_keboola_table("in.c-shop", "orders", "shop_orders")

    fake_storage = MagicMock()
    fake_storage.verify_token.return_value = {"isMasterToken": True}
    fake_metastore = MagicMock()
    fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: _ITEMS[item_type]

    with (
        patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
        patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
    ):
        sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

    legacy = [m for m in metric_repo().list() if m.get("source") == "keboola_semantic_layer"]

    # Project the documents the sync stored (source='keboola_metastore') the
    # same way the cutover will: merge every model into one document and call
    # project_document once (its prune is scoped to (source, source_ref)).
    docs = [m for m in semantic_model_repo().list_all(source="keboola_metastore")]
    merged = {"semantic_model": []}
    for m in docs:
        parsed = m.get("document_json") or json.loads(m["document"])
        merged["semantic_model"].extend(parsed.get("semantic_model") or [])
    project_document(merged, source="keboola_metastore", source_ref=None)
    projector = [m for m in metric_repo().list() if m.get("source") == "keboola_metastore"]

    return _by_name(legacy), _by_name(projector)


class TestLoadBearingParity:
    """The fields a query actually depends on must be identical."""

    def test_same_set_of_metric_names(self, synced):
        legacy, projector = synced
        # ghost_metric (unregistered table) and cross_metric (foreign alias, no
        # relationship) are skipped by BOTH paths — so the surviving set is
        # identical, which is the whole point.
        assert set(legacy) == {"total_revenue", "order_count"}
        assert set(projector) == {"total_revenue", "order_count"}

    def test_runnable_sql_matches(self, synced):
        legacy, projector = synced
        for name in ("total_revenue", "order_count"):
            assert legacy[name]["sql"] == projector[name]["sql"], name
            assert projector[name]["sql"].startswith("SELECT ")
            assert "FROM" in projector[name]["sql"]

    def test_table_binding_matches(self, synced):
        legacy, projector = synced
        for name in ("total_revenue", "order_count"):
            assert legacy[name]["table_name"] == projector[name]["table_name"] == "shop_orders"

    def test_constraints_match(self, synced):
        legacy, projector = synced
        lv = _norm_validation(legacy["total_revenue"]["validation"])
        pv = _norm_validation(projector["total_revenue"]["validation"])
        assert lv is not None and pv is not None
        assert [r["name"] for r in lv["rules"]] == [r["name"] for r in pv["rules"]] == ["non_negative"]
        assert lv["rules"][0]["rule"] == pv["rules"][0]["rule"] == "value >= 0"

    def test_a_metric_without_constraints_has_none_on_both(self, synced):
        legacy, projector = synced
        assert _norm_validation(legacy["order_count"]["validation"]) is None
        assert _norm_validation(projector["order_count"]["validation"]) is None


class TestIntendedDifferences:
    """Differences the wave-1 N4 decision made on purpose. Pinned here so the
    cutover cannot quietly reintroduce the misattribution wave 0/1 removed."""

    def test_id_shape_differs_by_design(self, synced):
        legacy, projector = synced
        assert legacy["total_revenue"]["id"] == "keboola/model-1/total_revenue"
        assert projector["total_revenue"]["id"] == "keboola_metastore/_/core/total_revenue"

    def test_grain_is_dropped_by_the_projector(self, synced):
        legacy, projector = synced
        # Legacy stamps the dataset's grain onto the metric; the projector does
        # not — it is a fact about the dataset, not the metric.
        assert legacy["total_revenue"]["grain"] == "monthly"
        assert projector["total_revenue"]["grain"] is None

    def test_dataset_grain_survives_as_a_projector_note(self, synced):
        _, projector = synced
        assert any("monthly" in n for n in (projector["total_revenue"]["notes"] or []))

    def test_dimensions_from_primary_key_are_dropped(self, synced):
        legacy, projector = synced
        assert legacy["total_revenue"]["dimensions"] == ["order_id"]
        assert not projector["total_revenue"]["dimensions"]
