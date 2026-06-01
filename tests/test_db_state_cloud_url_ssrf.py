# tests/test_db_state_cloud_url_ssrf.py
"""MED-2 — _validate_cloud_url rejects reserved/private address ranges."""
from __future__ import annotations

import pytest


_REJECT_CASES = [
    # IPv4 loopback
    "postgresql+psycopg://u:p@127.0.0.1:5432/db",
    "postgresql+psycopg://u:p@127.5.4.3:5432/db",
    # IPv4 GCE metadata + AWS IMDS
    "postgresql+psycopg://u:p@169.254.169.254:5432/db",
    # IPv4 link-local
    "postgresql+psycopg://u:p@169.254.10.20:5432/db",
    # RFC1918 private
    "postgresql+psycopg://u:p@10.0.0.5:5432/db",
    "postgresql+psycopg://u:p@192.168.1.10:5432/db",
    "postgresql+psycopg://u:p@172.16.4.4:5432/db",
    # CGNAT (RFC6598)
    "postgresql+psycopg://u:p@100.64.0.1:5432/db",
    # IPv6 loopback + ULA
    "postgresql+psycopg://u:p@[::1]:5432/db",
    "postgresql+psycopg://u:p@[fd00::1]:5432/db",
    # Hostname literal localhost — special-case (no DNS resolution).
    "postgresql+psycopg://u:p@localhost:5432/db",
]


@pytest.mark.parametrize("url", _REJECT_CASES)
def test_cloud_url_rejects_reserved_addresses(url: str) -> None:
    from app.api.db_state import _validate_cloud_url
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _validate_cloud_url(url)
    assert exc.value.status_code == 400
    detail = str(exc.value.detail).lower()
    assert (
        "reserved" in detail
        or "private" in detail
        or "loopback" in detail
        or "link-local" in detail
        or "metadata" in detail
        or "cgnat" in detail
    ), exc.value.detail


_ACCEPT_CASES = [
    "postgresql+psycopg://u:p@db.example.com:5432/agnes",
    "postgresql+psycopg://u:p@8.8.8.8:5432/db",
    "postgresql+psycopg://u:p@cloudsql.gcp.example/db",
]


@pytest.mark.parametrize("url", _ACCEPT_CASES)
def test_cloud_url_accepts_public_hosts(url: str) -> None:
    from app.api.db_state import _validate_cloud_url

    # No exception → pass.
    _validate_cloud_url(url)


def test_cloud_url_opt_in_allows_loopback_for_tests(monkeypatch) -> None:
    """An explicit env opt-in unblocks 127.0.0.1 for the test harness."""
    monkeypatch.setenv("AGNES_ALLOW_RESERVED_CLOUD_URL", "1")
    from app.api.db_state import _validate_cloud_url

    _validate_cloud_url("postgresql+psycopg://u:p@127.0.0.1:5432/db")
