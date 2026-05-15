"""`agnes stack` — user-facing stack management (v49 unified stack).

Three subcommands mirror the user-side `/api/stack/*` endpoints:

  - `agnes stack list [--type plugin|data_package|memory_domain]`
  - `agnes stack add <type> <id>`
  - `agnes stack remove <type> <id>`

The `data_package` and `memory_domain` types are routed through the new
`/api/stack` surface; `plugin` is intentionally NOT supported by this
subcommand (per design D1 — plugins keep the existing
``/api/marketplace`` flow). Passing `--type plugin` to `list` is a soft
error pointing at `agnes marketplace`.

Output is a Rich table for humans; `--json` is honored on `list` for
scripts. Typed server errors (`already_required`, `no_grant`,
`cannot_remove_required`) are surfaced as one-line messages with hints.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_get, api_post, api_delete

stack_app = typer.Typer(help="Manage your stack (data packages + memory domains)")


_SUPPORTED_TYPES = ("data_package", "memory_domain")
_PLUGIN_HINT = (
    "Plugins are managed via the marketplace flow — see `agnes marketplace`."
)


def _fail(resp, *, expected: tuple[int, ...] = (200, 201)) -> None:
    """Render a typed-error message and exit non-zero. Mirrors admin.py."""
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        msg = detail
    elif isinstance(detail, dict):
        msg = detail.get("kind") or detail.get("message") or json.dumps(detail)
    else:
        msg = resp.text or f"HTTP {resp.status_code}"
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _validate_type(value: str, *, allow_plugin_for_list: bool = False) -> str:
    if value == "plugin":
        typer.echo(_PLUGIN_HINT, err=True)
        raise typer.Exit(2)
    if value not in _SUPPORTED_TYPES:
        typer.echo(
            f"Unknown --type {value!r}. Supported: {', '.join(_SUPPORTED_TYPES)}.",
            err=True,
        )
        raise typer.Exit(2)
    return value


@stack_app.command("list")
def stack_list(
    type_filter: Optional[str] = typer.Option(
        None, "--type", help="data_package | memory_domain (omit for both)"
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """List items in your effective stack.

    Effective stack = required ∪ (subscribed ∩ available). Without
    ``--type`` both data_packages and memory_domains are fetched and
    concatenated (the server has no all-types endpoint by design — keeps
    the API contract narrow).
    """
    if type_filter:
        types = [_validate_type(type_filter)]
    else:
        types = list(_SUPPORTED_TYPES)
    aggregated: list[dict] = []
    for t in types:
        resp = api_get("/api/stack", params={"type": t})
        if resp.status_code != 200:
            _fail(resp)
        body = resp.json() or {}
        for it in body.get("items", []):
            it["type"] = t
            aggregated.append(it)

    if as_json:
        typer.echo(json.dumps(aggregated, indent=2))
        return

    if not aggregated:
        typer.echo("Your stack is empty.")
        return

    name_w = max(len("NAME"), max((len(i.get("name", "")) for i in aggregated), default=4))
    type_w = max(len("TYPE"), max((len(i.get("type", "")) for i in aggregated), default=4))
    req_w = max(len("REQUIREMENT"), 11)
    header = (
        f"{'NAME':<{name_w}}  {'TYPE':<{type_w}}  "
        f"{'REQUIREMENT':<{req_w}}  DESCRIPTION"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for it in aggregated:
        desc = (it.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        typer.echo(
            f"{it.get('name','')[:name_w]:<{name_w}}  "
            f"{it.get('type',''):<{type_w}}  "
            f"{it.get('requirement',''):<{req_w}}  "
            f"{desc}"
        )


@stack_app.command("add")
def stack_add(
    resource_type: str = typer.Argument(..., help="data_package | memory_domain"),
    resource_id: str = typer.Argument(..., help="Resource id to subscribe to"),
):
    """Subscribe to an available data_package or memory_domain."""
    rt = _validate_type(resource_type)
    resp = api_post(
        "/api/stack/subscribe",
        json={"resource_type": rt, "resource_id": resource_id},
    )
    if resp.status_code != 200:
        # Translate server detail codes into actionable hints.
        try:
            body = resp.json()
        except Exception:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str):
            if detail.startswith("already_required"):
                typer.echo(
                    f"{resource_id} is already required for one of your groups "
                    f"— no subscription needed.",
                    err=True,
                )
                raise typer.Exit(0)
            if detail == "no_grant":
                typer.echo(
                    f"Access denied: your groups have no grant on "
                    f"{rt}/{resource_id}. Ask an admin to grant it.",
                    err=True,
                )
                raise typer.Exit(1)
        _fail(resp)
    typer.echo(f"Added {rt}/{resource_id} to your stack.")


@stack_app.command("remove")
def stack_remove(
    resource_type: str = typer.Argument(..., help="data_package | memory_domain"),
    resource_id: str = typer.Argument(..., help="Resource id to unsubscribe from"),
):
    """Unsubscribe from an available data_package or memory_domain.

    Removing a *required* resource is refused with a hint pointing at the
    grant — required items can only leave the stack when the admin
    downgrades the grant (or removes it).
    """
    rt = _validate_type(resource_type)
    resp = api_delete(f"/api/stack/subscription/{rt}/{resource_id}")
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.startswith("cannot_remove_required"):
            typer.echo(
                f"{rt}/{resource_id} is required by your group's grant — "
                f"ask an admin to downgrade to `available` first.",
                err=True,
            )
            raise typer.Exit(1)
        _fail(resp)
    typer.echo(f"Removed {rt}/{resource_id} from your stack.")
