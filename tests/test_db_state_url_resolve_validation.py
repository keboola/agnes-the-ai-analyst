"""MED-1-PARTIAL — _validate_cloud_url resolves hostnames and rejects
any that map to a reserved IP range, not just IP literals."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException


def test_hostname_resolving_to_metadata_ip_is_rejected() -> None:
    """A hostname that resolves to the GCE metadata IP must be rejected
    — same outcome as posting the literal 169.254.169.254."""
    from app.api.db_state import _validate_cloud_url

    # AGNES_ALLOW_RESERVED_CLOUD_URL must NOT be set for this test.
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("AGNES_ALLOW_RESERVED_CLOUD_URL", None)
        with patch("app.api.db_state._resolve_host",
                   lambda h: {"169.254.169.254"} if h == "metadata.google.internal" else set()):
            with pytest.raises(HTTPException) as exc:
                _validate_cloud_url(
                    "postgresql+psycopg://u:p@metadata.google.internal:5432/db"
                )
            assert exc.value.status_code == 400
            # The error should name the resolved IP so the operator can
            # diagnose the rejection.
            assert "169.254.169.254" in str(exc.value.detail) or "metadata" in str(exc.value.detail).lower()


def test_hostname_resolving_to_rfc1918_is_rejected() -> None:
    """Hostnames pointing at private ranges must also fail."""
    from app.api.db_state import _validate_cloud_url

    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("AGNES_ALLOW_RESERVED_CLOUD_URL", None)
        with patch("app.api.db_state._resolve_host",
                   lambda h: {"10.0.0.5"} if h == "internal.example" else set()):
            with pytest.raises(HTTPException) as exc:
                _validate_cloud_url(
                    "postgresql+psycopg://u:p@internal.example:5432/db"
                )
            assert exc.value.status_code == 400


def test_hostname_resolving_to_public_ip_passes() -> None:
    """A regular hostname that resolves to a public IP passes (the
    pre-fix behaviour for hostnames is preserved for safe cases).

    Uses 8.8.8.8 (Google public DNS — genuinely public) rather than
    203.0.113.50 (TEST-NET-3, RFC 5737) which Python's ipaddress module
    marks as is_private=True since 3.11.
    """
    from app.api.db_state import _validate_cloud_url

    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("AGNES_ALLOW_RESERVED_CLOUD_URL", None)
        with patch("app.api.db_state._resolve_host",
                   lambda h: {"8.8.8.8"} if h == "cloud.example" else set()):
            _validate_cloud_url(  # must not raise
                "postgresql+psycopg://u:p@cloud.example:5432/db"
            )


def test_hostname_with_dns_failure_does_not_crash() -> None:
    """If DNS fails (empty resolution set), validation falls back to
    'allow' — same conservative behaviour as the pre-fix code for
    unresolvable hostnames. The migrator's connect attempt will fail
    cleanly downstream."""
    from app.api.db_state import _validate_cloud_url

    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("AGNES_ALLOW_RESERVED_CLOUD_URL", None)
        with patch("app.api.db_state._resolve_host", lambda h: set()):
            _validate_cloud_url(  # must not raise
                "postgresql+psycopg://u:p@nonexistent.invalid:5432/db"
            )


def test_mixed_resolution_with_one_reserved_ip_rejects() -> None:
    """If ANY resolved IP is reserved (e.g. dual-stack v4+v6 where one
    address is link-local), reject. Defence in depth — a partially
    reserved-range answer is still attacker-controlled."""
    from app.api.db_state import _validate_cloud_url

    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("AGNES_ALLOW_RESERVED_CLOUD_URL", None)
        with patch("app.api.db_state._resolve_host",
                   lambda h: {"8.8.8.8", "127.0.0.1"} if h == "mixed.example" else set()):
            with pytest.raises(HTTPException) as exc:
                _validate_cloud_url(
                    "postgresql+psycopg://u:p@mixed.example:5432/db"
                )
            assert exc.value.status_code == 400
