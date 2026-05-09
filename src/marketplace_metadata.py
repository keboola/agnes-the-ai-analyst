"""Parser + resolver for upstream ``.claude-plugin/agnes-metadata.json``.

Each curated marketplace repo can ship a sibling file next to ``marketplace.json``
that adds Agnes-only enrichment (cover photos, video URLs, doc links, category
overrides) per plugin / skill / agent. Claude Code ignores this file because
its contract reads only ``marketplace.json`` — see
``app/marketplace_server/packager.py`` for the ZIP-stripping rule that keeps
the synth Claude Code marketplace clean of Agnes-only files.

Two read paths exist:

* :func:`read_agnes_metadata` — invoked from the sync pipeline
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
from typing import Any, Dict, List, Tuple

from src.marketplace_assets import (
    DocLinkRef,
    parse_cover_photo_ref,
    parse_doc_link,
)

logger = logging.getLogger(__name__)

AGNES_METADATA_REL = Path(".claude-plugin") / "agnes-metadata.json"
"""Path inside the cloned marketplace working tree where the file is expected.
Sibling to ``marketplace.json`` so curators have one well-known place to put
both Claude Code and Agnes-side metadata."""


def read_agnes_metadata(marketplace_root: Path) -> Dict[str, Any]:
    """Load the agnes-metadata.json document from a cloned marketplace.

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
    path = marketplace_root / AGNES_METADATA_REL
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("agnes-metadata: %s unreadable: %s", path, e)
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        logger.warning(
            "agnes-metadata: %s malformed JSON, treating as empty: %s",
            path, e,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "agnes-metadata: %s top-level must be an object, got %s",
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


def resolve_plugin_metadata(
    metadata: Dict[str, Any],
    plugin_name: str,
) -> Dict[str, Any]:
    """Resolve plugin-level enrichment into the dict shape persisted to DB.

    Returns a dict with keys (any of which may be missing when the upstream
    file didn't supply that field):

    * ``cover_photo_ref`` — ``("internal", path)`` or ``("external", url)``
      tuple, or ``None``. The caller (sync pipeline) feeds it through the
      asset mirror to produce the final served URL.
    * ``video_url`` — string or ``None``. Always external; never mirrored.
    * ``category`` — string or ``None``. Overrides ``marketplace.json``
      category for this plugin.
    * ``doc_links`` — list of :class:`DocLinkRef`. May be empty.
    * ``raw_section`` — the original dict (for the inner-detail path that
      needs to drill into ``skills`` / ``agents``).
    """
    section = get_plugin_section(metadata, plugin_name)
    if not section:
        return {}

    log_prefix = f"agnes-metadata plugin={plugin_name}:"
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
        f"agnes-metadata plugin={plugin_name} {kind[:-1]}={inner_name}:"
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


