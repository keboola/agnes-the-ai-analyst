"""
Knowledge collector for Corporate Memory.

Uses a "full refresh" approach with hash-based change detection:
1. Track MD5 hashes of each user's CLAUDE.local.md in user_hashes.json
2. If no file changed since last run, skip entirely
3. When any file changed, collect ALL users' content + existing catalog
   and send to HAIKU in ONE call for a unified catalog refresh
4. HAIKU preserves existing item IDs (critical for vote stability),
   merges similar knowledge, and tracks source_users per item
"""

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from .prompts import CATALOG_REFRESH_PROMPT, SENSITIVITY_CHECK_PROMPT

# Configuration
CORPORATE_MEMORY_DIR = Path(os.environ.get("CORPORATE_MEMORY_DIR", "/data/corporate-memory"))
KNOWLEDGE_FILE = CORPORATE_MEMORY_DIR / "knowledge.json"
COLLECTION_LOG = CORPORATE_MEMORY_DIR / "collection.log"
USER_HASHES_FILE = CORPORATE_MEMORY_DIR / "user_hashes.json"
HOME_BASE = Path("/home")

# HAIKU model for cost-effective extraction
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# JSON Schema for catalog refresh structured output
CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "existing_id": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "data_analysis", "api_integration", "debugging",
                            "performance", "workflow", "infrastructure",
                            "business_logic",
                        ],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source_users": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "existing_id", "title", "content",
                    "category", "tags", "source_users",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# JSON Schema for sensitivity check structured output
SENSITIVITY_SCHEMA = {
    "type": "object",
    "properties": {
        "safe": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["safe"],
    "additionalProperties": False,
}


def _read_json(path: Path) -> dict:
    """Read a JSON file, return empty structure if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read {path}: {e}")
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write JSON data to file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)  # group-readable for data-ops
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _generate_id(content: str) -> str:
    """Generate a stable ID from content hash."""
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"km_{h}"


def _get_claude_client() -> anthropic.Anthropic:
    """Get Anthropic client with API key from environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")
    return anthropic.Anthropic(api_key=api_key)


def _find_claude_local_files() -> list[tuple[str, Path]]:
    """Find all CLAUDE.local.md files in user home directories.

    Returns list of (username, path) tuples.
    """
    files = []

    if not HOME_BASE.exists():
        logger.warning(f"Home base directory {HOME_BASE} does not exist")
        return files

    for user_dir in HOME_BASE.iterdir():
        if not user_dir.is_dir():
            continue

        claude_local = user_dir / "CLAUDE.local.md"
        if claude_local.exists() and claude_local.is_file():
            username = user_dir.name
            files.append((username, claude_local))
            logger.info(f"Found CLAUDE.local.md for user: {username}")

    return files


def _check_for_changes() -> tuple[bool, dict[str, tuple[str, str]]]:
    """Check if any user's CLAUDE.local.md has changed since last run.

    Reads all CLAUDE.local.md files, computes MD5 hashes, and compares
    with stored hashes in user_hashes.json.

    Returns:
        (has_changes, user_files) where user_files maps
        username -> (content, md5_hash)
    """
    files = _find_claude_local_files()

    if not files:
        logger.info("No CLAUDE.local.md files found")
        return False, {}

    # Read all files and compute hashes
    user_files: dict[str, tuple[str, str]] = {}
    for username, filepath in files:
        try:
            content = filepath.read_text(encoding="utf-8")
            md5_hash = hashlib.md5(content.encode()).hexdigest()
            user_files[username] = (content, md5_hash)
        except Exception as e:
            logger.error(f"Failed to read {filepath}: {e}")
            continue

    if not user_files:
        return False, {}

    # Load stored hashes
    stored_hashes = _read_json(USER_HASHES_FILE)

    # Compare: check if any file changed, was added, or was removed
    current_hashes = {user: h for user, (_, h) in user_files.items()}
    stored = stored_hashes.get("hashes", {})

    if current_hashes == stored:
        logger.info("No changes detected in any CLAUDE.local.md files")
        return False, user_files

    # Log what changed
    for user, h in current_hashes.items():
        if user not in stored:
            logger.info(f"New user file detected: {user}")
        elif stored[user] != h:
            logger.info(f"Changed file detected: {user}")
    for user in stored:
        if user not in current_hashes:
            logger.info(f"Removed user file detected: {user}")

    return True, user_files


def _format_existing_catalog(existing: dict) -> str:
    """Format existing knowledge items for the HAIKU prompt.

    Returns a text block listing each existing item with its ID, title,
    content, category, tags, and source_users.
    """
    items = existing.get("items", {})
    if not items:
        return "(No existing items - this is a fresh catalog)"

    lines = []
    for item_id, item in items.items():
        tags_str = ", ".join(item.get("tags", []))
        users_str = ", ".join(item.get("source_users", []))
        lines.append(
            f"- ID: {item_id}\n"
            f"  Title: {item.get('title', 'Untitled')}\n"
            f"  Content: {item.get('content', '')}\n"
            f"  Category: {item.get('category', 'workflow')}\n"
            f"  Tags: {tags_str}\n"
            f"  Source users: {users_str}"
        )

    return "\n".join(lines)


def _format_user_files(user_files: dict[str, tuple[str, str]]) -> str:
    """Format all user CLAUDE.local.md contents for the HAIKU prompt.

    Returns a text block with each user's content clearly labeled.
    """
    sections = []
    for username, (content, _) in sorted(user_files.items()):
        sections.append(
            f"### User: {username}\n"
            f"```\n{content.strip()}\n```"
        )

    return "\n\n".join(sections)


def _process_catalog_response(
    response_items: list[dict],
    existing: dict,
) -> dict[str, dict]:
    """Map HAIKU's response back to real IDs, preserving existing ones.

    For items with existing_id: keep that ID, update fields.
    For new items (existing_id is null): generate SHA256 ID from title+content.

    Returns dict of items keyed by ID.
    """
    existing_items = existing.get("items", {})
    existing_ids = set(existing_items.keys())
    now = datetime.now(timezone.utc).isoformat()

    result: dict[str, dict] = {}

    for item in response_items:
        existing_id = item.get("existing_id")

        if existing_id and existing_id in existing_ids:
            # Preserve existing item with updated fields
            old_item = existing_items[existing_id]
            result[existing_id] = {
                "id": existing_id,
                "title": item["title"],
                "content": item["content"],
                "category": item["category"],
                "tags": item["tags"],
                "source_users": item["source_users"],
                "extracted_at": old_item.get("extracted_at", now),
                "updated_at": now,
            }
        else:
            # New item - generate ID from title+content
            content_hash = item["title"] + item["content"]
            item_id = _generate_id(content_hash)

            # Handle collision with existing ID (unlikely but safe)
            if item_id in result:
                content_hash += now
                item_id = _generate_id(content_hash)

            result[item_id] = {
                "id": item_id,
                "title": item["title"],
                "content": item["content"],
                "category": item["category"],
                "tags": item["tags"],
                "source_users": item["source_users"],
                "extracted_at": now,
                "updated_at": now,
            }

    return result


def check_sensitivity(client: anthropic.Anthropic, item: dict) -> bool:
    """Check if a knowledge item is safe to share.

    Returns True if safe, False if contains sensitive data.
    """
    prompt = SENSITIVITY_CHECK_PROMPT.format(
        title=item.get("title", ""),
        content=item.get("content", ""),
        tags=", ".join(item.get("tags", [])),
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": SENSITIVITY_SCHEMA,
                },
            },
        )

        result = json.loads(response.content[0].text)

        if not result.get("safe", False):
            reason = result.get("reason", "unknown")
            logger.info(f"Filtered sensitive item: {item.get('title', 'untitled')} - {reason}")
            return False

        return True

    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.warning(f"Sensitivity check failed, assuming unsafe: {e}")
        return False


