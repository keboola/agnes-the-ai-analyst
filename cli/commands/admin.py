"""Admin commands — agnes admin."""

import json

import typer

from cli.client import api_get, api_post, api_delete, api_patch, api_put
from cli.commands.admin_activity import activity_app
from cli.commands.admin_ask import app as admin_ask_app
from cli.commands.admin_autodoc import autodoc_tables
from cli.commands.admin_data_package import admin_data_package_app
from cli.commands.admin_data_semantics import admin_data_semantics_app
from cli.commands.admin_memory_domain import admin_memory_domain_app
from cli.commands.admin_metrics import admin_metrics_app
from cli.commands.db import db_app as admin_db_app
from cli.commands.admin_news import admin_news_app
from cli.commands.admin_sessions import sessions_app as admin_sessions_app
from cli.commands.admin_store import admin_store_app
from cli.commands.admin_usage import app as admin_usage_app
from cli.commands.memory_admin import memory_admin_app

from src.repositories import (
    column_metadata_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)
admin_app = typer.Typer(help="Admin operations (requires admin role)")
admin_app.add_typer(activity_app, name="activity", help="Activity Center — audit_log timeline, health pulse, sync history")
admin_app.add_typer(admin_ask_app, name="ask", help="Ask a natural-language question about telemetry")
admin_app.add_typer(admin_metrics_app, name="metrics")
admin_app.add_typer(admin_sessions_app, name="sessions", help="Browse Claude Code sessions across all users")
admin_app.add_typer(admin_store_app, name="store")
admin_app.add_typer(admin_news_app, name="news")
admin_app.add_typer(memory_admin_app, name="memory")
# Telemetry subcommand: primary name is "telemetry", "usage" kept as an
# alias so existing operator scripts that call `agnes admin usage export …`
# keep working through this release. Drop the alias in a future cleanup
# once external callers have caught up.
admin_app.add_typer(admin_usage_app, name="telemetry", help="Telemetry export and admin queries")
admin_app.add_typer(admin_usage_app, name="usage", help="(deprecated alias of `telemetry`)")
admin_app.add_typer(admin_data_package_app, name="data-package", help="Data Package CRUD (v49)")
admin_app.add_typer(admin_data_semantics_app, name="data-semantics", help="Generate the workspace data-semantics pack (#469)")
admin_app.add_typer(admin_memory_domain_app, name="memory-domain", help="Memory Domain CRUD (v49)")
admin_app.add_typer(admin_db_app, name="db", help="Manage app-state DB backend (DuckDB / Postgres)")
# Single direct command (mirrors `register-table` / `discover-and-register`):
# LLM-generate descriptions for undescribed tables (#399).
admin_app.command("autodoc-tables")(autodoc_tables)


