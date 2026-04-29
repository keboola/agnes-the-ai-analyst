"""Admin commands for corporate memory — ``da admin memory ...``.

Mounted under the existing ``da admin`` Typer group. API/CLI parity is the
design rule (issue #62): every endpoint exposed by ``app/api/memory.py``'s
admin surface has a CLI counterpart here. Output defaults to a compact
human-readable form; pass ``--json`` for machine-friendly output.
"""

import json as _json
from typing import Optional

import typer

from cli.client import api_get, api_post, api_patch

memory_admin_app = typer.Typer(
    help="Corporate memory admin operations (requires admin role)",
    no_args_is_help=True,
)

duplicates_app = typer.Typer(
    help="List and resolve duplicate-candidate hints",
    no_args_is_help=True,
)
memory_admin_app.add_typer(duplicates_app, name="duplicates")


def _fail(resp, what: str) -> None:
    """Print a CLI-friendly error and exit non-zero."""
    try:
        body = resp.json()
        msg = body.get("detail") or body.get("error") or resp.text
    except Exception:
        msg = resp.text
    typer.echo(f"Failed to {what}: {msg}", err=True)
    raise typer.Exit(1)


# ----- tree -----


@memory_admin_app.command("tree")
def tree(
    axis: str = typer.Option("domain", "--axis", help="domain | category | tag | audience"),
    status_filter: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    source_type: Optional[str] = typer.Option(None, "--source-type", help="Filter by source_type"),
    audience: Optional[str] = typer.Option(None, "--audience", help="Filter by audience value"),
    q: Optional[str] = typer.Option(None, "-q", "--query", help="Substring filter on title/content"),
    has_duplicate: bool = typer.Option(False, "--has-duplicate", help="Only items with unresolved duplicate-candidates"),
    per_page: int = typer.Option(50, "--per-page", help="Groups per page"),
    page: int = typer.Option(1, "--page"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Group knowledge items by ``axis`` and apply chip filters."""
    params: dict = {"axis": axis, "page": page, "per_page": per_page}
    if status_filter:
        params["status_filter"] = status_filter
    if source_type:
        params["source_type"] = source_type
    if audience:
        params["audience"] = audience
    if q:
        params["q"] = q
    if has_duplicate:
        params["has_duplicate"] = "true"
    resp = api_get("/api/memory/tree", params=params)
    if resp.status_code != 200:
        _fail(resp, "list tree")
    data = resp.json()
    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return
    typer.echo(f"axis={data['axis']}  groups={data['total_groups']}  items={data['total_items']}")
    for g in data.get("groups", []):
        typer.echo(f"  [{g['count']:>4}] {g['label']}")
        for item in g.get("items", [])[:5]:
            typer.echo(f"        - {item.get('title', '(untitled)')}  (id={item.get('id', '')[:12]})")
        if g["count"] > 5:
            typer.echo(f"        ... +{g['count'] - 5} more")


# ----- edit -----


@memory_admin_app.command("edit")
def edit(
    item_id: str = typer.Argument(..., help="Knowledge item id"),
    title: Optional[str] = typer.Option(None, "--title"),
    content: Optional[str] = typer.Option(None, "--content"),
    category: Optional[str] = typer.Option(None, "--category"),
    domain: Optional[str] = typer.Option(None, "--domain"),
    audience: Optional[str] = typer.Option(None, "--audience"),
    add_tag: list[str] = typer.Option([], "--add-tag", help="Tag to add (repeatable)"),
    remove_tag: list[str] = typer.Option([], "--remove-tag", help="Tag to remove (repeatable)"),
    set_tags: Optional[str] = typer.Option(None, "--set-tags", help="Comma-separated replacement set (overrides add/remove)"),
):
    """Patch a knowledge item — partial update."""
    body: dict = {}
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content"] = content
    if category is not None:
        body["category"] = category
    if domain is not None:
        body["domain"] = domain
    if audience is not None:
        body["audience"] = audience

    if set_tags is not None:
        body["tags"] = [t.strip() for t in set_tags.split(",") if t.strip()]
    elif add_tag or remove_tag:
        # PATCH only takes ``tags`` (full replacement). Compose against the
        # existing tags client-side so single --add-tag / --remove-tag flags
        # don't wipe the rest of the tag set.
        cur = api_get(f"/api/memory")
        existing: list[str] = []
        if cur.status_code == 200:
            for it in cur.json().get("items", []):
                if it.get("id") == item_id:
                    raw = it.get("tags") or []
                    if isinstance(raw, str):
                        try:
                            raw = _json.loads(raw)
                        except Exception:
                            raw = []
                    existing = [str(t) for t in raw] if isinstance(raw, list) else []
                    break
        merged = list(existing)
        for t in add_tag:
            if t not in merged:
                merged.append(t)
        if remove_tag:
            rm = set(remove_tag)
            merged = [t for t in merged if t not in rm]
        body["tags"] = merged

    if not body:
        typer.echo("Nothing to update — pass at least one --title/--content/--category/--domain/--audience/--add-tag/--remove-tag/--set-tags option.", err=True)
        raise typer.Exit(2)

    resp = api_patch(f"/api/memory/admin/{item_id}", json=body)
    if resp.status_code != 200:
        _fail(resp, "patch item")
    typer.echo(f"Updated {item_id}: {', '.join(resp.json().get('updated', []))}")


# ----- bulk-edit -----


@memory_admin_app.command("bulk-edit")
def bulk_edit(
    ids: str = typer.Option(..., "--ids", help="Comma-separated knowledge item ids"),
    category: Optional[str] = typer.Option(None, "--category"),
    domain: Optional[str] = typer.Option(None, "--domain"),
    audience: Optional[str] = typer.Option(None, "--audience"),
    add_tag: list[str] = typer.Option([], "--add-tag"),
    remove_tag: list[str] = typer.Option([], "--remove-tag"),
):
    """Apply the same updates to many items in one call."""
    item_ids = [s.strip() for s in ids.split(",") if s.strip()]
    if not item_ids:
        typer.echo("No --ids provided.", err=True)
        raise typer.Exit(2)
    updates: dict = {}
    if category is not None:
        updates["category"] = category
    if domain is not None:
        updates["domain"] = domain
    if audience is not None:
        updates["audience"] = audience
    if add_tag:
        updates["tags_add"] = list(add_tag)
    if remove_tag:
        updates["tags_remove"] = list(remove_tag)
    if not updates:
        typer.echo("Nothing to update — pass at least one mutation option.", err=True)
        raise typer.Exit(2)
    resp = api_post(
        "/api/memory/admin/bulk-update",
        json={"item_ids": item_ids, "updates": updates},
    )
    if resp.status_code != 200:
        _fail(resp, "bulk-update")
    body = resp.json()
    typer.echo(
        f"Updated: {len(body.get('updated', []))}  "
        f"Not found: {len(body.get('not_found', []))}  "
        f"Errors: {len(body.get('errors', {}))}"
    )
    for item_id, msg in (body.get("errors") or {}).items():
        typer.echo(f"  ! {item_id}: {msg}")


# ----- stats -----


@memory_admin_app.command("stats")
def stats(as_json: bool = typer.Option(False, "--json")):
    """Knowledge-base aggregations including the new by_tag / by_audience."""
    resp = api_get("/api/memory/stats")
    if resp.status_code != 200:
        _fail(resp, "load stats")
    data = resp.json()
    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return
    typer.echo(f"Total: {data.get('total', 0)}")
    typer.echo("By status:")
    for k, v in (data.get("by_status") or {}).items():
        typer.echo(f"  {k:>12}: {v}")
    typer.echo("By domain:")
    for k, v in (data.get("by_domain") or {}).items():
        typer.echo(f"  {k:>12}: {v}")
    typer.echo("By source_type:")
    for k, v in (data.get("by_source_type") or {}).items():
        typer.echo(f"  {k:>20}: {v}")
    typer.echo("By tag (top 10):")
    for k, v in list((data.get("by_tag") or {}).items())[:10]:
        typer.echo(f"  {k:>20}: {v}")
    typer.echo("By audience:")
    for k, v in (data.get("by_audience") or {}).items():
        typer.echo(f"  {k:>20}: {v}")


# ----- duplicates -----


@duplicates_app.command("list")
def duplicates_list(
    resolved: Optional[bool] = typer.Option(
        None,
        "--resolved/--unresolved",
        help="Filter by resolution state (default: unresolved)",
    ),
    limit: int = typer.Option(100, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List duplicate-candidate relations."""
    params: dict = {"limit": limit}
    # Default to unresolved when neither flag is set, matching the API.
    params["resolved"] = "true" if resolved is True else "false"
    resp = api_get("/api/memory/admin/duplicate-candidates", params=params)
    if resp.status_code != 200:
        _fail(resp, "list duplicates")
    data = resp.json()
    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return
    relations = data.get("relations", [])
    typer.echo(f"Duplicate candidates: {len(relations)}")
    for r in relations:
        a = r.get("item_a") or {}
        b = r.get("item_b") or {}
        score = r.get("score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        typer.echo(
            f"  [{score_str}] {r['item_a_id'][:12]}={a.get('title', '?')[:60]!r} "
            f"<-> {r['item_b_id'][:12]}={b.get('title', '?')[:60]!r}"
        )


@duplicates_app.command("resolve")
def duplicates_resolve(
    item_a_id: str = typer.Argument(...),
    item_b_id: str = typer.Argument(...),
    as_: str = typer.Option(..., "--as", help="duplicate | different | dismissed"),
):
    """Resolve a duplicate-candidate pair."""
    resp = api_post(
        "/api/memory/admin/duplicate-candidates/resolve",
        params={"item_a_id": item_a_id, "item_b_id": item_b_id},
        json={"resolution": as_},
    )
    if resp.status_code != 200:
        _fail(resp, "resolve duplicate")
    typer.echo(f"Resolved {item_a_id} <-> {item_b_id} as '{as_}'.")
