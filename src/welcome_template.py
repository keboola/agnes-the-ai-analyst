"""Render the analyst-onboarding welcome prompt (CLAUDE.md).

Two layers:
  1. Template source — admin override from welcome_template.content,
     or the shipped default at config/claude_md_template.txt.
  2. Render context — built from instance config, table_registry,
     metric_definitions, and the calling user's RBAC-filtered marketplaces.

The Jinja2 environment uses StrictUndefined so that any typo in the
template raises immediately rather than rendering empty strings.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
from jinja2 import Environment, StrictUndefined

from app.instance_config import (
    get_data_source_type,
    get_instance_name,
    get_instance_subtitle,
    get_sync_interval,
)
from src.marketplace_filter import resolve_allowed_plugins
from src.repositories.welcome_template import WelcomeTemplateRepository

_DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "claude_md_template.txt"
)


def _load_default_template() -> str:
    if _DEFAULT_TEMPLATE_PATH.exists():
        return _DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    # Last-resort embedded fallback if the OSS template file is missing
    # from the install (e.g., partial Docker COPY).
    return (
        "# {{ instance.name }} — AI Data Analyst\n\n"
        "This workspace is connected to {{ server.url }}.\n"
        "Data refreshes every {{ sync_interval }}.\n"
    )


def _list_tables(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT name, description, query_mode
           FROM table_registry
           ORDER BY name"""
    ).fetchall()
    return [
        {"name": r[0], "description": r[1] or "", "query_mode": r[2] or "local"}
        for r in rows
    ]


def _metrics_summary(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM metric_definitions GROUP BY category"
        ).fetchall()
    except duckdb.CatalogException:
        return {"count": 0, "categories": []}
    return {
        "count": sum(r[1] for r in rows),
        "categories": sorted({r[0] for r in rows if r[0]}),
    }


def _marketplaces_for_user(
    conn: duckdb.DuckDBPyConnection, user: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return marketplaces with the plugins the user is allowed to see.

    Delegates RBAC filtering entirely to resolve_allowed_plugins, which
    returns List[dict] with marketplace_slug, original_name, etc.
    Results are grouped by marketplace slug; display names are fetched
    from marketplace_registry in a single query.
    """
    allowed = resolve_allowed_plugins(conn, user)
    if not allowed:
        return []

    # Build slug → display name lookup from registry
    slugs = list({p["marketplace_slug"] for p in allowed})
    placeholders = ",".join(["?"] * len(slugs))
    name_rows = conn.execute(
        f"SELECT id, name FROM marketplace_registry WHERE id IN ({placeholders})",
        slugs,
    ).fetchall()
    slug_to_name: dict[str, str] = {r[0]: r[1] for r in name_rows}

    grouped: dict[str, dict[str, Any]] = {}
    for plugin in allowed:
        slug = plugin["marketplace_slug"]
        bucket = grouped.setdefault(
            slug,
            {
                "slug": slug,
                "name": slug_to_name.get(slug, slug),
                "plugins": [],
            },
        )
        bucket["plugins"].append({"name": plugin["original_name"]})

    return list(grouped.values())


def build_context(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any],
    server_url: str,
) -> dict[str, Any]:
    """Compose the Jinja2 render context. Pure, no side effects."""
    now = datetime.now(timezone.utc)
    parsed = urlparse(server_url)
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "sync_interval": get_sync_interval(),
        "data_source": {"type": get_data_source_type()},
        "tables": _list_tables(conn),
        "metrics": _metrics_summary(conn),
        "marketplaces": _marketplaces_for_user(conn, user),
        "user": {
            "id": user.get("id", ""),
            "email": user.get("email", ""),
            "name": user.get("name") or "",
            "is_admin": bool(user.get("is_admin")),
            "groups": user.get("groups") or [],
        },
        "now": now,
        "today": date.today().isoformat(),
    }


def _resolve_template_source(conn: duckdb.DuckDBPyConnection) -> str:
    row = WelcomeTemplateRepository(conn).get()
    return row["content"] if row.get("content") else _load_default_template()


def render_welcome(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any],
    server_url: str,
) -> str:
    """Resolve the active template and render it for the given user."""
    source = _resolve_template_source(conn)
    env = Environment(undefined=StrictUndefined, autoescape=False)
    template = env.from_string(source)
    return template.render(**build_context(conn, user=user, server_url=server_url))
