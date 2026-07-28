# tests/test_pii_scrub_walks_keys.py
"""LOW-1 — PII scrub walks JSON keys, never matches values."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest


def _make_duckdb_with_audit_rows(tmp_path: Path, rows: list[dict]) -> Path:
    db = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db))
    c.execute(
        """CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            actor_user_id VARCHAR,
            action VARCHAR,
            params VARCHAR,
            params_before VARCHAR,
            timestamp TIMESTAMP
        )"""
    )
    for r in rows:
        c.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, current_timestamp)",
            [r["id"], r.get("actor"), r["action"], r["params"], r.get("params_before")],
        )
    c.close()
    return db


def test_scrub_does_not_redact_value_only_match(tmp_path: Path) -> None:
    """Row whose VALUE text contains 'password' but no key is 'password'
    must be kept verbatim. LOW-1 over-redaction repro: HTTP path
    ``/reset-password`` matched the regex when scanning the whole
    serialised JSON.
    """
    from scripts.db_state_migrator import scrub_audit_log_pii

    benign_params = json.dumps({"path": "/reset-password", "method": "POST"})
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r1", "action": "http_request", "params": benign_params}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 0, summary

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r1'").fetchone()[0]
    c.close()
    assert json.loads(out) == {"path": "/reset-password", "method": "POST"}


def test_scrub_does_redact_key_match(tmp_path: Path) -> None:
    """Row whose JSON has a sensitive KEY must be redacted."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    sensitive = json.dumps({"username": "alice", "password": "topsecret"})
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r2", "action": "login", "params": sensitive}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 1

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r2'").fetchone()[0]
    c.close()
    after = json.loads(out)
    assert after.get("password") != "topsecret"
    # And the non-sensitive sibling key survives.
    assert after.get("username") == "alice"


def test_scrub_handles_nested_keys(tmp_path: Path) -> None:
    """LOW-1 fix should recurse into nested dicts/lists."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    sensitive = json.dumps(
        {"creds": {"token": "abc", "user": "bob"}, "items": [{"api_key": "k1"}]}
    )
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r3", "action": "x", "params": sensitive}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 1

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r3'").fetchone()[0]
    c.close()
    after = json.loads(out)
    assert after["creds"].get("token") != "abc"
    assert after["creds"].get("user") == "bob"
    assert after["items"][0].get("api_key") != "k1"


def test_scrub_leaves_non_json_rows_alone(tmp_path: Path) -> None:
    """A row whose params is not JSON (or NULL) is left as-is."""
    from scripts.db_state_migrator import scrub_audit_log_pii

    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [
            {"id": "r4", "action": "x", "params": "not json"},
            {"id": "r5", "action": "x", "params": None},
        ],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 0


def test_scrub_does_not_match_innocent_substrings(tmp_path: Path) -> None:
    """Keys like ``secretary``, ``tokenize``, ``customer_token_count``
    must NOT trigger redaction — they only contain sensitive words
    as substrings. Word-boundary anchors in the regex prevent this.
    """
    from scripts.db_state_migrator import scrub_audit_log_pii

    benign = json.dumps({
        "secretary": "Alice",
        "tokenize": True,
        "customer_token_count": 42,
        "passwordless_login": False,
    })
    db = _make_duckdb_with_audit_rows(
        tmp_path,
        [{"id": "r6", "action": "x", "params": benign}],
    )
    summary = scrub_audit_log_pii(db)
    assert summary["rows_redacted"] == 0, summary

    c = duckdb.connect(str(db), read_only=True)
    out = c.execute("SELECT params FROM audit_log WHERE id = 'r6'").fetchone()[0]
    c.close()
    after = json.loads(out)
    assert after["secretary"] == "Alice"
    assert after["tokenize"] is True
    assert after["customer_token_count"] == 42
    assert after["passwordless_login"] is False
