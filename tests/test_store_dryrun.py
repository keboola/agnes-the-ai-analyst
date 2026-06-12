"""Tests for the pre-submit dry-run endpoint — POST /api/store/entities/dryrun.

The dry-run runs the full guardrail pipeline (inline checks + LLM review)
against a candidate bundle WITHOUT persisting any ``store_entities`` or
``store_submissions`` row, so submitters can iterate before the real upload.

Asserted invariants:
  * clean bundle  → 200, ``would_publish=true``, manifest status ``pass``
  * banned bundle → ``would_publish=false`` with the issue surfaced
  * NEITHER path writes a row (entities + submissions counts unchanged)

The Anthropic call is mocked — these tests never hit a live LLM.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    # Provider-ready so the dry-run exercises the LLM branch (mocked below).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dryrun-key")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


_OK_DESC = "Use when validating the store dry-run pipeline across every guardrail tier"
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2


def _make_skill_zip(skill_name: str = "code-review", desc: str = _OK_DESC, body: str = _OK_BODY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {desc}\n---\n\n{body}\n",
        )
    return buf.getvalue()


def _make_banned_skill_zip(skill_name: str = "evil-skill") -> bytes:
    """A bundle whose script trips the static-security inline check (eval())."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
        # Code file (.py is in-scope for the static scanner) with a banned
        # construct — eval() on attacker-controlled input.
        zf.writestr(f"{skill_name}/run.py", "import sys\neval(sys.argv[1])\n")
    return buf.getvalue()


# Verdict the mocked LLM review returns for the happy path — safe, no
# findings, content quality pass → is_safe() == True.
_SAFE_VERDICT = {
    "risk_level": "safe",
    "summary": "Looks clean.",
    "findings": [],
    "template_placeholders_found": 0,
    "content_quality": {"verdict": "pass", "issues": []},
    "reviewed_by_model": "claude-test",
    "error": None,
}

# A risky verdict — high-severity finding → is_safe() == False.
_RISKY_VERDICT = {
    "risk_level": "high",
    "summary": "Exfiltrates environment variables.",
    "findings": [
        {
            "severity": "critical",
            "category": "exfiltration",
            "file": "run.py",
            "explanation": "Reads $ANTHROPIC_API_KEY and POSTs it to a remote host.",
            "fix_hint": "Remove the network callout.",
        }
    ],
    "template_placeholders_found": 0,
    "content_quality": {"verdict": "pass", "issues": []},
    "reviewed_by_model": "claude-test",
    "error": None,
}


def _row_counts():
    """(store_entities, store_submissions) row counts from system.duckdb."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        ents = conn.execute("SELECT COUNT(*) FROM store_entities").fetchone()[0]
        subs = conn.execute("SELECT COUNT(*) FROM store_submissions").fetchone()[0]
        return ents, subs
    finally:
        conn.close()


def _enable_guardrails(monkeypatch):
    """Flip the guardrail pipeline ON for a single test.

    The autouse ``_flea_guardrails_disabled_by_default`` conftest fixture
    patches ``app.api.store.get_guardrails_enabled`` to return False; the
    LLM tier only runs when both intent and provider-readiness are True.
    """
    monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
    monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)


class TestDryRunClean:
    def test_clean_bundle_would_publish_no_rows(self, web_client, monkeypatch):
        _enable_guardrails(monkeypatch)
        _create_user(web_client, "alice@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_SAFE_VERDICT,
        ) as mock_review:
            _, cookies = _create_user(web_client, "carol@x.com")
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip(), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["would_publish"] is True, body
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        # LLM verdict surfaced to the submitter.
        assert body["llm_findings"]["risk_level"] == "safe"
        mock_review.assert_called_once()

        # ZERO DB writes.
        assert _row_counts() == before


class TestDryRunBanned:
    def test_banned_content_blocks_no_rows(self, web_client):
        _, cookies = _create_user(web_client, "dave@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_SAFE_VERDICT,
        ):
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_banned_skill_zip(), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        # Inline static-security tier trips on eval() → cannot publish.
        assert body["would_publish"] is False, body
        assert body["inline_checks"]["static_security"]["status"] == "fail", body
        findings = body["inline_checks"]["static_security"]["findings"]
        assert any("eval" in json.dumps(f).lower() for f in findings), findings

        # ZERO DB writes even on a blocked dry-run.
        assert _row_counts() == before

    def test_llm_risky_verdict_blocks_no_rows(self, web_client, monkeypatch):
        """Inline passes but the LLM flags a high-severity finding."""
        _enable_guardrails(monkeypatch)
        _, cookies = _create_user(web_client, "erin@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_RISKY_VERDICT,
        ):
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("clean-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        assert body["would_publish"] is False, body
        assert body["llm_findings"]["risk_level"] == "high"

        assert _row_counts() == before


class TestDryRunAuth:
    def test_requires_auth(self, web_client):
        r = web_client.post(
            "/api/store/entities/dryrun",
            files={"file": ("s.zip", _make_skill_zip(), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
        )
        assert r.status_code in (401, 403), r.text
