"""Client-side machinery for the Initial Workspace Template override.

This is the analyst-facing half of the per-instance template feature
(server half: ``app/api/initial_workspace.py`` + ``src/initial_workspace.py``).
When the operator has configured a template via ``/admin/server-config``,
``agnes init`` calls :func:`probe_status` and, on a configured response,
runs :func:`apply_override` instead of the default workspace generation.

Public entry points:

- :func:`probe_status` — early CLI probe; returns ``None`` on 404 so old
  servers fall through to the default ``agnes init`` flow.
- :func:`apply_override` — orchestrates download → confirm → extract →
  audit-event. Called from ``cli/commands/init.py`` when probe came back
  ``configured: true``.

OVERRIDE MODE — intentional behavior, NOT a bug.
When this flow runs, Agnes does NOT install hooks, slash commands, the
statusLine, or write ``.claude/CLAUDE.local.md`` / ``AGNES_WORKSPACE.md``.
Admin's repo is the sole source of truth for workspace contents. See
``docs/initial-workspace-override.md`` and CHANGELOG for the full
responsibility-transfer contract.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post
from cli.error_render import render_error

logger = logging.getLogger(__name__)


@dataclass
class StatusInfo:
    """Parsed payload of ``GET /api/initial-workspace``."""

    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    """Outcome of writing the zip into the workspace."""

    overwritten: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


def probe_status(server_url: str, token: str) -> Optional[StatusInfo]:
    """Probe the server for an Initial Workspace Template registration.

    Returns ``None`` on 404 — old server that doesn't know the endpoint.
    The CLI then falls through to the existing default ``agnes init``
    flow without any user-visible noise. On any other non-200 status,
    raises ``typer.Exit(1)`` with a typed error rendered to stderr.
    """
    from cli.lib.pull import _override_server_env

    with _override_server_env(server_url, token):
        resp = api_get("/api/initial-workspace")
    if resp.status_code == 404:
        # Old server — endpoint doesn't exist. Silent fall-through to
        # default flow. The CLI must NOT emit anything user-visible here:
        # any "404" string in stderr is liable to be interpreted as an
        # error by a Claude Code session running `agnes init`.
        return None
    if resp.status_code == 401:
        typer.echo(
            render_error(
                401,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": f"Token expired or invalid — get a fresh one at {server_url}/setup",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(
            render_error(
                resp.status_code,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": f"Unexpected status {resp.status_code} from /api/initial-workspace",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    try:
        body = resp.json()
    except Exception:
        return StatusInfo(configured=False)

    return StatusInfo(
        configured=bool(body.get("configured")),
        synced=bool(body.get("synced")),
        template_source=body.get("template_source"),
        template_sha=body.get("template_sha"),
        synced_at=body.get("synced_at"),
        files=list(body.get("files") or []),
    )


def _classify_files(
    workspace: Path, server_files: list[str]
) -> tuple[list[str], list[str]]:
    """Split server-side file list into (will-overwrite, will-create)
    based on what's already on disk in ``workspace``.
    """
    overwrite: list[str] = []
    create: list[str] = []
    for rel in server_files:
        if (workspace / rel).exists():
            overwrite.append(rel)
        else:
            create.append(rel)
    return overwrite, create


def prompt_force_confirmation(
    workspace: Path,
    overwrite: list[str],
    create: list[str],
) -> bool:
    """Print warning + require literal ``YES`` to proceed.

    Returns True iff the operator typed ``YES`` (uppercase, stripped).
    Anything else aborts. We use uppercase-strict rather than a Y/N
    confirm so:
      (a) a fat-finger doesn't accidentally wipe a workspace
      (b) Claude Code sessions running ``agnes init`` are less likely
          to auto-acknowledge a destructive prompt
    """
    typer.echo("")
    typer.echo("⚠️  WARNING — Initial Workspace Template will be applied with --force.")
    typer.echo("")
    typer.echo(f"Workspace: {workspace}")
    typer.echo("")
    if overwrite:
        typer.echo(f"Files that will be OVERWRITTEN ({len(overwrite)}):")
        for rel in overwrite[:50]:
            typer.echo(f"  ~ {rel}")
        if len(overwrite) > 50:
            typer.echo(f"  … and {len(overwrite) - 50} more")
        typer.echo("")
    if create:
        typer.echo(f"Files that will be CREATED ({len(create)}):")
        for rel in create[:50]:
            typer.echo(f"  + {rel}")
        if len(create) > 50:
            typer.echo(f"  … and {len(create) - 50} more")
        typer.echo("")
    typer.echo("Files in your workspace that are NOT in the template will be preserved.")
    typer.echo("This action is irreversible (no backup) and will be logged on the server.")
    typer.echo("")
    response = typer.prompt(
        "Type YES to continue, anything else to abort",
        type=str,
        default="",
        show_default=False,
    )
    return response.strip() == "YES"


def download_zip(server_url: str, token: str) -> bytes:
    """Fetch ``GET /api/initial-workspace.zip`` and return the bytes.

    Raises ``typer.Exit(1)`` on any non-200 response with a typed error
    surfaced to stderr.
    """
    from cli.lib.pull import _override_server_env

    with _override_server_env(server_url, token):
        resp = api_get("/api/initial-workspace.zip")
    if resp.status_code == 503:
        typer.echo(
            render_error(
                503,
                {
                    "detail": {
                        "kind": "initial_workspace_not_synced",
                        "hint": "Admin must Sync now in /admin/server-config",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(
            render_error(
                resp.status_code,
                {
                    "detail": {
                        "kind": "initial_workspace_fetch_failed",
                        "hint": f"Unexpected status {resp.status_code} fetching zip",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)
    return resp.content


def extract_zip_to_workspace(
    zip_bytes: bytes, workspace: Path
) -> ExtractResult:
    """Validate then extract the zip's entries into ``workspace``.

    Rejects entries with ``..``, absolute paths, or paths that escape
    ``workspace`` after resolution. (Server already validates on
    ``build_zip``; this is defense in depth — the bytes on the wire are
    untrusted from the CLI's perspective.)

    Returns an :class:`ExtractResult` so the caller can include real
    counts in the ``POST /api/initial-workspace/applied`` audit event.
    """
    overwritten: list[str] = []
    created: list[str] = []

    import io

    workspace = workspace.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Sanity-check every name before writing anything so we don't
        # end up with a half-extracted workspace if a bad entry is
        # somewhere in the middle of the archive.
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                typer.echo(
                    render_error(
                        0,
                        {
                            "detail": {
                                "kind": "initial_workspace_unsafe_entry",
                                "hint": f"Zip entry {name!r} is unsafe — extraction aborted",
                            }
                        },
                    ),
                    err=True,
                )
                raise typer.Exit(1)
            target = (workspace / name).resolve()
            try:
                target.relative_to(workspace)
            except ValueError:
                typer.echo(
                    render_error(
                        0,
                        {
                            "detail": {
                                "kind": "initial_workspace_unsafe_entry",
                                "hint": f"Zip entry {name!r} escapes workspace — aborted",
                            }
                        },
                    ),
                    err=True,
                )
                raise typer.Exit(1)

        # All entries verified — now extract.
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            target = workspace / name
            if target.exists():
                overwritten.append(name)
            else:
                created.append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)

    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))


def report_applied(
    server_url: str,
    token: str,
    *,
    mode: str,
    template_sha: Optional[str],
    overwritten_count: int,
    created_count: int,
) -> None:
    """Best-effort audit event. Failure logged but does NOT block init.

    The authoritative anchor is the server-side
    ``initial_workspace.fetch_started`` event written by ``GET .../zip``
    (PAT-holder cannot spoof). This call adds a confirmation row so
    operators can correlate "downloaded" with "actually applied".
    """
    from cli.lib.pull import _override_server_env

    payload = {
        "mode": mode,
        "template_sha": template_sha,
        "files_overwritten": overwritten_count,
        "files_created": created_count,
    }
    try:
        with _override_server_env(server_url, token):
            resp = api_post("/api/initial-workspace/applied", json=payload)
        if resp.status_code != 200:
            logger.warning(
                "audit event /applied returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception:
        # Non-fatal — the workspace is on disk and the analyst can use it.
        logger.exception("audit event /applied failed")


def write_override_sentinel(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> None:
    """Write the extended sentinel that flags this workspace as an
    override workspace. Read by ``cli.lib.override.is_override_workspace``
    on every subsequent CLI invocation to short-circuit Agnes writers
    that would otherwise clobber admin's content.

    Path: ``<workspace>/.claude/init-complete``.
    """
    from datetime import datetime, timezone

    sentinel = workspace / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"agnes_version: {agnes_version}\n"
        f"server_url: {server_url}\n"
        f"override: true\n"
        f"template_source: {template_source or ''}\n"
        f"template_sha: {template_sha or ''}\n",
        encoding="utf-8",
    )


def _dotenv_quote(value: str) -> str:
    """Quote a value for a POSIX dotenv file. Strings without shell
    metacharacters and without whitespace ship bare; everything else
    wraps in double quotes with embedded ``"`` and ``\\`` escaped.

    Defensive on top of API-side validation — operator typos in
    instance.yaml shouldn't be able to inject a newline into the
    rendered .env (which would shadow subsequent keys).
    """
    safe_bare = all(
        c.isalnum() or c in "._-/+:@"
        for c in value
    )
    if value and safe_bare:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _fetch_connector_params(server_url: str, token: str) -> Optional[dict]:
    """``GET /api/connectors/params`` — returns parsed payload or ``None``
    when the endpoint is missing (older servers) or the call fails. Never
    aborts ``agnes init`` — a missing .env.agnes is a degradation, not a
    fatal error (seed skills fall back to interactive prompts).
    """
    from cli.lib.pull import _override_server_env

    try:
        with _override_server_env(server_url, token):
            resp = api_get("/api/connectors/params")
    except Exception:
        logger.exception("connector params fetch raised; skipping .env.agnes")
        return None

    if resp.status_code == 404:
        # Older server — endpoint doesn't exist yet. Silent skip.
        return None
    if resp.status_code != 200:
        logger.warning(
            "connector params fetch HTTP %d; skipping .env.agnes",
            resp.status_code,
        )
        return None
    try:
        return resp.json()
    except Exception:
        logger.exception("connector params response was not JSON; skipping .env.agnes")
        return None


def _flatten_connector_params(payload: dict) -> dict[str, str]:
    """Flatten the ``{globals, params}`` payload into a single key→value
    map. ``globals`` win on conflict (instance-wide brand always
    overrides a per-connector key with the same name).
    """
    flat: dict[str, str] = {}
    params = payload.get("params") if isinstance(payload, dict) else None
    if isinstance(params, dict):
        for slug, block in params.items():
            if not isinstance(block, dict):
                continue
            for k, v in block.items():
                if v is None:
                    continue
                flat[str(k)] = str(v)
    globals_block = payload.get("globals") if isinstance(payload, dict) else None
    if isinstance(globals_block, dict):
        for k, v in globals_block.items():
            if v is None:
                continue
            flat[str(k)] = str(v)
    return flat


def write_agnes_env(
    workspace: Path,
    server_url: str,
    token: str,
) -> Optional[Path]:
    """Write ``<workspace>/.claude/agnes/.env`` with per-tenant operator
    params fetched from ``GET /api/connectors/params``. Atomic via
    tempfile + ``os.replace``; chmod 600. Idempotent on re-init.

    Returns the path on success, ``None`` when no params were fetched
    (older server, network failure, empty overlay) — the caller treats
    that as a soft signal that seed skills should fall back to their
    interactive prompts.

    Never contains secret VALUES — the file carries the names of env
    vars that hold secrets (e.g. ``AGNES_GWS_CLIENT_SECRET_ENV``), not
    the secrets themselves.
    """
    import hashlib
    import os
    import tempfile

    payload = _fetch_connector_params(server_url, token)
    if payload is None:
        return None
    flat = _flatten_connector_params(payload)
    if not flat:
        return None

    env_dir = workspace / ".claude" / "agnes"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_path = env_dir / ".env"

    lines = [
        "# Generated by `agnes init` from the Agnes server's per-tenant",
        "# `connectors:` overlay. Re-run `agnes init` (or `agnes init --force`)",
        "# after the operator changes the overlay.",
        "# DO NOT EDIT — your edits will be lost on re-init.",
        "#",
        "# schema_version=1",
    ]
    for key in sorted(flat):
        lines.append(f"{key}={_dotenv_quote(flat[key])}")
    body = "\n".join(lines) + "\n"

    # Content-hash header lets a future verifier detect manual edits
    # without re-fetching from the server.
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    final_body = (
        f"# content_sha256={content_hash}\n"
        + body
    )

    # Atomic write: temp file in same dir, os.replace swaps in. chmod
    # 600 happens on the temp file BEFORE the rename so a reader never
    # observes a world-readable transient state.
    fd, tmp_str = tempfile.mkstemp(prefix=".env.", dir=str(env_dir))
    tmp_path = Path(tmp_str)
    try:
        os.write(fd, final_body.encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(tmp_path, env_path)
    except Exception:
        # Clean up the temp file if anything went wrong before rename.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return env_path


def apply_override(
    workspace: Path,
    status: StatusInfo,
    server_url: str,
    token: str,
    *,
    force: bool,
    agnes_version: str,
) -> ExtractResult:
    """Top-level override flow.

    Pre-conditions enforced by the caller (``cli/commands/init.py``):
      * ``status.configured`` is True
      * The existing-workspace gate has been evaluated using
        :func:`cli.lib.override.is_override_workspace` — if the sentinel
        already says ``override: true`` and ``--force`` was NOT passed,
        the caller exits ``partial_state`` BEFORE invoking us.

    Steps:
      1. Download zip
      2. If ``force`` AND the workspace already has the override sentinel:
         classify files vs local FS, prompt for literal YES, abort on
         anything else.
      3. Extract zip into workspace.
      4. Write extended sentinel with ``override: true``.
      5. POST audit event (best-effort).

    Returns the :class:`ExtractResult` so the caller can include counts
    in its final summary.
    """
    from cli.lib.override import is_override_workspace

    if not status.synced:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "initial_workspace_not_synced",
                        "hint": "Admin must Sync now in /admin/server-config",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # Confirmation gate — fires only when --force was used to overwrite
    # an existing override workspace. Fresh installs (no prior sentinel)
    # skip the confirmation; nothing to wipe.
    is_force_overwrite = force and is_override_workspace(workspace)
    if is_force_overwrite:
        overwrite, create = _classify_files(workspace, status.files)
        if not prompt_force_confirmation(workspace, overwrite, create):
            typer.echo("Aborted by user; workspace unchanged.", err=True)
            raise typer.Exit(1)

    zip_bytes = download_zip(server_url, token)
    result = extract_zip_to_workspace(zip_bytes, workspace)

    # Operator-provisioned per-tenant params → <workspace>/.claude/agnes/.env.
    # Best-effort: a missing or empty overlay is fine, seed skills fall
    # back to interactive prompts. Errors are logged but don't abort.
    try:
        write_agnes_env(workspace, server_url, token)
    except Exception:
        logger.exception("write_agnes_env failed; .env not written")

    write_override_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=status.template_source,
        template_sha=status.template_sha,
    )

    report_applied(
        server_url,
        token,
        mode="force_overwrite" if is_force_overwrite else "fresh_install",
        template_sha=status.template_sha,
        overwritten_count=len(result.overwritten),
        created_count=len(result.created),
    )

    return result
