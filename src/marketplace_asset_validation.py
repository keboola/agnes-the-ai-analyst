"""Asset allowlists + validators shared between the curated marketplace mirror
flow (``src/marketplace_asset_mirror.py``) and the Flea / Store upload flow
(``app/api/store.py``).

Two allowlists are exposed:

* **Documents** — PDF, Markdown, plain text. The set is deliberately narrow so
  that what we serve back to users is something a browser can render directly
  or download cleanly. HTML and DOCX are rejected (HTML has unbounded
  external-asset dependencies and looks broken offline; DOCX is opaque to
  most readers).
* **Images** — PNG, JPEG, WEBP. SVG is rejected because inline ``<script>``
  inside an SVG is a ready-made XSS vector when the file is served with the
  ``image/svg+xml`` Content-Type.

Validators come in two shapes:

* :func:`validate_doc_file` / :func:`validate_image_file` — for **already
  downloaded** bytes (Flea uploads, mirror cache writes).
* :func:`accept_doc_response` / :func:`accept_image_response` — for **HTTP
  responses** during external-URL mirroring, where the body may not yet be
  in memory and the decision needs to be made from the URL + Content-Type.

All functions return a small ``(ok, reason)`` tuple instead of raising, so
the caller decides whether a rejection is a HTTP 400 (Flea) or a silent log
(curated mirror).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Allowlist constants
# ---------------------------------------------------------------------------

DOC_EXTENSIONS = (".pdf", ".md", ".markdown", ".txt")
"""Lowercase extensions accepted as documents. Used as both the MIME-fallback
hint (when servers send ``application/octet-stream``) and the Flea ``accept``
attribute source-of-truth."""

DOC_CONTENT_TYPES = (
    "application/pdf",
    "text/markdown",
    "text/x-markdown",
    "text/plain",
)
"""Content-Types unambiguously accepted for documents."""

DOC_GENERIC_CONTENT_TYPES = (
    "application/octet-stream",
    "application/x-download",
    "binary/octet-stream",
)
"""Generic Content-Types that need an extension match before acceptance.
Real-world CDNs frequently send these for ``.md`` / ``.pdf`` files when the
MIME database doesn't have a hit."""

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

IMAGE_CONTENT_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
)

# Magic-bytes prefix for PDF files — first 4 bytes are always ``%PDF`` regardless
# of PDF version. We don't try to validate Markdown / plain text by sniffing
# (every byte sequence is "valid" Markdown) — for those we rely on extension.
_PDF_MAGIC = b"%PDF"

# Magic-bytes prefixes for image formats. Used as a belt-and-suspenders check
# alongside Content-Type so an attacker can't trivially smuggle an SVG through
# a renamed ``.png`` file.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
# WEBP is RIFF-wrapped: first 4 bytes "RIFF", bytes 8-12 "WEBP".
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # convenience for `if result:`
        return self.ok


_OK = ValidationResult(True)


def _ext(path_or_url: str) -> str:
    """Return the lowercase trailing extension of ``path_or_url`` (with dot).

    Works on bare filenames, paths, and URLs (query strings are dropped).
    Returns ``""`` when there is no extension.
    """
    if not path_or_url:
        return ""
    # Strip query / fragment so URLs like https://x/file.pdf?token=… are
    # classified by their visible extension.
    cleaned = path_or_url.split("?", 1)[0].split("#", 1)[0]
    suffix = PurePosixPath(cleaned).suffix
    return suffix.lower() if suffix else ""


def _normalize_content_type(value: str) -> str:
    """Strip parameters (``; charset=…``) and lowercase. Returns ``""`` for None."""
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# External URL detection
# ---------------------------------------------------------------------------

_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_external_url(value: str) -> bool:
    """Return True when ``value`` looks like an absolute http(s) URL.

    Used to discriminate between ``cover_photo: ".agnes/cover.png"`` (internal
    git-tree path) and ``cover_photo: "https://cdn.example.com/cover.png"``
    (external URL — eligible for the asset mirror).
    """
    return bool(value) and bool(_HTTP_URL_RE.match(value.strip()))


# ---------------------------------------------------------------------------
# Body-based validators (Flea uploads, mirror cache writes)
# ---------------------------------------------------------------------------


