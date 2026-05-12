"""Parser + resolver for upstream ``.claude-plugin/marketplace-metadata.json``.

Each curated marketplace repo can ship a sibling file next to ``marketplace.json``
that adds Agnes-only enrichment (cover photos, video URLs, doc links, category
overrides) per plugin / skill / agent. Claude Code ignores this file because
its contract reads only ``marketplace.json`` — see
``app/marketplace_server/packager.py`` for the ZIP-stripping rule that keeps
the synth Claude Code marketplace clean of Agnes-only files.

Two read paths exist:

* :func:`read_marketplace_metadata` — invoked from the sync pipeline
  (``src/marketplace.py``). Lenient: missing file, malformed JSON, and partial
  schemas all fall back to ``{}`` rather than aborting the sync.
* :func:`resolve_plugin_metadata` — given a parsed metadata blob and a plugin
  name, return the plugin-level enrichment as a dict ready for
  :meth:`MarketplacePluginsRepository.replace_for_marketplace` (with
  ``cover_photo_url`` / ``video_url`` / ``doc_links`` / ``category`` keys
  resolved into served-URL form).

Skill / agent sub-trees stay nested in the parsed blob — the inner-detail
endpoint reads them on demand at request time. The reasoning lives in the
plan: keeping per-skill metadata out of DuckDB matches the existing pattern
where SKILL.md frontmatter is parsed lazily from disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.marketplace_asset_validation import (
    DocLinkRef,
    parse_cover_photo_ref,
    parse_doc_link,
)

logger = logging.getLogger(__name__)

MARKETPLACE_METADATA_REL = Path(".claude-plugin") / "marketplace-metadata.json"
"""Path inside the cloned marketplace working tree where the file is expected.
Sibling to ``marketplace.json`` so curators have one well-known place to put
both Claude Code and Agnes-side metadata."""

MARKETPLACE_METADATA_MAX_BYTES = 1 * 1024 * 1024
"""Hard cap on the size of an marketplace-metadata.json file.

