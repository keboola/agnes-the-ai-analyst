"""Regression test: ``seed_admin`` adds the user to BOTH Admin AND
Everyone groups.

When LOCAL_DEV_MODE is on (or ``SEED_ADMIN_EMAIL`` is set in
production), ``app/main.py`` seeds an admin user on startup. Previously
it only added them to ``Admin``, so Everyone-scoped grants — the
canonical pattern for "every-user-sees-this" required onboarding —
didn't surface on the seed admin's own /catalog. Looked like a bug.

This regression locks in the dual-group seeding so a fresh
LOCAL_DEV_MODE checkout can demonstrate Required-tier grants without a
manual ``/admin/access`` Everyone-membership step first.
"""

from __future__ import annotations

import logging
import uuid

import duckdb

from src.db import (
    SYSTEM_ADMIN_GROUP,
    SYSTEM_EVERYONE_GROUP,
    _ensure_schema,
)
from src.repositories.audit import AuditRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.users import UserRepository
from src.user_identity import normalize_email


def _run_seed_admin_block_with_failure(conn, email: str, boom: Exception) -> None:
    """Replicate the FAILURE branch of the seed_admin block from
    ``app.main`` lifespan — the ``except`` clause that used to swallow every
    failure into ``logger.warning``.

    Kept in step with the real block — see
    ``test_seed_admin_failure_is_logged_and_recorded_durably``, which pins
    the source itself so this replica cannot quietly drift away from it.
    """
    logger = logging.getLogger("app.main")
    email = normalize_email(email)
    try:
        raise boom
    except Exception as e:
        logger.error("Seed admin failed for %s: %s", email, e, exc_info=True)
        try:
            AuditRepository(conn).log(
                user_id=None,
                action="startup.seed_admin_failed",
                resource=email,
                result="error",
                params={"error": str(e)},
            )
        except Exception:
            logger.debug("Could not record seed-admin failure to audit_log", exc_info=True)


def _run_seed_admin_block(conn, email: str) -> str:
    """Replicate the seed_admin block from ``app.main`` lifespan.

    Kept in step with the real block — see
    ``test_seed_admin_block_resolves_identity_case_insensitively``, which pins
    the source itself so this replica cannot quietly drift away from it.
    """
    repo = UserRepository(conn)
    email = normalize_email(email)
    existing = repo.get_by_email_ci(email)
    if not existing:
        user_id = str(uuid.uuid4())
        repo.create(id=user_id, email=email, name="Admin", password_hash=None)
    else:
        user_id = existing["id"]
    admin_group = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?",
        [SYSTEM_ADMIN_GROUP],
    ).fetchone()
    if admin_group:
        UserGroupMembersRepository(conn).add_member(
            user_id=user_id,
            group_id=admin_group[0],
            source="system_seed",
            added_by="app.main:seed_admin",
        )
    everyone_group = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?",
        [SYSTEM_EVERYONE_GROUP],
    ).fetchone()
    if everyone_group:
        UserGroupMembersRepository(conn).add_member(
            user_id=user_id,
            group_id=everyone_group[0],
            source="system_seed",
            added_by="app.main:seed_admin",
        )
    return user_id


