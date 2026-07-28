"""
One-time migration: JSON files -> DuckDB.

Usage: python -m scripts.migrate_json_to_duckdb [--data-dir /data]

Idempotent -- safe to run multiple times. Uses UPSERT to avoid duplicates.
"""

import json
import logging
import os
from pathlib import Path

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Skipping {path}: {e}")
        return None


def migrate_all(data_dir: str = None) -> dict:
    if data_dir:
        os.environ["DATA_DIR"] = data_dir
    data = Path(data_dir or os.environ.get("DATA_DIR", "./data"))

    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.knowledge import KnowledgeRepository
    from src.repositories.notifications import TelegramRepository
    from src.repositories.users import UserRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.profiles import ProfileRepository

    conn = get_system_db()
    stats = {}

    # 1. Sync state
    sync_data = _load_json(str(data / "src_data" / "metadata" / "sync_state.json"))
    count = 0
    if sync_data and isinstance(sync_data, dict):
        repo = SyncStateRepository(conn)
        tables = sync_data.get("tables", sync_data)
        if not isinstance(tables, dict):
            tables = {}
        for table_id, info in tables.items():
            if isinstance(info, dict):
                repo.update_sync(
                    table_id=table_id,
                    rows=info.get("rows", 0),
                    file_size_bytes=info.get("file_size_bytes", 0),
                    hash=info.get("hash", ""),
                    uncompressed_size_bytes=info.get("uncompressed_size_bytes", 0),
                    columns=info.get("columns", 0),
                )
                count += 1
    stats["sync_state"] = count
    logger.info(f"Migrated {count} sync state entries")

    # 2. Knowledge items
    knowledge = _load_json(str(data / "corporate-memory" / "knowledge.json"))
    count = 0
    if knowledge and isinstance(knowledge, list):
        repo = KnowledgeRepository(conn)
        for item in knowledge:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id", "")
            if not item_id:
                continue
            # Check if exists (idempotent)
            existing = repo.get_by_id(item_id)
            if existing:
                continue
            repo.create(
                id=item_id,
                title=item.get("title", ""),
                content=item.get("content", ""),
                category=item.get("category", ""),
                source_user=item.get("source_user"),
                tags=item.get("tags"),
                status=item.get("status", "pending"),
                confidence=item.get("confidence"),
                domain=item.get("domain"),
                entities=item.get("entities"),
                source_type=item.get("source_type", "claude_local_md"),
                source_ref=item.get("source_ref"),
                sensitivity=item.get("sensitivity", "internal"),
                is_personal=item.get("is_personal", False),
            )
            count += 1
    stats["knowledge"] = count
    logger.info(f"Migrated {count} knowledge items")

    # 3. Telegram users
    telegram = _load_json(str(data / "notifications" / "telegram_users.json"))
    count = 0
    if telegram and isinstance(telegram, dict):
        repo = TelegramRepository(conn)
        for user_id, info in telegram.items():
            if isinstance(info, dict) and "chat_id" in info:
                repo.link_user(user_id, chat_id=info["chat_id"])
                count += 1
    stats["telegram"] = count
    logger.info(f"Migrated {count} telegram links")

    # 4. Password users
    password_users = _load_json(str(data / "auth" / "password_users.json"))
    count = 0
    if password_users and isinstance(password_users, dict):
        user_repo = UserRepository(conn)
        for email, info in password_users.items():
            if not isinstance(info, dict):
                continue
            existing = user_repo.get_by_email(email)
            if existing:
                continue
            import uuid

            user_repo.create(
                id=str(uuid.uuid4()),
                email=email,
                name=info.get("name", email.split("@")[0]),
                password_hash=info.get("password_hash"),
            )
            count += 1
    stats["users"] = count
    logger.info(f"Migrated {count} password users")

    # 5. Table registry
    registry = _load_json(str(data / "src_data" / "metadata" / "table_registry.json"))
    count = 0
    if registry and isinstance(registry, dict):
        repo = TableRegistryRepository(conn)
        tables_list = registry.get("tables", [])
        if isinstance(tables_list, list):
            for t in tables_list:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id", "")
                if not tid:
                    continue
                existing = repo.get(tid)
                if existing:
                    continue
                repo.register(
                    id=tid,
                    name=t.get("name", tid),
                    folder=t.get("folder"),
                    sync_strategy=t.get("sync_strategy"),
                    primary_key=t.get("primary_key"),
                    description=t.get("description"),
                    registered_by=t.get("registered_by"),
                )
                count += 1
    stats["table_registry"] = count
    logger.info(f"Migrated {count} table registry entries")

    # 6. Profiles
    profiles = _load_json(str(data / "src_data" / "metadata" / "profiles.json"))
    count = 0
    if profiles and isinstance(profiles, dict):
        repo = ProfileRepository(conn)
        tables_data = profiles.get("tables", profiles)
        if isinstance(tables_data, dict):
            for table_id, profile in tables_data.items():
                if isinstance(profile, dict):
                    repo.save(table_id, profile)
                    count += 1
    stats["profiles"] = count
    logger.info(f"Migrated {count} table profiles")

    conn.close()
    logger.info(f"Migration complete: {stats}")
    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migrate JSON state to DuckDB")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    migrate_all(args.data_dir)
