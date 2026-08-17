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

import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# The `Metadata` key `put_file`/`put_bytes`/`head_md5` stamp and read
# (`{"md5": ...}`). One constant so the PUT side and the HEAD/GET side can
# never drift apart on the key name.
_MD5_METADATA_KEY = "md5"

# S3 echoes user-supplied object metadata back as `x-amz-meta-<key>` response
# headers on HEAD *and* plain GET — lowercase, boto3/S3's own convention, not
# an Agnes-defined contract (contrast `src.distribution.CONTENT_MD5_HEADER`,
# which IS ours, on the app-served part-download response). A GET caller that
# wants to know what an object's stamp claims about itself — e.g.
# `cli/lib/pull.py::_fetch_signed_url`, distinguishing "the object moved on"
# from "the bytes are damaged" on a signed-URL fetch that fails
# `_verify_and_promote` — reads this header rather than issuing a separate
# HEAD.
OBJECT_STORE_MD5_METADATA_HEADER = f"x-amz-meta-{_MD5_METADATA_KEY}"

# Matches every other content-md5 chunking in this codebase (`cli/lib/pull.py
# ::_file_md5`, `app/api/sync.py::_file_hash`) — kept identical so a
# byte-for-byte-equal file always hashes to the same digest, though nothing
# here requires that; it is just one fewer thing to explain if someone reads
# them side by side.
_HASH_CHUNK_BYTES = 8192


def hash_file_md5(path: str | Path, chunk_size: int = _HASH_CHUNK_BYTES) -> str:
    """Stream *path* and return the md5 of the bytes actually read — never
    loaded fully into memory, so this is safe on a multi-GB parquet.

    Exists so a caller about to stamp an object's metadata (:func:`put_file`)
    can hash the SAME bytes it is about to upload rather than trusting a
    label that came from somewhere else (e.g. a DB row read earlier, and
    possibly staler than the file by the time it is sent) — the same
    "hash exactly what you serve, from one read" rule
    ``app/api/data.py::_serve_part_self_describing`` established for the
    app-served part-download response (``CONTENT_MD5_HEADER`` /
    ``X-Agnes-Content-MD5``, wired via ``src/distribution.py``). This is the
    object-store side of that same rule — see
    ``app/worker/kinds.py::_run_distribution_mirror`` (issue #1360), its
    first caller.
    """
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


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

    def delete_object(self, key: str) -> None:
        """Delete the object at *key*. A no-op (never raises) if it does
        not exist — mirrors S3's own DELETE semantics. Used by the
        `DELETE /api/v1/agents/{id}` cascade (agent-api V1b Task 8, C14) to
        scrub harvested-artifact blobs when their owning agent is deleted."""
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
            ExtraArgs={"Metadata": {_MD5_METADATA_KEY: md5}},
        )

    def head_md5(self, key: str) -> Optional[str]:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        metadata: dict = response.get("Metadata", {}) or {}
        value = metadata.get(_MD5_METADATA_KEY)
        return str(value) if value is not None else None

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            Metadata={_MD5_METADATA_KEY: md5},
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

    def delete_object(self, key: str) -> None:
        # S3's DeleteObject is already idempotent/no-op-on-missing-key —
        # no need to duck-type a not-found error away here the way
        # head_md5/get_bytes do.
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))


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
    if boto3 is None:
        # A bucket is configured but the [distribution] extra is not
        # installed (the default image ships without it). Raising here
        # would propagate through object_store() into every manifest
        # build (GET /api/sync/manifest → 500) and the mirror job —
        # manifest availability outranks strictness, so degrade to
        # app-served downloads and tell the operator why, loudly, for
        # both `auto` and explicit `on` modes.
        log.error(
            "object-store bucket %r is configured but boto3 is not "
            "installed — signed-URL distribution disabled, parquets are "
            "served via the app download endpoint instead. %s",
            config.get("bucket"),
            _BOTO3_MISSING_MSG,
        )
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
