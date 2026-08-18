"""Case-table tests for the storage-independent semantic-layer read tools
(``src/semantic_context.py``): ``get_semantic_context`` and
``get_semantic_schema``.

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 4, "Agent read tools and skill"). Same style as
``tests/test_semantic_validation.py`` -- a hand-built fixture document, no
database.
"""

from __future__ import annotations

from src.semantic_context import get_semantic_context, get_semantic_schema


def _fixture_document(name: str = "retail_model") -> dict:
    return {
        "name": name,
        "description": "Retail orders and customers.",
        "datasets": [
            {
                "name": "orders",
                "source": "analytics.orders",
                "description": "One row per placed order.",
                "primary_key": ["order_id"],
                "fields": [
                    {"name": "order_id", "datatype": "String", "description": "Primary key."},
                    {"name": "order_date", "datatype": "Date", "description": "Order date."},
                ],
            },
            {
                "name": "customers",
                "source": "analytics.customers",
                "ai_context": {"instructions": "Use for customer demographics, not order history."},
                "fields": [{"name": "customer_id", "datatype": "String"}],
            },
        ],
        "metrics": [
            {
                "name": "revenue",
                "dataset": "orders",
                "description": "Total order revenue.",
                "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
            },
        ],
        "relationships": [
            {"name": "orders_to_customers", "from": "orders", "to": "customers", "description": "FK."},
        ],
    }


# --------------------------------------------------------------------------- #
# get_semantic_context -- compact vs full
# --------------------------------------------------------------------------- #


class TestGetSemanticContextCompact:
    def test_absent_ids_returns_all_objects_compactly(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset"}])
        assert result["unknown_types"] == []
        assert len(result["results"]) == 1
        entry = result["results"][0]
        assert entry["semantic_type"] == "dataset"
        assert entry["mode"] == "compact"
        names = {o["name"] for o in entry["objects"]}
        assert names == {"orders", "customers"}
        # Compact form: name + summary + model, never the full attribute set.
        orders = next(o for o in entry["objects"] if o["name"] == "orders")
        assert set(orders.keys()) == {"name", "summary", "model"}
        assert orders["summary"] == "One row per placed order."
        assert orders["model"] == "retail_model"

    def test_empty_ids_list_also_means_all_compactly(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "metric", "ids": []}])
        entry = result["results"][0]
        assert entry["mode"] == "compact"
        assert {o["name"] for o in entry["objects"]} == {"revenue"}

    def test_compact_summary_falls_back_to_ai_context_instructions(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset"}])
        customers = next(o for o in result["results"][0]["objects"] if o["name"] == "customers")
        assert customers["summary"] == "Use for customer demographics, not order history."


class TestGetSemanticContextFull:
    def test_explicit_ids_returns_full_attributes(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset", "ids": ["orders"]}])
        entry = result["results"][0]
        assert entry["mode"] == "full"
        assert len(entry["objects"]) == 1
        obj = entry["objects"][0]
        assert obj["name"] == "orders"
        assert obj["source"] == "analytics.orders"
        assert obj["primary_key"] == ["order_id"]
        assert len(obj["fields"]) == 2
        assert obj["model"] == "retail_model"

    def test_id_matching_is_case_insensitive(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset", "ids": ["ORDERS"]}])
        assert {o["name"] for o in result["results"][0]["objects"]} == {"orders"}

    def test_unmatched_id_returns_no_objects_not_an_error(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset", "ids": ["nope"]}])
        assert result["results"][0]["objects"] == []

    def test_multiple_selections_in_one_call(self):
        result = get_semantic_context(
            [_fixture_document()],
            [{"semantic_type": "dataset", "ids": ["orders"]}, {"semantic_type": "metric"}],
        )
        assert [r["semantic_type"] for r in result["results"]] == ["dataset", "metric"]
        assert result["results"][0]["mode"] == "full"
        assert result["results"][1]["mode"] == "compact"


class TestGetSemanticContextMultiModelAndUnknown:
    def test_unions_across_documents_grouped_by_model(self):
        doc_a = _fixture_document("model_a")
        doc_b = _fixture_document("model_b")
        result = get_semantic_context([doc_a, doc_b], [{"semantic_type": "dataset", "ids": ["orders"]}])
        objects = result["results"][0]["objects"]
        assert {o["model"] for o in objects} == {"model_a", "model_b"}
        assert len(objects) == 2

    def test_unknown_semantic_type_is_reported_not_raised(self):
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "glossary"}])
        assert result["unknown_types"] == ["glossary"]
        assert result["results"] == []

    def test_empty_documents_returns_empty_objects_not_an_error(self):
        result = get_semantic_context([], [{"semantic_type": "dataset"}])
        assert result["results"][0]["objects"] == []

    def test_non_dict_selection_is_skipped(self):
        result = get_semantic_context([_fixture_document()], ["not-a-dict", {"semantic_type": "dataset"}])
        assert len(result["results"]) == 1

    def test_a_bare_string_ids_is_treated_as_one_id_not_char_iterated(self):
        """Devin #1398: `"ids": "orders"` must match the `orders` object, not
        char-iterate into {"o","r","d","e","s"} and match nothing."""
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset", "ids": "orders"}])
        entry = result["results"][0]
        assert entry["mode"] == "full"
        assert [o["name"] for o in entry["objects"]] == ["orders"]

    def test_a_non_iterable_ids_degrades_to_compact_not_a_crash(self):
        """A number (or any non-iterable) for `ids` must not raise — it
        degrades to the compact 'every object of this type' answer."""
        result = get_semantic_context([_fixture_document()], [{"semantic_type": "dataset", "ids": 5}])
        entry = result["results"][0]
        assert entry["mode"] == "compact"
        assert entry["objects"]  # every dataset, compactly — no error


# --------------------------------------------------------------------------- #
# get_semantic_schema -- served from the vendored schema, never hand-written
# --------------------------------------------------------------------------- #


class TestGetSemanticSchema:
    def test_returns_ref_and_defs_for_a_known_type(self):
        result = get_semantic_schema(["dataset"])
        assert result["types"]["dataset"] == {"$ref": "#/$defs/Dataset"}
        assert "Dataset" in result["$defs"]
        assert result["unknown_types"] == []

    def test_result_is_self_contained_and_resolvable(self):
        """The $ref must point at a $defs entry that is actually present --
        i.e. the returned dict is a valid, self-contained JSON Schema slice,
        not a dangling reference into a schema the caller doesn't have."""
        result = get_semantic_schema(["metric", "relationship"])
        for type_name, ref in result["types"].items():
            def_name = ref["$ref"].rsplit("/", 1)[-1]
            assert def_name in result["$defs"], f"{type_name} points at a missing $defs entry"

    def test_schema_matches_the_vendored_document_validation_schema(self):
        """Never a hand-written copy -- pulled straight from
        src.semantic.document_validation's vendored, pinned schema."""
        from src.semantic.document_validation import get_schema_defs

        result = get_semantic_schema(["dataset"])
        assert result["$defs"]["Dataset"] == get_schema_defs()["Dataset"]

    def test_unknown_type_is_reported_not_raised(self):
        result = get_semantic_schema(["glossary", "dataset"])
        assert result["unknown_types"] == ["glossary"]
        assert "dataset" in result["types"]

    def test_empty_semantic_types_returns_empty_types(self):
        result = get_semantic_schema([])
        assert result["types"] == {}
        assert result["unknown_types"] == []
        # $defs is still the full vendored bag even with no types requested.
        assert "Dataset" in result["$defs"]
