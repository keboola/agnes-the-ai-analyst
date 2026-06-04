"""Differential sweep: every param-free GET endpoint must return the SAME
HTTP status on DuckDB and Postgres, given identical seeded state.

A status that differs between backends (e.g. 200 on DuckDB, 500 on Postgres)
is the signature of a backend-split bug on an endpoint no targeted parity test
covers. This sweeps the whole live route table, so it catches divergences the
hand-written cluster tests miss.
"""
from __future__ import annotations

_STATUS: dict[str, dict[str, int]] = {}

# Paths to skip: intentional-throw debug routes + streaming/SSE (would hang).
_SKIP = {"/api/debug/throw", "/_debug/throw/exc"}


def _is_sweepable(route) -> bool:
    methods = getattr(route, "methods", None) or set()
    path = getattr(route, "path", "") or ""
    if "GET" not in methods or "{" in path or path in _SKIP:
        return False
    if "stream" in path or "sse" in path or path.endswith("/events"):
        return False
    return True


def test_collect_get_statuses(seeded_app_both):
    backend = seeded_app_both["backend"]
    client = seeded_app_both["client"]
    auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
    seen: dict[str, int] = {}
    for route in client.app.routes:
        if not _is_sweepable(route):
            continue
        path = route.path
        try:
            r = client.get(path, headers=auth, follow_redirects=False)
            seen[path] = r.status_code
        except Exception as exc:  # noqa: BLE001 — record, don't abort the sweep
            seen[path] = -1  # transport-level failure
            seen[f"{path}::exc"] = type(exc).__name__  # type: ignore[assignment]
    _STATUS[backend] = seen


def test_get_status_is_identical_across_backends():
    assert {"duckdb", "pg"} <= set(_STATUS), (
        f"sweep didn't run on both backends: {set(_STATUS)}"
    )
    duck, pg = _STATUS["duckdb"], _STATUS["pg"]
    paths = {p for p in (set(duck) | set(pg)) if "::exc" not in p}
    divergences = {
        p: (duck.get(p), pg.get(p)) for p in paths if duck.get(p) != pg.get(p)
    }
    assert not divergences, (
        "GET status diverges between DuckDB and Postgres (backend-split):\n"
        + "\n".join(
            f"  {p}: duck={d} pg={g}" for p, (d, g) in sorted(divergences.items())
        )
    )
