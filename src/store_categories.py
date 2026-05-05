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