def validate_doc_file(filename: str, body: bytes) -> ValidationResult:
    """Accept iff filename has an allowed extension AND (for PDF) magic bytes match.

    Markdown and plain text aren't sniffed — any byte sequence is technically
    valid text. We rely on the extension for those. PDF gets the magic-byte
    check because mislabeled ``.pdf`` files (someone renamed an EXE) are a
    real concern.
    """
    ext = _ext(filename)
    if ext not in DOC_EXTENSIONS:
        return ValidationResult(
            False,
            f"unsupported_doc_extension: {ext or '(none)'} not in {DOC_EXTENSIONS}",
        )
    if ext == ".pdf" and not body.startswith(_PDF_MAGIC):
        return ValidationResult(False, "pdf_magic_bytes_mismatch")
    return _OK


def validate_image_file(filename: str, body: bytes) -> ValidationResult:
    """Accept iff extension is in the image allowlist AND magic bytes match.

    SVG is not in the allowlist — it isn't ``image/svg+xml`` here even if the
    extension says so. Magic bytes are the authoritative signal: a renamed
    ``payload.png`` carrying SVG XML fails this check.
    """
    ext = _ext(filename)
    if ext not in IMAGE_EXTENSIONS:
        return ValidationResult(
            False,
            f"unsupported_image_extension: {ext or '(none)'} not in {IMAGE_EXTENSIONS}",
        )
    if ext == ".png" and not body.startswith(_PNG_MAGIC):
        return ValidationResult(False, "png_magic_bytes_mismatch")
    if ext in (".jpg", ".jpeg") and not body.startswith(_JPEG_MAGIC):
        return ValidationResult(False, "jpeg_magic_bytes_mismatch")
    if ext == ".webp":
        # WEBP is "RIFF" + 4 bytes size + "WEBP". Need at least 12 bytes.
        if len(body) < 12 or body[:4] != _WEBP_RIFF or body[8:12] != _WEBP_TAG:
            return ValidationResult(False, "webp_magic_bytes_mismatch")
    return _OK


# ---------------------------------------------------------------------------
# Response-based validators (curated mirror — pre-download checks)
# ---------------------------------------------------------------------------


def accept_doc_response(url: str, content_type: str) -> ValidationResult:
    """Should we mirror this external doc URL based on its HTTP HEAD response?

    Resolution order:

    1. Content-Type matches an unambiguous doc allowlist entry → accept.
    2. Content-Type is generic (octet-stream / x-download) AND URL extension
       matches → accept (real-world CDN behavior for ``.md`` / ``.pdf``).
    3. Otherwise reject.

    HTML page links (Confluence, Notion, GitHub Wiki) don't survive this
    filter — ``text/html`` is not in either list. The caller's contract for
    rejected entries is to skip the mirror but keep the original URL as a
    plain external link in the served ``doc_links`` (b1 fallback).
    """
    ct = _normalize_content_type(content_type)
    if ct in DOC_CONTENT_TYPES:
        return _OK
    if ct in DOC_GENERIC_CONTENT_TYPES and _ext(url) in DOC_EXTENSIONS:
        return _OK
    return ValidationResult(
        False, f"doc_content_type_rejected: {ct or '(empty)'}"
    )


def accept_image_response(url: str, content_type: str) -> ValidationResult:
    """Should we mirror this external image URL based on its HTTP HEAD response?

    Stricter than docs — an image must report an explicit ``image/png``,
    ``image/jpeg``, or ``image/webp`` Content-Type. Generic octet-stream is
    NOT accepted for images because the downstream renderer needs to know
    the format and ``<img src>`` won't sniff the body.
    """
    ct = _normalize_content_type(content_type)
    if ct in IMAGE_CONTENT_TYPES:
        return _OK
    return ValidationResult(
        False, f"image_content_type_rejected: {ct or '(empty)'}"
    )


