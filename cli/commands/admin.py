"""Admin commands — da admin."""

import json

import typer

from cli.client import api_get, api_post, api_delete, api_patch

admin_app = typer.Typer(help="Admin operations (requires admin role)")


@admin_app.command("add-user")
def add_user(
    email: str = typer.Argument(..., help="User email"),
    name: str = typer.Option("", help="User display name"),
    role: str = typer.Option("analyst", help="Role: viewer, analyst, admin, km_admin"),
):
    """Add a new user."""
    resp = api_post("/api/users", json={"email": email, "name": name or email.split("@")[0], "role": role})
    if resp.status_code == 201:
        data = resp.json()
        typer.echo(f"Created user: {data['email']} (id: {data['id']}, role: {data['role']})")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("list-users")
def list_users(as_json: bool = typer.Option(False, "--json")):
    """List all users."""
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    users = resp.json()
    if as_json:
        typer.echo(json.dumps(users, indent=2))
    else:
        for u in users:
            status_str = "active" if u.get("active", True) else "DEACTIVATED"
            typer.echo(
                f"  {u['email']:30s} role={u['role']:10s} {status_str:12s} id={u['id'][:8]}"
            )


@admin_app.command("remove-user")
def remove_user(user_id: str = typer.Argument(..., help="User ID to remove")):
    """Remove a user."""
    resp = api_delete(f"/api/users/{user_id}")
    if resp.status_code == 204:
        typer.echo("User removed.")
    else:
        typer.echo(f"Failed: {resp.text}", err=True)
        raise typer.Exit(1)


