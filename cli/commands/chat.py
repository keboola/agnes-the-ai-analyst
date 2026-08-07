"""`agnes chat` — CLI surface for chat.

Independent things live under one `chat` verb:

  - `agnes chat skills [--json]` — mirrors `GET /api/chat/skills`, the
    server-normalized skills + commands catalog that backs the web chat
    composer's slash menu (see `app/chat/skills_catalog.py`).
  - `agnes chat upload <file> [--kind data|image|document]
      [--as-table NAME] [--json]` — upload a local file into your chat
    workspace via `POST /api/chat/uploads`.

  - `agnes chat <slug> [--once "<prompt>" [--json]]` — the V1c terminal thin
    client: an interactive, streaming REPL over one composed agent's V1b
    session API (`POST /api/v1/agents/{slug}/sessions` +
    `POST /api/v1/sessions/{id}/messages` SSE, `app/api/agent_sessions.py` +
    `app/api/agent_sse.py`'s AG-UI event vocabulary). A pure client of that
    public API — no privileged backchannel, same auth (session token or
    agent PAT) as every other `agnes agent *` subcommand.

Dispatch note: `chat_app` is a Typer *group* with one real subcommand
(`skills`) but the REPL is invoked as a bare positional argument
(`agnes chat <slug>`), not a named subcommand — Click doesn't let a Group
have both an argument-taking callback and named subcommands without one
shadowing the other (a positional `slug` argument on the callback would
swallow the literal token "skills" before subcommand resolution ever runs).
`_ChatGroup` below resolves this the way `click-default-group` does: the
REPL lives in a hidden `_repl` command, and `_ChatGroup.parse_args`
transparently redirects any first token that ISN'T a registered subcommand
name to it. The sharp edge this creates: an agent literally named `skills`
is shadowed by `agnes chat skills` (the catalog command wins) — and by any
future subcommand this group grows. The escape hatch is `agnes chat
--agent <slug>`, handled in `_ChatGroup.parse_args` before subcommand
resolution so it can never be shadowed; called out in both the group's
`--help` epilog and `_repl`'s own help text.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import typer
from typer.core import TyperGroup

from cli.client import AgnesTransportError, ApiSseError, api_delete, api_get, api_post, api_post_sse
from cli.error_render import render_error


class _ChatGroup(TyperGroup):
    """Redirect an unrecognized first token to the hidden `_repl` command.

    `agnes chat skills` still dispatches to the real `skills` subcommand
    (it IS in `self.commands`); `agnes chat research-bot --once hi` becomes
    `agnes chat _repl research-bot --once hi` under the hood. A leading
    option (e.g. `agnes chat --help`) is left alone so group-level help
    still works.

    Escape hatch: `agnes chat --agent <slug>` (or `--agent=<slug>`) always
    addresses an agent regardless of name collisions with a real
    subcommand (e.g. an agent literally named `skills`) or any future
    subcommand this group grows — it's handled here, before subcommand
    resolution, so it can never be shadowed.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if args and (args[0] == "--agent" or args[0].startswith("--agent=")):
            if args[0] == "--agent":
                if len(args) < 2:
                    typer.echo("Error: --agent requires an agent slug.", err=True)
                    raise typer.Exit(2)
                slug, rest = args[1], args[2:]
            else:
                slug, rest = args[0][len("--agent=") :], args[1:]
            return super().parse_args(ctx, ["_repl", slug, *rest])
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["_repl", *args]
        return super().parse_args(ctx, args)


