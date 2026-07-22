"""Tests for the ObjectStore seam (wave 2-H, WF-1) — config resolution +
the S3-compatible implementation against a fake boto3 client.

Presigned-URL distribution is opt-in and vendor-agnostic: one
``ObjectStore`` protocol, one S3-compatible implementation (AWS S3, GCS's
S3-interop endpoint, SeaweedFS, managed buckets all speak this API).
``boto3`` is an optional extra — the missing-dependency path must raise a
clear ``RuntimeError``, never a bare ``ImportError``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.object_store as object_store_mod
from app.instance_config import (
    distribution_object_store_config,
    distribution_signed_urls_mode,
)
from src.object_store import (
    S3ObjectStore,
    object_store,
    reset_object_store_cache,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "AGNES_DISTRIBUTION_SIGNED_URLS",
        "AGNES_DISTRIBUTION_OBJECT_STORE_ENDPOINT_URL",
        "AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET",
        "AGNES_DISTRIBUTION_OBJECT_STORE_PREFIX",
        "AGNES_DISTRIBUTION_OBJECT_STORE_REGION",
        "AGNES_DISTRIBUTION_OBJECT_STORE_ACCESS_KEY_ENV",
        "AGNES_DISTRIBUTION_OBJECT_STORE_SECRET_KEY_ENV",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_object_store_cache()
    yield
    reset_object_store_cache()


def _no_yaml(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *a, **k: k.get("default"))


# --- distribution_signed_urls_mode() ------------------------------------


def test_signed_urls_mode_defaults_to_auto(monkeypatch):
    _no_yaml(monkeypatch)
    assert distribution_signed_urls_mode() == "auto"


def test_signed_urls_mode_yaml_selects_on(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "on" if keys == ("distribution", "signed_urls") else default,
    )
    assert distribution_signed_urls_mode() == "on"


def test_signed_urls_mode_env_overrides_yaml(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "on" if keys == ("distribution", "signed_urls") else default,
    )
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "off")
    assert distribution_signed_urls_mode() == "off"


def test_signed_urls_mode_unknown_falls_back_to_auto(monkeypatch):
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "bogus")
    assert distribution_signed_urls_mode() == "auto"


# --- distribution_object_store_config() ---------------------------------


def test_object_store_config_none_when_no_bucket(monkeypatch):
    _no_yaml(monkeypatch)
    assert distribution_object_store_config() is None


def test_object_store_config_resolves_from_yaml(monkeypatch):
    values = {
        ("distribution", "object_store", "bucket"): "my-bucket",
        ("distribution", "object_store", "endpoint_url"): "https://s3.example.com",
        ("distribution", "object_store", "region"): "us-east-1",
    }
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: values.get(keys, default),
    )
    config = distribution_object_store_config()
    assert config is not None
    assert config["bucket"] == "my-bucket"
    assert config["endpoint_url"] == "https://s3.example.com"
    assert config["region"] == "us-east-1"
    assert config["prefix"] == "agnes/distribution"


def test_object_store_config_env_overrides_yaml_bucket(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *keys, default=None: "yaml-bucket" if keys == ("distribution", "object_store", "bucket") else default,
    )
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "env-bucket")
    config = distribution_object_store_config()
    assert config is not None
    assert config["bucket"] == "env-bucket"


def test_object_store_config_resolves_credentials_from_named_env_vars(monkeypatch):
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "creds-bucket")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_ACCESS_KEY_ENV", "MY_ACCESS_KEY")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_SECRET_KEY_ENV", "MY_SECRET_KEY")
    monkeypatch.setenv("MY_ACCESS_KEY", "AKIA-fake")
    monkeypatch.setenv("MY_SECRET_KEY", "secret-fake")
    config = distribution_object_store_config()
    assert config is not None
    assert config["access_key"] == "AKIA-fake"
    assert config["secret_key"] == "secret-fake"


def test_object_store_config_custom_prefix(monkeypatch):
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_PREFIX", "custom/prefix")
    config = distribution_object_store_config()
    assert config is not None
    assert config["prefix"] == "custom/prefix"


# --- S3ObjectStore against a fake boto3 client --------------------------


def _fake_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(object_store_mod, "boto3", MagicMock(client=MagicMock(return_value=client)))
    return client


def test_s3_object_store_presign_get_params(monkeypatch):
    client = _fake_client(monkeypatch)
    client.generate_presigned_url.return_value = "https://signed.example.com/x"
    store = S3ObjectStore(bucket="my-bucket", prefix="agnes/distribution")

    url = store.presign_get("orders.parquet", ttl_s=600)

    assert url == "https://signed.example.com/x"
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "my-bucket", "Key": "agnes/distribution/orders.parquet"},
        ExpiresIn=600,
    )


def test_s3_object_store_presign_get_default_ttl(monkeypatch):
    client = _fake_client(monkeypatch)
    store = S3ObjectStore(bucket="b", prefix="p")
    store.presign_get("k.parquet")
    _, kwargs = client.generate_presigned_url.call_args
    assert kwargs["ExpiresIn"] == 900


def test_s3_object_store_key_normalizes_slashes(monkeypatch):
    client = _fake_client(monkeypatch)
    store = S3ObjectStore(bucket="b", prefix="/agnes//distribution/")
    store.presign_get("/orders.parquet")
    _, kwargs = client.generate_presigned_url.call_args
    assert kwargs["Params"]["Key"] == "agnes/distribution/orders.parquet"


def test_s3_object_store_put_file_passes_md5_metadata(monkeypatch, tmp_path):
    client = _fake_client(monkeypatch)
    local = tmp_path / "orders.parquet"
    local.write_bytes(b"data")
    store = S3ObjectStore(bucket="b", prefix="agnes/distribution")

    store.put_file(local, "orders.parquet", md5="abc123")

    client.upload_file.assert_called_once()
    args, kwargs = client.upload_file.call_args
    assert args[0] == str(local)
    assert args[1] == "b"
    assert args[2] == "agnes/distribution/orders.parquet"
    assert kwargs["ExtraArgs"]["Metadata"] == {"md5": "abc123"}


def test_s3_object_store_head_md5_returns_metadata(monkeypatch):
    client = _fake_client(monkeypatch)
    client.head_object.return_value = {"Metadata": {"md5": "deadbeef"}}
    store = S3ObjectStore(bucket="b", prefix="p")
    assert store.head_md5("orders.parquet") == "deadbeef"


def test_s3_object_store_head_md5_returns_none_on_404(monkeypatch):
    from botocore.exceptions import ClientError

    client = _fake_client(monkeypatch)
    client.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    store = S3ObjectStore(bucket="b", prefix="p")
    assert store.head_md5("missing.parquet") is None


def test_s3_object_store_head_md5_reraises_other_errors(monkeypatch):
    from botocore.exceptions import ClientError

    client = _fake_client(monkeypatch)
    client.head_object.side_effect = ClientError({"Error": {"Code": "500", "Message": "Internal error"}}, "HeadObject")
    store = S3ObjectStore(bucket="b", prefix="p")
    with pytest.raises(ClientError):
        store.head_md5("orders.parquet")


def test_s3_object_store_put_bytes_passes_md5_metadata(monkeypatch):
    client = _fake_client(monkeypatch)
    store = S3ObjectStore(bucket="b", prefix="agnes/distribution")

    store.put_bytes("_mirrored.json", b'{"tables": {}}', md5="abc123")

    client.put_object.assert_called_once_with(
        Bucket="b",
        Key="agnes/distribution/_mirrored.json",
        Body=b'{"tables": {}}',
        Metadata={"md5": "abc123"},
    )


def test_s3_object_store_get_bytes_returns_body(monkeypatch):
    client = _fake_client(monkeypatch)
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}
    store = S3ObjectStore(bucket="b", prefix="p")
    assert store.get_bytes("_mirrored.json") == b"payload"


def test_s3_object_store_get_bytes_returns_none_on_404(monkeypatch):
    from botocore.exceptions import ClientError

    client = _fake_client(monkeypatch)
    client.get_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject")
    store = S3ObjectStore(bucket="b", prefix="p")
    assert store.get_bytes("missing.json") is None


def test_s3_object_store_get_bytes_reraises_other_errors(monkeypatch):
    from botocore.exceptions import ClientError

    client = _fake_client(monkeypatch)
    client.get_object.side_effect = ClientError({"Error": {"Code": "500", "Message": "Internal error"}}, "GetObject")
    store = S3ObjectStore(bucket="b", prefix="p")
    with pytest.raises(ClientError):
        store.get_bytes("orders.parquet")


def test_s3_object_store_missing_boto3_raises_clean_runtime_error(monkeypatch):
    monkeypatch.setattr(object_store_mod, "boto3", None)
    with pytest.raises(RuntimeError, match="distribution.*extra"):
        S3ObjectStore(bucket="b", prefix="p")


# --- object_store() factory ----------------------------------------------


def test_factory_returns_none_when_mode_off(monkeypatch):
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "off")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    assert object_store() is None


def test_factory_returns_none_when_no_store_configured(monkeypatch):
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "auto")
    assert object_store() is None


def test_factory_returns_store_when_auto_and_store_configured(monkeypatch):
    _fake_client(monkeypatch)
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "auto")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    store = object_store()
    assert isinstance(store, S3ObjectStore)


def test_factory_returns_store_when_on_and_store_configured(monkeypatch):
    _fake_client(monkeypatch)
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "on")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    store = object_store()
    assert isinstance(store, S3ObjectStore)


def test_factory_caches_until_reset(monkeypatch):
    _fake_client(monkeypatch)
    _no_yaml(monkeypatch)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "auto")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    first = object_store()
    assert first is not None
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "off")
    # Cached — still returns the previously-built store despite the flip.
    assert object_store() is first
    reset_object_store_cache()
    assert object_store() is None


def test_factory_degrades_to_none_when_bucket_set_but_boto3_missing(monkeypatch, caplog):
    """A configured bucket on an image without the [distribution] extra must
    degrade (loud ERROR, app-served downloads) — not propagate RuntimeError
    into every manifest build (GET /api/sync/manifest would 500)."""
    import logging

    _no_yaml(monkeypatch)
    monkeypatch.setattr(object_store_mod, "boto3", None)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "auto")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    with caplog.at_level(logging.ERROR):
        assert object_store() is None
    assert any("boto3" in r.getMessage() and "distribution" in r.getMessage() for r in caplog.records), (
        "degrade must be loudly logged so the operator learns why signed URLs are off"
    )


def test_factory_degrades_even_when_mode_on(monkeypatch, caplog):
    """Explicit `signed_urls: on` with missing boto3 also degrades — manifest
    availability outranks strictness; the ERROR log carries the misconfig."""
    import logging

    _no_yaml(monkeypatch)
    monkeypatch.setattr(object_store_mod, "boto3", None)
    monkeypatch.setenv("AGNES_DISTRIBUTION_SIGNED_URLS", "on")
    monkeypatch.setenv("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET", "b")
    with caplog.at_level(logging.ERROR):
        assert object_store() is None
    assert any("boto3" in r.getMessage() for r in caplog.records)
