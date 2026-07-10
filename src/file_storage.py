"""Content-addressed corpus file storage.

Mirrors the pattern in ``app/api/uploads.py`` (sha256-named files, atomic
write via a ``.part`` tmp sibling, idempotent on re-upload of the same
content).

Files land at::

    ${DATA_DIR}/file_corpora/<corpus_id>/<sha256>.<ext>

The sha256 hex-digest is the canonical identity of each blob.  If the same
bytes are uploaded twice the second call is a no-op (the target already
exists) and returns the same ``StoredFile``.

Path traversal is neutralised: only the file extension is taken from the
supplied filename; the name portion is replaced by the sha256 digest.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile

from src.corpus_allowlist import MAX_UPLOAD_BYTES
from src.db import _get_data_dir

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024  # 64 KiB read chunks


@dataclass(frozen=True)
class StoredFile:
    """Immutable result of a successful ``store_corpus_file`` call."""

    sha256: str
    storage_path: str  # absolute path on disk
    size_bytes: int
    ext: str  # includes the leading dot, e.g. ".pdf"


def _corpus_dir(corpus_id: str) -> Path:
    """Resolve (and lazily create) the per-corpus storage directory."""
    p = _get_data_dir() / "file_corpora" / corpus_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_ext(filename: str) -> str:
    """Extract the file extension, stripping any directory components.

    Returns the suffix with a leading dot (e.g. ``".pdf"``), or an empty
    string when the filename has no extension.  The stem (name before the
    dot) is discarded — the stored filename is always ``<sha256><ext>``.
    """
    if not filename:
        return ""
    # Use only the basename to neutralise path-traversal sequences.
    name = Path(filename).name
    return Path(name).suffix.lower()


async def store_corpus_file(
    corpus_id: str,
    filename: str,
    upload: UploadFile,
) -> StoredFile:
    """Stream ``upload`` to disk content-addressed under ``corpus_id``.

    Args:
        corpus_id: The ``file_corpora.id`` this file belongs to.
        filename:  Original filename from the upload form (used only for
                   the extension; the stem is replaced by the sha256 digest).
        upload:    FastAPI ``UploadFile`` to read from.

    Returns:
        A :class:`StoredFile` describing the stored blob.

    Raises:
        ``HTTPException(413)`` when the upload exceeds ``MAX_UPLOAD_BYTES``.
        ``HTTPException(400)`` for zero-byte uploads.
    """
    ext = _safe_ext(filename)
    sha = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0

    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file_too_large:max_{MAX_UPLOAD_BYTES}_bytes",
            )
        sha.update(chunk)
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="empty_upload")

    digest = sha.hexdigest()
    target = _corpus_dir(corpus_id) / f"{digest}{ext}"

    if not target.exists():
        # Atomic write: write to a .part sibling then rename.  Concurrent
        # parallel uploads of the same content are safe — the last rename
        # wins, but all produce identical bytes so the result is correct.
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                for c in chunks:
                    fh.write(c)
            tmp.replace(target)
        except Exception:
            # Clean up any partial tmp file on error.
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    logger.info(
        "corpus_file stored corpus_id=%s sha=%s size=%d ext=%s",
        corpus_id,
        digest[:12],
        total,
        ext,
    )
    return StoredFile(
        sha256=digest,
        storage_path=str(target),
        size_bytes=total,
        ext=ext,
    )


def store_corpus_bytes(corpus_id: str, filename: str, data: bytes) -> StoredFile:
    """Store an in-memory blob content-addressed under ``corpus_id``.

    Sync sibling of :func:`store_corpus_file` for bundle members already in
    memory after archive extraction. Same layout (``<sha256><ext>``), size
    cap, path-traversal neutralisation, and atomic ``.part`` write.

    Raises:
        ``HTTPException(413)`` when ``data`` exceeds ``MAX_UPLOAD_BYTES``.
        ``HTTPException(400)`` for empty blobs.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file_too_large:max_{MAX_UPLOAD_BYTES}_bytes",
        )
    if not data:
        raise HTTPException(status_code=400, detail="empty_upload")

    ext = _safe_ext(filename)
    digest = hashlib.sha256(data).hexdigest()
    target = _corpus_dir(corpus_id) / f"{digest}{ext}"
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return StoredFile(sha256=digest, storage_path=str(target), size_bytes=len(data), ext=ext)


def delete_corpus_file(storage_path: str) -> None:
    """Remove the blob at ``storage_path`` if it exists.

    Silently ignores missing files so callers can call this idempotently
    during cleanup or rollback without extra existence checks.
    """
    try:
        Path(storage_path).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_corpus_file: could not remove %s: %s", storage_path, exc)