chat_app = typer.Typer(
    cls=_ChatGroup,
    help="Streaming terminal chat with a composed agent, plus the skills/commands catalog.",
    epilog=(
        'Usage: agnes chat <slug> [--once "<prompt>" [--json]]\n\n'
        "Starts an interactive REPL against agent <slug> over its streaming "
        "session API; --once sends one prompt non-interactively (scriptable) "
        "and exits. If <slug> collides with a subcommand name (e.g. an agent "
        "named 'skills'), use `agnes chat --agent <slug>` instead — it always "
        "addresses the agent.\n\n"
        "Disconnecting (killing this process, a network drop) does NOT stop "
        "the run or refund budget — only Ctrl-C (which cancels the turn) or "
        "/exit does. A gap between messages longer than the server's "
        "paused-sandbox TTL (operator-configured, default 7 days) gets the "
        "idle sandbox reclaimed in the background; the next message still "
        "works but loses any in-sandbox state."
    ),
)


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = detail if isinstance(detail, str) and detail else (resp.text or f"HTTP {resp.status_code}")
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@chat_app.command("skills")
def chat_skills(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """List skills + slash commands invokable in your web chat sandbox.

    Merges the bundled chat workspace-template skills with your
    RBAC-filtered marketplace/store plugin skills (marketplace wins name
    clashes) — the same set installed into your live chat sandbox.
    """
    resp = api_get("/api/chat/skills")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}

    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return

    skills: list[dict] = body.get("skills", [])
    commands: list[dict] = body.get("commands", [])

    if not skills and not commands:
        typer.echo("No skills or commands available.")
        return

    if skills:
        typer.echo("Skills:")
        name_w = max(len("NAME"), max((len(s.get("name", "")) for s in skills), default=4))
        for s in skills:
            desc = s.get("description") or ""
            typer.echo(f"  {s.get('name', ''):<{name_w}}  [{s.get('source', '')}]  {desc}")

    if commands:
        if skills:
            typer.echo("")
        typer.echo("Commands:")
        for c in commands:
            desc = c.get("description") or ""
            typer.echo(f"  {c.get('name', ''):<20}  {desc}")


@chat_app.command("upload")
def chat_upload(
    file: Path = typer.Argument(..., help="Local file to upload into your chat workspace."),
    kind: str = typer.Option(
        "data",
        "--kind",
        help="File kind: data | image | document",
    ),
    as_table: Optional[str] = typer.Option(
        None,
        "--as-table",
        help="Register uploaded data file as a workspace-local queryable table with this name.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON response."),
) -> None:
    """Upload a local file into your chat workspace (POST /api/chat/uploads).

    The file lands in your per-user workspace ``uploads/`` folder and is
    available to Claude in your next chat sandbox session.

    For data files (CSV, parquet, XLSX) pass ``--as-table NAME`` to register
    the file as a workspace-local queryable table so ``agnes query`` can reach
    it in-session without an admin table-registry entry.

    Examples::

        agnes chat upload data.csv --kind data --as-table my_data
        agnes chat upload report.pdf --kind document
        agnes chat upload chart.png --kind image --json
    """
    if not file.exists():
        typer.echo(
            f"Error: file '{file}' not found. Check the path and try again.",
            err=True,
        )
        raise typer.Exit(1)

    valid_kinds = {"data", "image", "document"}
    if kind not in valid_kinds:
        typer.echo(
            f"Error: unknown kind '{kind}'. Choose one of: {', '.join(sorted(valid_kinds))}",
            err=True,
        )
        raise typer.Exit(1)

    data: dict[str, str] = {"kind": kind}
    if as_table is not None:
        data["register_as_table"] = "true"
        data["table_name"] = as_table

    with file.open("rb") as fh:
        resp = api_post(
            "/api/chat/uploads",
            files={"file": (file.name, fh, _guess_content_type(file))},
            data=data,
        )

    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return

    typer.echo(f"Uploaded: {body.get('filename')}  ({body.get('size_bytes', 0):,} bytes)")
    if body.get("table_name"):
        typer.echo(f"  Registered as table: {body['table_name']}")
    typer.echo(f"  Workspace path: {body.get('workspace_path')}")
    hint = body.get("hint")
    if hint:
        typer.echo(f"  Hint: {hint}")


def _guess_content_type(path: Path) -> str:
    """Return a plausible MIME type for a file based on its extension."""
    _MAP = {
        ".csv": "text/csv",
        ".parquet": "application/octet-stream",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }
    return _MAP.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# `agnes chat <slug>` — streaming terminal REPL over the V1b session API
# (V1c Task 6)
# ---------------------------------------------------------------------------


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


