"""TTL-purge of blocked-bundle bytes.

Covers:
* Bundles older than TTL get rmtree'd, entity row deleted, submission
  row stamped with bundle_purged_at and entity_id nulled. Sha + size
  survive on the row for forensic correlation.
* Approved / overridden / pending submissions are not touched.
* Idempotent — running twice doesn't re-purge.
* ttl_days=0 short-circuits to a no-op.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src import db
    db._system_db_conn = None
    db._system_db_path = None
    c = db.get_system_db()
    yield c
    c.close()


def _seed_with_bundle(conn, store_root: Path, owner_id: str, name: str,
                      status: str, days_old: int) -> tuple[str, str]:
    """Stage entity + bundle on disk + submission row at a given age."""
    import uuid
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=owner_id, email=f"{owner_id}@x.com", name=owner_id)

    eid = uuid.uuid4().hex
    plugin_dir = store_root / eid / "plugin" / "skills" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")

    StoreEntitiesRepository(conn).create(
        id=eid, owner_user_id=owner_id, owner_username=owner_id,
        type="skill", name=name, description="x", category=None,
        version="1.0.0", file_size=10, visibility_status="hidden",
    )

    sub_id = StoreSubmissionsRepository(conn).create(
        submitter_id=owner_id, submitter_email=f"{owner_id}@x.com",
        type="skill", name=name, version="1.0.0",
        status=status, entity_id=eid,
        file_size=42, bundle_sha256="deadbeef" * 8,
    )
    # Backdate created_at so the TTL test sees the row as old.
    if days_old > 0:
        old = datetime.now(timezone.utc) - timedelta(days=days_old)
        conn.execute(
            "UPDATE store_submissions SET created_at = ? WHERE id = ?",
            [old, sub_id],
        )
    return sub_id, eid


class TestPurgeBlockedBundles:
    def test_purges_old_blocked_bundle(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.repositories.store_entities import StoreEntitiesRepository

        sub_id, eid = _seed_with_bundle(
            conn, tmp_path / "store", "u1", "old-bad",
            status="blocked_llm", days_old=45,
        )
        plugin_dir = tmp_path / "store" / eid / "plugin"
        assert plugin_dir.exists()

        result = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert result["purged"] == 1
        assert sub_id in result["ids"]

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["bundle_purged_at"] is not None
        assert sub["entity_id"] is None
        # SHA + size survive for forensics
        assert sub["bundle_sha256"] == "deadbeef" * 8
        assert sub["file_size"] == 42

        assert not plugin_dir.exists()
        assert StoreEntitiesRepository(conn).get(eid) is None

    def test_skips_recent_blocked(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles
        from src.repositories.store_submissions import StoreSubmissionsRepository

        sub_id, eid = _seed_with_bundle(
            conn, tmp_path / "store", "u1", "fresh-bad",
            status="blocked_llm", days_old=2,
        )
        result = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert result["purged"] == 0
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["bundle_purged_at"] is None
        assert (tmp_path / "store" / eid / "plugin").exists()

    def test_skips_approved(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles

        sub_id, eid = _seed_with_bundle(
            conn, tmp_path / "store", "u1", "old-approved",
            status="approved", days_old=100,
        )
        result = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert result["purged"] == 0
        assert (tmp_path / "store" / eid / "plugin").exists()

    def test_skips_overridden(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles

        sub_id, eid = _seed_with_bundle(
            conn, tmp_path / "store", "u1", "old-override",
            status="overridden", days_old=100,
        )
        result = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert result["purged"] == 0

    def test_idempotent(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles

        _seed_with_bundle(
            conn, tmp_path / "store", "u1", "x",
            status="blocked_llm", days_old=45,
        )
        first = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert first["purged"] == 1
        # Second run must purge nothing (bundle_purged_at already set,
        # entity_id is null).
        second = purge_blocked_bundles(
            conn, ttl_days=30,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert second["purged"] == 0

    def test_ttl_zero_is_noop(self, conn, tmp_path):
        from src.store_guardrails.purge import purge_blocked_bundles

        sub_id, eid = _seed_with_bundle(
            conn, tmp_path / "store", "u1", "x",
            status="blocked_llm", days_old=999,
        )
        result = purge_blocked_bundles(
            conn, ttl_days=0,
            store_dir_resolver=lambda: tmp_path / "store",
        )
        assert result == {"purged": 0, "ids": [], "skipped": True}
        assert (tmp_path / "store" / eid / "plugin").exists()
