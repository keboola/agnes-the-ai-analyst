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

OVERRIDE MODE — what Agnes owns vs the admin template.
The admin's repo is the source of truth for workspace *content* (CLAUDE.md,
skills, docs, data, and the non-Agnes keys of ``settings.json``). But Agnes
STILL owns and installs its own elements — SessionStart/End hooks, the
statusLine, and managed slash-commands — on top of the template after
extraction, in BOTH default and override modes (the call site in
``cli/commands/init.py`` runs ``install_claude_hooks`` / ``install_claude_commands``
unconditionally). Agnes does NOT write ``.claude/CLAUDE.local.md``,
``AGNES_WORKSPACE.md``, or the default model/permissions seed in override mode.
Re-applying an EXISTING override workspace (``agnes init --force`` / the
``agnes update`` convergence) routes through the backup-aware 3-way merge, so
analyst edits are copied to ``<name>.bak.<ts>`` before being updated — not
blind-overwritten. See ``docs/initial-workspace-override.md`` and CHANGELOG.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post
from cli.config import _config_dir
from cli.error_render import render_error
from src.initial_workspace import (
    ExtractResult,
    UpdatePlan,
    UpdateResult,
    classify_workspace_update,
    extract_zip_to_workspace as _extract_zip_pure,
    initialize_workspace_from_template,
    is_override_workspace,
    update_workspace_from_template,
)

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
        typer.echo(f"Files that will be UPDATED ({len(overwrite)}) — your edits backed up first:")
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
    typer.echo("Files you edited are backed up to <name>.bak.<timestamp> before being")
    typer.echo("updated. This action is logged on the server.")
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


def _render_unsafe_entry_error(exc: ValueError) -> None:
    """Render a typed ``initial_workspace_unsafe_entry`` error and exit.

    Single source of truth for the CLI's reaction to a ``ValueError``
    raised by the pure ``extract_zip_to_workspace`` /
    ``initialize_workspace_from_template`` — both call sites funnel
    through here so the error shape stays consistent.
    """
    typer.echo(
        render_error(
            0,
            {
                "detail": {
                    "kind": "initial_workspace_unsafe_entry",
                    "hint": str(exc),
                }
            },
        ),
        err=True,
    )
    raise typer.Exit(1) from exc


def extract_zip_to_workspace(
    zip_bytes: bytes, workspace: Path
) -> ExtractResult:
    """CLI-facing wrapper around the pure :func:`src.initial_workspace.extract_zip_to_workspace`.

    On unsafe entries (``..`` traversal, absolute paths, workspace escapes)
    renders a typed error to stderr and raises ``typer.Exit(1)`` so the CLI
    fails cleanly. (Server already validates on ``build_zip``; this is
    defense in depth from the CLI's perspective.)

    Returns an :class:`ExtractResult` so the caller can include real counts
    in the ``POST /api/initial-workspace/applied`` audit event.
    """
    try:
        return _extract_zip_pure(zip_bytes, workspace)
    except ValueError as exc:
        _render_unsafe_entry_error(exc)
        raise  # unreachable — _render_unsafe_entry_error always raises typer.Exit


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


# ---------------------------------------------------------------------------
# `agnes update-workspace` — backup-aware re-apply of the IWT into an
# existing workspace. Unlike `agnes init --force`, this:
#   * reads server URL + PAT from saved config (no --server-url),
#   * does NOT re-pull parquets,
#   * BACKS UP analyst-modified files to `<name>.bak.<ts>` (3-way diff
#     against the stored baseline) instead of overwriting blind.
# ---------------------------------------------------------------------------


def _baseline_path(workspace: Path) -> Path:
    """Where this workspace's installed-template baseline is stored.

    Kept in the client config dir (NOT inside the workspace, so it never
    pollutes the analyst's tree, never lands in a git commit, and can't
    collide with template content) — alongside ``config.yaml`` /
    ``token.json``. Keyed by a hash of the resolved absolute workspace path
    so multiple workspaces on one machine don't clobber each other. A moved
    workspace simply loses its baseline and the next update degrades to the
    conservative "back up every changed file" path.
    """
    key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]
    return _config_dir() / "workspace-baselines" / f"{key}.zip"