#: Message set on `.error` when the SSE stream ends (generator exhausted,
#: connection closed cleanly by the server) without ever emitting a
#: `RUN_FINISHED` or `RUN_ERROR` terminal event. The server's own
#: `_event_stream` (`app/api/agent_sessions.py`) breaks out on
#: `StopAsyncIteration` — sandbox died, manager detached — with no terminal
#: event of its own, and that looks identical on the wire to an ordinary
#: mid-stream connection drop. Either way, the caller must not treat this
#: as a successful turn.
TRUNCATED_STREAM_MESSAGE = (
    "stream ended without a terminal event — the run may still be in progress or the sandbox ended"
)


@dataclass
class TurnResult:
    """Outcome of consuming one turn's SSE stream.

    Exactly one of ``http_error`` / ``cancelled`` / ``error`` /
    ``transport_error`` is set on a non-clean turn; all are
    ``None``/``False`` on a plain `RUN_FINISHED`. ``error`` is also set
    (to ``TRUNCATED_STREAM_MESSAGE``) when the stream ends without either
    terminal event — see that constant's docstring.
    """

    events: list[dict] = field(default_factory=list)
    answer: str = ""
    error: Optional[str] = None
    http_error: Optional[tuple[int, Any]] = None
    cancelled: bool = False
    transport_error: Optional[AgnesTransportError] = None


def _best_effort_cancel(session_id: str) -> None:
    """C7/C8: on Ctrl-C, tell the server to actually stop the run — a mere
    client-side disconnect does NOT stop it or refund budget (only
    `/cancel` does). Best-effort: a failure here must not crash the REPL;
    the reaper is the fallback.

    Catches `BaseException`, not `Exception`, by design: this runs from
    inside `_send_turn`'s `except KeyboardInterrupt:` handler, so a SECOND
    Ctrl-C landing on this blocking POST would otherwise raise a fresh
    `KeyboardInterrupt` right here, escape the handler mid-cleanup, and
    crash the whole REPL with a raw traceback instead of the one clean
    "Cancelled." line. There is deliberately no distinct "force quit"
    gesture on double Ctrl-C — it's equivalent to a single Ctrl-C (one
    best-effort cancel attempt, then back to the prompt). `/exit` or
    Ctrl-D is the intentional way to leave the REPL.
    """
    try:
        api_post(f"/api/v1/sessions/{session_id}/cancel", timeout=10.0)
    except BaseException:
        pass


def _best_effort_delete_session(session_id: str) -> None:
    """C8: `/exit` (or falling off the end of `--once`) best-effort frees
    the sandbox via DELETE. A bare disconnect (killing this process,
    network drop) does NOT do this — the session is left for the server's
    own idle/paused reaper to reclaim on its own schedule."""
    try:
        api_delete(f"/api/v1/sessions/{session_id}", timeout=10.0)
    except Exception:
        pass


