"""Bundle (zip) ingestion — unpack an uploaded archive into child corpus files.

The archive row itself never carries chunks; each supported member becomes its
own ``corpus_files`` row (``parent_file_id`` → the archive) stored
content-addressed like a direct upload, then driven through the normal
``ingest_file`` router. The archive row's final status aggregates its
children: ``indexed`` when at least one child indexed, else ``needs_review``.

Safety: member names are validated against zip-slip (absolute paths, ``..``);
nested archives are rejected per-member; member count and total uncompressed
size are capped before any extraction. Metadata junk (``__MACOSX/``,
``.DS_Store``) is skipped silently.

Idempotent re-ingest: children are matched to existing rows by
``(filename, sha256)`` and reused, so per-file idempotency downstream
(chunk replacement, derived-table re-registration) applies; unmatched
leftovers from a previous run are deleted along with their chunks.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from typing import Any, Callable, Optional

from src.corpus_allowlist import MAX_UPLOAD_BYTES, classify
from src.file_storage import store_corpus_bytes
from src.ingest.confluence import normalize_html
from src.repositories import corpus_chunks_repo, corpus_files_repo

logger = logging.getLogger(__name__)

MAX_BUNDLE_MEMBERS = 1000
MAX_BUNDLE_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed

_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_BASENAMES = {".DS_Store", "Thumbs.db"}


def _is_unsafe(name: str) -> bool:
    """True for member names that could escape the extraction root."""
    if name.startswith(("/", "\\")):
        return True
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return True
    # Windows drive letters ("C:\...") in the first segment.
    return ":" in norm.split("/", 1)[0]


def _is_junk(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or posixpath.basename(name) in _SKIP_BASENAMES


def ingest_bundle(
    corpus_id: str,
    file_id: str,
    storage_path: str,
    *,
    ingest_child: Optional[Callable[[str], str]] = None,
) -> str:
    """Unpack the archive at ``storage_path`` and ingest each member.

    Sets the archive row's own ``processing_status`` in every path and
    returns it (``indexed | needs_review | rejected``). ``ingest_child``
    is injectable for tests; production uses ``ingest_file``.
    """
    if ingest_child is None:
        from src.ingest.runner import ingest_file as ingest_child  # circular-safe

    cf_repo = corpus_files_repo()

    try:
        zf = zipfile.ZipFile(storage_path)
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_junk(i.filename)]
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("bundle open failed file_id=%s: %s", file_id, exc)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": "invalid_archive"})
        return "rejected"

    with zf:
        return _ingest_bundle_members(zf, infos, file_id, corpus_id, cf_repo, ingest_child)


def _ingest_bundle_members(
    zf: "zipfile.ZipFile",
    infos: list,
    file_id: str,
    corpus_id: str,
    cf_repo: Any,
    ingest_child: Callable[[str], str],
) -> str:
    if len(infos) > MAX_BUNDLE_MEMBERS:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "too_many_members", "members": len(infos), "max": MAX_BUNDLE_MEMBERS},
        )
        return "rejected"
    if sum(i.file_size for i in infos) > MAX_BUNDLE_TOTAL_BYTES:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "bundle_too_large", "max_bytes": MAX_BUNDLE_TOTAL_BYTES},
        )
        return "rejected"

    # Existing children from a previous run, for row reuse.
    prior = {(k["filename"], k["sha256"]): k for k in cf_repo.list_children(file_id)}
    kept_ids: set[str] = set()
    counts: dict[str, int] = {"indexed": 0, "rejected": 0, "needs_review": 0, "pending": 0, "processing": 0}
    children = 0

    def _add_rejected(name: str, size: int, reason: str) -> None:
        cid = cf_repo.add(
            corpus_id=corpus_id,
            filename=name,
            sha256="",
            file_type=None,
            size_bytes=size,
            storage_path=None,
            parent_file_id=file_id,
        )
        cf_repo.set_status(cid, status="rejected", detail={"reason": reason})
        kept_ids.add(cid)
        counts["rejected"] += 1

    for info in infos:
        name = info.filename
        children += 1

        if _is_unsafe(name):
            _add_rejected(name, info.file_size, "unsafe_path")
            continue
        tier = classify(name)
        if tier == "bundle":
            _add_rejected(name, info.file_size, "nested_archive_unsupported")
            continue
        if tier is None:
            _add_rejected(name, info.file_size, "unsupported_type")
            continue
        if info.file_size > MAX_UPLOAD_BYTES:
            _add_rejected(name, info.file_size, "member_too_large")
            continue

        data = zf.read(info)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("html", "htm"):
            data = normalize_html(data)
        if not data:
            _add_rejected(name, info.file_size, "empty_member")
            continue

        stored = store_corpus_bytes(corpus_id, name, data)
        existing = prior.get((name, stored.sha256))
        if existing:
            child_id = existing["id"]
        else:
            child_id = cf_repo.add(
                corpus_id=corpus_id,
                filename=name,
                sha256=stored.sha256,
                file_type=stored.ext.lstrip(".") or None,
                size_bytes=stored.size_bytes,
                storage_path=stored.storage_path,
                parent_file_id=file_id,
            )
        kept_ids.add(child_id)
        status = ingest_child(child_id)
        counts[status] = counts.get(status, 0) + 1

    # Prune children from a prior run that no longer match (renamed/changed).
    chunks_repo = corpus_chunks_repo()
    for row in prior.values():
        if row["id"] not in kept_ids:
            chunks_repo.delete_for_file(row["id"])
            cf_repo.delete(row["id"])

    detail = {"kind": "bundle", "children": children, **counts}
    if counts["indexed"] > 0:
        cf_repo.set_status(file_id, status="indexed", detail=detail)
        return "indexed"
    cf_repo.set_status(file_id, status="needs_review", detail={**detail, "reason": "no_member_indexed"})
    return "needs_review"