# ---------------------------------------------------------------------------
# Convenience helpers used by the marketplace-metadata.json parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocLinkRef:
    """A single ``doc_links[]`` entry resolved into one of three shapes:

    * ``kind="internal"`` → ``path`` set, points at a file in the cloned repo.
    * ``kind="external"`` → ``url`` set, original URL (used after a successful
      mirror is replaced with ``mirrored_key`` by the asset-mirror layer, or
      when mirroring failed and we link out).
    * ``kind="mirrored"`` → ``url`` is the original; ``mirrored_key`` is set
      to the cache lookup key. The marketplace-metadata parser only produces
      ``internal`` and ``external`` — the mirror layer flips ``external`` to
      ``mirrored`` after a successful fetch.
    """
    name: str
    kind: str  # "internal" | "external" | "mirrored"
    path: str = ""
    url: str = ""
    mirrored_key: str = ""


def parse_doc_link(entry: dict) -> Tuple[bool, DocLinkRef | str]:
    """Validate one ``doc_links[]`` dict from marketplace-metadata.json.

    Returns ``(True, DocLinkRef)`` on accept, ``(False, reason)`` on reject.
    Rejection reasons surface to the sync log so the curator can fix them.

    Schema rules:
    - ``name`` required (string).
    - Exactly one of ``path`` or ``url``. Both → reject (ambiguous).
    - ``path`` (when present) must not start with ``/`` and must not contain
      ``..`` segments — the asset endpoint enforces this again at serve time
      but rejecting at parse time means the entry never reaches the cache.
    - ``url`` (when present) must be ``http(s)://``.
    """
    if not isinstance(entry, dict):
        return False, "doc_link_not_object"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "doc_link_missing_name"
    has_path = "path" in entry
    has_url = "url" in entry
    if has_path == has_url:
        return False, "doc_link_must_have_exactly_one_of_path_or_url"
    if has_path:
        path = entry["path"]
        if not isinstance(path, str) or not path.strip():
            return False, "doc_link_path_empty"
        if path.startswith("/") or ".." in PurePosixPath(path).parts:
            return False, "doc_link_path_traversal_or_absolute"
        # Internal paths must point at an allowlisted document type (PDF /
        # Markdown / plain text). The serve endpoint enforces this again at
        # download time, but rejecting at parse time means the entry never
        # reaches the served `doc_links` list at all — exactly the user-facing
        # contract: "any URL Agnes can't render as a real document is treated
        # as if it weren't there."
        if _ext(path) not in DOC_EXTENSIONS:
            return False, (
                f"doc_link_path_unsupported_extension: {_ext(path) or '(none)'} "
                f"not in {DOC_EXTENSIONS}"
            )
        return True, DocLinkRef(name=name.strip(), kind="internal", path=path)
    url = entry["url"]
    if not isinstance(url, str) or not is_external_url(url):
        return False, "doc_link_url_must_be_http_or_https"
    # External URLs whose final extension is unambiguously NOT in the doc
    # allowlist are dropped early — saves the mirror layer from a wasted HEAD
    # request on something we'd never accept anyway. URLs without a clear
    # extension still pass through (e.g. CDN pretty paths) and the mirror's
    # Content-Type check decides at fetch time.
    ext = _ext(url)
    if ext and ext not in DOC_EXTENSIONS:
        return False, (
            f"doc_link_url_unsupported_extension: {ext} not in {DOC_EXTENSIONS}"
        )
    return True, DocLinkRef(name=name.strip(), kind="external", url=url)


def parse_cover_photo_ref(value: object) -> Tuple[bool, Tuple[str, str] | str]:
    """Resolve a ``cover_photo`` value into ``(kind, target)``.

    Accepts:
    * external URL (``http(s)://...``) → ``("external", url)``.
    * internal git-tree path → ``("internal", path)``.
    * empty / None / non-string → reject silently (callers tolerate absence).

    The internal-path branch validates against directory traversal at parse
    time. The serving endpoint validates again with ``Path.resolve()`` so the
    parser-time check is defense-in-depth, not the only gate.
    """
    if value is None or value == "":
        return False, "cover_photo_empty"
    if not isinstance(value, str):
        return False, "cover_photo_not_string"
    v = value.strip()
    if is_external_url(v):
        return True, ("external", v)
    if v.startswith("/") or ".." in PurePosixPath(v).parts:
        return False, "cover_photo_path_traversal_or_absolute"
    return True, ("internal", v)