def collect_all(dry_run: bool = False) -> dict:
    """Main collection routine using full-refresh approach.

    1. Check if any CLAUDE.local.md file changed (MD5 hash comparison)
    2. If no changes, skip entirely
    3. If changes found, send ALL user files + existing catalog to HAIKU
    4. HAIKU produces updated catalog preserving existing IDs
    5. Run sensitivity check on NEW items only
    6. Save updated knowledge.json and user_hashes.json

    Args:
        dry_run: If True, don't write to knowledge.json, just return results.

    Returns:
        Statistics about the collection run.
    """
    stats: dict[str, Any] = {
        "users_scanned": 0,
        "files_found": 0,
        "items_extracted": 0,
        "items_filtered": 0,
        "items_preserved": 0,
        "items_new": 0,
        "skipped": False,
        "errors": [],
    }

    # Step 1: Check for changes
    has_changes, user_files = _check_for_changes()
    stats["files_found"] = len(user_files)
    stats["users_scanned"] = len(user_files)

    if not user_files:
        logger.info("No user files found, skipping collection")
        stats["skipped"] = True
        return stats

    if not has_changes:
        logger.info("No changes detected, skipping collection")
        stats["skipped"] = True
        return stats

    # Step 2: Initialize client
    try:
        client = _get_claude_client()
    except ValueError as e:
        stats["errors"].append(str(e))
        logger.error(str(e))
        return stats

    # Step 3: Load existing catalog
    existing = _read_json(KNOWLEDGE_FILE)
    if not existing:
        existing = {"items": {}, "metadata": {}}

    existing_ids = set(existing.get("items", {}).keys())

    # Step 4: Format prompt inputs
    catalog_text = _format_existing_catalog(existing)
    users_text = _format_user_files(user_files)

    # Step 5: Call HAIKU with full context
    prompt = CATALOG_REFRESH_PROMPT.format(
        existing_catalog=catalog_text,
        user_files=users_text,
    )

    logger.info(
        f"Sending catalog refresh to HAIKU with {len(user_files)} user files "
        f"and {len(existing.get('items', {}))} existing items"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": CATALOG_SCHEMA,
                },
            },
        )

        response_data = json.loads(response.content[0].text)
        response_items = response_data.get("items", [])
        stats["items_extracted"] = len(response_items)
        logger.info(f"HAIKU returned {len(response_items)} catalog items")

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse HAIKU response as JSON: {e}")
        stats["errors"].append(f"JSON parse error: {e}")
        return stats
    except anthropic.APIError as e:
        logger.error(f"HAIKU API error: {e}")
        stats["errors"].append(f"API error: {e}")
        return stats

    # Step 6: Process response - map to existing IDs
    processed_items = _process_catalog_response(response_items, existing)

    # Step 7: Run sensitivity check on NEW items only
    # Items with IDs that existed before already passed the check
    final_items: dict[str, dict] = {}

    for item_id, item in processed_items.items():
        if item_id in existing_ids:
            # Existing item - already passed sensitivity check before
            final_items[item_id] = item
            stats["items_preserved"] += 1
        else:
            # New item - run sensitivity check
            if check_sensitivity(client, item):
                final_items[item_id] = item
                stats["items_new"] += 1
                logger.info(f"Added new knowledge item: {item['title']}")
            else:
                stats["items_filtered"] += 1

    # Step 8: Build updated knowledge.json
    updated = {
        "items": final_items,
        "metadata": {
            "last_collection": datetime.now(timezone.utc).isoformat(),
            "total_users": stats["users_scanned"],
        },
    }

    # Step 9: Save unless dry run
    if not dry_run:
        _write_json(KNOWLEDGE_FILE, updated)
        logger.info(
            f"Knowledge base updated: {stats['items_preserved']} preserved, "
            f"{stats['items_new']} new, {stats['items_filtered']} filtered"
        )

        # Save user hashes after successful processing
        current_hashes = {user: h for user, (_, h) in user_files.items()}
        _write_json(USER_HASHES_FILE, {
            "hashes": current_hashes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("User hashes updated")
    else:
        logger.info(
            f"Dry run - would preserve {stats['items_preserved']}, "
            f"add {stats['items_new']}, filter {stats['items_filtered']}"
        )

    return stats


def reset_knowledge() -> None:
    """Clear all knowledge data for a fresh recalculation.

    Removes knowledge.json, user_hashes.json, and votes.json so the next
    collection run starts completely fresh. Does NOT remove collection.log.
    """
    for path in [KNOWLEDGE_FILE, USER_HASHES_FILE]:
        if path.exists():
            path.unlink()
            logger.info(f"Removed {path}")

    # Clear votes too - item IDs will change after reset
    votes_file = CORPORATE_MEMORY_DIR / "votes.json"
    if votes_file.exists():
        votes_file.unlink()
        logger.info(f"Removed {votes_file}")

    # Clean up all user .claude_rules directories (stale rules for old IDs)
    for user_dir in HOME_BASE.iterdir():
        if not user_dir.is_dir():
            continue
        rules_dir = user_dir / ".claude_rules"
        if rules_dir.exists():
            for rule_file in rules_dir.glob("km_*.md"):
                rule_file.unlink()
                logger.info(f"Removed stale rule: {rule_file}")


def main() -> int:
    """CLI entry point for the collector."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Collect knowledge from CLAUDE.local.md files"
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear all data and recalculate from scratch (also clears votes)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Configure file logging
    CORPORATE_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(COLLECTION_LOG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    if args.reset:
        print("Resetting Corporate Memory (clearing all data)...")
        reset_knowledge()
        print("Data cleared. Running fresh collection...\n")

    logger.info("Starting knowledge collection...")
    stats = collect_all(dry_run=args.dry_run)

    print("\nCollection complete:")
    if stats["skipped"]:
        print("  Status: SKIPPED (no changes detected)")
    print(f"  Users scanned: {stats['users_scanned']}")
    print(f"  Files found: {stats['files_found']}")
    print(f"  Items extracted: {stats['items_extracted']}")
    print(f"  Items preserved: {stats['items_preserved']}")
    print(f"  Items new: {stats['items_new']}")
    print(f"  Items filtered (sensitive): {stats['items_filtered']}")

    if stats["errors"]:
        print(f"\nErrors ({len(stats['errors'])}):")
        for error in stats["errors"]:
            print(f"  - {error}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