@admin_app.command("register-table")
def register_table(
    name: str = typer.Argument(..., help="Table display name"),
    source_type: str = typer.Option("keboola", help="Source type"),
    bucket: str = typer.Option("", help="Source bucket/dataset"),
    source_table: str = typer.Option("", help="Source table name"),
    query_mode: str = typer.Option("local", help="Query mode: local or remote"),
    description: str = typer.Option("", help="Table description"),
):
    """Register a single table."""
    resp = api_post("/api/admin/register-table", json={
        "name": name,
        "source_type": source_type,
        "bucket": bucket,
        "source_table": source_table or name,
        "query_mode": query_mode,
        "description": description,
    })
    if resp.status_code == 201:
        typer.echo(f"Registered: {name}")
    elif resp.status_code == 409:
        typer.echo(f"Already exists: {name}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("discover-and-register")
def discover_and_register(
    source_type: str = typer.Option("keboola", help="Source type"),
    token: str = typer.Option(None, help="Keboola Storage API token"),
    url: str = typer.Option(None, help="Keboola stack URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be registered"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Discover all tables from source and register them."""
    import httpx
    import os

    kbc_token = token or os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
    kbc_url = url or os.environ.get("KEBOOLA_STACK_URL", "")

    if not kbc_token or not kbc_url:
        typer.echo("Need KEBOOLA_STORAGE_TOKEN and KEBOOLA_STACK_URL (env or --token/--url)", err=True)
        raise typer.Exit(1)

    typer.echo(f"Discovering tables from {kbc_url}...")
    resp = httpx.get(f"{kbc_url.rstrip('/')}/v2/storage/tables",
                     headers={"X-StorageApi-Token": kbc_token}, timeout=30)
    resp.raise_for_status()
    tables = resp.json()
    typer.echo(f"Found {len(tables)} tables")

    if as_json and dry_run:
        typer.echo(json.dumps([{"id": t["id"], "name": t["name"],
                                "bucket": t.get("bucket", {}).get("id", ""),
                                "rows": t.get("rowsCount", 0)} for t in tables], indent=2))
        return

    registered = 0
    skipped = 0
    errors = 0

    for t in tables:
        table_id = t["id"]
        name = t["name"]
        bucket_id = t.get("bucket", {}).get("id", "")

        if dry_run:
            typer.echo(f"  [DRY RUN] {name:30s} bucket={bucket_id:20s} rows={t.get('rowsCount', 0):>10,}")
            continue

        resp = api_post("/api/admin/register-table", json={
            "name": name,
            "source_type": source_type,
            "bucket": bucket_id,
            "source_table": name,
            "query_mode": "local",
            "description": f"Auto-discovered from {source_type}",
        })

        if resp.status_code == 201:
            registered += 1
            typer.echo(f"  ✓ {name}")
        elif resp.status_code == 409:
            skipped += 1
        else:
            errors += 1
            typer.echo(f"  ✗ {name}: {resp.json().get('detail', resp.text)}")

    if not dry_run:
        typer.echo(f"\nDone: {registered} registered, {skipped} already existed, {errors} errors")


@admin_app.command("list-tables")
def list_tables(as_json: bool = typer.Option(False, "--json")):
    """List registered tables."""
    resp = api_get("/api/admin/registry")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.text}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(f"Registered tables: {data['count']}")
        for t in data["tables"]:
            typer.echo(f"  {t['name']:30s} src={t.get('source_type','?'):10s} mode={t.get('query_mode','?'):6s} bucket={t.get('bucket',''):20s}")


@admin_app.command("metadata-show")
def metadata_show(
    table_id: str = typer.Argument(..., help="Table ID to show metadata for"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show column metadata for a table."""
    resp = api_get(f"/api/admin/metadata/{table_id}")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        columns = data.get("columns", [])
        if not columns:
            typer.echo(f"No column metadata for table: {table_id}")
            return
        typer.echo(f"Column metadata for table: {table_id} ({len(columns)} columns)")
        typer.echo(f"  {'COLUMN':<30s} {'BASETYPE':<12s} {'CONFIDENCE':<12s} DESCRIPTION")
        typer.echo("  " + "-" * 80)
        for col in columns:
            typer.echo(
                f"  {col['column_name']:<30s} {col.get('basetype') or '':^12s} "
                f"{col.get('confidence') or '':^12s} {col.get('description') or ''}"
            )


@admin_app.command("metadata-apply")
def metadata_apply(
    proposal_path: str = typer.Argument(..., help="Path to proposal JSON file"),
    push_to_source: bool = typer.Option(False, "--push-to-source", help="Push metadata to Keboola after import"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without applying"),
):
    """Apply a metadata proposal JSON to DuckDB."""
    import os

    if not os.path.exists(proposal_path):
        typer.echo(f"Proposal file not found: {proposal_path}", err=True)
        raise typer.Exit(1)

    with open(proposal_path, "r", encoding="utf-8") as f:
        proposal = json.load(f)

    tables = proposal.get("tables", {})
    total = sum(len(t.get("columns", {})) for t in tables.values())

    if dry_run:
        typer.echo(f"[DRY RUN] Would import {total} column(s) from {len(tables)} table(s):")
        for table_id, table_data in tables.items():
            columns = table_data.get("columns", {})
            for col_name, col_data in columns.items():
                typer.echo(
                    f"  {table_id}.{col_name}: basetype={col_data.get('basetype')} "
                    f"description={col_data.get('description')}"
                )
        return

    from src.repositories.column_metadata import ColumnMetadataRepository
    from src.db import get_system_db

    conn = get_system_db()
    try:
        repo = ColumnMetadataRepository(conn)
        count = repo.import_proposal(proposal_path)
        typer.echo(f"Imported {count} column(s) from proposal.")
    finally:
        conn.close()

    if push_to_source:
        for table_id in tables:
            resp = api_post(f"/api/admin/metadata/{table_id}/push")
            if resp.status_code == 200:
                typer.echo(f"Pushed metadata for {table_id} to source.")
            else:
                typer.echo(f"Failed to push {table_id}: {resp.json().get('detail', resp.text)}", err=True)


# ---- User management (#11) ----


def _resolve_user_id(ref: str) -> str:
    """Accept either a UUID or an email; look up email → id via list."""
    if "@" not in ref:
        return ref
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Could not list users: {resp.text}", err=True)
        raise typer.Exit(1)
    for u in resp.json():
        if u.get("email") == ref:
            return u["id"]
    typer.echo(f"User not found: {ref}", err=True)
    raise typer.Exit(1)


def _print_user_result(resp, ok_msg: str) -> None:
    if resp.status_code in (200, 204):
        typer.echo(ok_msg)
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Failed: {detail}", err=True)
        raise typer.Exit(1)


@admin_app.command("set-role")
def set_role(
    user_ref: str = typer.Argument(..., help="User id or email"),
    role: str = typer.Argument(..., help="viewer | analyst | km_admin | admin"),
):
    """Set a user's role."""
    uid = _resolve_user_id(user_ref)
    resp = api_patch(f"/api/users/{uid}", json={"role": role})
    _print_user_result(resp, f"Updated role for {user_ref} → {role}")


@admin_app.command("deactivate")
def deactivate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Deactivate a user (blocks login, existing tokens also rejected)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/deactivate")
    _print_user_result(resp, f"Deactivated {user_ref}")


@admin_app.command("activate")
def activate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Re-activate a deactivated user."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/activate")
    _print_user_result(resp, f"Activated {user_ref}")


@admin_app.command("reset-password")
def reset_password(user_ref: str = typer.Argument(..., help="User id or email")):
    """Generate a reset token (emailed if SMTP/SendGrid configured)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/reset-password")
    if resp.status_code == 200:
        data = resp.json()
        typer.echo(f"Reset token: {data['reset_token']}")
        typer.echo(f"Email sent: {data['email_sent']}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("set-password")
def set_password(
    user_ref: str = typer.Argument(..., help="User id or email"),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True,
        help="New password (hidden input)",
    ),
):
    """Set a user's password directly (force-reset flow)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/set-password", json={"password": password})
    if resp.status_code == 204:
        typer.echo(f"Password set for {user_ref}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


# ---- Role management (v9 — internal_roles + group_mappings + user_role_grants) ----
#
# Calls the role-management REST API under /api/admin (see app/api/role_management.py).
# All endpoints require core.admin; PAT auth is supported uniformly via the v9
# require_internal_role two-path resolver.

role_app = typer.Typer(help="Internal-role browsing (read-only)")
mapping_app = typer.Typer(help="External group → internal role mapping CRUD")
admin_app.add_typer(role_app, name="role")
admin_app.add_typer(mapping_app, name="mapping")


def _fail(resp, prefix: str = "Failed") -> None:
    """Print API failure detail and raise typer.Exit(1)."""
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"{prefix}: {detail}", err=True)
    raise typer.Exit(1)


def _print_rows(rows: list, columns: list[tuple[str, str, int]]) -> None:
    """Render a list of dicts as a fixed-width table.

    columns: list of (key, header, width) — order matches the column display.
    """
    header = "  " + "  ".join(f"{h:<{w}s}" for _, h, w in columns)
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        cells = []
        for key, _, width in columns:
            val = row.get(key)
            cells.append(f"{(str(val) if val is not None else ''):<{width}s}")
        typer.echo("  " + "  ".join(cells))


@role_app.command("list")
def role_list(as_json: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List all internal roles (registered capability keys)."""
    resp = api_get("/api/admin/internal-roles")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    # Endpoint may return a list or {roles: [...]} — accept either shape.
    roles = data["roles"] if isinstance(data, dict) and "roles" in data else data
    if as_json:
        typer.echo(json.dumps(roles, indent=2))
        return
    if not roles:
        typer.echo("No internal roles registered.")
        return
    typer.echo(f"Internal roles: {len(roles)}")
    _print_rows(roles, [
        ("key", "KEY", 30),
        ("display_name", "DISPLAY NAME", 28),
        ("owner_module", "OWNER", 16),
        ("is_core", "CORE", 5),
    ])


@role_app.command("show")
def role_show(
    role_key: str = typer.Argument(..., help="Role key, e.g. core.admin"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show a single role detail with mapping + grant counts."""
    # The list endpoint is the canonical reader; iterate to find the key.
    resp = api_get("/api/admin/internal-roles")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    roles = data["roles"] if isinstance(data, dict) and "roles" in data else data
    role = next((r for r in roles if r.get("key") == role_key), None)
    if role is None:
        typer.echo(f"Role not found: {role_key}", err=True)
        raise typer.Exit(1)

    mappings_resp = api_get("/api/admin/group-mappings")
    if mappings_resp.status_code != 200:
        _fail(mappings_resp)
    mdata = mappings_resp.json()
    mappings = mdata["mappings"] if isinstance(mdata, dict) and "mappings" in mdata else mdata
    matching_mappings = [
        m for m in mappings
        if m.get("role_key") == role_key or m.get("internal_role_key") == role_key
    ]

    # Grants are exposed per-user in the API contract; we summarize what's
    # cheaply visible here (matched mappings) and leave per-user grants to
    # `da admin effective-roles <email>`.
    payload = {
        "role": role,
        "mapping_count": len(matching_mappings),
        "mappings": matching_mappings,
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Role: {role.get('key')}")
    typer.echo(f"  display_name : {role.get('display_name', '')}")
    typer.echo(f"  description  : {role.get('description', '') or ''}")
    typer.echo(f"  owner_module : {role.get('owner_module', '') or ''}")
    typer.echo(f"  is_core      : {bool(role.get('is_core'))}")
    implies = role.get("implies")
    if isinstance(implies, str):
        try:
            implies = json.loads(implies)
        except (TypeError, ValueError):
            implies = []
    typer.echo(f"  implies      : {', '.join(implies) if implies else '(none)'}")
    typer.echo(f"  mappings     : {len(matching_mappings)}")


@mapping_app.command("list")
def mapping_list(as_json: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List all external-group → internal-role mappings."""
    resp = api_get("/api/admin/group-mappings")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    mappings = data["mappings"] if isinstance(data, dict) and "mappings" in data else data
    if as_json:
        typer.echo(json.dumps(mappings, indent=2))
        return
    if not mappings:
        typer.echo("No group mappings configured.")
        return
    # Normalize role_key (some API shapes nest it under `internal_role_key`).
    for m in mappings:
        if "role_key" not in m and "internal_role_key" in m:
            m["role_key"] = m["internal_role_key"]
    typer.echo(f"Group mappings: {len(mappings)}")
    _print_rows(mappings, [
        ("external_group_id", "EXTERNAL GROUP", 40),
        ("role_key", "ROLE KEY", 28),
        ("assigned_by", "ASSIGNED BY", 24),
        ("id", "MAPPING ID", 36),
    ])


@mapping_app.command("create")
def mapping_create(
    external_group_id: str = typer.Argument(..., help="Cloud Identity group ID"),
    role_key: str = typer.Argument(..., help="Internal role key, e.g. core.admin"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Map an external group to an internal role."""
    resp = api_post(
        "/api/admin/group-mappings",
        json={"external_group_id": external_group_id, "role_key": role_key},
    )
    if resp.status_code not in (200, 201):
        _fail(resp)
    created = resp.json()
    if as_json:
        typer.echo(json.dumps(created, indent=2))
        return
    typer.echo(
        f"Created mapping: {created.get('external_group_id')} → "
        f"{created.get('role_key') or created.get('internal_role_key')} "
        f"(id={created.get('id')})"
    )


@mapping_app.command("delete")
def mapping_delete(
    mapping_id: str = typer.Argument(..., help="Mapping ID to delete"),
):
    """Delete a group mapping by ID."""
    resp = api_delete(f"/api/admin/group-mappings/{mapping_id}")
    if resp.status_code in (200, 204):
        typer.echo(f"Deleted mapping {mapping_id}")
        return
    _fail(resp)


@admin_app.command("grant-role")
def grant_role(
    user_email: str = typer.Argument(..., help="User email"),
    role_key: str = typer.Argument(..., help="Internal role key, e.g. core.admin"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Grant an internal role directly to a user (PAT-friendly flow)."""
    uid = _resolve_user_id(user_email)
    resp = api_post(
        f"/api/admin/users/{uid}/role-grants",
        json={"role_key": role_key},
    )
    if resp.status_code not in (200, 201):
        _fail(resp)
    granted = resp.json()
    if as_json:
        typer.echo(json.dumps(granted, indent=2))
        return
    typer.echo(
        f"Granted {role_key} to {user_email} (grant_id={granted.get('id')})"
    )


@admin_app.command("revoke-role")
def revoke_role(
    user_email: str = typer.Argument(..., help="User email"),
    role_key: str = typer.Argument(..., help="Internal role key to revoke"),
):
    """Revoke a previously-granted internal role from a user."""
    uid = _resolve_user_id(user_email)
    list_resp = api_get(f"/api/admin/users/{uid}/role-grants")
    if list_resp.status_code != 200:
        _fail(list_resp, prefix="Failed to list grants")
    data = list_resp.json()
    grants = data["grants"] if isinstance(data, dict) and "grants" in data else data
    matching = [
        g for g in grants
        if g.get("role_key") == role_key or g.get("internal_role_key") == role_key
    ]
    if not matching:
        typer.echo(
            f"No active grant for {user_email} with role_key={role_key}", err=True,
        )
        raise typer.Exit(1)
    grant_id = matching[0].get("id")
    if not grant_id:
        typer.echo(f"Grant row missing id: {matching[0]!r}", err=True)
        raise typer.Exit(1)
    del_resp = api_delete(f"/api/admin/users/{uid}/role-grants/{grant_id}")
    if del_resp.status_code in (200, 204):
        typer.echo(f"Revoked {role_key} from {user_email}")
        return
    _fail(del_resp)


@admin_app.command("effective-roles")
def effective_roles(
    user_email: str = typer.Argument(..., help="User email"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show the user's effective roles (direct + group + expanded)."""
    uid = _resolve_user_id(user_email)
    resp = api_get(f"/api/admin/users/{uid}/effective-roles")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return

    typer.echo(f"Effective roles for {user_email}:")
    direct = data.get("direct") or data.get("direct_roles") or []
    group = data.get("group") or data.get("group_roles") or []
    expanded = data.get("expanded") or data.get("effective") or data.get("effective_roles") or []
    typer.echo(f"  direct   : {', '.join(direct) if direct else '(none)'}")
    typer.echo(f"  group    : {', '.join(group) if group else '(none)'}")
    typer.echo(f"  expanded : {', '.join(expanded) if expanded else '(none)'}")
