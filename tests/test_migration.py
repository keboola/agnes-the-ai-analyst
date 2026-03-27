"""Tests for JSON -> DuckDB migration script."""

import json
import os
import pytest


@pytest.fixture
def migration_env(tmp_path):
    """Create temp dir with sample JSON files mimicking production layout."""
    data_dir = tmp_path / "data"
    (data_dir / "notifications").mkdir(parents=True)
    (data_dir / "corporate-memory").mkdir(parents=True)
    (data_dir / "auth").mkdir(parents=True)
    (data_dir / "src_data" / "metadata").mkdir(parents=True)

    # sync_state.json
    (data_dir / "src_data" / "metadata" / "sync_state.json").write_text(json.dumps({
        "tables": {
            "orders": {"rows": 1000, "file_size_bytes": 5000, "hash": "abc"},
            "customers": {"rows": 500, "file_size_bytes": 2000, "hash": "def"},
        }
    }))

    # knowledge.json
    (data_dir / "corporate-memory" / "knowledge.json").write_text(json.dumps([
        {"id": "k1", "title": "MRR", "content": "Monthly...", "category": "metrics", "status": "approved"},
        {"id": "k2", "title": "Churn", "content": "Rate of...", "category": "metrics", "status": "pending"},
    ]))

    # telegram_users.json
    (data_dir / "notifications" / "telegram_users.json").write_text(json.dumps({
        "petr@acme.com": {"chat_id": 12345, "linked_at": "2026-01-01"},
    }))

    # password_users.json
    (data_dir / "auth" / "password_users.json").write_text(json.dumps({
        "ext@partner.com": {"name": "External User", "password_hash": "$argon2id$hash123"},
    }))

    # table_registry.json
    (data_dir / "src_data" / "metadata" / "table_registry.json").write_text(json.dumps({
        "tables": [
            {"id": "orders", "name": "Orders", "folder": "sales", "sync_strategy": "incremental"},
        ]
    }))

    # profiles.json
    (data_dir / "src_data" / "metadata" / "profiles.json").write_text(json.dumps({
        "tables": {
            "orders": {"row_count": 1000, "columns": [{"name": "id"}]},
        }
    }))

    os.environ["DATA_DIR"] = str(data_dir)
    return str(data_dir)


def test_migration_runs(migration_env):
    from scripts.migrate_json_to_duckdb import migrate_all
    stats = migrate_all(migration_env)
    assert stats["sync_state"] == 2
    assert stats["knowledge"] == 2
    assert stats["telegram"] == 1
    assert stats["users"] == 1
    assert stats["table_registry"] == 1
    assert stats["profiles"] == 1


def test_migration_idempotent(migration_env):
    from scripts.migrate_json_to_duckdb import migrate_all
    stats1 = migrate_all(migration_env)
    stats2 = migrate_all(migration_env)
    # Second run should find existing items and skip them
    assert stats2["knowledge"] == 0  # already existed
    assert stats2["users"] == 0
    assert stats2["table_registry"] == 0
    # sync_state uses UPSERT so count stays same
    assert stats2["sync_state"] == 2


def test_migration_with_missing_files(tmp_path):
    """Migration should handle missing JSON files gracefully."""
    data_dir = tmp_path / "empty_data"
    data_dir.mkdir()
    os.environ["DATA_DIR"] = str(data_dir)
    from scripts.migrate_json_to_duckdb import migrate_all
    stats = migrate_all(str(data_dir))
    assert stats["sync_state"] == 0
    assert stats["knowledge"] == 0
    assert stats["telegram"] == 0