def _send_turn(session_id: str, text: str, *, live_render: bool) -> TurnResult:
    """Send one turn and consume its AG-UI SSE stream to completion.

    Renders live (unless ``live_render=False``, the `--once --json` path,
    which only needs the raw event list): `TEXT_MESSAGE_CONTENT` deltas
    stream straight to stdout, `TOOL_CALL_START` prints a dim `⚙ <name>`
    line, `RUN_FINISHED` ends the turn cleanly, `RUN_ERROR` ends it with
    `.error` set. Every other AG-UI event type (`RUN_STARTED`,
    `TEXT_MESSAGE_END`, `TOOL_CALL_END`) is collected into `.events` for
    `--json` but not rendered — the brief only calls out the four above.

    C7 (Ctrl-C mid-stream): Python's default SIGINT disposition already
    raises `KeyboardInterrupt` at whatever blocking read is in flight
    inside `api_post_sse`'s httpx stream — no separate `signal.signal()`
    registration is needed to *get* the interrupt, only to *handle* it
    here before it reaches Click's top-level Abort/exit machinery
    (`cli/main.py::main()` re-raises a bare `KeyboardInterrupt`, which
    would otherwise kill the whole `agnes chat` process instead of just
    this turn). On catch: stop consuming immediately, best-effort POST
    `/cancel`, and return a `cancelled` result so the REPL loop can redraw
    its prompt — never a half-read stream or a wedged terminal.

    Truncated streams: a `RUN_FINISHED`/`RUN_ERROR` terminal event is the
    only signal that the run actually completed. If the underlying
    generator is exhausted (server closed the connection, or the sandbox
    died mid-run — `app/api/agent_sessions.py`'s `_event_stream` breaks on
    `StopAsyncIteration` with no terminal event of its own) without one
    ever arriving, that is indistinguishable on the wire from a clean
    finish except for the missing terminal event — so it is reported as an
    error (`TRUNCATED_STREAM_MESSAGE`), never as a quiet success.

    Transport errors: `api_post_sse` raises `AgnesTransportError` for
    httpx-level failures (connect refused, read timeout, connection reset).
    Caught here (rather than left to propagate) so a single flaky turn
    doesn't unwind the interactive REPL's loop — see `_run_interactive`,
    which renders `.transport_error` and keeps the session and prompt,
    mirroring how it handles Ctrl-C.
    """
    events: list[dict] = []
    answer_parts: list[str] = []
    error_message: Optional[str] = None
    terminal_seen = False
    gen = api_post_sse(f"/api/v1/sessions/{session_id}/messages", json={"input": text})
    try:
        for event in gen:
            events.append(event)
            etype = event.get("type")
            if etype == "TEXT_MESSAGE_CONTENT":
                delta = event.get("delta") or ""
                answer_parts.append(delta)
                if live_render:
                    sys.stdout.write(delta)
                    sys.stdout.flush()
            elif etype == "TOOL_CALL_START":
                if live_render:
                    name = event.get("name") or "tool"
                    sys.stdout.write(f"\n{_dim(f'⚙ {name}')}\n")
                    sys.stdout.flush()
            elif etype == "RUN_FINISHED":
                terminal_seen = True
                break
            elif etype == "RUN_ERROR":
                terminal_seen = True
                error_message = event.get("message") or "run error"
                break
    except ApiSseError as exc:
        return TurnResult(events=events, answer="".join(answer_parts), http_error=(exc.status_code, exc.body))
    except AgnesTransportError as exc:
        return TurnResult(events=events, answer="".join(answer_parts), transport_error=exc)
    except KeyboardInterrupt:
        _best_effort_cancel(session_id)
        return TurnResult(events=events, answer="".join(answer_parts), cancelled=True)
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()
    if not terminal_seen:
        error_message = TRUNCATED_STREAM_MESSAGE
    return TurnResult(events=events, answer="".join(answer_parts), error=error_message)


def _render_turn_error(result: TurnResult) -> None:
    if result.http_error is not None:
        status, body = result.http_error
        msg = render_error(status, body)
        if status == 409:
            msg += (
                "\n  A previous turn on this session is still running — "
                "wait for it to finish before sending another message."
            )
        typer.echo(msg, err=True)
    elif result.cancelled:
        typer.echo("Cancelled.", err=True)
    elif result.transport_error is not None:
        exc = result.transport_error
        typer.echo(f"Error: {exc.user_message}", err=True)
        if exc.hint:
            typer.echo(exc.hint, err=True)
    elif result.error is not None:
        typer.echo(f"Error: {result.error}", err=True)


