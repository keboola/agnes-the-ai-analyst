"""Extension-to-tier classification for corpus file uploads.

Two tiers decide how an uploaded file is processed:

* **tier1** — text/document formats the ingestion pipeline can currently
  extract text from (PDF, Office, plain text, structured data).
* **tier2** — image formats stored now and processed later via vision/OCR
  (Slice 5); accepted and written to disk today with status ``'pending'``.
* **None** — unsupported; upload is rejected with HTTP 422.

100 MiB ceiling per file. The cap is enforced during streaming by
``src.file_storage.store_corpus_file`` — rejected before any bytes land
on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIER1_EXTENSIONS: frozenset[str] = frozenset(
    {
        "txt",
        "md",
        "html",
        "rtf",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "xlsx",
        "parquet",
        "docx",
        "pptx",
        "epub",
        "eml",
        "msg",
        "pdf",
    }
)

TIER2_EXTENSIONS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "tif",
        "tiff",
    }
)

# 100 MiB — roomy enough for realistic document uploads; blocks accidental
# camera dumps and large binary assets that would swamp the ingestion queue.
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify(filename: str) -> Optional[str]:
    """Return ``'tier1'``, ``'tier2'``, or ``None`` (unsupported / reject).

    Classification is based solely on the file extension (lower-cased).
    Files without an extension always return ``None``.

    Args:
        filename: Original filename from the upload (e.g. ``"report.PDF"``).
                  The stem is irrelevant; only the suffix is examined.

    Returns:
        ``'tier1'`` for text/document formats, ``'tier2'`` for image formats,
        ``None`` for unsupported or extension-less files.
    """
    if not filename:
        return None
    suffix = Path(filename).suffix
    if not suffix:
        return None
    ext = suffix.lstrip(".").lower()
    if not ext:
        return None
    if ext in TIER1_EXTENSIONS:
        return "tier1"
    if ext in TIER2_EXTENSIONS:
        return "tier2"
    return None
