"""`agnes semantic-model validate-query` — validate SQL against the caller's
accessible semantic models before running it (query-validation engine
wiring, parity spec §5).

CLI counterpart to ``POST /api/semantic-models/validate-query`` and the MCP
``validate_semantic_query`` foundation tool — same request/response shape
across all three surfaces. Not to be confused with
``agnes admin semantic-model validate``, which schema-checks a *document*
locally against the vendored Ossie spec; this validates a *query* against
whatever valid semantic models the caller can already read (mirrors the
non-admin ``/api/semantic-models/search`` + ``export`` RBAC tier — a Data
Package or direct model grant, not admin-only).
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_post

semantic_model_app = typer.Typer(help="Validate SQL against the semantic layer")


@semantic_model_app.command("validate-query")
def validate_query(
    sql: str = typer.Argument(..., help="SQL statement to validate"),
    expect: Optional[str] = typer.Option(
        None,
        "--expect",
        help='JSON list of expected objects, e.g. \'[{"type":"metric","name":"mrr"}]\'',
    ),
    target_engine: str = typer.Option("duckdb", "--target-engine", help="Engine the query will run on"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Validate SQL against the semantic layer: constraint violations,
    dialect fit, and (optionally) which expected datasets/metrics/
    relationships the query hits.

    Best-effort text matching against declared semantic-model documents —
    not SQL parsing (see the server-side validator's own LIMITATIONS
    docstring). Reads every `status='valid'` semantic model you can access;
    if none exist (or none are accessible to you), prints a "no semantic
    model" notice instead of a misleading all-clear.
    """
    payload: dict = {"sql": sql, "target_engine": target_engine}
    if expect:
        try:
            payload["expected"] = json.loads(expect)
        except ValueError as exc:
            typer.echo(f"--expect is not valid JSON: {exc}", err=True)
            raise typer.Exit(1) from exc

    resp = api_post("/api/semantic-models/validate-query", json=payload)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        typer.echo(f"Error ({resp.status_code}): {detail or resp.text}", err=True)
        raise typer.Exit(1)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if not body.get("available", True):
        typer.echo(body.get("message") or "No semantic model is available to validate against.")
        return

    status = "VALID" if body.get("valid") else "INVALID"
    typer.echo(f"{status} — {body.get('summary', '')}")

    if body.get("violations"):
        typer.echo("Violations:")
        for v in body["violations"]:
            typer.echo(f"  [{v.get('severity')}] {v.get('name')}: {v.get('reason')}")

    if body.get("post_execution_checks"):
        typer.echo("Cannot be checked before running:")
        for chk in body["post_execution_checks"]:
            typer.echo(f"  {chk.get('name')}: {chk.get('reason')}")

    if body.get("mixed_dialect_warning"):
        typer.echo(f"Warning: {body['mixed_dialect_warning']}")

    if not body.get("locally_executable", True):
        typer.echo("Warning: one or more used metrics are not locally executable on the target engine.")

    if "missing_expected_objects" in body and body["missing_expected_objects"]:
        typer.echo("Missing expected objects:")
        for obj in body["missing_expected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")
    if "unexpected_detected_objects" in body and body["unexpected_detected_objects"]:
        typer.echo("Unexpected detected objects:")
        for obj in body["unexpected_detected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")