def _run_interactive(session_id: str, slug: str) -> None:
    typer.echo(f"Chatting with '{slug}' — session {session_id}.")
    typer.echo("Type a message and press enter. /exit or Ctrl-D to quit; Ctrl-C cancels an in-flight turn.")
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                typer.echo("")
                break
            except KeyboardInterrupt:
                # Ctrl-C at the bare prompt (no turn in flight) — redraw
                # the prompt rather than exiting the whole REPL, matching
                # ordinary interactive-shell behavior.
                typer.echo("")
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped in ("/exit", "/quit"):
                break
            result = _send_turn(session_id, stripped, live_render=True)
            typer.echo("")  # newline after streamed text (or nothing, if none arrived)
            # M2: a transient transport error (`.transport_error`) is
            # rendered and swallowed right here — same as `.cancelled` —
            # so the loop keeps going and the session survives. Only
            # falling out of this `while` (`/exit`, EOF, or an
            # unhandled exception) reaches the `finally` below and frees
            # the session; a single flaky read timeout must not do that.
            if (
                result.http_error is not None
                or result.cancelled
                or result.error is not None
                or result.transport_error is not None
            ):
                _render_turn_error(result)
            elif not result.answer:
                typer.echo("(no answer)")
    finally:
        # C8: `/exit`, EOF, or an unhandled error all still free the
        # sandbox on the way out. A killed process / crashed terminal
        # skips this `finally` entirely — that IS the documented
        # disconnect-≠-cancel gap; the reaper reclaims it later.
        _best_effort_delete_session(session_id)


@chat_app.command("_repl", hidden=True)
def _repl(
    slug: str = typer.Argument(
        ...,
        help=(
            "Agent slug to chat with (see `agnes agent list`). Note: an agent "
            "literally named 'skills' is shadowed by `agnes chat skills`; use "
            "`agnes chat --agent <slug>` to address it unambiguously."
        ),
    ),
    once: Optional[str] = typer.Option(
        None, "--once", help="Send a single prompt non-interactively and exit (scriptable)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="With --once, print the full AG-UI event list instead of just the answer text"
    ),
) -> None:
    """Interactive terminal chat with a composed agent, streamed live over
    the agent-as-API session endpoints.

    KNOWN LIMITATION (idle/paused sandbox): every `POST /messages` resets
    both the session's idle-activity clock and — if the sandbox had
    already paused between turns — its paused-sandbox clock (see
    `app.chat.manager.attach`/`_deliver_local_user_message`), so ordinary
    think-pauses between messages are safe and never 404 the next turn. A
    gap between messages longer than the server's `paused_ttl_seconds`
    (operator-configured, default 7 days) gets reaped in the background;
    the next message still works — a fresh sandbox spawns transparently —
    but loses any state (filesystem, running processes) the old one had.

    Disconnecting (Ctrl-\\, killing this process, a network drop) does NOT
    stop the run or refund budget — only `/cancel` does (which Ctrl-C
    triggers for you here). `/exit` best-effort deletes the session,
    freeing the sandbox immediately; a bare disconnect instead leaves it
    for the server's own idle/paused reaper to reclaim later.
    """
    if as_json and once is None:
        typer.echo("Error: --json only applies together with --once.", err=True)
        raise typer.Exit(2)

    resp = api_post(f"/api/v1/agents/{slug}/sessions", json={})
    if resp.status_code != 201:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        msg = render_error(resp.status_code, body)
        if resp.status_code == 404:
            msg += "\n  List your agents with: agnes agent list"
        typer.echo(msg, err=True)
        raise typer.Exit(1)
    session_id = resp.json()["session_id"]

    if once is None:
        _run_interactive(session_id, slug)
        return

    try:
        result = _send_turn(session_id, once, live_render=not as_json)
        if as_json:
            typer.echo(json.dumps(result.events, indent=2))
        else:
            typer.echo("")  # newline after streamed text (or nothing, if none arrived)

        exit_code = 0
        if result.http_error is not None:
            _render_turn_error(result)
            exit_code = 1
        elif result.cancelled:
            _render_turn_error(result)
            exit_code = 130
        elif result.transport_error is not None:
            # M2: --once still exits non-zero on a transport error (unlike
            # the interactive REPL, there's no next prompt to keep alive
            # for) — rendered only outside --json so it can't corrupt the
            # JSON dumped above.
            if not as_json:
                _render_turn_error(result)
            exit_code = 1
        elif result.error is not None:
            if not as_json:
                _render_turn_error(result)
            exit_code = 1
        elif not as_json and not result.answer:
            typer.echo("(no answer)")
    finally:
        _best_effort_delete_session(session_id)

    if exit_code:
        raise typer.Exit(exit_code)
