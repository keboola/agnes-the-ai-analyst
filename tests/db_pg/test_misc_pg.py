"""Postgres-side tests for the miscellaneous cluster:
profiles, welcome_template, notifications (telegram/pending/script), news_template.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def misc_engine(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------

def test_profile_save_and_get(misc_engine):
    from src.repositories.profiles_pg import ProfilePgRepository

    repo = ProfilePgRepository(misc_engine)
    repo.save("orders", {"rows": 1000, "columns": {"amount": {"type": "decimal"}}})
    profile = repo.get("orders")
    assert profile["rows"] == 1000
    assert "profiled_at" in profile


def test_profile_get_all(misc_engine):
    from src.repositories.profiles_pg import ProfilePgRepository

    repo = ProfilePgRepository(misc_engine)
    repo.save("a", {"r": 1})
    repo.save("b", {"r": 2})
    all_profiles = repo.get_all()
    assert set(all_profiles) == {"a", "b"}
    assert all_profiles["a"]["r"] == 1


def test_profile_upsert(misc_engine):
    from src.repositories.profiles_pg import ProfilePgRepository

    repo = ProfilePgRepository(misc_engine)
    repo.save("orders", {"r": 1})
    repo.save("orders", {"r": 2})
    assert repo.get("orders")["r"] == 2


# ---------------------------------------------------------------------------
# welcome_template
# ---------------------------------------------------------------------------

def test_welcome_template_seed_and_set(misc_engine):
    from src.repositories.welcome_template_pg import WelcomeTemplatePgRepository

    repo = WelcomeTemplatePgRepository(misc_engine)
    assert repo.get() == {"id": 1, "content": None, "updated_at": None, "updated_by": None}
    repo.set("Welcome to Agnes!", updated_by="admin")
    assert repo.get()["content"] == "Welcome to Agnes!"
    repo.reset(updated_by="admin")
    assert repo.get()["content"] is None


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def test_telegram_link_upsert(misc_engine):
    from src.repositories.notifications_pg import TelegramPgRepository

    repo = TelegramPgRepository(misc_engine)
    repo.link_user("u1", 12345)
    repo.link_user("u1", 67890)  # overwrites
    link = repo.get_link("u1")
    assert link["chat_id"] == 67890
    repo.unlink_user("u1")
    assert repo.get_link("u1") is None


def test_pending_code_verify_consumes(misc_engine):
    from src.repositories.notifications_pg import PendingCodePgRepository

    repo = PendingCodePgRepository(misc_engine)
    repo.create_code("ABCDEF", 12345)
    row = repo.verify_code("ABCDEF")
    assert row["chat_id"] == 12345
    # Second verify returns None — code is consumed
    assert repo.verify_code("ABCDEF") is None


def test_script_deploy_and_atomic_claim(misc_engine):
    from src.repositories.notifications_pg import ScriptPgRepository

    repo = ScriptPgRepository(misc_engine)
    repo.deploy(id="s1", name="hello", source="print('hi')")
    assert repo.claim_for_run("s1") is True
    # Second claim must fail (already running)
    assert repo.claim_for_run("s1") is False

    repo.record_run_result("s1", "success")
    # After clearing, claim is back available
    assert repo.claim_for_run("s1") is True


def test_script_record_run_result_validates_status(misc_engine):
    from src.repositories.notifications_pg import ScriptPgRepository

    repo = ScriptPgRepository(misc_engine)
    repo.deploy(id="s1", name="hello", source="print('hi')")
    with pytest.raises(ValueError):
        repo.record_run_result("s1", "running")


# ---------------------------------------------------------------------------
# news_template
# ---------------------------------------------------------------------------

def test_news_save_draft_creates_first_version(misc_engine):
    from src.repositories.news_template_pg import NewsTemplatePgRepository

    repo = NewsTemplatePgRepository(misc_engine)
    draft = repo.save_draft(intro="hello", content="full body", by="admin")
    assert draft["version"] == 1
    assert draft["published"] is False


def test_news_publish_draft_flips_bit(misc_engine):
    from src.repositories.news_template_pg import NewsTemplatePgRepository

    repo = NewsTemplatePgRepository(misc_engine)
    repo.save_draft(intro="hello", content="body", by="admin")
    published = repo.publish_draft(by="admin")
    assert published["published"] is True
    assert repo.get_current_published()["version"] == 1
    assert repo.get_active_draft() is None


def test_news_save_draft_updates_existing_draft(misc_engine):
    from src.repositories.news_template_pg import NewsTemplatePgRepository

    repo = NewsTemplatePgRepository(misc_engine)
    d1 = repo.save_draft(intro="v1", content="body", by="admin")
    d2 = repo.save_draft(intro="v1b", content="body2", by="admin")
    # Same version, edited in place
    assert d2["version"] == d1["version"]
    assert d2["intro"] == "v1b"


def test_news_publish_raises_when_no_draft(misc_engine):
    from src.repositories.news_template_pg import (
        NewsTemplatePgRepository,
        NoDraftError,
    )

    repo = NewsTemplatePgRepository(misc_engine)
    with pytest.raises(NoDraftError):
        repo.publish_draft(by="admin")


def test_news_unpublish_blocked_by_existing_draft(misc_engine):
    from src.repositories.news_template_pg import (
        AlreadyDraftError,
        NewsTemplatePgRepository,
    )

    repo = NewsTemplatePgRepository(misc_engine)
    repo.save_draft(intro="v1", content="body", by="admin")
    repo.publish_draft(by="admin")
    # Now create a new draft
    repo.save_draft(intro="v2", content="body2", by="admin")
    # Cannot unpublish v1 while v2 is the active draft
    with pytest.raises(AlreadyDraftError):
        repo.unpublish(version=1, by="admin")


def test_news_version_conflict(misc_engine):
    from src.repositories.news_template_pg import (
        NewsTemplatePgRepository,
        VersionConflictError,
    )

    repo = NewsTemplatePgRepository(misc_engine)
    d1 = repo.save_draft(intro="v1", content="body", by="admin")
    # Caller expects v1 → succeeds
    repo.save_draft(intro="v1b", content="body", by="admin", expected_version=d1["version"])
    # Caller expects v999 → fails
    with pytest.raises(VersionConflictError):
        repo.save_draft(intro="x", content="y", by="admin", expected_version=999)
