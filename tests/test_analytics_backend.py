"""Tests for the analytics backend seam — resolution matrix + DuckLake
config accessors.

Contract behavior of the DuckLake session/catalog itself is out of scope
here (later task); this file is only about wiring — which backend name
wins (env vs. yaml vs. default) and where the catalog/data paths resolve
to when nothing/something explicit is configured.
"""

from __future__ import annotations

import pytest

from src.analytics_backend import (
    analytics_backend,
    ducklake_catalog_dsn,
    ducklake_data_path,
    ducklake_snapshot_retention_days,
    reset_analytics_backend_cache,
    resolve_analytics_backend_name,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "AGNES_ANALYTICS_BACKEND",
        "AGNES_DUCKLAKE_CATALOG_DSN",
        "AGNES_DUCKLAKE_DATA_PATH",
        "DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_analytics_backend_cache()
    yield
    reset_analytics_backend_cache()


# --- resolution matrix -------------------------------------------------


def test_default_is_legacy(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    assert resolve_analytics_backend_name() == "legacy"
    assert analytics_backend() == "legacy"


def test_yaml_selects_ducklake(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "ducklake" if keys == ("analytics", "backend") else default,
    )
    assert resolve_analytics_backend_name() == "ducklake"


def test_env_override_wins_over_yaml(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "legacy" if keys == ("analytics", "backend") else default,
    )
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    assert resolve_analytics_backend_name() == "ducklake"


def test_invalid_token_raises(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        resolve_analytics_backend_name()


def test_invalid_yaml_token_raises(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "bogus" if keys == ("analytics", "backend") else default,
    )
    with pytest.raises(ValueError):
        resolve_analytics_backend_name()


def test_case_and_whitespace_normalized(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "  DuckLake  ")
    assert resolve_analytics_backend_name() == "ducklake"


def test_analytics_backend_caches_until_reset(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    assert analytics_backend() == "legacy"
    # Flip the env after the first call — cached value should stick.
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    assert analytics_backend() == "legacy"
    reset_analytics_backend_cache()
    assert analytics_backend() == "ducklake"


# --- ducklake_catalog_dsn() ---------------------------------------------


def test_catalog_dsn_defaults_to_data_dir_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    dsn = ducklake_catalog_dsn()
    assert dsn == str(tmp_path / "analytics" / "catalog.ducklake")


def test_catalog_dsn_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", "postgresql://user:pw@host:5432/agnes")
    assert ducklake_catalog_dsn() == "postgresql://user:pw@host:5432/agnes"


def test_catalog_dsn_explicit_yaml_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: (
            "postgresql://user:pw@host:5432/agnes" if keys == ("ducklake", "catalog_dsn") else default
        ),
    )
    assert ducklake_catalog_dsn() == "postgresql://user:pw@host:5432/agnes"


def test_catalog_dsn_env_wins_over_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "postgresql://yaml-host/agnes" if keys == ("ducklake", "catalog_dsn") else default,
    )
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", "postgresql://env-host/agnes")
    assert ducklake_catalog_dsn() == "postgresql://env-host/agnes"


# --- ducklake_data_path() -----------------------------------------------


def test_data_path_defaults_to_data_dir_lake_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    path = ducklake_data_path()
    assert path == str(tmp_path / "analytics" / "lake") + "/"


def test_data_path_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_DATA_PATH", "/mnt/ducklake-data/")
    assert ducklake_data_path() == "/mnt/ducklake-data/"


# --- ducklake_snapshot_retention_days() ----------------------------------


def test_retention_defaults_to_seven_days(monkeypatch):
    monkeypatch.delenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", raising=False)
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    assert ducklake_snapshot_retention_days() == 7


def test_retention_explicit_env_wins(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "14")
    assert ducklake_snapshot_retention_days() == 14


def test_retention_explicit_yaml_wins(monkeypatch):
    monkeypatch.delenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: 21 if keys == ("ducklake", "snapshot_retention_days") else default,
    )
    assert ducklake_snapshot_retention_days() == 21


def test_retention_env_wins_over_yaml(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: 21 if keys == ("ducklake", "snapshot_retention_days") else default,
    )
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "3")
    assert ducklake_snapshot_retention_days() == 3


def test_retention_zero_is_a_valid_override(monkeypatch):
    """0 means 'no retention grace' — a deliberate operator choice, not a
    typo to fall back from."""
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "0")
    assert ducklake_snapshot_retention_days() == 0


def test_retention_negative_falls_back_to_default(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "-5")
    assert ducklake_snapshot_retention_days() == 7


def test_retention_non_integer_falls_back_to_default(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: None)
    monkeypatch.setenv("AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS", "not-a-number")
    assert ducklake_snapshot_retention_days() == 7
