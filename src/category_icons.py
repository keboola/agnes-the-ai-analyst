"""Inline SVG icons for the marketplace category pills.

Source: Heroicons v2 (MIT license, https://heroicons.com). The SVG path
markup is copied directly into this dict — there's no runtime dependency on
the Heroicons package, and bundling the path data inline keeps the templates
self-contained.

Keyed by the canonical category names from ``src/store_categories.py``
(plus ``"Other"`` for plugins without a category).
"""

from __future__ import annotations

from typing import Optional

# Each value is the inner ``<path …/>`` markup for a 24x24 outline icon.
# The wrapping ``<svg>`` element is supplied by the caller (template) so we
# don't bake in size/stroke attrs and the icon scales with the surrounding
# pill.
_PATHS: dict[str, str] = {
    "Code & Engineering": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M14.25 9.75 16.5 12l-2.25 2.25m-4.5 0L7.5 12l2.25-2.25M6.375 19.5'
        'h11.25c1.035 0 1.875-.84 1.875-1.875V6.375c0-1.035-.84-1.875-1.875-'
        '1.875H6.375c-1.035 0-1.875.84-1.875 1.875v11.25c0 1.035.84 1.875 '
        '1.875 1.875Z" />'
    ),
    "Data & Analytics": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 '
        '1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 '
        '19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 '
        '1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 '
        '1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-'
        '1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 '
        '1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z" />'
    ),
    "Documentation": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 '
        '0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625'
        'c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75'
        'c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />'
    ),
    "Productivity": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="m3.75 13.5 10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75Z" />'
    ),
    "Communication": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 '
        '2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694'
        '-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 '
        '0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-'
        '1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637'
        'c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 '
        '0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 '
        '1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />'
    ),
    "DevOps & Infra": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M5.25 14.25h13.5m-13.5 0a3 3 0 0 1-3-3m3 3a3 3 0 1 0 0 6h13.5a3 3 '
        '0 1 0 0-6m-16.5-3a3 3 0 0 1 3-3h13.5a3 3 0 0 1 3 3m-19.5 0a4.5 4.5 0 '
        '0 1 .9-2.7L5.737 5.1a3.375 3.375 0 0 1 2.7-1.35h7.126c1.062 0 2.062'
        '.5 2.7 1.35l2.587 3.45a4.5 4.5 0 0 1 .9 2.7" />'
    ),
    "Security": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M9 12.75 11.25 15 15 9.75M21 12c0 1.268-.63 2.39-1.593 3.068a3.745'
        ' 3.745 0 0 1-1.043 3.296 3.745 3.745 0 0 1-3.296 1.043A3.745 3.745 0 '
        '0 1 12 21c-1.268 0-2.39-.63-3.068-1.593a3.746 3.746 0 0 1-3.296-'
        '1.043 3.745 3.745 0 0 1-1.043-3.296A3.745 3.745 0 0 1 3 12c0-1.268.'
        '63-2.39 1.593-3.068a3.745 3.745 0 0 1 1.043-3.296 3.746 3.746 0 0 1 '
        '3.296-1.043A3.746 3.746 0 0 1 12 3c1.268 0 2.39.63 3.068 1.593a3.746 '
        '3.746 0 0 1 3.296 1.043 3.746 3.746 0 0 1 1.043 3.296A3.745 3.745 0 '
        '0 1 21 12Z" />'
    ),
    "Research": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 '
        '10.607 10.607Z" />'
    ),
    "Other": (
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M3.75 9.776c.112-.017.227-.026.344-.026h15.812c.117 0 .232.009.344'
        '.026m-16.5 0a2.25 2.25 0 0 0-1.883 2.542l.857 6a2.25 2.25 0 0 0 '
        '2.227 1.932H19.05a2.25 2.25 0 0 0 2.227-1.932l.857-6a2.25 2.25 0 0 0 '
        '-1.883-2.542m-16.5 0V6A2.25 2.25 0 0 1 6 3.75h3.879a1.5 1.5 0 0 1 '
        '1.06.44l2.122 2.12a1.5 1.5 0 0 0 1.06.44H18A2.25 2.25 0 0 1 20.25 6v'
        '3.776" />'
    ),
}


def icon_svg(category: Optional[str]) -> str:
    """Return the inner ``<path/>`` markup for ``category``.

    Unknown / missing categories fall back to the ``"Other"`` folder icon.
    The caller wraps in ``<svg viewBox="0 0 24 24" …>`` to apply size + stroke.
    """
    return _PATHS.get(category or "Other", _PATHS["Other"])


def all_keys() -> list[str]:
    """Stable list of category keys this module supplies an icon for."""
    return list(_PATHS.keys())


def all_paths() -> dict[str, str]:
    """Return a copy of the full ``{category_name: <path/>}`` map.

    Consumed by the marketplace template so the frontend renders SVG icons
    without round-tripping a per-icon API call.
    """
    return dict(_PATHS)
