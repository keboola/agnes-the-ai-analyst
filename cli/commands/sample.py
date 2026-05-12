"""`agnes sample <table>` — shorthand for `agnes describe <table> -n 5`.

CLAUDE.md and the agent-rails protocol have long referenced `agnes sample
<table>` as the "look at a few rows" command, but the binary only ever
shipped `describe`. AI agents following CLAUDE.md literally fell off
their first try until they discovered `describe`. This module is a thin
forwarder so `agnes sample <table>` Just Works.

See GitHub issue #254 for the discovery context (sub-agent perf tests
on 2026-05-12).
"""

from __future__ import annotations

import typer

from cli.commands.describe import describe as _describe


def sample(
    table_id: str = typer.Argument(...),
    n: int = typer.Option(5, "-n", "--rows", help="Sample rows count"),
    json: bool = typer.Option(False, "--json"),
):
    """Show schema + N sample rows for a table. Equivalent to
    ``agnes describe <table> -n <n>``.
    """
    return _describe(table_id=table_id, n=n, json=json)