def test_seed_admin_lands_in_both_admin_and_everyone():
    """The seed admin must be in both groups so Everyone-scoped Required
    grants surface for them on /catalog without manual operator action."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    user_id = _run_seed_admin_block(conn, "dev@localhost")

    groups = {
        r[0]
        for r in conn.execute(
            "SELECT g.name FROM user_group_members m JOIN user_groups g ON g.id = m.group_id WHERE m.user_id = ?",
            [user_id],
        ).fetchall()
    }
    assert SYSTEM_ADMIN_GROUP in groups, "seed admin must be in Admin (admin authorization)"
    assert SYSTEM_EVERYONE_GROUP in groups, (
        "seed admin must be in Everyone (Everyone-scoped grants must "
        "surface for them — Required onboarding grant target)"
    )


def test_seed_admin_is_idempotent_on_re_run():
    """Re-running ``seed_admin`` (lifespan startup hook fires every
    boot) must not duplicate membership rows."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    user_id = _run_seed_admin_block(conn, "dev@localhost")
    _run_seed_admin_block(conn, "dev@localhost")  # re-fire

    counts = {}
    for group_name in (SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP):
        counts[group_name] = conn.execute(
            "SELECT COUNT(*) FROM user_group_members m "
            "JOIN user_groups g ON g.id = m.group_id "
            "WHERE m.user_id = ? AND g.name = ?",
            [user_id, group_name],
        ).fetchone()[0]
    assert counts[SYSTEM_ADMIN_GROUP] == 1, "Admin membership must not duplicate"
    assert counts[SYSTEM_EVERYONE_GROUP] == 1, "Everyone membership must not duplicate"


def test_seed_admin_block_resolves_identity_case_insensitively():
    """Pin the real lifespan block, not just the replica above.

    Seeding is an account-CREATING path. With a mixed-case
    ``SEED_ADMIN_EMAIL`` over an existing normalized row, an exact-match
    existence check mints a SECOND account and puts Admin + Everyone on it —
    while every auth door resolves the OLDEST match, i.e. the row the person
    actually signs in as, which has neither. A static read of the source is
    the honest gate here: the block lives inline in ``lifespan`` and cannot be
    called without standing up the whole app.
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    start = src.index("seed_email = ")
    block = src[start : src.index('added_by="app.main:seed_admin"', start)]
    assert "normalize_email(" in block, "seed_email must be normalized on write"
    assert "get_by_email_ci(seed_email)" in block, "the existence check must fold case"
    assert "get_by_email(seed_email)" not in block, "exact-match existence check would duplicate the account"


def test_seed_admin_failure_is_logged_and_recorded_durably():
    """A raised exception in the FAILURE branch must be logged at ERROR
    (with the traceback) and recorded to ``audit_log`` — not swallowed into
    ``logger.warning`` with no durable trace, which is what made a failed
    seed invisible short of reading container logs on the VM."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    caplog_records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            caplog_records.append(record)

    logger = logging.getLogger("app.main")
    handler = _Handler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        _run_seed_admin_block_with_failure(conn, "dev@localhost", RuntimeError("boom"))
    finally:
        logger.removeHandler(handler)

    error_records = [r for r in caplog_records if r.levelno == logging.ERROR]
    assert error_records, "seed-admin failure must be logged at ERROR"
    assert error_records[0].exc_info is not None, (
        "the ERROR log must carry the exception (exc_info), not just its message"
    )

    row = conn.execute("SELECT result, params FROM audit_log WHERE action = 'startup.seed_admin_failed'").fetchone()
    assert row is not None, "seed-admin failure must be recorded to audit_log (durable, readable via /admin/activity)"
    assert row[0] == "error"


def test_seed_admin_block_surfaces_failure_loudly_in_real_source():
    """Pin the real lifespan block's FAILURE branch — the ``except`` clause
    that used to be ``logger.warning(f"Could not seed admin: {e}")`` and
    nothing else. A static read of the source is the honest gate here: the
    block lives inline in ``lifespan`` and cannot be called without standing
    up the whole app (see ``test_seed_admin_block_resolves_identity_case_insensitively``
    above for the same rationale)."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    start = src.index("seed_email = normalize_email(")
    end = src.index("# Seed the synthetic scheduler user", start)
    block = src[start:end]

    assert "except Exception as e:" in block, "must still catch Exception, never bare except / BaseException"
    assert "except BaseException" not in block
    assert "logger.error(" in block, "a failed seed must be logged at ERROR, not WARNING"
    assert "exc_info=True" in block, "the ERROR log must carry the traceback"
    assert 'action="startup.seed_admin_failed"' in block, "the failure must be recorded durably (audit_log)"
    assert 'logger.warning(f"Could not seed admin: {e}")' not in block, "the old silent-swallow warning must be gone"
