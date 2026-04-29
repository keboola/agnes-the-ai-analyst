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
    name: str = typer.Argument(..., help="Table display name (DuckDB view name for BQ)"),
    source_type: str = typer.Option("keboola", help="Source type: keboola | bigquery | jira | local"),
    bucket: str = typer.Option("", help="Source bucket (Keboola) or dataset (BigQuery)"),
    source_table: str = typer.Option("", help="Source table name in the bucket/dataset"),
    query_mode: str = typer.Option("local", help="Query mode: local or remote (forced to 'remote' for bigquery)"),
    description: str = typer.Option("", help="Table description"),
    sync_schedule: str = typer.Option(
        "",
        help="Cron schedule (BigQuery only — note: scheduler not yet wired, see #79 / M3 of #108)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run validation + (BQ) source-side check without writing to the registry",
    ),
):
    """Register a single table.

    For BigQuery: dataset goes in --bucket, the BQ table/view name in
    --source-table, the DuckDB view name in NAME. The server forces
    query_mode=remote and profile_after_sync=False; --dry-run goes
    through /precheck and prints rows + size + columns without writing.
    """
    payload = {
        "name": name,
        "source_type": source_type,
        "bucket": bucket,
        "source_table": source_table or name,
        "query_mode": query_mode,
        "description": description,
    }
    if sync_schedule:
        payload["sync_schedule"] = sync_schedule

    if dry_run:
        # Hits /precheck — no DB write, but for BQ does a real
        # bigquery.Client(project).get_table() round-trip so the operator
        # gets the same NotFound / Forbidden error they'd see at
        # registration time, before committing.
        resp = api_post("/api/admin/register-table/precheck", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            t = data.get("table") or {}
            typer.echo("[DRY RUN] precheck OK")
            typer.echo(f"  name:         {t.get('name')}")
            typer.echo(f"  source_type:  {t.get('source_type')}")
            typer.echo(f"  bucket:       {t.get('bucket')}")
            typer.echo(f"  source_table: {t.get('source_table')}")
            if t.get("project_id"):
                typer.echo(f"  project_id:   {t.get('project_id')}")
            if t.get("rows") is not None:
                typer.echo(f"  rows:         {t.get('rows'):,}")
            if t.get("size_bytes") is not None:
                typer.echo(f"  size_bytes:   {t.get('size_bytes'):,}")
            cols = t.get("columns") or []
            if cols:
                typer.echo(f"  columns ({len(cols)}):")
                for c in cols:
                    typer.echo(f"    - {c.get('name'):<32s} {c.get('type', '')}")
            return
        typer.echo(f"Precheck failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    resp = api_post("/api/admin/register-table", json=payload)
    # 200 (BQ sync materialize OK), 201 (legacy non-BQ), and 202 (BQ
    # background materialize) are all success.
    if resp.status_code in (200, 201, 202):
        if resp.status_code == 202:
            typer.echo(f"Registered (materializing in background): {name}")
        else:
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

        # 200 (BQ synchronous materialize), 201 (legacy non-BQ insert),
        # and 202 (BQ background materialize) are all success — mirrors
        # the matrix in the single-table register-table command. Pre-fix
        # this only accepted 201, so every successful BQ row counted as
        # an error (review NIT 6 in #119).
        if resp.status_code in (200, 201, 202):
            registered += 1
            suffix = " (materializing in background)" if resp.status_code == 202 else ""
            typer.echo(f"  ✓ {name}{suffix}")
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
        typer.echo(f"Reset URL: {data['reset_url']}")
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


# ---- Access management (v12 — user_groups + members + resource_grants) ----
#
# Calls the unified access REST API under /api/admin (see app/api/access.py).
# Every endpoint requires Admin user_group membership.

group_app = typer.Typer(help="User group + membership management")
grant_app = typer.Typer(help="Resource grant CRUD")
admin_app.add_typer(group_app, name="group")
admin_app.add_typer(grant_app, name="grant")


def _fail(resp, prefix: str = "Failed") -> None:
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"{prefix}: {detail}", err=True)
    raise typer.Exit(1)


def _print_rows(rows: list, columns: list[tuple[str, str, int]]) -> None:
    header = "  " + "  ".join(f"{h:<{w}s}" for _, h, w in columns)
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        cells = []
        for key, _, width in columns:
            val = row.get(key)
            cells.append(f"{(str(val) if val is not None else ''):<{width}s}")
        typer.echo("  " + "  ".join(cells))


def _resolve_group_id(ref: str) -> str:
    """Accept group id (UUID-ish) or name; look up via /api/admin/groups."""
    resp = api_get("/api/admin/groups")
    if resp.status_code != 200:
        _fail(resp, prefix="Could not list groups")
    for g in resp.json():
        if g["id"] == ref or g["name"] == ref:
            return g["id"]
    typer.echo(f"Group not found: {ref}", err=True)
    raise typer.Exit(1)


@group_app.command("list")
def group_list(as_json: bool = typer.Option(False, "--json")):
    """List all user groups."""
    resp = api_get("/api/admin/groups")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2)); return
    typer.echo(f"User groups: {len(rows)}")
    _print_rows(rows, [
        ("name", "NAME", 24),
        ("description", "DESCRIPTION", 40),
        ("is_system", "SYSTEM", 7),
        ("member_count", "MEMBERS", 8),
        ("grant_count", "GRANTS", 7),
    ])


