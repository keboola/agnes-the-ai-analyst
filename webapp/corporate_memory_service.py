"""
Corporate Memory service for the webapp.

Manages knowledge items, voting, and user rules generation.
Follows patterns from telegram_service.py for JSON I/O.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CORPORATE_MEMORY_DIR = Path(os.environ.get("CORPORATE_MEMORY_DIR", "/data/corporate-memory"))
KNOWLEDGE_FILE = CORPORATE_MEMORY_DIR / "knowledge.json"
VOTES_FILE = CORPORATE_MEMORY_DIR / "votes.json"

def _load_user_mappings():
    """Load user display names and username mappings from instance config."""
    try:
        from config.loader import load_instance_config, get_instance_value
        config = load_instance_config()
        users = get_instance_value(config, "users", default={})
        mapping = get_instance_value(config, "username_mapping", default={})
        return users or {}, mapping or {}
    except Exception:
        return {}, {}


_USER_CONFIG = _load_user_mappings()
USER_DISPLAY_NAMES = _USER_CONFIG[0]
WEBAPP_TO_SERVER_USERNAME = _USER_CONFIG[1]


def get_user_display(username: str) -> dict:
    """Get display info for a username.
    Returns dict with 'name' and 'initials' keys.
    Falls back to generating initials from username."""
    if username in USER_DISPLAY_NAMES:
        return USER_DISPLAY_NAMES[username]
    # Fallback: for "first.last" format, use first letter of each part
    parts = username.replace(".", " ").replace("_", " ").split()
    if len(parts) >= 2:
        initials = (parts[0][0] + parts[-1][0]).upper()
    else:
        initials = username[:2].upper()
    name = " ".join(p.capitalize() for p in parts)
    return {"name": name, "initials": initials}


def _get_server_username(webapp_username: str) -> str:
    """Map webapp username (email-derived) to server home directory name.
    Most users match, only needed when they differ."""
    return WEBAPP_TO_SERVER_USERNAME.get(webapp_username, webapp_username)


def _read_json(path: Path) -> dict:
    """Read a JSON file, return empty dict if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
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


def get_knowledge(
    category: str | None = None,
    search: str | None = None,
    page: int = 0,
    per_page: int = 20,
    sort: str = "score",
    username: str | None = None,
    my_rules: bool = False,
) -> dict[str, Any]:
    """Get knowledge items with optional filtering and pagination.

    Args:
        category: Filter by category (data_analysis, debugging, etc.)
        search: Search in title and content
        page: Page number (0-indexed)
        per_page: Items per page
        sort: Sort field (score, updated_at, contributors)
        username: Current user's username (for my_rules filter)
        my_rules: If True, only show items user has upvoted

    Returns:
        Dict with items list, total count, and pagination info.
    """
    data = _read_json(KNOWLEDGE_FILE)
    items_dict = data.get("items", {})
    votes_data = _read_json(VOTES_FILE)

    # Convert to list and calculate scores
    items = []
    for item_id, item in items_dict.items():
        # Calculate upvotes and downvotes separately
        upvotes = 0
        downvotes = 0
        for user_votes in votes_data.values():
            v = user_votes.get(item_id, 0)
            if v > 0:
                upvotes += 1
            elif v < 0:
                downvotes += 1

        score = upvotes - downvotes

        item_copy = dict(item)
        item_copy["score"] = score
        item_copy["upvotes"] = upvotes
        item_copy["downvotes"] = downvotes
        # Synced = this user personally upvoted the item
        item_copy["synced"] = (
            username is not None
            and votes_data.get(username, {}).get(item_id, 0) > 0
        )
        # Add display info for source users (proper name initials)
        item_copy["source_users_display"] = [
            {"username": u, **get_user_display(u)}
            for u in item.get("source_users", [])
        ]
        items.append(item_copy)

    # Apply filters
    if category:
        items = [i for i in items if i.get("category") == category]

    if search:
        search_lower = search.lower()
        items = [
            i for i in items
            if search_lower in i.get("title", "").lower()
            or search_lower in i.get("content", "").lower()
            or any(search_lower in tag.lower() for tag in i.get("tags", []))
        ]

    # Filter for user's upvoted items only
    if my_rules and username:
        user_votes = votes_data.get(username, {})
        items = [i for i in items if user_votes.get(i.get("id"), 0) > 0]

    # Sort by selected field
    if sort == "updated_at":
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    elif sort == "contributors":
        items.sort(key=lambda x: len(x.get("source_users", [])), reverse=True)
    else:  # default: score
        items.sort(key=lambda x: (x.get("score", 0), x.get("updated_at", "")), reverse=True)

    # Paginate
    total = len(items)
    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
    }


def get_stats() -> dict[str, Any]:
    """Get statistics for the dashboard widget.

    Returns:
        Dict with contributor count, knowledge count, etc.
    """
    data = _read_json(KNOWLEDGE_FILE)
    items = data.get("items", {})
    metadata = data.get("metadata", {})

    # Count unique contributors
    contributors = set()
    for item in items.values():
        contributors.update(item.get("source_users", []))

    # Count categories
    categories = {}
    for item in items.values():
        cat = item.get("category", "general")
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "knowledge_count": len(items),
        "contributors": len(contributors),
        "categories": categories,
        "last_collection": metadata.get("last_collection"),
    }


