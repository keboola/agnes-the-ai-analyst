"""`agnes chat` — CLI surface for chat.

Two independent things live under one `chat` verb:

  - `agnes chat skills [--json]` — mirrors `GET /api/chat/skills`, the
    server-normalized skills + commands catalog that backs the web chat
    composer's slash menu (see `app/chat/skills_catalog.py`).

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
name to it. The one sharp edge this creates: an agent literally named
`skills` is shadowed by `agnes chat skills` (the catalog command wins) —
called out in `_repl`'s help text.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import typer
from typer.core import TyperGroup

from cli.client import ApiSseError, api_delete, api_get, api_post, api_post_sse
from cli.error_render import render_error


class _ChatGroup(TyperGroup):
    """Redirect an unrecognized first token to the hidden `_repl` command.

    `agnes chat skills` still dispatches to the real `skills` subcommand
    (it IS in `self.commands`); `agnes chat research-bot --once hi` becomes
    `agnes chat _repl research-bot --once hi` under the hood. A leading
    option (e.g. `agnes chat --help`) is left alone so group-level help
    still works.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["_repl", *args]
        return super().parse_args(ctx, args)


chat_app = typer.Typer(cls=_ChatGroup, help="Cloud chat — skills/commands catalog + streaming agent REPL")


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


# ---------------------------------------------------------------------------
# `agnes chat <slug>` — streaming terminal REPL over the V1b session API
# (V1c Task 6)
# ---------------------------------------------------------------------------


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


@dataclass
class TurnResult:
    """Outcome of consuming one turn's SSE stream.

    Exactly one of ``http_error`` / ``cancelled`` / ``error`` is set on a
    non-clean turn; all are ``None``/``False`` on a plain `RUN_FINISHED`.
    """

    events: list[dict] = field(default_factory=list)
    answer: str = ""
    error: Optional[str] = None
    http_error: Optional[tuple[int, Any]] = None
    cancelled: bool = False


def _best_effort_cancel(session_id: str) -> None:
    """C7/C8: on Ctrl-C, tell the server to actually stop the run — a mere
    client-side disconnect does NOT stop it or refund budget (only
    `/cancel` does). Best-effort: a failure here must not crash the REPL;
    the reaper is the fallback."""
    try:
        api_post(f"/api/v1/sessions/{session_id}/cancel", timeout=10.0)
    except Exception:
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
    """
    events: list[dict] = []
    answer_parts: list[str] = []
    error_message: Optional[str] = None
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
                break
            elif etype == "RUN_ERROR":
                error_message = event.get("message") or "run error"
                break
    except ApiSseError as exc:
        return TurnResult(events=events, answer="".join(answer_parts), http_error=(exc.status_code, exc.body))
    except KeyboardInterrupt:
        _best_effort_cancel(session_id)
        return TurnResult(events=events, answer="".join(answer_parts), cancelled=True)
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()
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
            if result.http_error is not None or result.cancelled or result.error is not None:
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
            "literally named 'skills' is shadowed by `agnes chat skills`."
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