@admin_app.command("add-user")
def add_user(
    email: str = typer.Argument(..., help="User email"),
    name: str = typer.Option("", help="User display name"),
):
    """Add a new user. New users start with no group memberships — to make
    them admin, add them to the Admin group separately:

        agnes admin group add-member <admin-group-id> <email>
    """
    resp = api_post("/api/users", json={"email": email, "name": name or email.split("@")[0]})
    if resp.status_code == 201:
        data = resp.json()
        typer.echo(f"Created user: {data['email']} (id: {data['id']})")
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
            admin_flag = "admin" if u.get("is_admin") else "user"
            typer.echo(
                f"  {u['email']:30s} {admin_flag:6s} {status_str:12s} id={u['id'][:8]}"
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
    query_mode: str = typer.Option("local", help="Query mode: local | remote | materialized"),
    query: str = typer.Option(
        "",
        "--query",
        help=(
            "SQL body for query_mode='materialized' (BigQuery only). "
            "Inline SQL or `@path/to.sql` to read from disk."
        ),
    ),
    description: str = typer.Option("", help="Table description"),
    sync_schedule: str = typer.Option(
        "",
        help="Cron schedule (e.g. 'every 6h' / 'daily 03:00'); honored by materialized BQ rows",
    ),
    # v26 Keboola sync-strategy support
    sync_strategy: str = typer.Option(
        "full_refresh",
        "--sync-strategy",
        help="Keboola: full_refresh (default) | incremental | partitioned",
    ),
    primary_key: str = typer.Option(
        "",
        "--primary-key",
        help="Primary key column(s), comma-separated. Required for incremental dedup.",
    ),
    incremental_window_days: int = typer.Option(
        None,
        "--incremental-window-days",
        help="Backtrack window applied to last_sync (default 7 at sync time)",
    ),
    max_history_days: int = typer.Option(
        None,
        "--max-history-days",
        help="Cap on first-sync history depth (None = unbounded)",
    ),
    where_filters_json: str = typer.Option(
        "",
        "--where-filters-json",
        help=(
            "JSON array of {column, operator, values}. Inline JSON or "
            "@path/to/filters.json. Date placeholders supported: "
            "{{today}}, {{last_week}}, {{last_3_months}}, etc. "
            "(see connectors.keboola.where_filters for the full list). "
            "Filters force the SDK extraction path (slower than the "
            "DuckDB extension); use only when needed."
        ),
    ),
    partition_by: str = typer.Option(
        "",
        "--partition-by",
        help="Date column driving partition keys (required for partitioned strategy)",
    ),
    partition_granularity: str = typer.Option(
        "",
        "--partition-granularity",
        help="day | month (default) | year — for partitioned strategy",
    ),
    initial_load_chunk_days: int = typer.Option(
        None,
        "--initial-load-chunk-days",
        help="Chunk size for partitioned first-sync chunked initial load (default 30)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run validation + (BQ) source-side check without writing to the registry",
    ),
):
    """Register a single table.

    Modes:
    - **local** (Keboola): batch pull, parquet on disk. Requires
      `--bucket` + `--source-table`.
    - **remote** (BigQuery): view only, queries go to BQ. Requires
      `--bucket` + `--source-table`.
    - **materialized** (BigQuery): server-side scheduled SQL → parquet.
      Requires `--query` (inline or `@file.sql`) AND `--bucket` (BQ
      dataset of the destination identifier). `--source-table` defaults
      to the registered `name` when omitted; explicit override is rare.
      Note: `agnes schema <name>` builds the BQ identifier as
      `bq.<bucket>.<source_table>` even for materialized rows, so an
      empty `--bucket` here registers the row but breaks subsequent
      schema/describe calls.

    `--dry-run` goes through /precheck (BQ remote only — for materialized
    rows, dry-run is a no-op since the SQL itself is the contract).
    """
    from pathlib import Path

    # Resolve --query @file.sql shorthand.
    source_query = ""
    if query:
        if query.startswith("@"):
            sql_path = Path(query[1:])
            if not sql_path.exists():
                typer.echo(f"Error: SQL file not found: {sql_path}", err=True)
                raise typer.Exit(2)
            source_query = sql_path.read_text(encoding="utf-8").strip()
        else:
            source_query = query.strip()

    # Keboola materialized rows can omit --query: a NULL source_query means
    # "full-table export via Storage API export-async" (see v25→v26
    # migration notes). For BigQuery materialized rows, --query is still
    # required — BQ has no analogous "full table" semantic at the registry
    # layer (the path is a SELECT against `<project>.<dataset>.<table>`,
    # which the admin must spell out).
    if query_mode == "materialized" and not source_query and source_type != "keboola":
        typer.echo(
            "Error: --query-mode materialized requires --query (literal SQL or @path.sql) for source_type=" + source_type,
            err=True,
        )
        raise typer.Exit(2)

    # Bucket is load-bearing on materialized rows. For BQ it backs the
    # destination identifier (`agnes schema <name>` builds `bq."<bucket>"."
    # <src>"` from it; an empty bucket trips "unsafe BQ identifier in
    # registry" at query time). For Keboola it's the bucket id passed to
    # `/v2/storage/tables/<bucket>.<source_table>/export-async` — without
    # it the export call would 404. Same requirement, different rationale.
    if query_mode == "materialized" and not bucket:
        typer.echo(
            "Error: --query-mode materialized requires --bucket (the "
            "BQ dataset / Keboola bucket id for the source identifier).",
            err=True,
        )
        raise typer.Exit(2)

    payload = {
        "name": name,
        "source_type": source_type,
        "bucket": bucket,
        "source_table": source_table or name,
        "query_mode": query_mode,
        "description": description,
    }
    # Omit empty optional fields so the server-side validator doesn't see
    # `source_query=""` on a remote/local row (which would trigger the
    # "source_query forbidden" branch).
    if source_query:
        payload["source_query"] = source_query
    if sync_schedule:
        payload["sync_schedule"] = sync_schedule

    # v26 sync-strategy support fields. Always send sync_strategy (it has a
    # default). Send the rest only when the operator set them — empty/None
    # → omit so the server stores NULL.
    payload["sync_strategy"] = sync_strategy
    if primary_key:
        payload["primary_key"] = [c.strip() for c in primary_key.split(",") if c.strip()]
    if incremental_window_days is not None:
        payload["incremental_window_days"] = incremental_window_days
    if max_history_days is not None:
        payload["max_history_days"] = max_history_days
    if partition_by:
        payload["partition_by"] = partition_by
    if partition_granularity:
        payload["partition_granularity"] = partition_granularity
    if initial_load_chunk_days is not None:
        payload["initial_load_chunk_days"] = initial_load_chunk_days
    if where_filters_json:
        # Inline JSON or @path/to.json
        if where_filters_json.startswith("@"):
            wf_path = Path(where_filters_json[1:])
            if not wf_path.exists():
                typer.echo(f"Error: where_filters file not found: {wf_path}", err=True)
                raise typer.Exit(2)
            wf_text = wf_path.read_text(encoding="utf-8")
        else:
            wf_text = where_filters_json
        try:
            import json as _json
            payload["where_filters"] = _json.loads(wf_text)
        except _json.JSONDecodeError as e:
            typer.echo(f"Error: --where-filters-json is not valid JSON: {e}", err=True)
            raise typer.Exit(2)

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

        # Post-success hints. Two operator gotchas this catches:
        #
        # 1. `agnes pull` does not auto-materialize newly-registered
        #    rows — registration adds a registry row, but the parquet
        #    is built only when the scheduler tick runs (or first-sync
        #    is triggered manually). Without this hint operators see
        #    "Updated 0 tables" on `agnes pull` and assume something
        #    is broken.
        # 2. `register-table` does NOT auto-grant. `agnes catalog`
        #    filters per-user via `resource_grants`, so operators
        #    other than the registering admin won't see the new row
        #    until a grant is created.
        #
        # Hint #1 only fires for `local` and `materialized` (the modes
        # that actually produce a parquet); 202-async path covers a
        # different signal, so don't double-message there.
        if query_mode in ("local", "materialized") and resp.status_code != 202:
            typer.echo(
                "  Next: run `agnes setup first-sync` to materialize "
                "the parquet (or wait for the scheduler tick)."
            )
        typer.echo(
            f"  Note: register-table does not auto-grant. Run "
            f"`agnes admin grant create <group> table {name}` to "
            f"make this visible in `agnes catalog` for non-admin users."
        )
        # Third hint: BQ-remote rows can fail at first analyst query if the
        # SA lacks dataViewer/jobUser. Pointing at the smoke command
        # surfaces the failure at registration time, not 30 minutes later.
        if query_mode == "remote":
            typer.echo(
                f"  Note: this is a remote-query table. Verify the SA can read it:\n"
                f"    agnes query --remote \"SELECT COUNT(*) FROM {name}\"\n"
                f"  If it 403s, see docs/admin/query-modes.md → \"BigQuery → IAM\"."
            )
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

        # Keboola tables always go through the Storage API export-async
        # path (`materialize_query`), which is `query_mode='materialized'`
        # in the registry. A NULL source_query means "full table export"
        # — same effective semantics the old 'local' mode gave, but via
        # the Storage API instead of the DuckDB extension. See
        # connectors/keboola/storage_api.py + the v25→v26 migration.
        # Other connectors keep their per-source default.
        default_mode = "materialized" if source_type == "keboola" else "local"
        resp = api_post("/api/admin/register-table", json={
            "name": name,
            "source_type": source_type,
            "bucket": bucket_id,
            "source_table": name,
            "query_mode": default_mode,
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


@admin_app.command("unregister-table")
def unregister_table(
    table_id: str = typer.Argument(..., help="Table id to unregister"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt (for scripts).",
    ),
):
    """Unregister a table from the registry.

    Calls `DELETE /api/admin/registry/{table_id}`. The server unhooks the
    master view, removes the canonical parquet for materialized rows, and
    clears the matching `sync_state` row. Issue #177.
    """
    if not yes:
        typer.echo(f"About to unregister table: {table_id}")
        if not typer.confirm("Continue?"):
            typer.echo("Aborted.")
            raise typer.Exit(0)
    resp = api_delete(f"/api/admin/registry/{table_id}")
    if resp.status_code == 204:
        typer.echo(f"Unregistered: {table_id}")
        return
    if resp.status_code == 404:
        typer.echo(f"Not registered: {table_id}", err=True)
        raise typer.Exit(1)
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"Failed: {detail}", err=True)
    raise typer.Exit(1)


@admin_app.command("update-table")
def update_table(
    table_id: str = typer.Argument(..., help="Table id to update"),
    name: str = typer.Option(None, "--name", help="New display name"),
    bucket: str = typer.Option(None, "--bucket", help="New bucket / dataset"),
    source_table: str = typer.Option(
        None, "--source-table", help="New source table name"
    ),
    query_mode: str = typer.Option(
        None,
        "--query-mode",
        help="New query mode: local | remote | materialized",
    ),
    query: str = typer.Option(
        None,
        "--query",
        help=(
            "New SQL body for query_mode='materialized' (BigQuery). "
            "Inline SQL or `@path/to.sql` to read from disk. Use "
            "`--query=` (empty value) to clear."
        ),
    ),
    description: str = typer.Option(
        None, "--description", help="New description"
    ),
    sync_schedule: str = typer.Option(
        None,
        "--sync-schedule",
        help="New cron schedule (e.g. 'every 6h' / 'daily 03:00'); honored by materialized BQ rows",
    ),
    source_type: str = typer.Option(
        None,
        "--source-type",
        help="Change source type. Rare — most edits keep this fixed.",
    ),
):
    """Update a registered table.

    Calls `PUT /api/admin/registry/{table_id}` with only the supplied
    fields. Field omitted → unchanged. Issue #177.

    For BQ rows, the server schedules a background rebuild so the master
    view picks up the change without waiting for the next scheduled sync.
    Switching `query_mode` away from `materialized` clears the stale
    `source_query` automatically.
    """
    from pathlib import Path

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if bucket is not None:
        payload["bucket"] = bucket
    if source_table is not None:
        payload["source_table"] = source_table
    if query_mode is not None:
        payload["query_mode"] = query_mode
    if description is not None:
        payload["description"] = description
    if sync_schedule is not None:
        payload["sync_schedule"] = sync_schedule
    if source_type is not None:
        payload["source_type"] = source_type
    if query is not None:
        if query.startswith("@"):
            sql_path = Path(query[1:])
            if not sql_path.exists():
                typer.echo(f"Error: SQL file not found: {sql_path}", err=True)
                raise typer.Exit(2)
            payload["source_query"] = sql_path.read_text(encoding="utf-8").strip()
        else:
            payload["source_query"] = query.strip()

    if not payload:
        typer.echo(
            "No fields supplied. Pass at least one of --name, --bucket, "
            "--source-table, --query-mode, --query, --description, "
            "--sync-schedule, --source-type.",
            err=True,
        )
        raise typer.Exit(2)

    resp = api_put(f"/api/admin/registry/{table_id}", json=payload)
    if resp.status_code == 200:
        data = resp.json()
        updated = data.get("updated") or sorted(payload.keys())
        typer.echo(f"Updated {table_id}: {', '.join(updated)}")
        return
    if resp.status_code == 404:
        typer.echo(f"Not registered: {table_id}", err=True)
        raise typer.Exit(1)
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"Failed: {detail}", err=True)
    raise typer.Exit(1)


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

    from src.db import get_system_db

    conn = get_system_db()
    try:
        repo = column_metadata_repo()
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
    role: str = typer.Argument(..., help="(removed — see message)"),
):
    """[REMOVED] Roles were replaced by group memberships in v0.25."""
    typer.echo(
        "Error: 'agnes admin set-role' was removed in v0.25.\n"
        "  Roles were replaced by group memberships.\n"
        f"  Make {user_ref!r} admin:\n"
        "    agnes admin group list                        # find Admin group id\n"
        f"    agnes admin group add-member <admin-id> {user_ref}\n",
        err=True,
    )
    raise typer.Exit(2)


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


