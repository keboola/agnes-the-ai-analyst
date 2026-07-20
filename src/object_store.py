"""S3-compatible object store seam for signed-URL distribution (three-plane
wave 2-H, WS F, task WF-1 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

Vendor-agnostic by design: one :class:`ObjectStore` protocol, one
S3-compatible implementation (:class:`S3ObjectStore`) built on ``boto3``.
That single implementation covers AWS S3, GCS's S3-interop endpoint,
SeaweedFS, and other managed buckets — there is no GCS-/AWS-native client
in this module, and there must never be one (see the wave plan's
"Non-negotiable design decisions"). Presigned URLs always go through
boto3's battle-tested V4 signer — never hand-rolled signing.

``boto3`` is an optional extra (``pip install agnes[distribution]``) so the
base install stays lean; the import is guarded and construction of
:class:`S3ObjectStore` without it raises a clear, actionable
``RuntimeError`` rather than a bare ``ImportError`` deep in some call
stack.

The module-level :func:`object_store` factory resolves configuration via
``app.instance_config`` (``distribution.signed_urls`` /
``distribution.object_store.*``) and caches the built instance —
:func:`reset_object_store_cache` is the test-facing invalidation hook,
mirroring the singleton-cache shape used by
``src.analytics_backend.analytics_backend`` /
``reset_analytics_backend_cache``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, Protocol

try:
    import boto3
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    boto3 = None  # type: ignore[assignment]

_BOTO3_MISSING_MSG = (
    "boto3 is required for signed-URL distribution; install the 'distribution' extra: pip install agnes[distribution]"
)


class ObjectStore(Protocol):
    """Seam every object-store backend implements — currently only
    :class:`S3ObjectStore`, but keeping this a ``Protocol`` (rather than an
    ABC every caller imports) means WF-2/WF-3/WF-4 depend on the method
    shapes, not a concrete class."""

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        """Return a short-TTL presigned GET URL for *key*."""
        ...

    def put_file(self, local_path: str | Path, key: str, md5: str) -> None:
        """Upload *local_path* to *key*, stamping the object's metadata
        with *md5* so :meth:`head_md5` can later answer "is this object
        already current" without re-downloading it."""
        ...

    def head_md5(self, key: str) -> Optional[str]:
        """Return the ``md5`` metadata stamped on *key* by a prior
        :meth:`put_file`, or ``None`` if the object does not exist."""
        ...

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        """Upload *data* directly to *key* (no local file involved),
        stamping the object's metadata with *md5* — same contract as
        :meth:`put_file`. Used for small generated payloads (the
        distribution-mirror marker index, wave 2-H WF-3) where writing a
        temp file first would be pure overhead."""
        ...

    def get_bytes(self, key: str) -> Optional[bytes]:
        """Return the raw bytes stored at *key*, or ``None`` if the object
        does not exist. Counterpart to :meth:`put_bytes` — used to read
        back small generated payloads (the marker index) rather than
        presigning + fetching over HTTP."""
        ...


def _normalize_key(prefix: str, key: str) -> str:
    """Join *prefix* and *key* with exactly one ``/`` between segments,
    collapsing any duplicate/leading/trailing slashes either side might
    carry (operator-typo'd yaml prefixes, callers passing a leading
    ``/table.parquet``, etc.)."""
    segments = [part for part in prefix.split("/") if part] + [part for part in key.split("/") if part]
    return "/".join(segments)


class S3ObjectStore:
    """S3-compatible :class:`ObjectStore` implementation via ``boto3``.

    One implementation, many compatible endpoints: leave ``endpoint_url``
    unset for real AWS S3, or point it at a GCS S3-interop endpoint,
    SeaweedFS, or any other managed bucket that speaks the S3 API.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: Optional[str] = None,
        region: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        if boto3 is None:
            raise RuntimeError(_BOTO3_MISSING_MSG)
        self.bucket = bucket
        self.prefix = prefix.strip("/")

        client_kwargs: dict = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region:
            client_kwargs["region_name"] = region
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key
        self._client = boto3.client("s3", **client_kwargs)

    def _key(self, key: str) -> str:
        return _normalize_key(self.prefix, key)

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key)},
            ExpiresIn=ttl_s,
        )
        return url

    def put_file(self, local_path: str | Path, key: str, md5: str) -> None:
        self._client.upload_file(
            str(local_path),
            self.bucket,
            self._key(key),
            ExtraArgs={"Metadata": {"md5": md5}},
        )

    def head_md5(self, key: str) -> Optional[str]:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        metadata: dict = response.get("Metadata", {}) or {}
        value = metadata.get("md5")
        return str(value) if value is not None else None

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            Metadata={"md5": md5},
        )

    def get_bytes(self, key: str) -> Optional[bytes]:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        body = response["Body"].read()
        return bytes(body)


def _is_not_found(exc: Exception) -> bool:
    """True when *exc* is a boto3/botocore ``ClientError`` signaling the
    object does not exist (HEAD 404 / ``NoSuchKey`` / ``NotFound``).
    Duck-typed off ``exc.response`` rather than importing
    ``botocore.exceptions.ClientError`` at module scope — botocore ships
    with boto3, so this stays consistent with the guarded top-of-file
    import (no hard dependency on boto3 internals outside the ``S3ObjectStore``
    code path that already requires it)."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {}) or {}
    code = str(error.get("Code", ""))
    status = (response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


_lock = threading.Lock()
_store_cache: Optional[ObjectStore] = None
_store_cache_ready = False


def _build_object_store() -> Optional[ObjectStore]:
    from app.instance_config import (
        distribution_object_store_config,
        distribution_signed_urls_mode,
    )

    if distribution_signed_urls_mode() == "off":
        return None
    config = distribution_object_store_config()
    if config is None:
        return None
    return S3ObjectStore(
        bucket=config["bucket"],
        prefix=config.get("prefix") or "",
        endpoint_url=config.get("endpoint_url"),
        region=config.get("region"),
        access_key=config.get("access_key"),
        secret_key=config.get("secret_key"),
    )


def object_store() -> Optional[ObjectStore]:
    """Return the process-wide configured :class:`ObjectStore`, or
    ``None`` when signed-URL distribution is off or no store is
    configured. Resolved lazily on first call and cached until
    :func:`reset_object_store_cache` — mirrors
    ``src.analytics_backend.analytics_backend``'s singleton-cache shape.
    """
    global _store_cache, _store_cache_ready
    if _store_cache_ready:
        return _store_cache
    with _lock:
        if not _store_cache_ready:
            _store_cache = _build_object_store()
            _store_cache_ready = True
    return _store_cache


def reset_object_store_cache() -> None:
    """Drop the cached :class:`ObjectStore` instance so the next
    :func:`object_store` call re-reads config and rebuilds it. Used by
    tests that flip ``AGNES_DISTRIBUTION_*`` / instance.yaml across cases,
    and by any admin-config-save hook that should re-evaluate the store
    (consistent with ``reset_analytics_backend_cache`` /
    ``reset_database_cache`` elsewhere in the codebase)."""
    global _store_cache, _store_cache_ready
    with _lock:
        _store_cache = None
        _store_cache_ready = False
