import pytest

from src.connection_specs import validate_connection_config


def test_keboola_normalizes_trailing_slash():
    cfg = validate_connection_config("keboola", {"stack_url": "https://connection.example.com/"})
    assert cfg["stack_url"] == "https://connection.example.com"


def test_keboola_requires_https_stack_url():
    with pytest.raises(ValueError, match="stack_url"):
        validate_connection_config("keboola", {})
    with pytest.raises(ValueError, match="https"):
        validate_connection_config("keboola", {"stack_url": "ftp://x"})


def test_bigquery_requires_project_defaults_location():
    cfg = validate_connection_config("bigquery", {"project": "my-proj"})
    assert cfg["location"] == "us"
    with pytest.raises(ValueError, match="project"):
        validate_connection_config("bigquery", {})


def test_unknown_source_type_rejected():
    with pytest.raises(ValueError, match="unknown source_type"):
        validate_connection_config("oracle", {})