def _resolve_grant_id(ref: str) -> str:
    """Accept full grant UUID or 8-char prefix (as printed by ``grant list``).

    Grants have no human-readable name — the only identifier is the UUID
    that gets generated at create time. The default tabular output of
    ``agnes admin grant list`` shows the first 8 chars under the ``short_id``
    column so an operator can eyeball-copy it into ``grant delete``; this
    helper bridges that workflow by listing all grants and matching the ref
    against either the full id or the 8-char prefix. Ambiguous prefix
    matches abort with a clear error rather than picking one silently.
    """
    resp = api_get("/api/admin/grants")
    if resp.status_code != 200:
        _fail(resp, prefix="Could not list grants")
    matches = [
        g for g in resp.json()
        if g.get("id") == ref or (g.get("id") or "").startswith(ref)
    ]
    if not matches:
        typer.echo(f"Grant not found: {ref}", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(
            f"Ambiguous grant prefix {ref!r} matches {len(matches)} grants: "
            + ", ".join(m["id"][:8] for m in matches),
            err=True,
        )
        raise typer.Exit(1)
    return matches[0]["id"]


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
    # Surface a short id so the default tabular output is usable as
    # input to `agnes admin grant delete <id>` without first re-running
    # with --json. First 8 chars of the UUID are unambiguous in practice
    # (grant ids are random UUIDs; collisions on the 8-char prefix
    # within a single instance's resource_grants table are astronomically
    # unlikely). The matching bridge lives in `_resolve_grant_id` so
    # `grant delete` accepts either the full UUID or the 8-char short_id
    # printed here — and aborts loudly on the rare ambiguous prefix.
    for r in rows:
        r["short_id"] = (r.get("id") or "")[:8]
    _print_rows(rows, [
        ("short_id", "ID", 9),
        ("group_name", "GROUP", 20),
        ("resource_type", "RESOURCE TYPE", 22),
        ("resource_id", "RESOURCE ID", 40),
        ("requirement", "REQUIREMENT", 12),
        ("assigned_by", "ASSIGNED BY", 24),
    ])


@grant_app.command("create")
def grant_create(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    resource_type: str = typer.Argument(..., help="Resource type (e.g. marketplace_plugin)"),
    resource_id: str = typer.Argument(..., help="Resource path (e.g. foundry-ai/metrics-plugin)"),
    requirement: str = typer.Option(
        "available", "--requirement",
        help="'available' (user opts in via stack) or 'required' (auto-in-stack for all group members)",
    ),
):
    """Grant a group access to a specific resource.

    Arguments are positional, not flags — adjust shell completions /
    scripts accordingly:

    \b
        agnes admin grant create <group> <resource_type> <resource_id>

    Example:

    \b
        agnes admin grant create analysts table order_economics
        agnes admin grant create analysts marketplace_plugin foundry-ai/metrics
        agnes admin grant create critical-ops data_package weekly-revenue --requirement required

    v49: the optional ``--requirement`` flag controls whether the grant
    is opt-in (``available``, default) or always-in-stack (``required``).
    When passed on a NEW (group, resource_type, resource_id) tuple the
    server creates an ``available`` grant and the CLI then PUTs the
    requirement update — this two-step is needed because POST doesn't
    accept the field directly. When the tuple already exists, the 409
    is followed by a list+match to find the existing grant id and a
    PUT to flip the requirement (idempotent if it's already at the
    desired level).
    """
    if requirement not in ("available", "required"):
        typer.echo(
            f"--requirement must be 'available' or 'required', got {requirement!r}",
            err=True,
        )
        raise typer.Exit(2)
    gid = _resolve_group_id(group_ref)
    resp = api_post("/api/admin/grants", json={
        "group_id": gid,
        "resource_type": resource_type,
        "resource_id": resource_id,
    })
    if resp.status_code == 409:
        # Existing grant — find its id so we can PUT a requirement update.
        # Re-list with both filters to scope the lookup tightly.
        ls = api_get(
            "/api/admin/grants",
            params={"group_id": gid, "resource_type": resource_type},
        )
        if ls.status_code != 200:
            _fail(ls)
        existing = next(
            (r for r in ls.json() if r.get("resource_id") == resource_id),
            None,
        )
        if not existing:
            typer.echo(
                f"Server reported grant exists but list lookup couldn't find it.",
                err=True,
            )
            raise typer.Exit(1)
        grant_id = existing["id"]
        current = existing.get("requirement") or "available"
        if current == requirement:
            typer.echo(
                f"Grant {group_ref}: {resource_type}/{resource_id} "
                f"already exists with requirement={requirement}"
            )
            return
        upd = api_put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": requirement},
        )
        if upd.status_code != 200:
            _fail(upd)
        typer.echo(
            f"Updated existing grant {group_ref}: {resource_type}/"
            f"{resource_id} requirement={requirement}"
        )
        return
    if resp.status_code != 201:
        _fail(resp)
    new_grant = resp.json()
    grant_id = new_grant["id"]
    # If the caller wanted 'required', flip with a PUT — server POST
    # always creates 'available'.
    if requirement == "required":
        upd = api_put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "required"},
        )
        if upd.status_code != 200:
            _fail(upd)
        typer.echo(
            f"Granted {group_ref}: {resource_type}/{resource_id} requirement=required"
        )
        return
    typer.echo(f"Granted {group_ref}: {resource_type}/{resource_id}")


@grant_app.command("delete")
def grant_delete(grant_ref: str = typer.Argument(..., help="Grant id (full UUID or 8-char short_id from `grant list`)")):
    """Delete a grant by id.

    Accepts either the full UUID or the 8-char short_id printed by
    ``agnes admin grant list``. See :func:`_resolve_grant_id` for the
    matching rules (exact match preferred; otherwise unique prefix match).
    """
    grant_id = _resolve_grant_id(grant_ref)
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
        users = users_repo()
        groups = user_groups_repo()
        members = user_group_members_repo()

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
