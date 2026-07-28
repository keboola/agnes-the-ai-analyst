"""Predefined Store category taxonomy.

The Store organizes uploaded skills / agents / plugins by **subject matter**,
not by RBAC groups. The categories here are the controlled vocabulary that
the upload form, listing filters, and `_validate_category` all read from.

Adding a category: append a string to ``STORE_CATEGORIES``. Existing entities
referencing categories that have since been removed continue to surface (they
just won't match the dropdown filters until re-saved with a current value).
"""

from __future__ import annotations

STORE_CATEGORIES: list[str] = [
    "Code & Engineering",
    "Data & Analytics",
    "Documentation",
    "Productivity",
    "Communication",
    "DevOps & Infra",
    "Security",
    "Research",
    "Other",
]


def is_valid_category(value: str) -> bool:
    return value in STORE_CATEGORIES


def normalize_category(value: str) -> str | None:
    """Resolve ``value`` to its canonical taxonomy entry, or ``None``.

    Matching is case-insensitive and whitespace-tolerant so
    ``--category documentation`` resolves to ``Documentation`` instead of
    bouncing with ``invalid_category``. The canonical casing is what gets
    persisted (listing filters compare exact strings).
    """
    needle = (value or "").strip().lower()
    for canonical in STORE_CATEGORIES:
        if canonical.lower() == needle:
            return canonical
    return None
