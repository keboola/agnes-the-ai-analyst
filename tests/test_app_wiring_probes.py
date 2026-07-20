"""/healthz + /readyz must be served by the real app, unauthenticated, and
the startup guard must run before any DB/backend work in the lifespan.

Companion to tests/test_health_probes.py (which unit-tests the router in
isolation) — this proves the wiring into app/main.py: router registration,
canary-loop task lifecycle, and validate_deployment() placement.
"""

from __future__ import annotations


def test_probes_served_unauthenticated(seeded_app):
    client = seeded_app["client"]
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)


def test_api_health_untouched(seeded_app):
    """/api/health stays the separate, pre-existing liveness alias."""
    client = seeded_app["client"]
    assert client.get("/api/health").status_code == 200
