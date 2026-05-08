"""`agnes onboarded {on,off,status}` — self-scoped onboarded-flag toggle.

Self-mark equivalent of the in-page button on `/home`. PAT-authed via the
shared `/api/me/onboarded` endpoint; same `source` audit field is
recorded so flips made from the CLI vs the web button are
distinguishable in `audit_log`.

`status` is a thin GET wrapper that reads the calling user's row via the
same endpoint (POSTing `onboarded` matching the current value is a
no-op + idempotent, so we treat it as a read here when no sub-command
is given). For now `status` calls `/api/me/onboarded` GET-style — added
in the API alongside the toggle.
"""

from __future__ import annotations

import typer

from cli.client import api_get, api_post

onboarded_app = typer.Typer(help="Toggle your own users.onboarded flag.")


def _do_post(*, target: bool, source: str) -> dict:
    """POST /api/me/onboarded with the requested value + audit source.
    Exits with the rendered server-side error on non-200."""
    resp = api_post(
        "/api/me/onboarded",
        json={"source": source, "onboarded": target},
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"server returned {resp.status_code}: {detail}", err=True)
        raise typer.Exit(2)
    return resp.json()


@onboarded_app.command("on")
def on(
    source: str = typer.Option(
        "self_acknowledged", "--source",
        help="Audit-log source string. Default 'self_acknowledged' matches "
             "the on-page button. Use 'agnes_init' from automation that's "
             "completing the install flow.",
    ),
):
    """Set users.onboarded = TRUE for the calling user."""
    body = _do_post(target=True, source=source)
    typer.echo(f"onboarded: {body.get('onboarded')}")


@onboarded_app.command("off")
def off(
    source: str = typer.Option(
        "self_unmark", "--source",
        help="Audit-log source string. Default 'self_unmark' matches the "
             "'Mark me as offboarded' button on /home.",
    ),
):
    """Set users.onboarded = FALSE for the calling user. Brings back the
    not-onboarded /home view (full inline install flow + connectors)
    so you can re-walk setup, e.g. after wiping ~/Agnes."""
    body = _do_post(target=False, source=source)
    typer.echo(f"onboarded: {body.get('onboarded')}")


@onboarded_app.command("status")
def status():
    """Print the calling user's current onboarded flag.

    Reads from `/api/me/profile` when present; falls back to the web
    `/home` route's view-state by inspecting the response body for the
    onboarded-branch markers. (We avoid POSTing `/api/me/onboarded` here
    so a `status` call doesn't write an audit_log row.)
    """
    # Try the simple profile endpoint first.
    resp = api_get("/api/me/profile")
    if resp.status_code == 200:
        body = resp.json()
        flag = body.get("onboarded")
        if flag is not None:
            typer.echo(f"onboarded: {flag}")
            return

    # Fallback: inspect /home's rendered body for the onboarded-state
    # marker. Cheap; doesn't write audit_log; works in LOCAL_DEV_MODE
    # and PAT-authed contexts.
    home = api_get("/home")
    if home.status_code != 200:
        typer.echo(f"could not determine status (status: {home.status_code})", err=True)
        raise typer.Exit(2)
    body_text = home.text or ""
    if "Step 1 &amp; Step 2 done" in body_text or "Mark me as offboarded" in body_text:
        typer.echo("onboarded: True")
    else:
        typer.echo("onboarded: False")
