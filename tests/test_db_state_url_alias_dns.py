"""B2-NEW — _urls_alias detects hostname-vs-IP same-DB aliases."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_alias_detects_hostname_vs_resolved_ip() -> None:
    """``postgres`` (compose service name) resolving to 172.18.0.2 must
    alias-match ``postgresql://...@172.18.0.2:5432/agnes`` — pre-B2-NEW
    they compared string-equal-only and bypassed the guard.
    """
    from app.api.db_state import _urls_alias

    with patch("app.api.db_state._resolve_host", lambda h: {"172.18.0.2"}
               if h == "postgres" else {"203.0.113.50"} if h == "cloud.example.com"
               else set()):
        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@172.18.0.2:5432/agnes"
        c = "postgresql+psycopg://u:p@cloud.example.com:5432/agnes"
        assert _urls_alias(a, b) is True, (a, b)
        assert _urls_alias(b, a) is True, (b, a)
        # Different hosts, no IP overlap → not aliases.
        assert _urls_alias(a, c) is False, (a, c)


def test_alias_falls_back_to_string_compare_on_dns_failure() -> None:
    """When DNS resolution fails for either side, we conservatively
    treat them as ALIAS if string-normalised host+db match — and as
    NON-ALIAS otherwise. The pre-B2-NEW guard is retained for the
    common case."""
    from app.api.db_state import _urls_alias

    with patch("app.api.db_state._resolve_host", lambda h: set()):
        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@postgres:5432/agnes"
        assert _urls_alias(a, b) is True

        a = "postgresql+psycopg://u:p@postgres:5432/agnes"
        b = "postgresql+psycopg://u:p@cloud.example.com:5432/agnes"
        assert _urls_alias(a, b) is False