The file is curator-controlled and read into memory in full before parsing.
Without a cap, a curator could commit a multi-GB document and OOM the sync
worker (or — more interestingly — slip past the size check and use deep
nesting to blow the parser's recursion stack). 1 MB is generous: a maximal
real-world metadata file with covers, docs, and categories for ~50 plugins
sits well under 100 KB."""


def read_marketplace_metadata(marketplace_root: Path) -> Dict[str, Any]:
    """Load the marketplace-metadata.json document from a cloned marketplace.

    Returns the parsed dict on success, or ``{}`` when the file is missing,
    unreadable, or contains malformed JSON. A malformed file logs a warning
    so the curator notices on the admin sync log — but never aborts the
    upstream sync, mirroring the existing handling in
    :func:`src.marketplace.read_plugins`.

    The schema is documented in ``docs/curated-marketplace-format.md``.
    Top-level shape::

        {
          "version": 1,
          "plugins": {
            "<plugin-name>": { ...plugin-level enrichment... }
          }
        }
    """
    path = marketplace_root / MARKETPLACE_METADATA_REL
    if not path.is_file():
        return {}
    try:
        size = path.stat().st_size
    except OSError as e:
        logger.warning("marketplace-metadata: %s stat failed: %s", path, e)
        return {}
    if size > MARKETPLACE_METADATA_MAX_BYTES:
        logger.warning(
            "marketplace-metadata: %s exceeds %d-byte cap (%d bytes), refusing to read",
            path, MARKETPLACE_METADATA_MAX_BYTES, size,
        )
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("marketplace-metadata: %s unreadable: %s", path, e)
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        # ValueError covers malformed-JSON; RecursionError covers a curator
        # who tries to crash the sync via deeply-nested structure that fits
        # under the size cap (e.g. ``{"a":{"a":{"a":...}}}``). Both reduce
        # to the same outcome — degrade gracefully so one bad upstream
        # doesn't abort the whole sync.
        logger.warning(
            "marketplace-metadata: %s parse failed (%s), treating as empty: %s",
            path, type(e).__name__, e,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "marketplace-metadata: %s top-level must be an object, got %s",
            path, type(data).__name__,
        )
        return {}
    return data


def get_plugin_section(metadata: Dict[str, Any], plugin_name: str) -> Dict[str, Any]:
    """Return the per-plugin sub-tree, or ``{}`` if absent or malformed.

    Curator-facing schema is keyed by plugin name (matches the ``name`` field
    in ``marketplace.json``). Stripping nested invalid types keeps downstream
    consumers from special-casing missing keys.
    """
    plugins = metadata.get("plugins") if isinstance(metadata, dict) else None
    if not isinstance(plugins, dict):
        return {}
    section = plugins.get(plugin_name)
    return section if isinstance(section, dict) else {}


def get_inner_section(
    metadata: Dict[str, Any],
    plugin_name: str,
    kind: str,
    inner_name: str,
) -> Dict[str, Any]:
    """Return the per-skill / per-agent sub-tree under a plugin.

    ``kind`` must be ``"skills"`` or ``"agents"``. Returns ``{}`` when any
    layer of the lookup chain is missing or the wrong type.
    """
    if kind not in ("skills", "agents"):
        return {}
    plugin_section = get_plugin_section(metadata, plugin_name)
    inner_map = plugin_section.get(kind)
    if not isinstance(inner_map, dict):
        return {}
    inner = inner_map.get(inner_name)
    return inner if isinstance(inner, dict) else {}


def _validated_doc_links(raw: Any, log_prefix: str) -> List[DocLinkRef]:
    """Run each ``doc_links[]`` entry through :func:`parse_doc_link`.

    Rejected entries are logged at WARNING and dropped — surviving entries are
    returned in source order so the curator's ordering is preserved in the UI.
    """
    if not isinstance(raw, list):
        return []
    out: List[DocLinkRef] = []
    for i, entry in enumerate(raw):
        ok, value = parse_doc_link(entry)
        if not ok:
            logger.warning("%s doc_links[%d] rejected: %s", log_prefix, i, value)
            continue
        out.append(value)  # type: ignore[arg-type]
    return out


def _validated_string(raw: Any, field_name: str, log_prefix: str) -> str:
    """Return ``raw`` stripped, or ``""`` for non-string / empty values.

    Used for the plain-text rich fields (display_name, tagline) where the
    curator-facing UI requires a single line. Markdown bodies (description,
    sample_interaction.assistant) skip this and keep multi-line content.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        logger.warning(
            "%s %s rejected: not a string (got %s)",
            log_prefix, field_name, type(raw).__name__,
        )
        return ""
    return raw.strip()


def _validated_markdown(raw: Any, field_name: str, log_prefix: str) -> str:
    """Return ``raw`` stripped of leading / trailing whitespace, preserving
    interior structure (blank lines, indentation) so the markdown renderer
    can interpret paragraphs / lists / fenced code blocks correctly. Empty
    or wrong-type input collapses to ``""``."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        logger.warning(
            "%s %s rejected: not a string (got %s)",
            log_prefix, field_name, type(raw).__name__,
        )
        return ""
    return raw.strip("\n").rstrip()


def _validated_use_cases(raw: Any, log_prefix: str) -> List[Dict[str, str]]:
    """Validate ``use_cases[]`` from a metadata block.

    Each surviving entry is a dict with exactly the three string keys the
    template expects: ``title``, ``description``, ``prompt``. Entries
    missing any of those (or with non-string values) are dropped with a
    warning so the curator can see what went wrong in the sync log.

    Source order is preserved.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "%s use_cases rejected: not a list (got %s)",
            log_prefix, type(raw).__name__,
        )
        return []
    out: List[Dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning(
                "%s use_cases[%d] rejected: not an object", log_prefix, i,
            )
            continue
        title = entry.get("title")
        description = entry.get("description")
        prompt = entry.get("prompt")
        if not all(isinstance(v, str) and v.strip()
                   for v in (title, description, prompt)):
            logger.warning(
                "%s use_cases[%d] rejected: missing title/description/prompt",
                log_prefix, i,
            )
            continue
        out.append({
            "title": title.strip(),                       # type: ignore[union-attr]
            "description": description.strip(),           # type: ignore[union-attr]
            "prompt": prompt.strip(),                     # type: ignore[union-attr]
        })
    return out


def _validated_sample_interaction(
    raw: Any, log_prefix: str,
) -> Optional[Dict[str, str]]:
    """Validate ``sample_interaction``: ``{user, assistant}`` both required.

    Returns ``None`` when either side is missing or wrong-typed — the UI
    only renders this section when both halves of the dialog exist, so
    partial input never reaches the template.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "%s sample_interaction rejected: not an object (got %s)",
            log_prefix, type(raw).__name__,
        )
        return None
    user = raw.get("user")
    assistant = raw.get("assistant")
    if not (isinstance(user, str) and user.strip()
            and isinstance(assistant, str) and assistant.strip()):
        logger.warning(
            "%s sample_interaction rejected: user/assistant both required",
            log_prefix,
        )
        return None
    return {
        "user": user.strip(),
        "assistant": assistant.strip("\n").rstrip(),  # markdown body
    }


def resolve_plugin_metadata(
    metadata: Dict[str, Any],
    plugin_name: str,
) -> Dict[str, Any]:
    """Resolve plugin-level enrichment into the dict shape persisted to DB.

    Returns a dict with keys (any of which may be missing when the upstream
    file didn't supply that field):

    Visual / classification (persisted in ``marketplace_plugins``):
    * ``cover_photo_ref`` — ``("internal", path)`` or ``("external", url)``
      tuple, or ``None``. The caller (sync pipeline) feeds it through the
      asset mirror to produce the final served URL.
    * ``video_url`` — string or ``None``. Always external; never mirrored.
    * ``category`` — string or ``None``. Overrides ``marketplace.json``
      category for this plugin.
    * ``doc_links`` — list of :class:`DocLinkRef`. May be empty.

    Rich user-facing content (read on-demand by the request handlers, NOT
    persisted in the DB — curator edits land immediately, no sync needed):
    * ``display_name`` — string (single line). Friendly name shown on the
      detail-page h1 and listing card, falling back to the raw plugin name
      when missing.
    * ``tagline`` — string (single line). Hero subtitle and 2-line listing
      card description.
    * ``description`` — string (markdown body). Rendered through
      :func:`app.markdown_render.render_safe` before reaching the
      ``description_long_html`` API field.
    * ``use_cases`` — list of ``{title, description, prompt}`` dicts.
    * ``sample_interaction`` — ``{user, assistant}`` dict or ``None``.

    * ``raw_section`` — the original dict (for the inner-detail path that
      needs to drill into ``skills`` / ``agents``).
    """
    section = get_plugin_section(metadata, plugin_name)
    if not section:
        return {}

    log_prefix = f"marketplace-metadata plugin={plugin_name}:"
    out: Dict[str, Any] = {"raw_section": section}

    cover = section.get("cover_photo")
    if cover is not None:
        ok, value = parse_cover_photo_ref(cover)
        if ok:
            out["cover_photo_ref"] = value
        else:
            logger.warning("%s cover_photo rejected: %s", log_prefix, value)

    video = section.get("video_url")
    if isinstance(video, str) and video.strip():
        # video_url is always external; reuse cover_photo_ref's URL test only
        # to keep error reporting consistent. Internal video paths are
        # nonsensical (videos in git → no thanks) so we accept anything that
        # *looks* like an http(s) URL and leave deeper sanity to the
        # frontend embed code.
        if video.strip().lower().startswith(("http://", "https://")):
            out["video_url"] = video.strip()
        else:
            logger.warning("%s video_url must be http(s)://", log_prefix)

    category = section.get("category")
    if isinstance(category, str) and category.strip():
        out["category"] = category.strip()

    out["doc_links"] = _validated_doc_links(section.get("doc_links"), log_prefix)

    # Rich user-facing fields (added 2026-05-12 for plugin-level rich content
    # rendering). All optional — UI sections only render when present.
    display_name = _validated_string(
        section.get("display_name"), "display_name", log_prefix,
    )
    if display_name:
        out["display_name"] = display_name
    tagline = _validated_string(
        section.get("tagline"), "tagline", log_prefix,
    )
    if tagline:
        out["tagline"] = tagline
    description = _validated_markdown(
        section.get("description"), "description", log_prefix,
    )
    if description:
        out["description"] = description
    use_cases = _validated_use_cases(section.get("use_cases"), log_prefix)
    if use_cases:
        out["use_cases"] = use_cases
    sample_interaction = _validated_sample_interaction(
        section.get("sample_interaction"), log_prefix,
    )
    if sample_interaction is not None:
        out["sample_interaction"] = sample_interaction

    return out


def resolve_inner_metadata(
    metadata: Dict[str, Any],
    plugin_name: str,
    kind: str,
    inner_name: str,
) -> Dict[str, Any]:
    """Same shape as :func:`resolve_plugin_metadata`, scoped to skill or agent."""
    section = get_inner_section(metadata, plugin_name, kind, inner_name)
    if not section:
        return {}

    log_prefix = (
        f"marketplace-metadata plugin={plugin_name} {kind[:-1]}={inner_name}:"
    )
    out: Dict[str, Any] = {"raw_section": section}

    cover = section.get("cover_photo")
    if cover is not None:
        ok, value = parse_cover_photo_ref(cover)
        if ok:
            out["cover_photo_ref"] = value
        else:
            logger.warning("%s cover_photo rejected: %s", log_prefix, value)

    video = section.get("video_url")
    if isinstance(video, str) and video.strip():
        if video.strip().lower().startswith(("http://", "https://")):
            out["video_url"] = video.strip()
        else:
            logger.warning("%s video_url must be http(s)://", log_prefix)

    out["doc_links"] = _validated_doc_links(section.get("doc_links"), log_prefix)

    return out


def collect_external_urls(
    plugin_resolved: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Return ``[(kind, url)]`` tuples for every external URL the asset mirror
    needs to fetch for this plugin.

    ``kind`` is one of ``"cover"`` or ``"doc"``, used by the mirror to pick
    the cache sub-directory. Internal paths are skipped — they're served from
    the git working tree directly, no mirror needed.
    """
    urls: List[Tuple[str, str]] = []
    cover_ref = plugin_resolved.get("cover_photo_ref")
    if isinstance(cover_ref, tuple) and cover_ref[0] == "external":
        urls.append(("cover", cover_ref[1]))
    for link in plugin_resolved.get("doc_links") or []:
        if isinstance(link, DocLinkRef) and link.kind == "external":
            urls.append(("doc", link.url))
    return urls


def collect_all_external_urls(
    metadata: Dict[str, Any],
    plugin_name: str,
) -> List[Tuple[str, str]]:
    """Walk plugin + every nested skill / agent and return all external URLs.

    The plugin-level sync flow uses this to seed the mirror fetch list — by
    fetching inner-level external URLs at sync time too, the request-time
    skill / agent detail render can look them up in the manifest and drop
    entries Agnes can't deliver, matching the plugin-level behavior.

    Cover URLs and doc URLs from skills/agents share the per-plugin cache
    namespace (``${DATA_DIR}/marketplace-cache/<slug>/<plugin>/...``) — the
    inner sub-tree is keyed by URL, not by skill name, so two skills inside
    the same plugin pointing at the same external URL share the cache entry.
    """
    out: List[Tuple[str, str]] = []
    plugin_resolved = resolve_plugin_metadata(metadata, plugin_name)
    out.extend(collect_external_urls(plugin_resolved))

    plugin_section = get_plugin_section(metadata, plugin_name)
    for kind in ("skills", "agents"):
        inner_map = plugin_section.get(kind)
        if not isinstance(inner_map, dict):
            continue
        for inner_name in inner_map.keys():
            inner_resolved = resolve_inner_metadata(
                metadata, plugin_name, kind, inner_name,
            )
            out.extend(collect_external_urls(inner_resolved))
    return out


