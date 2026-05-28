"""Tests for Slack identity binding (verification code flow).

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""
from pathlib import Path

import duckdb
import pytest
from src.db import _ensure_schema

from services.slack_bot.binding import (
    issue_verification_code,
    lookup_user_email,
    redeem_verification_code,
)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    c.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'u@x', 'U')")
    return c


def test_issue_and_redeem(conn):
    code = issue_verification_code(conn, slack_user_id="U123")
    assert len(code) == 6 and code.isdigit()
    ok = redeem_verification_code(conn, user_email="u@x", code=code)
    assert ok is True
    assert lookup_user_email(_RepoStub(conn), "U123") == "u@x"


def test_redeem_rejects_bad_code(conn):
    issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code="000000") is False


def test_redeem_rejects_expired(conn, monkeypatch):
    import services.slack_bot.binding as b
    monkeypatch.setattr(b, "_CODE_TTL_SECONDS", -1)
    code = issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code=code) is False


class _RepoStub:
    def __init__(self, conn): self._conn = conn
