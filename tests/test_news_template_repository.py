"""NewsTemplateRepository — repository-level tests for the v29 news entity.

Covers the draft singleton invariant, monotonic versioning, publish /
unpublish flow, sanitization on save, and the prune rule (drop >30d
EXCEPT the currently-displayed published version).
"""

from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _conn():
    from src.db import get_system_db
    return get_system_db()


def test_initial_state_no_published_no_draft(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        assert repo.get_current_published() is None
        assert repo.get_active_draft() is None
        assert repo.list_versions() == []
    finally:
        conn.close()


def test_save_draft_creates_row_then_updates_in_place(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        a = repo.save_draft(intro="<p>a</p>", content="<p>A</p>", by="alice@x")
        assert a["version"] == 1
        assert a["published"] is False
        assert a["created_by"] == "alice@x"

        # Second save while draft active must update the same row.
        b = repo.save_draft(intro="<p>b</p>", content="<p>B</p>", by="alice@x")
        assert b["version"] == 1, "draft should update in place, not bump version"
        assert b["intro"] == "<p>b</p>"

        rows = repo.list_versions()
        assert len(rows) == 1
        assert rows[0]["status"] == "draft"
    finally:
        conn.close()


def test_publish_then_new_draft_increments_version(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(intro="<p>v1</p>", content="<p>V1</p>", by="alice@x")
        p1 = repo.publish_draft(by="alice@x")
        assert p1["version"] == 1
        assert p1["published"] is True
        assert repo.get_active_draft() is None

        d2 = repo.save_draft(intro="<p>v2</p>", content="<p>V2</p>", by="alice@x")
        assert d2["version"] == 2
        assert d2["published"] is False

        # Web sees only the published v1 until v2 is published.
        cp = repo.get_current_published()
        assert cp is not None and cp["version"] == 1
    finally:
        conn.close()


def test_publish_with_no_draft_raises(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository, NoDraftError
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        with pytest.raises(NoDraftError):
            repo.publish_draft(by="alice@x")
    finally:
        conn.close()


def test_unpublish_falls_back_to_prior_published(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(intro="<p>v1</p>", content="V1", by="alice@x")
        repo.publish_draft(by="alice@x")
        repo.save_draft(intro="<p>v2</p>", content="V2", by="alice@x")
        repo.publish_draft(by="alice@x")

        cp = repo.get_current_published()
        assert cp["version"] == 2

        # Unpublish v2 — web should fall back to v1.
        repo.unpublish(version=2, by="alice@x")
        cp2 = repo.get_current_published()
        assert cp2 is not None and cp2["version"] == 1
    finally:
        conn.close()


def test_unpublish_blocked_when_draft_active(fresh_db):
    from src.repositories.news_template import (
        AlreadyDraftError,
        NewsTemplateRepository,
    )
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(intro="<p>v1</p>", content="V1", by="alice@x")
        repo.publish_draft(by="alice@x")
        repo.save_draft(intro="<p>v2 draft</p>", content="V2", by="alice@x")

        with pytest.raises(AlreadyDraftError):
            repo.unpublish(version=1, by="alice@x")
    finally:
        conn.close()


def test_unpublish_unknown_version_raises_not_found(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository, NotFoundError
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        with pytest.raises(NotFoundError):
            repo.unpublish(version=99, by="alice@x")
    finally:
        conn.close()


def test_unpublish_already_draft_raises(fresh_db):
    from src.repositories.news_template import (
        AlreadyDraftError,
        NewsTemplateRepository,
    )
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(intro="<p>v1</p>", content="V1", by="alice@x")
        # version 1 is a draft, not published — unpublish should refuse
        with pytest.raises(AlreadyDraftError):
            repo.unpublish(version=1, by="alice@x")
    finally:
        conn.close()


def test_save_draft_sanitizes_input(fresh_db):
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(
            intro="<p>hi</p><script>alert(1)</script>",
            content='<iframe src="https://evil.com/x"></iframe><p>ok</p>',
            by="alice@x",
        )
        d = repo.get_active_draft()
        assert "<script>" not in d["intro"]
        assert "evil.com" not in d["content"]
        # Allowlisted iframe survives.
        repo.save_draft(
            intro="",
            content='<iframe src="https://www.youtube.com/embed/abc"></iframe>',
            by="alice@x",
        )
        d2 = repo.get_active_draft()
        assert "youtube.com/embed/abc" in d2["content"]
    finally:
        conn.close()


def test_prune_skips_current_published(fresh_db):
    """Even when the only published version is older than the threshold,
    it must NOT be pruned. The system keeps showing it indefinitely
    until a newer version is published."""
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        repo.save_draft(intro="<p>v1</p>", content="V1", by="alice@x")
        repo.publish_draft(by="alice@x")

        # Force-age the v1 row so prune would see it as old.
        conn.execute(
            "UPDATE news_template SET created_at = current_timestamp - INTERVAL '60 days'"
        )

        repo.prune_old(threshold_days=30)

        cp = repo.get_current_published()
        assert cp is not None and cp["version"] == 1, "current published was wrongly pruned"
    finally:
        conn.close()


def test_prune_drops_old_superseded_and_old_drafts(fresh_db):
    """An old draft + an old superseded published row should both go;
    the most-recent published row stays."""
    from src.repositories.news_template import NewsTemplateRepository
    conn = _conn()
    try:
        repo = NewsTemplateRepository(conn)
        # v1 published, then v2 published (v1 becomes "superseded"), then v3 draft.
        repo.save_draft(intro="<p>v1</p>", content="V1", by="alice@x")
        repo.publish_draft(by="alice@x")
        repo.save_draft(intro="<p>v2</p>", content="V2", by="alice@x")
        repo.publish_draft(by="alice@x")
        repo.save_draft(intro="<p>v3 draft</p>", content="V3 draft", by="alice@x")

        # Age v1 + v3 to >30d. Leave v2 fresh.
        conn.execute(
            "UPDATE news_template "
            "SET created_at = current_timestamp - INTERVAL '60 days' "
            "WHERE version IN (1, 3)"
        )

        repo.prune_old(threshold_days=30)

        versions = {row["version"] for row in repo.list_versions()}
        assert 2 in versions, "current published v2 must remain"
        assert 1 not in versions, "old superseded v1 should be pruned"
        assert 3 not in versions, "old abandoned draft v3 should be pruned"
    finally:
        conn.close()