def save_template_baseline(workspace: Path, zip_bytes: bytes) -> Path:
    """Persist ``zip_bytes`` as the workspace's baseline (atomic write)."""
    path = _baseline_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=".baseline.", dir=str(path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(zip_bytes)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def load_template_baseline(workspace: Path) -> Optional[bytes]:
    """Return the stored baseline zip bytes, or ``None`` when this workspace
    has no baseline yet (initialised by an older CLI, moved, or cleared).
    """
    path = _baseline_path(workspace)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def preview_update(workspace: Path, new_zip_bytes: bytes) -> UpdatePlan:
    """Classify what an update would do, without touching disk.

    Reads the stored baseline (``None`` for workspaces initialised by an
    older CLI — then every changed file is treated as analyst-modified and
    gets a ``.bak``).
    """
    baseline = load_template_baseline(workspace)
    return classify_workspace_update(workspace, new_zip_bytes, baseline)


def prompt_update_confirmation(workspace: Path, plan: UpdatePlan) -> bool:
    """Print the update plan + require literal ``YES`` to proceed.

    Uppercase-strict (same rationale as :func:`prompt_force_confirmation`):
    a fat-finger shouldn't trigger it, and a Claude Code session is less
    likely to auto-acknowledge. Returns True iff the operator typed ``YES``.
    """
    typer.echo("")
    typer.echo("⚠️  Update workspace from the Initial Workspace Template.")
    typer.echo("")
    typer.echo(f"Workspace: {workspace}")
    typer.echo("")
    if plan.backed_up:
        typer.echo(
            f"Files YOU changed — backed up to <name>.bak.<timestamp>, "
            f"then updated ({len(plan.backed_up)}):"
        )
        for rel in plan.backed_up[:50]:
            typer.echo(f"  ~ {rel}")
        if len(plan.backed_up) > 50:
            typer.echo(f"  … and {len(plan.backed_up) - 50} more")
        typer.echo("")
    if plan.updated:
        typer.echo(f"Files updated in place — you hadn't changed them ({len(plan.updated)}):")
        for rel in plan.updated[:50]:
            typer.echo(f"  · {rel}")
        if len(plan.updated) > 50:
            typer.echo(f"  … and {len(plan.updated) - 50} more")
        typer.echo("")
    if plan.created:
        typer.echo(f"New files added ({len(plan.created)}):")
        for rel in plan.created[:50]:
            typer.echo(f"  + {rel}")
        if len(plan.created) > 50:
            typer.echo(f"  … and {len(plan.created) - 50} more")
        typer.echo("")
    typer.echo("Files not in the template are left untouched.")
    typer.echo("This action is logged on the server.")
    typer.echo("")
    response = typer.prompt(
        "Type YES to continue, anything else to abort",
        type=str,
        default="",
        show_default=False,
    )
    return response.strip() == "YES"


def apply_update(
    workspace: Path,
    new_zip_bytes: bytes,
    status: StatusInfo,
    server_url: str,
    token: str,
    *,
    agnes_version: str,
) -> UpdateResult:
    """Execute the backup-aware re-apply, then POST the audit event.

    Renders a typed ``initial_workspace_unsafe_entry`` error and exits on an
    unsafe zip entry (defense in depth — the server already validates).
    """
    try:
        result = update_workspace_from_template(
            workspace,
            new_zip_bytes,
            load_template_baseline(workspace),
            agnes_version=agnes_version,
            server_url=server_url,
            template_source=status.template_source,
            template_sha=status.template_sha,
        )
    except ValueError as exc:
        _render_unsafe_entry_error(exc)
        raise  # unreachable — _render_unsafe_entry_error always raises typer.Exit

    # Persist the new baseline for the next update's 3-way diff. Best-effort:
    # a failed write just means the next update degrades to "back up every
    # changed file" — the workspace itself is already correct.
    try:
        save_template_baseline(workspace, new_zip_bytes)
    except Exception:
        logger.exception("save_template_baseline failed after update")

    report_applied(
        server_url,
        token,
        mode="update",
        template_sha=status.template_sha,
        overwritten_count=len(result.updated) + len(result.backed_up),
        created_count=len(result.created),
    )
    return result


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
    # Escape backslashes + quotes (POSIX dotenv canonical) AND newlines
    # + CRs. The newline escape is the load-bearing one: a literal \n
    # inside a double-quoted dotenv value is treated as end-of-line by
    # most shell-based parsers, so a value like
    # ``foo\nMALICIOUS_KEY=value`` shadows subsequent keys in the file.
    # The docstring above promises operator typos can't inject newlines
    # — make it true here.
    escaped = (
        value.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
    )
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
    #
    # Two failure modes to guard against:
    #   1. Raw fd leak. `tempfile.mkstemp` returns an integer fd that
    #      Python's GC does NOT auto-close — if any of the writes / chmod
    #      / replace below raises before the explicit `os.close(fd)`,
    #      the fd leaks until the process exits. On Windows the leaked
    #      fd also locks the underlying file, blocking the cleanup
    #      `tmp_path.unlink()`.
    #   2. `os.fchmod` is not implemented on Windows (raises
    #      AttributeError / OSError depending on CPython version). The
    #      .env contents are still useful there; NTFS ACLs cover perms.
    #      Treat the chmod as best-effort so the writer doesn't abort
    #      the entire init on Windows analyst laptops.
    fd, tmp_str = tempfile.mkstemp(prefix=".env.", dir=str(env_dir))
    tmp_path = Path(tmp_str)
    try:
        try:
            os.write(fd, final_body.encode("utf-8"))
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                # Windows / filesystem that doesn't honor fchmod —
                # NTFS / SMB ACLs apply, .env content still lands.
                pass
        finally:
            # ALWAYS close the fd — even on partial write failure the
            # rename below would still try to swap in whatever was
            # written, but a leaked fd here is unrecoverable.
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
    # Reinstall over an EXISTING override workspace routes through the
    # backup-aware 3-way merge: analyst-edited files are copied to
    # ``<name>.bak.<ts>`` BEFORE being updated, instead of blind-overwritten.
    # A FRESH install (no prior override sentinel) has nothing to preserve, so
    # it uses the plain extract. When no baseline is stored (older CLI / moved
    # workspace) the merge treats every changed file as analyst-modified and
    # backs it up — more ``.bak`` files, but never silent data loss.
    try:
        if is_override_workspace(workspace):
            upd = update_workspace_from_template(
                workspace,
                zip_bytes,
                load_template_baseline(workspace),
                agnes_version=agnes_version,
                server_url=server_url,
                template_source=status.template_source,
                template_sha=status.template_sha,
            )
            result = ExtractResult(
                overwritten=sorted(list(upd.updated) + [name for name, _ in upd.backed_up]),
                created=sorted(list(upd.created)),
            )
        else:
            result = initialize_workspace_from_template(
                workspace,
                zip_bytes,
                agnes_version=agnes_version,
                server_url=server_url,
                template_source=status.template_source,
                template_sha=status.template_sha,
            )
    except ValueError as exc:
        _render_unsafe_entry_error(exc)
        raise  # unreachable — _render_unsafe_entry_error always raises typer.Exit

    # Store the installed zip as the workspace baseline so a later
    # `agnes update-workspace` can tell apart analyst edits from upstream
    # template changes (3-way diff). Best-effort: a failed baseline write
    # must not abort init — the update command degrades to "back up every
    # changed file" when the baseline is missing.
    try:
        save_template_baseline(workspace, zip_bytes)
    except Exception:
        logger.exception("save_template_baseline failed; baseline not written")

    # Operator-provisioned per-tenant params → <workspace>/.claude/agnes/.env.
    # Best-effort: a missing or empty overlay is fine, seed skills fall
    # back to interactive prompts. Errors are logged but don't abort.
    try:
        write_agnes_env(workspace, server_url, token)
    except Exception:
        logger.exception("write_agnes_env failed; .env not written")

    report_applied(
        server_url,
        token,
        mode="force_overwrite" if is_force_overwrite else "fresh_install",
        template_sha=status.template_sha,
        overwritten_count=len(result.overwritten),
        created_count=len(result.created),
    )

    return result
