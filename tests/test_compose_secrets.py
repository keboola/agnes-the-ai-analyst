# tests/test_compose_secrets.py
"""LOW-2 — compose overlay must not carry a literal POSTGRES_PASSWORD
default; missing env should fail fast, not silently default to a
known-weak credential."""
from __future__ import annotations

from pathlib import Path


def test_postgres_overlay_has_no_literal_password_default() -> None:
    """``${POSTGRES_PASSWORD:-agnes}`` is the LOW-2 footgun. Reject the
    shell-default form anywhere in docker-compose.postgres.yml — the
    overlay must use bare ``${POSTGRES_PASSWORD}`` so docker compose
    errors out if the env var is unset rather than booting Postgres
    with credentials ``agnes/agnes``."""
    overlay = Path("docker-compose.postgres.yml").read_text()
    bad = []
    for lineno, line in enumerate(overlay.splitlines(), start=1):
        if "POSTGRES_PASSWORD" in line and ":-agnes" in line:
            bad.append(f"line {lineno}: {line.strip()}")
    assert not bad, (
        "LOW-2: ``${POSTGRES_PASSWORD:-agnes}`` ships known-weak "
        "fallback creds; replace with ``${POSTGRES_PASSWORD}``\n"
        + "\n".join(bad)
    )
