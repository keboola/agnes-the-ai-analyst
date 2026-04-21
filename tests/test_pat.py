"""Tests for #12 — personal access tokens (PAT)."""

import os
import tempfile
import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def test_schema_v6_creates_pat_table(fresh_db):
    from src.db import get_system_db, get_schema_version, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()
        col_names = [c[1] for c in cols]
        for expected in ("id", "user_id", "name", "token_hash", "prefix",
                         "scopes", "created_at", "expires_at", "last_used_at", "revoked_at"):
            assert expected in col_names
        assert get_schema_version(conn) >= 6
    finally:
        conn.close()
        close_system_db()
