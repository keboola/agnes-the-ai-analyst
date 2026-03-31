"""Admin commands — da admin."""

import json

import typer

from cli.client import api_get, api_post, api_delete

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
            typer.echo(f"  {u['email']:30s} role={u['role']:10s} id={u['id'][:8]}")


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
