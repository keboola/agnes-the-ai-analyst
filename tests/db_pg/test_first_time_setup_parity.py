"""Parity test: GET /first-time-setup redirects to /login on both backends when
users already exist.

The handler gates the wizard on "are there any users yet?". It counted users
with a raw `SELECT count(*)` on the always-DuckDB `_get_db` connection — on a
Postgres instance the DuckDB system DB is empty, so the wizard reported
first-time even when PG already had users, rendering the setup page (200)
instead of redirecting to /login (302). The count now goes through
`users_repo().count_all()`, which routes to the active backend.

`seeded_app_both` seeds an admin + analyst, so the wizard must redirect (302)
on whichever backend is active.
"""
from __future__ import annotations


def test_first_time_setup_redirects_when_users_exist(seeded_app_both):
    backend = seeded_app_both["backend"]
    client = seeded_app_both["client"]

    r = client.get("/first-time-setup", follow_redirects=False)

    assert r.status_code == 302, (
        f"[{backend}] /first-time-setup returned {r.status_code}, expected 302 "
        f"redirect — the wizard counted users off the wrong backend and thinks "
        f"this is a fresh install."
    )
    assert r.headers.get("location", "").endswith("/login"), (
        f"[{backend}] /first-time-setup redirected to "
        f"{r.headers.get('location')!r}, expected /login"
    )
