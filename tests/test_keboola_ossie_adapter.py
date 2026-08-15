"""KeboolaMetastoreAdapter — composes one Ossie document per semantic-model
from the six Metastore object types (connectors/keboola/semantic_ossie.py).

Fixture (tests/fixtures/metastore_six_types.json) mirrors the live-verified
wire shapes recorded in docs/superpowers/specs/2026-07-15-keboola-semantic-
layer-importer-design.md and docs/superpowers/specs/2026-07-17-keboola-
relationship-metrics-design.md; identifiers are fabricated placeholders.
Tests mock requests.Session directly (same pattern as
tests/test_keboola_metastore_client.py) so MetastoreClient's own,
already-tested client-side modelUUID filtering is exercised for real.
The construction point is patched at `connectors.keboola.metastore_client
.MetastoreClient` (its OWN defining module) — same target
tests/test_keboola_semantic_layer_sync.py patches — because the adapter
imports it locally inside `extract()`, not at module level, for exactly
this reason (see the comment at that import site).

Each test below asserts something the FLAT importer (semantic_layer.py's
build_metric_row / build_glossary_row / metric_definitions / glossary_terms)
discards on the way in and this adapter must keep.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import yaml

from connectors.keboola.metastore_client import MetastoreClient as RealMetastoreClient
from connectors.keboola.semantic_ossie import KeboolaMetastoreAdapter, compose_document
from src.semantic.document_validation import validate_document

FIXTURE = json.loads(Path("tests/fixtures/metastore_six_types.json").read_text())
_ITEM_TYPES = [k for k in FIXTURE if k != "_comment"]


def _mock_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


def _fake_session() -> MagicMock:
    """A requests.Session whose GET returns this fixture's data for whichever
    `/repository/{item_type}` path MetastoreClient asks for — the server
    would return ALL items for a type unfiltered; MetastoreClient itself
    does the client-side modelUUID filtering (real code, not mocked)."""
    session = MagicMock()

    def _get(url, headers=None, timeout=None):
        for item_type in _ITEM_TYPES:
            if url.endswith(f"/repository/{item_type}"):
                return _mock_response(200, {"data": FIXTURE[item_type]})
        raise AssertionError(f"unexpected Metastore GET {url}")

    session.get.side_effect = _get
    return session


@pytest.fixture
def adapter(monkeypatch):
    session = _fake_session()

    def _fake_metastore_client(*, url, token):
        return RealMetastoreClient(url=url, token=token, session=session)

    monkeypatch.setattr("connectors.keboola.metastore_client.MetastoreClient", _fake_metastore_client)
    return KeboolaMetastoreAdapter()


@pytest.fixture
def docs(adapter):
    return adapter.extract({"url": "https://connection.keboola.com", "token": "test-token"})


def _model(docs, index):
    return yaml.safe_load(docs[index])["semantic_model"][0]


def _custom_extensions(entity):
    return {e["vendor_name"]: json.loads(e["data"]) for e in entity.get("custom_extensions", [])}


class TestKeboolaMetastoreAdapter:
    def test_every_composed_document_is_schema_valid(self, docs):
        """A document this adapter emits that our own schema rejects is an
        adapter bug — checked first so every test below inspects output
        already known to conform."""
        for text in docs:
            result = validate_document(text)
            assert result.ok, result.errors

    def test_all_models_are_emitted_not_just_the_first(self, docs):
        assert len(docs) == 2
        assert _model(docs, 0)["name"] == "orders_model"
        assert _model(docs, 1)["name"] == "customers_model"

    def test_per_column_fields_survive(self, docs):
        fields = _model(docs, 0)["datasets"][0]["fields"]
        assert {f["name"] for f in fields} == {"order_id", "order_date", "amount"}
        assert any(f.get("description") for f in fields)

    def test_declared_dialect_becomes_a_dialect_tagged_expression(self, docs):
        model = _model(docs, 0)
        metric = model["metrics"][0]
        assert {d["dialect"] for d in metric["expression"]["dialects"]} == {"SNOWFLAKE"}
        field = model["datasets"][0]["fields"][0]
        assert {d["dialect"] for d in field["expression"]["dialects"]} == {"SNOWFLAKE"}

    def test_timestamp_role_becomes_a_time_dimension(self, docs):
        fields_by_name = {f["name"]: f for f in _model(docs, 0)["datasets"][0]["fields"]}
        assert fields_by_name["order_date"]["dimension"]["is_time"] is True
        assert "dimension" not in fields_by_name["amount"]

    def test_keywords_and_grain_survive_in_dataset_custom_extensions(self, docs):
        dataset = _model(docs, 0)["datasets"][0]
        assert dataset["ai_context"]["synonyms"] == ["sales", "purchases"]
        assert "Join via customer_id" in dataset["ai_context"]["instructions"]
        ext = _custom_extensions(dataset)["AGNES"]
        assert ext["keywords"] == ["orders", "revenue"]
        # `grain` has no first-class slot on the Dataset schema
        # (additionalProperties: false) even though the flat importer
        # surfaces it today (metric_definitions.grain) — it must not be
        # silently dropped.
        assert ext["grain"] == "One row per order"

    def test_relationships_survive_beyond_the_single_supported_case(self, docs):
        relationships = _model(docs, 0)["relationships"]
        assert len(relationships) == 3
        types = {_custom_extensions(rel)["AGNES"]["type"] for rel in relationships}
        # "left" is the flat importer's one supported case (metric's dataset
        # verified on the "to" side); "inner" is a type it explicitly skips
        # (unsupported_relationship_type); the third relationship is also
        # "left" but with the model's own dataset on the "from" side (the
        # importer's unverified_relationship_direction skip). None of these
        # would compose a JOIN metric today; the adapter keeps all three.
        assert types == {"left", "inner"}

    def test_constraints_ride_model_custom_extensions(self, docs):
        ext = _custom_extensions(_model(docs, 0))["AGNES"]
        constraints = ext["constraints"]
        assert len(constraints) == 1
        assert constraints[0]["rule"] == "value >= 0"
        assert constraints[0]["metrics"] == ["total_revenue"]

    def test_glossary_survives_even_though_the_mapping_table_omits_it(self, docs):
        """The plan's Task 13 mapping table (Metastore -> Ossie) lists five of
        the six object types this importer fetches and has no row at all for
        `semantic-glossary` — a real gap, not a design choice to drop it. It
        rides the same model-level custom_extensions as constraints, since
        Ossie has no first-class glossary concept either."""
        ext = _custom_extensions(_model(docs, 0))["AGNES"]
        assert ext["glossary"][0]["term"] == "Monthly Recurring Revenue"

    def test_second_model_with_no_relationships_or_constraints_still_composes(self, docs):
        model = _model(docs, 1)
        assert model["datasets"][0]["name"] == "customers"
        assert "relationships" not in model
        assert "custom_extensions" not in model


class TestComposeDocumentEdgeCases:
    def test_model_without_any_dataset_is_skipped_not_emitted_invalid(self):
        """`datasets` has `minItems: 1` on the vendored schema — a model with
        zero semantic-dataset entries has nothing valid to compose."""
        model_item = {"id": "m0", "attributes": {"name": "empty_model"}}
        model_items = {
            "semantic-dataset": [],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }
        assert compose_document(model_item, model_items) is None

    def test_missing_dialect_defaults_to_ansi_sql(self):
        model_item = {"id": "m1", "attributes": {"name": "no_dialect_model"}}
        model_items = {
            "semantic-dataset": [
                {
                    "attributes": {
                        "name": "t",
                        "tableId": "in.c-x.t",
                        "fields": [{"name": "a", "type": "INTEGER"}],
                    }
                }
            ],
            "semantic-metric": [],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }
        doc = yaml.safe_load(compose_document(model_item, model_items))
        field = doc["semantic_model"][0]["datasets"][0]["fields"][0]
        assert field["expression"]["dialects"][0]["dialect"] == "ANSI_SQL"

    def test_metric_without_a_name_is_dropped_not_emitted_invalid(self):
        """Metric.required includes `name` — a metric item missing it has no
        valid Ossie representation, mirroring build_metric_row's own
        "missing_name" skip for the flat importer."""
        model_item = {"id": "m1", "attributes": {"name": "model"}}
        model_items = {
            "semantic-dataset": [{"attributes": {"name": "t", "tableId": "in.c-x.t"}}],
            "semantic-metric": [{"attributes": {"sql": "SUM(1)"}}],
            "semantic-constraint": [],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }
        doc = yaml.safe_load(compose_document(model_item, model_items))
        assert "metrics" not in doc["semantic_model"][0]
