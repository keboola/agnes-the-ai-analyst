"""Differential sweep for mutation endpoints (POST/PUT/PATCH/DELETE).

Companion to the GET sweep: every parameter-free mutation route is called with
an empty JSON body on DuckDB and on Postgres (identical seeded state) and the
HTTP status must match. A status that differs between backends (e.g. 422 on
DuckDB, 500 on Postgres) is the signature of a handler that reads state off a
raw `Depends(_get_db)` connection during auth/validation — the backend-split
class the static guard can't see.

Safe to run blindly: `seeded_app_both` uses per-test ephemeral databases, so a
side-effecting mutation only touches a throwaway DB. Side-effecting / long
endpoints (sync triggers, cache warmup, materialize, exports) are skipped both
because they're slow and because an empty body wouldn't exercise the read path
meaningfully anyway.
"""
from __future__ import annotations

_MUT_STATUS: dict[str, dict[str, int]] = {}

# Substrings of paths to skip: heavy/side-effecting ops + binary/stream routes.
_SKIP_SUBSTR = (
    "stream", "sse", "/events", "trigger", "/run", "run-", "warmup",
    "materialize", "scan", "refresh", "rebuild", "upgrade", "restart",
    "shutdown", "export", "download", ".zip", ".git", "throw",
)


def _mutation_methods(route):
    methods = set(getattr(route, "methods", None) or set())
    return sorted(methods & {"POST", "PUT", "PATCH", "DELETE"})


def _is_sweepable(route) -> bool:
    path = getattr(route, "path", "") or ""
    if "{" in path:
        return False
    if any(s in path for s in _SKIP_SUBSTR):
        return False
    return bool(_mutation_methods(route))


def test_collect_mutation_statuses(seeded_app_both):
    backend = seeded_app_both["backend"]
    client = seeded_app_both["client"]
    auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
    seen: dict[str, int] = {}
    for route in client.app.routes:
        if not _is_sweepable(route):
            continue
        for method in _mutation_methods(route):
            key = f"{method} {route.path}"
            try:
                r = client.request(
                    method, route.path, json={}, headers=auth, follow_redirects=False
                )
                seen[key] = r.status_code
            except Exception:  # noqa: BLE001 — record as transport failure
                seen[key] = -1
    _MUT_STATUS[backend] = seen


def test_mutation_status_is_identical_across_backends():
    assert {"duckdb", "pg"} <= set(_MUT_STATUS), (
        f"sweep didn't run on both backends: {set(_MUT_STATUS)}"
    )
    duck, pg = _MUT_STATUS["duckdb"], _MUT_STATUS["pg"]
    keys = set(duck) | set(pg)
    divergences = {
        k: (duck.get(k), pg.get(k)) for k in keys if duck.get(k) != pg.get(k)
    }
    assert not divergences, (
        "Mutation status diverges between DuckDB and Postgres (backend-split):\n"
        + "\n".join(
            f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
        )
    )
