"""``AGNES_DATA_APPS_ENABLED`` env override for :func:`get_data_apps_config`.

The customer-instance module flips data apps on by setting this in ``.env``
(Terraform-friendly, mirrors ``AGNES_HOME_ROUTE`` / ``PUBLIC_URL``) rather than
editing the instance.yaml overlay. Per the canonical flag resolution (#1022),
when set the env var wins over instance.yaml in both directions; when truthy it
also backfills the example-config defaults so spec-builders never KeyError.
Instances without the var stay byte-for-byte unchanged.
"""

from __future__ import annotations

import pytest

import app.instance_config as ic


@pytest.fixture
def no_yaml_data_apps(monkeypatch):
    # Simulate instance.yaml carrying no ``data_apps:`` block.
    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    monkeypatch.delenv("AGNES_DATA_APPS_RUNTIME_IMAGE", raising=False)


def test_disabled_by_default(no_yaml_data_apps):
    assert ic.get_data_apps_config() == {}


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_enables_and_backfills_defaults(no_yaml_data_apps, monkeypatch, raw):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", raw)
    cfg = ic.get_data_apps_config()
    assert cfg["enabled"] is True
    # Spec-builders read these by key — must be present.
    for k in (
        "runtime_image",
        "default_sleep_mode",
        "default_mem_limit",
        "default_cpus",
        "default_idle_timeout_s",
        "max_apps_per_user",
    ):
        assert k in cfg, k


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_env_falsey_stays_disabled(no_yaml_data_apps, monkeypatch, raw):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", raw)
    assert ic.get_data_apps_config().get("enabled") in (None, False)


def test_runtime_image_pin(no_yaml_data_apps, monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    monkeypatch.setenv("AGNES_DATA_APPS_RUNTIME_IMAGE", "example.com/custom:9")
    assert ic.get_data_apps_config()["runtime_image"] == "example.com/custom:9"


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_env_falsey_forces_off_even_when_yaml_enables(monkeypatch, raw):
    # Canonical resolution (#1022): a set env var wins in both directions —
    # matches the request gates that resolve the same var via feature_enabled.
    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: {"enabled": True} if keys == ("data_apps",) else default,
    )
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", raw)
    assert ic.get_data_apps_config()["enabled"] is False


def test_yaml_block_wins_over_defaults(monkeypatch):
    # An explicit instance.yaml block keeps its values; env only forces enabled.
    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: {"enabled": False, "max_apps_per_user": 9} if keys == ("data_apps",) else default,
    )
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    cfg = ic.get_data_apps_config()
    assert cfg["enabled"] is True
    assert cfg["max_apps_per_user"] == 9  # yaml value preserved, not the default 3
