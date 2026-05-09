"""URL builders for served curated-marketplace assets.

Single source of truth for the three served paths the FastAPI router
under ``app/api/marketplace.py`` exposes::

    /api/marketplace/curated/<slug>/<plugin>/asset/<path>     ← internal_asset_url
    /api/marketplace/curated/<slug>/<plugin>/doc/<path>       ← internal_doc_url
    /api/marketplace/curated/<slug>/<plugin>/mirrored/<key>   ← mirrored_url

The same shapes were previously inlined in two places — ``src/marketplace.py``
(sync-time enrichment, where the served URL gets stored in
``marketplace_plugins.cover_photo_url`` / ``doc_links``) and
``app/api/marketplace.py`` (request-time inner-detail enrichment for
skills / agents). Both call sites now import from here so a future
URL-format tweak (added prefix, signed-URL token, …) only needs to change
one file. The router endpoints themselves still own the path string
literals — keeping the builders' definition identical to the route
declaration is a checklist item, not a runtime guarantee.
"""

from __future__ import annotations


_ROUTE_PREFIX = "/api/marketplace/curated"


def internal_asset_url(slug: str, plugin_name: str, path: str) -> str:
    """Served URL for an internal asset (cover photo, icon, …) inside the
    cloned marketplace working tree.
    """
    return f"{_ROUTE_PREFIX}/{slug}/{plugin_name}/asset/{path}"


def internal_doc_url(slug: str, plugin_name: str, path: str) -> str:
    """Served URL for an internal doc reference inside the cloned working tree."""
    return f"{_ROUTE_PREFIX}/{slug}/{plugin_name}/doc/{path}"


def mirrored_url(slug: str, plugin_name: str, key: str) -> str:
    """Served URL for an external asset that has been mirrored to the cache.

    ``key`` is the cache-relative path minus the leading ``<plugin>/``
    segment (the endpoint takes the plugin from the URL path, not the
    key). See ``app/api/marketplace.py:curated_mirrored`` for the
    consumer side.
    """
    return f"{_ROUTE_PREFIX}/{slug}/{plugin_name}/mirrored/{key}"