@group_app.command("create")
def group_create(
    name: str = typer.Argument(..., help="Group name"),
    description: str = typer.Option("", help="Description"),
):
    """Create a new user group."""
    resp = api_post("/api/admin/groups", json={"name": name, "description": description or None})
    if resp.status_code != 201:
        _fail(resp)
    typer.echo(f"Created group: {name} (id={resp.json()['id']})")


@group_app.command("delete")
def group_delete(group_ref: str = typer.Argument(..., help="Group id or name")):
    """Delete a user group (and its members + grants)."""
    gid = _resolve_group_id(group_ref)
    resp = api_delete(f"/api/admin/groups/{gid}")
    if resp.status_code in (200, 204):
        typer.echo(f"Deleted group {group_ref}"); return
    _fail(resp)


@group_app.command("members")
def group_members(group_ref: str = typer.Argument(..., help="Group id or name")):
    """List members of a group."""
    gid = _resolve_group_id(group_ref)
    resp = api_get(f"/api/admin/groups/{gid}/members")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    typer.echo(f"Members: {len(rows)}")
    _print_rows(rows, [
        ("email", "EMAIL", 30),
        ("name", "NAME", 20),
        ("source", "SOURCE", 14),
        ("active", "ACTIVE", 7),
    ])


@group_app.command("add-member")
def group_add_member(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    email: str = typer.Argument(..., help="User email"),
):
    """Add a user to a group (source='admin' — survives Google sync)."""
    gid = _resolve_group_id(group_ref)
    resp = api_post(f"/api/admin/groups/{gid}/members", json={"email": email})
    if resp.status_code != 201:
        _fail(resp)
    typer.echo(f"Added {email} to {group_ref}")


@group_app.command("remove-member")
def group_remove_member(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    email: str = typer.Argument(..., help="User email"),
):
    """Remove a user from a group (only admin-source rows can be removed this way)."""
    gid = _resolve_group_id(group_ref)
    user_id = _resolve_user_id(email)
    resp = api_delete(f"/api/admin/groups/{gid}/members/{user_id}")
    if resp.status_code in (200, 204):
        typer.echo(f"Removed {email} from {group_ref}"); return
    _fail(resp)


