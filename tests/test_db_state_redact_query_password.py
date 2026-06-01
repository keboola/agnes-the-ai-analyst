# tests/test_db_state_redact_query_password.py
"""MED-3 — ``_redact_url`` redacts query-string passwords too."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "url,expected_redacted_form",
    [
        # Userinfo style — already handled pre-MED-3.
        # SQLAlchemy renders the mask as "***" (3 asterisks).
        (
            "postgresql+psycopg://agnes:s3cret@host:5432/agnes",
            "postgresql+psycopg://agnes:***@host:5432/agnes",
        ),
        # Query-string style — the MED-3 regression target.
        (
            "postgresql://user@host:5432/db?password=topsecret&sslmode=require",
            # SQLAlchemy renders the masked form however it chooses; the
            # invariant is "topsecret" must not appear in the result.
            None,
        ),
        # Mixed (userinfo + query).
        (
            "postgresql://u:passA@host/db?password=passB",
            None,
        ),
        # Query-string PEM key passphrase — libpq sslpassword.
        (
            "postgresql://user@host:5432/db?sslpassword=pempass&sslmode=require",
            None,  # secret-not-in-output is the contract
        ),
    ],
)
def test_redact_url_removes_all_password_forms(
    url: str, expected_redacted_form: str | None
) -> None:
    from app.api.db_state import _redact_url

    out = _redact_url(url)
    assert out is not None
    # Any literal secret substring from the input must NOT appear in the
    # redacted form. This covers both userinfo and query-string placement.
    for secret in ("s3cret", "topsecret", "passA", "passB", "pempass"):
        if secret in url:
            assert secret not in out, (
                f"redacted form must not echo {secret!r}; got {out!r}"
            )
    if expected_redacted_form is not None:
        assert out == expected_redacted_form


def test_redact_url_none_returns_none() -> None:
    from app.api.db_state import _redact_url

    assert _redact_url(None) is None


def test_redact_url_unparseable_returns_placeholder() -> None:
    """Garbage in → a safe placeholder out, never the original string."""
    from app.api.db_state import _redact_url

    out = _redact_url("not a url with :: weird ::stuff")
    # The exact placeholder is implementation choice; assert it's not the
    # input verbatim and doesn't crash.
    assert out != "not a url with :: weird ::stuff"
