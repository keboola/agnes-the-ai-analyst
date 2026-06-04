"""Cross-backend mutation status-parity sweep (POST/PUT/PATCH/DELETE).

Companion to the GET sweep: every parameter-free mutation route is called with
an empty JSON body on DuckDB and on Postgres (identical seed) and the HTTP
status must match. A status that differs between backends (e.g. 422 on DuckDB,
500 on Postgres) is the signature of a handler that reads state off a raw
``Depends(_get_db)`` connection during auth/validation.

Safe to run blindly: each backend client uses a per-test ephemeral database, so
a side-effecting mutation only touches a throwaway DB. Heavy / side-effecting /
binary / streaming endpoints are skipped — they're slow and an empty body
wouldn't exercise the read path meaningfully anyway.

Single test, both backends collected in-process — see ``_parity_sweep_util`` for
why (the older parametrized-fixture + module-dict pattern was dead under
``pytest -n auto``).
"""
from __future__ import annotations

from ._parity_sweep_util import (
    build_seeded_client,
    collect_statuses,
    diff_statuses,
)

_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Substrings of paths to skip: heavy/side-effecting ops + binary/stream routes.
_SKIP_SUBSTR = (
    "stream", "sse", "/events", "trigger", "/run", "run-", "warmup",
    "materialize", "scan", "refresh", "rebuild", "upgrade", "restart",
    "shutdown", "export", "download", ".zip", ".git", "throw",
)


def test_mutation_status_is_identical_across_backends(tmp_path, monkeypatch, pg_engine):
    duck_client, duck_token = build_seeded_client(
        "duckdb", tmp_path / "duck", monkeypatch, pg_engine
    )
    duck = collect_statuses(
        duck_client, duck_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR
    )

    pg_client, pg_token = build_seeded_client(
        "pg", tmp_path / "pg", monkeypatch, pg_engine
    )
    pg = collect_statuses(
        pg_client, pg_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR
    )

    divergences = diff_statuses(duck, pg)
    assert not divergences, (
        "Mutation status diverges between DuckDB and Postgres (backend-split):\n"
        + "\n".join(
            f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
        )
    )