def get_user_stats(username: str) -> dict[str, Any]:
    """Get user-specific statistics.

    Args:
        username: The username to get stats for.

    Returns:
        Dict with user's vote count and rules count.
    """
    votes_data = _read_json(VOTES_FILE)
    user_votes = votes_data.get(username, {})

    # Count items the user has upvoted (personal choice, no threshold)
    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    rules_count = sum(
        1 for item_id, v in user_votes.items()
        if v > 0 and item_id in items
    )

    return {
        "your_votes": len(user_votes),
        "your_upvotes": sum(1 for v in user_votes.values() if v > 0),
        "your_rules": rules_count,
    }


def vote(username: str, item_id: str, vote_value: int) -> tuple[bool, str]:
    """Record a vote for a knowledge item.

    Args:
        username: The username voting.
        item_id: The knowledge item ID.
        vote_value: 1 for upvote, -1 for downvote, 0 to remove vote.

    Returns:
        Tuple of (success, message).
    """
    # Validate vote value
    if vote_value not in (-1, 0, 1):
        return False, "Invalid vote value. Use -1, 0, or 1."

    # Check item exists
    knowledge_data = _read_json(KNOWLEDGE_FILE)
    if item_id not in knowledge_data.get("items", {}):
        return False, f"Knowledge item {item_id} not found."

    # Update votes
    votes_data = _read_json(VOTES_FILE)

    if username not in votes_data:
        votes_data[username] = {}

    if vote_value == 0:
        # Remove vote
        votes_data[username].pop(item_id, None)
    else:
        votes_data[username][item_id] = vote_value

    _write_json(VOTES_FILE, votes_data)

    # Regenerate user rules after vote change
    _regenerate_user_rules(username)

    logger.info(f"User {username} voted {vote_value} on {item_id}")
    return True, "Vote recorded."


def get_user_votes(username: str) -> dict[str, int]:
    """Get all votes for a user.

    Args:
        username: The username.

    Returns:
        Dict mapping item_id to vote value.
    """
    votes_data = _read_json(VOTES_FILE)
    return votes_data.get(username, {})


def get_user_rules(username: str) -> list[dict]:
    """Get knowledge items that should be synced to user's rules.

    Returns all items the user has upvoted (personal choice, no threshold).

    Args:
        username: The username.

    Returns:
        List of knowledge items to sync.
    """
    votes_data = _read_json(VOTES_FILE)
    user_votes = votes_data.get(username, {})

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    rules = []
    for item_id, vote_val in user_votes.items():
        if vote_val > 0 and item_id in items:
            rules.append(items[item_id])

    return rules


def _regenerate_user_rules(username: str) -> None:
    """Regenerate .md rule files in a user's home directory.

    Writes rule files to /home/{server_username}/.claude_rules/ using
    sudo install-user-rules helper (same pattern as sync_settings_service).
    The helper creates the directory, removes old km_*.md files, and installs
    new ones with correct user ownership.

    Args:
        username: The webapp username (email-derived).
    """
    rules = get_user_rules(username)
    server_username = _get_server_username(username)

    # Write rules to a temp directory, then install via sudo helper
    tmp_dir = tempfile.mkdtemp(prefix="claude_rules_")
    try:
        for item in rules:
            item_id = item.get("id", "unknown")
            filename = f"{item_id}.md"
            filepath = os.path.join(tmp_dir, filename)
            content = _generate_rule_content(item)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

        # Install to user home via sudo (like sync_settings_service pattern)
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/local/bin/install-user-rules",
             server_username, tmp_dir],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error(
                f"Failed to install rules for {server_username}: {result.stderr}"
            )
        else:
            logger.info(f"Installed rules for {server_username}: {result.stdout.strip()}")

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing rules for {server_username}")
    except Exception as e:
        logger.error(f"Error installing rules for {server_username}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _generate_rule_content(item: dict) -> str:
    """Generate markdown content for a rule file.

    Args:
        item: Knowledge item dict.

    Returns:
        Markdown content string.
    """
    title = item.get("title", "Untitled")
    content = item.get("content", "")
    category = item.get("category", "general")
    tags = item.get("tags", [])

    lines = [
        f"# {title}",
        "",
        f"**Category:** {category}",
    ]

    if tags:
        lines.append(f"**Tags:** {', '.join(tags)}")

    lines.extend([
        "",
        "---",
        "",
        content,
        "",
        "---",
        "",
        f"*Source: Corporate Memory (ID: {item.get('id', 'unknown')})*",
    ])

    return "\n".join(lines)


def regenerate_all_user_rules() -> dict[str, int]:
    """Regenerate rule files for all users who have voted.

    Returns:
        Dict mapping username to number of rules generated.
    """
    votes_data = _read_json(VOTES_FILE)
    results = {}

    for username in votes_data:
        _regenerate_user_rules(username)
        rules = get_user_rules(username)
        results[username] = len(rules)

    return results
