from pathlib import Path

from src.semantic.document_validation import SPEC_VERSION, validate_document

VALID = """
version: "0.2.0.dev0"
semantic_model:
  - name: retail
    datasets:
      - name: orders
        source: "db.public.orders"
        fields:
          - name: order_date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: "order_date"
            datatype: Date
"""


def test_valid_document_passes():
    result = validate_document(VALID)
    assert result.ok is True
    assert result.errors == []
    assert result.parsed["semantic_model"][0]["name"] == "retail"
    assert result.spec_version == SPEC_VERSION


def test_missing_required_field_fails_with_path():
    bad = VALID.replace('        source: "db.public.orders"\n', "")
    result = validate_document(bad)
    assert result.ok is False
    assert result.parsed is None
    assert any("source" in e for e in result.errors)


def test_malformed_yaml_is_an_error_not_an_exception():
    result = validate_document("semantic_model: [oops")
    assert result.ok is False
    assert any("YAML" in e or "yaml" in e for e in result.errors)


def test_upstream_example_validates():
    text = Path("tests/fixtures/ossie/tpcds_semantic_model.yaml").read_text()
    result = validate_document(text)
    assert result.ok, result.errors
