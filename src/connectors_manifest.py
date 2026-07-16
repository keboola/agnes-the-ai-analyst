"""Connector manifest — scan seed-resident SKILL.md files, parse the
``connector:`` YAML frontmatter block, validate, return a stable list for
the install-prompt renderer and the ``/api/connectors/manifest`` endpoint.

The manifest source is the seed repo (operator-configured Initial Workspace
Template > bundled snapshot in the wheel). The seed's
``workspace/.claude/skills/connector-*/SKILL.md`` files are the SINGLE
source of truth — adding a fourth connector means dropping a new
``connector-newvendor/SKILL.md`` into the seed. The Agnes server reads
metadata from the frontmatter and renders a tile block; no Python edits
needed.

Validation is fail-soft: a malformed connector is SKIPPED with an
``audit_log`` warning, the rest of the manifest still renders. This avoids
a single bad seed commit hard-failing ``/home`` for every analyst.

Caching: in-process LRU keyed by ``(source_signature, file_hash_tuple)``
where ``source_signature`` is ``last_commit_sha`` for the IWT clone or
``"bundled"`` for the wheel snapshot. Admin "Sync now" advances the commit
SHA → cache miss → re-scan. No TTL is needed — both invalidation triggers
(file change in the seed clone, redeploy of the bundle) flip the cache
key automatically.

Codex review v1 fixes baked in:
  * H-2: frontmatter validated (required fields, length caps ≤200,
    HTML stripped on parse, types checked); invalid entries skipped with
    warning rather than failing the whole scan.
  * H-4: cache key uses commit SHA + file hash, NOT directory mtime
    (mtime misses nested SKILL.md content changes).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.initial_workspace import (
    bundled_seed_path,
    get_initial_workspace_dir,
    is_configured,
    list_seed_files,
)

logger = logging.getLogger(__name__)

# Seed-relative root for connector skill discovery.
_CONNECTOR_SKILLS_ROOT = "workspace/.claude/skills"

# Frontmatter parser caps. Defense in depth on top of CI lint in the seed.
_MAX_DISPLAY_LEN = 200
_MAX_SUMMARY_LEN = 200
_MAX_URL_LEN = 500

# Acceptable values for ``estimated_minutes``. Negative / absurd values are
# operator typos; clamp the schema rather than rendering them.
_MIN_MINUTES = 0
_MAX_MINUTES = 120


@dataclass(frozen=True)
class ConnectorEntry:
    """One validated connector entry as it appears in the manifest.

    Field set matches plan v5 §"Manifest contract" minus ``category`` and
    ``icon`` (deferred to the post-init connectors panel — out of v1
    scope). Adding fields requires updating ``parse_frontmatter`` AND
    bumping ``SCHEMA_VERSION`` in the API response.

    ``required=True`` moves the connector out of the optional Y/n tile
    list into the install prompt's mandatory "Install required tools"
    step (no per-tool ask; rendered before the optional tiles).
    """

    slug: str
    display_name: str
    short_summary: str
    estimated_minutes: int
    vendor_url: Optional[str] = None
    requires_oauth_app: bool = False
    required: bool = False


# Cache: { cache_key: list[ConnectorEntry] }. Single-process; refreshed
# whenever the IWT clone's commit_sha advances OR the bundled snapshot
# rotates (the wheel was redeployed and the module reloaded).
_cache: dict[tuple[str, str], list[ConnectorEntry]] = {}
_cache_lock = threading.Lock()


def _strip_html(value: str) -> str:
    """Defang HTML/JS injected into a display field. The renderer escapes
    on output too, but stripping here keeps the manifest API surface
    plaintext — JSON consumers (admin UI, agnes refresh-config) don't
    accidentally render markup.

    Order matters. Unescape entities FIRST so an obfuscated payload like
    ``&lt;script&gt;alert(1)&lt;/script&gt;`` resolves to literal
    ``<script>...`` markup, then strip the tags. Loop until stable so
    nested patterns (``<scr<script>ipt>``) can't survive one pass.
    """
    prev = None
    out = value
    while out != prev:
        prev = out
        out = html.unescape(out)
        out = re.sub(r"<[^>]*>", "", out)
    return out.strip()


def _parse_frontmatter(text: str) -> Optional[dict]:
    """Extract the YAML frontmatter block from a SKILL.md and return its
    parsed dict, or ``None`` if the file is not frontmatter-headed.

    Tolerates Windows line endings, surrounding blank lines, and a missing
    closing ``---`` (treated as the same parse failure as YAML errors).
    Uses ``yaml.safe_load`` — no constructors fire.
    """
    import yaml  # local: keep module import cheap when manifest isn't queried

    # Frontmatter must start at the very beginning of the file (modulo
    # leading whitespace) for SKILL.md to be a valid Claude Code skill.
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    body = stripped[3:]  # drop opening ---
    # Find closing fence; if absent, file is malformed.
    end_match = re.search(r"^---\s*$", body, re.MULTILINE)
    if not end_match:
        return None
    yaml_text = body[: end_match.start()]
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        logger.warning("connectors_manifest: YAML parse failed: %s", e)
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate(slug: str, raw: dict) -> Optional[ConnectorEntry]:
    """Validate one frontmatter block. Returns a ``ConnectorEntry`` on
    success, ``None`` on failure (with a warning logged). Fail-soft: a
    single bad connector should not break the whole manifest.
    """
    connector_block = raw.get("connector")
    if not isinstance(connector_block, dict):
        logger.warning(
            "connectors_manifest: %s missing or non-dict `connector:` block — skipped",
            slug,
        )
        return None

    display_name = connector_block.get("display_name")
    short_summary = connector_block.get("short_summary")
    estimated_minutes = connector_block.get("estimated_minutes")
    vendor_url = connector_block.get("vendor_url")
    requires_oauth_app = connector_block.get("requires_oauth_app", False)
    required = connector_block.get("required", False)

    # Required fields + types
    for field, value, expected_type in (
        ("display_name", display_name, str),
        ("short_summary", short_summary, str),
        ("estimated_minutes", estimated_minutes, int),
    ):
        if not isinstance(value, expected_type):
            logger.warning(
                "connectors_manifest: %s connector.%s missing or wrong type "
                "(got %r) — skipped",
                slug, field, type(value).__name__,
            )
            return None

    # Length caps + sanitize
    display_clean = _strip_html(display_name)[:_MAX_DISPLAY_LEN]
    summary_clean = _strip_html(short_summary)[:_MAX_SUMMARY_LEN]
    if not display_clean or not summary_clean:
        logger.warning(
            "connectors_manifest: %s display_name or short_summary empty "
            "after sanitization — skipped",
            slug,
        )
        return None

    # Estimated minutes — clamp absurd values rather than reject
    minutes = max(_MIN_MINUTES, min(_MAX_MINUTES, int(estimated_minutes)))

    # vendor_url: optional, must be plausible http(s) when present
    vendor_url_clean: Optional[str] = None
    if vendor_url is not None:
        if not isinstance(vendor_url, str) or len(vendor_url) > _MAX_URL_LEN:
            logger.warning(
                "connectors_manifest: %s vendor_url malformed — dropped",
                slug,
            )
        elif vendor_url.startswith(("http://", "https://")):
            vendor_url_clean = vendor_url

    return ConnectorEntry(
        slug=slug,
        display_name=display_clean,
        short_summary=summary_clean,
        estimated_minutes=minutes,
        vendor_url=vendor_url_clean,
        requires_oauth_app=bool(requires_oauth_app),
        required=bool(required),
    )


def _source_signature() -> str:
    """Identify the current source state for cache invalidation.

    Returns ``last_commit_sha`` when an IWT clone exists; the literal
    string ``"bundled"`` when falling back to the wheel-shipped snapshot.
    Either advances on a real content change: IWT sync updates the SHA,
    bundle rotation re-imports the module (Python doesn't cache across
    process restarts).
    """
    if is_configured():
        try:
            from app.api.initial_workspace import _read_section
            sha = _read_section().get("last_commit_sha") or ""
            return f"iwt:{sha}" if sha else "iwt:unsynced"
        except Exception:
            logger.exception("connectors_manifest: failed to read IWT commit SHA")
            return "iwt:unknown"
    return "bundled"


def _hash_paths(paths: list[Path]) -> str:
    """Hash a list of (path, size) tuples. The size catches in-place edits
    that don't change mtime; the path ordering catches additions/deletions.
    For SKILL.md files we could hash content, but size+path is enough to
    detect any meaningful change and is O(N) cheap.
    """
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            stat = p.stat()
            h.update(str(p).encode())
            h.update(b"\0")
            h.update(str(stat.st_size).encode())
            h.update(b"\0")
        except OSError:
            # File vanished between rglob() and stat() — skip silently;
            # cache will miss on next scan when it's gone.
            continue
    return h.hexdigest()[:16]


def load_manifest() -> list[ConnectorEntry]:
    """Return the validated connector manifest, sorted deterministically.

    Resolution: when an IWT clone has ANY ``connector-*/`` skill, the IWT
    is the source of truth and the bundle is ignored. When the IWT has
    none (or no IWT is configured), the bundled snapshot wins.

    The two tiers never mix — preventing a partial-override surprise where
    the operator adds a new connector but inadvertently inherits two
    bundled ones they meant to replace.
    """
    # Find connector SKILL.md files via the seed-resolution helper.
    all_files = list_seed_files(_CONNECTOR_SKILLS_ROOT)
    connector_files = [
        p for p in all_files
        if p.parent.name.startswith("connector-") and p.name == "SKILL.md"
    ]

    cache_key = (_source_signature(), _hash_paths(connector_files))
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    entries: list[ConnectorEntry] = []
    for path in connector_files:
        slug = path.parent.name  # e.g. "connector-asana"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("connectors_manifest: %s read failed: %s", slug, e)
            continue
        frontmatter = _parse_frontmatter(text)
        if frontmatter is None:
            logger.warning(
                "connectors_manifest: %s frontmatter parse failed — skipped",
                slug,
            )
            continue
        entry = _validate(slug, frontmatter)
        if entry is not None:
            entries.append(entry)

    # Deterministic order: alphabetical by display_name for tile rendering
    # stability. Two operator edits that reorder the seed files won't
    # change the visible order — only renames or additions move tiles.
    entries.sort(key=lambda e: e.display_name.lower())

    with _cache_lock:
        _cache[cache_key] = entries
    return entries


def invalidate_cache() -> None:
    """Drop the in-process cache. Called by the IWT sync endpoint after a
    successful clone update so the next render scan picks up the new
    SKILL.md files immediately.
    """
    with _cache_lock:
        _cache.clear()
