"""`agnes attachment` — fetch connector-catalogued attachment binaries.

    agnes attachment get jira 56340
    agnes attachment get jira 56340 -o ./image.png

One-shot fetch by id over the authenticated API (Tier-2 download, same
shape as `agnes admin sessions download`). RBAC is read access to the
source's catalogue table (e.g. `jira_attachments`) — the same gate as the
parquet download. No SSH to the server anywhere in the flow.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer

from cli.client import api_get
from cli.error_render import render_error

attachment_app = typer.Typer(help="Fetch connector-catalogued attachment files (e.g. Jira)")

_FILENAME_RE = re.compile(r'filename="?([^";]+)"?')


def _fail(resp) -> None:
    """Print a clean error and exit non-zero for non-2xx responses.

    Not the shared `_handle_error`: that helper collapses 403 into the
    401 "authentication required" hint, but here a 403 is an RBAC denial
    whose server detail (`table_not_in_stack_message`) already names the
    next step. Rendering goes through the shared `render_error`, which
    formats the route's `{code, hint}` detail dicts.
    """
    if resp.status_code < 400:
        return
    if resp.status_code == 401:
        typer.echo(
            "[err] authentication required — run `agnes auth login` or import a PAT",
            err=True,
        )
        raise typer.Exit(1)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    typer.echo(render_error(resp.status_code, body), err=True)
    raise typer.Exit(1)


@attachment_app.command("get")
def get(
    source: str = typer.Argument(..., help="Attachment source, e.g. 'jira'"),
    attachment_id: str = typer.Argument(..., help="Attachment id from the source's catalogue table"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to write the file. Defaults to the server-reported filename in the current dir.",
    ),
):
    """Download one attachment by id and report bytes written.

    A 404 with code `attachment_not_stored` means the catalogue row exists
    but the server holds no bytes (over-50MB skip or transform-time miss) —
    fetch those from the upstream system directly. Find ids in the source's
    catalogue table, e.g.
    `agnes query "SELECT attachment_id, filename FROM jira_attachments WHERE issue_key = '...'"`.
    """
    # `api_get` buffers the whole response — the Tier-2 one-shot-fetch shape
    # (`agnes admin sessions download` does the same). A future source that
    # stores files too large to buffer should grow a streaming client path
    # rather than lean on any connector's storage cap.
    resp = api_get(f"/api/attachments/{source}/{attachment_id}/download")
    _fail(resp)

    target = output
    if target is None:
        m = _FILENAME_RE.search(resp.headers.get("content-disposition", ""))
        # The server-reported name is untrusted — keep only its basename so a
        # crafted header can never write outside the current directory.
        name = Path(m.group(1)).name if m else ""
        target = Path(name or f"{source}_{attachment_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    typer.echo(f"Wrote {len(resp.content)} bytes to {target}")
