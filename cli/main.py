"""agnes — CLI tool for AI Data Analyst.

Primary interface for AI agents. Install: uv tool install agnes-the-ai-analyst
"""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer

# Force UTF-8 on Windows stdout/stderr at import time. The default Windows
# console codepage (cp1250 on cs-CZ, cp1252 on en-US, …) cannot encode the
# Braille spinner glyphs Rich uses for `agnes pull` progress, nor the
# em-dash / accented chars that show up in skill markdown via
# `agnes skills list`. Both crash with UnicodeEncodeError /
# UnicodeDecodeError before any command-level code runs. `reconfigure` is
# a no-op on non-TextIOWrapper streams (pytest capture, pipes wrapped by
# other tooling) — swallow the AttributeError there.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from cli.commands.auth import auth_app
from cli.commands.capture_session import capture_session_app
from cli.commands.init import init_app
from cli.commands.mark_private import mark_private_app
from cli.commands.onboarded import onboarded_app
from cli.commands.pull import pull_app
from cli.commands.push import push_app
from cli.commands.refresh_marketplace import refresh_marketplace_app
from cli.commands.statusline import statusline_app
from cli.commands.query import query_command
from cli.commands.status import status_app
from cli.commands.admin import admin_app
from cli.commands.diagnose import diagnose_app
from cli.commands.skills import skills_app
from cli.commands.self_upgrade import self_upgrade_app
from cli.commands.setup import setup_app
from cli.commands.server import server_app
from cli.commands.explore import explore_app
from cli.commands.catalog import catalog_app
from cli.commands.schema import schema_app
from cli.commands.describe import describe
from cli.commands.sample import sample
from cli.commands.snapshot import snapshot_app
from cli.commands.disk_info import disk_info_app
from cli.commands.store import store_app
from cli.commands.my_stack import my_stack_app
from cli.commands.marketplace import marketplace_app


def _cli_version() -> str:
    """Return the installed CLI version from package metadata.

    Falls back to `"unknown"` when the package is not installed (e.g. running
    from a source checkout without `uv pip install -e .`). Deliberately does
    not read pyproject.toml at runtime — that file is not shipped with the
    wheel and the metadata lookup is the canonical source.
    """
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"agnes {_cli_version()}")
        raise typer.Exit()


app = typer.Typer(
    name="agnes",
    help="Agnes — AI Data Analyst CLI",
    no_args_is_help=True,
)


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """Root callback — carries the --version option and fires the auto-update check.

    Update check runs before subcommand dispatch but after the --version flag
    (which exits early). It's best-effort: any failure is swallowed so a bad
    network never blocks a working `agnes` command. Disable with
    `AGNES_NO_UPDATE_CHECK=1`.
    """
    _maybe_warn_outdated()


def _maybe_warn_outdated() -> None:
    """Hit /cli/latest on the configured server (cached 24h) and emit a
    one-line stderr warning if the installed CLI is older. Never raises."""
    try:
        from cli.config import get_server_url
        from cli.update_check import check, format_outdated_notice
        info = check(get_server_url())
        if info and info.is_outdated():
            typer.echo(format_outdated_notice(info), err=True)
    except Exception:
        pass  # best-effort: never fail a command on the probe

# Register subcommands
app.add_typer(auth_app, name="auth")
app.add_typer(init_app, name="init")
app.add_typer(onboarded_app, name="onboarded")
app.add_typer(pull_app, name="pull")
app.add_typer(push_app, name="push")
app.add_typer(capture_session_app, name="capture-session")
app.add_typer(mark_private_app, name="mark-private")
app.add_typer(statusline_app, name="statusline")
app.add_typer(refresh_marketplace_app, name="refresh-marketplace")
app.command("query")(query_command)
app.add_typer(status_app, name="status")
app.add_typer(admin_app, name="admin")
app.add_typer(diagnose_app, name="diagnose")
app.add_typer(skills_app, name="skills")
app.add_typer(self_upgrade_app, name="self-upgrade")
app.add_typer(setup_app, name="setup")
app.add_typer(server_app, name="server")
app.add_typer(explore_app, name="explore")
app.add_typer(catalog_app, name="catalog")
app.add_typer(schema_app, name="schema")
app.command("describe")(describe)
# `agnes sample <table>` — shorthand for `agnes describe <table> -n 5`.
# CLAUDE.md and the agent-rails protocol have referenced `sample` for a
# while; AI analysts following docs literally now Just Work. Issue #254.
app.command("sample")(sample)
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(disk_info_app, name="disk-info")
app.add_typer(store_app, name="store")
app.add_typer(my_stack_app, name="my-stack")
app.add_typer(marketplace_app, name="marketplace")


def _capture_cli_exception(exc: BaseException, kind: str) -> None:
    """Best-effort PostHog forward for CLI-level errors. No-op when off."""
    try:
        from src.observability import get_posthog
        argv = sys.argv[1:]
        command = argv[0] if argv else "<no-command>"
        get_posthog().capture_exception(
            exc,
            distinct_id="cli",
            properties={
                "component": "cli",
                "command": command,
                "argv": " ".join(argv)[:512],
                "error_kind": kind,
            },
        )
        get_posthog().shutdown()
    except Exception:
        pass  # never replace the user-visible error with a tracing failure


def main() -> None:
    """Wrap ``app()`` so AgnesTransportError (and other typed CLI errors)
    surface as a one-line message + exit, never as a Python traceback. The
    full traceback is already logged to ``~/.config/agnes/last-error.log``
    by the api_* helpers — operators read it from there for support
    forwarding. Anything that escapes this wrapper IS a CLI bug worth
    fixing — log + print "internal error" so the analyst doesn't see a
    Pythonist's traceback either.

    Also forwards captured exceptions to PostHog (no-op when disabled) so
    operators can see CLI-level failures alongside server-side ones.
    Normal control-flow exits (typer.Exit / SystemExit / KeyboardInterrupt)
    are never reported.

    Pavel's #185 Phase 3B: previously a `httpx.ReadTimeout` from an
    `agnes query --remote` against a slow BQ view dumped a 30-frame
    traceback to the analyst's terminal. Now: one clean line + a hint,
    return code 1.
    """
    from cli.client import AgnesTransportError, _log_traceback, _LOG_FILE
    try:
        app()
    except AgnesTransportError as exc:
        _capture_cli_exception(exc, kind="transport")
        typer.echo(f"Error: {exc.user_message}", err=True)
        if exc.hint:
            typer.echo(exc.hint, err=True)
        sys.exit(1)
    except typer.Exit:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # last-resort net — escaped exceptions are bugs
        _capture_cli_exception(exc, kind="unhandled")
        log = _log_traceback(exc, context="unhandled at CLI top-level")
        typer.echo(
            f"Error: internal CLI error ({type(exc).__name__}). "
            f"Full traceback logged to {log}.",
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