@grant_app.command("list")
def grant_list(
    resource_type: str = typer.Option("", "--type", help="Filter by resource type"),
    group_ref: str = typer.Option("", "--group", help="Filter by group id or name"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List resource grants."""
    params = {}
    if resource_type:
        params["resource_type"] = resource_type
    if group_ref:
        params["group_id"] = _resolve_group_id(group_ref)
    resp = api_get("/api/admin/grants", params=params)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2)); return
    typer.echo(f"Resource grants: {len(rows)}")
    _print_rows(rows, [
        ("group_name", "GROUP", 20),
        ("resource_type", "RESOURCE TYPE", 22),
        ("resource_id", "RESOURCE ID", 40),
        ("assigned_by", "ASSIGNED BY", 24),
    ])


@grant_app.command("create")
def grant_create(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    resource_type: str = typer.Argument(..., help="Resource type (e.g. marketplace_plugin)"),
    resource_id: str = typer.Argument(..., help="Resource path (e.g. foundry-ai/metrics-plugin)"),
):
    """Grant a group access to a specific resource."""
    gid = _resolve_group_id(group_ref)
    resp = api_post("/api/admin/grants", json={
        "group_id": gid,
        "resource_type": resource_type,
        "resource_id": resource_id,
    })
    if resp.status_code != 201:
        _fail(resp)
    typer.echo(f"Granted {group_ref}: {resource_type}/{resource_id}")


@grant_app.command("delete")
def grant_delete(grant_id: str = typer.Argument(..., help="Grant id")):
    """Delete a grant by id."""
    resp = api_delete(f"/api/admin/grants/{grant_id}")
    if resp.status_code in (200, 204):
        typer.echo(f"Deleted grant {grant_id}"); return
    _fail(resp)


@grant_app.command("resource-types")
def grant_resource_types(as_json: bool = typer.Option(False, "--json")):
    """List the resource types modules have registered."""
    resp = api_get("/api/admin/resource-types")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2)); return
    _print_rows(rows, [
        ("key", "KEY", 28),
        ("display_name", "DISPLAY NAME", 28),
        ("id_format", "ID FORMAT", 36),
    ])


# ---------------------------------------------------------------------------
# Break-glass: out-of-band admin grant.
#
# Talks directly to system.duckdb — no HTTP, no auth dependency. The whole
# point is recovery for the case where the running server's authorization
# layer is broken or there is no admin left to authenticate as. Requires
# filesystem access to ${DATA_DIR}/state/system.duckdb and is therefore
# restricted to operators with shell access on the host.
# ---------------------------------------------------------------------------


breakglass_app = typer.Typer(
    help="Out-of-band recovery (talks directly to system.duckdb)",
)
admin_app.add_typer(breakglass_app, name="break-glass")


@breakglass_app.command("grant-admin")
def break_glass_grant_admin(
    email: str = typer.Argument(..., help="Email of the user to promote"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt"
    ),
) -> None:
    """Grant Admin-group membership to a user without going through the API.

    Operates directly on system.duckdb. Use when the server is up but the
    Admin group has no live members (race, mistake, accidental DELETE) or
    when bootstrapping a brand-new install before any admin exists. Membership
    is recorded with source='cli_break_glass' so it's distinguishable from
    google_sync / admin / system_seed in audits.

    The DuckDB file must not be locked by a running app process — stop the
    app or use a separate replica before running this.
    """
    import uuid as _uuid

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    if not yes:
        confirm = typer.confirm(
            f"Grant Admin-group membership to {email!r} (break-glass)?",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(1)

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        groups = UserGroupsRepository(conn)
        members = UserGroupMembersRepository(conn)

        admin_group = groups.get_by_name(SYSTEM_ADMIN_GROUP)
        if admin_group is None:
            typer.echo(
                f"FATAL: '{SYSTEM_ADMIN_GROUP}' group missing. Start the app "
                "once so _seed_system_groups can recreate it, then retry.",
                err=True,
            )
            raise typer.Exit(2)

        existing = users.get_by_email(email)
        if existing is None:
            user_id = _uuid.uuid4().hex
            users.create(
                id=user_id,
                email=email,
                name=email.split("@", 1)[0],
                role="admin",
            )
            typer.echo(f"Created user {email} (id={user_id[:8]}…)")
        else:
            user_id = existing["id"]

        if members.has_membership(user_id, admin_group["id"]):
            typer.echo(
                f"{email} is already a member of '{SYSTEM_ADMIN_GROUP}'."
            )
            return

        members.add_member(
            user_id=user_id,
            group_id=admin_group["id"],
            source="cli_break_glass",
            added_by="cli:break-glass",
        )
        typer.echo(
            f"Granted Admin to {email}. Audit source='cli_break_glass'."
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
