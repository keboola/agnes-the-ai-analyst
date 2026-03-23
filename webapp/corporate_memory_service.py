"""
Corporate Memory service for the webapp.

Manages knowledge items, voting, user rules generation, and governance.
Follows patterns from telegram_service.py for JSON I/O.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from config.loader import load_instance_config, get_instance_value

logger = logging.getLogger(__name__)

CORPORATE_MEMORY_DIR = Path(os.environ.get("CORPORATE_MEMORY_DIR", "/data/corporate-memory"))
KNOWLEDGE_FILE = CORPORATE_MEMORY_DIR / "knowledge.json"
VOTES_FILE = CORPORATE_MEMORY_DIR / "votes.json"
AUDIT_FILE = CORPORATE_MEMORY_DIR / "audit.jsonl"

VALID_STATUSES = frozenset({
    "pending", "approved", "mandatory", "rejected", "revoked", "expired",
})

VALID_TRANSITIONS = {
    "pending":   {"approved", "mandatory", "rejected"},
    "approved":  {"mandatory", "rejected"},
    "mandatory": {"approved", "revoked"},
    "rejected":  {"approved"},
    "revoked":   {"approved", "mandatory"},
    "expired":   {"approved", "mandatory", "rejected"},
}


def _load_user_mappings():
    """Load user display names and username mappings from instance config."""
    try:
        config = load_instance_config()
        users = get_instance_value(config, "users", default={})
        mapping = get_instance_value(config, "username_mapping", default={})
        return users or {}, mapping or {}
    except Exception:
        return {}, {}


_USER_CONFIG = _load_user_mappings()
USER_DISPLAY_NAMES = _USER_CONFIG[0]
WEBAPP_TO_SERVER_USERNAME = _USER_CONFIG[1]

# Module-level caches for governance config and groups
_governance_config_cache: dict | None = None
_groups_cache: dict | None = None


def _load_governance_config() -> dict:
    """Load corporate_memory section from instance config, cached at module level.

    Returns empty dict if not configured (legacy mode).
    """
    global _governance_config_cache
    if _governance_config_cache is not None:
        return _governance_config_cache

    try:
        config = load_instance_config()
        _governance_config_cache = get_instance_value(
            config, "corporate_memory", default={},
        ) or {}
    except Exception:
        _governance_config_cache = {}

    return _governance_config_cache


def _load_groups() -> dict:
    """Load groups section from instance config, cached at module level.

    Returns empty dict if not present.
    """
    global _groups_cache
    if _groups_cache is not None:
        return _groups_cache

    try:
        config = load_instance_config()
        _groups_cache = get_instance_value(config, "groups", default={}) or {}
    except Exception:
        _groups_cache = {}

    return _groups_cache


def get_governance_mode() -> str | None:
    """Return the governance distribution mode, or None if legacy (no config)."""
    gov = _load_governance_config()
    if not gov:
        return None
    return gov.get("distribution_mode", "hybrid")


def get_approval_mode() -> str | None:
    """Return the approval mode, or None if legacy (no config)."""
    gov = _load_governance_config()
    if not gov:
        return None
    return gov.get("approval_mode", "review_queue")


def is_km_admin(email: str) -> bool:
    """Check if the given email has km_admin privileges.

    Looks up the email in the users dict from instance.yaml.
    Returns False if no governance config or user not found.
    """
    try:
        config = load_instance_config()
        users = get_instance_value(config, "users", default={}) or {}
        user = users.get(email)
        if not user or not isinstance(user, dict):
            return False
        return bool(user.get("km_admin", False))
    except Exception:
        return False


def _write_audit_log(admin: str, action: str, item_id: str, details: dict) -> None:
    """Append one JSON line to the audit log file.

    Creates parent directory if needed. Uses append mode.
    """
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "admin": admin,
        "action": action,
        "item_id": item_id,
        "details": details,
    }

    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")


def _validate_transition(current_status: str, new_status: str) -> bool:
    """Check if a status transition is valid according to VALID_TRANSITIONS."""
    allowed = VALID_TRANSITIONS.get(current_status, set())
    return new_status in allowed


def _check_audience(item: dict, email: str) -> bool:
    """Check if a user is in the target audience for an item.

    audience of "all" or None means everyone.
    audience of "group:name" checks group membership.
    """
    audience = item.get("audience")
    if audience is None or audience == "all":
        return True

    if audience.startswith("group:"):
        group_name = audience[len("group:"):]
        groups = _load_groups()
        group = groups.get(group_name)
        if not group or not isinstance(group, dict):
            return False
        members = group.get("members", [])
        return email in members

    return False


def _default_review_by() -> str:
    """Return ISO8601 timestamp for now + review_period_months from config."""
    gov = _load_governance_config()
    months = gov.get("review_period_months", 6)
    review_date = datetime.now(timezone.utc) + timedelta(days=months * 30)
    return review_date.isoformat()


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
    include_statuses: set[str] | None = None,
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
        include_statuses: If governance active, filter to these statuses.
            None = default to approved+mandatory. Ignored in legacy mode.

    Returns:
        Dict with items list, total count, and pagination info.
    """
    data = _read_json(KNOWLEDGE_FILE)
    items_dict = data.get("items", {})
    votes_data = _read_json(VOTES_FILE)

    governance_mode = get_governance_mode()

    # Determine which statuses to include
    if governance_mode is not None:
        if include_statuses is not None:
            allowed_statuses = include_statuses
        else:
            allowed_statuses = {"approved", "mandatory"}
    else:
        allowed_statuses = None  # Legacy: no filtering

    # Convert to list and calculate scores
    items = []
    for item_id, item in items_dict.items():
        # Status filtering for governance mode
        if allowed_statuses is not None:
            item_status = item.get("status", "approved")
            if item_status not in allowed_statuses:
                continue

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

        # Add governance fields
        item_copy["is_mandatory"] = item.get("status") == "mandatory"
        item_copy["mandatory_reason"] = item.get("mandatory_reason")

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
        If governance is active, also includes status counts.
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

    result = {
        "knowledge_count": len(items),
        "contributors": len(contributors),
        "categories": categories,
        "last_collection": metadata.get("last_collection"),
    }

    # Add governance status counts if active
    if get_governance_mode() is not None:
        pending_count = 0
        approved_count = 0
        mandatory_count = 0
        for item in items.values():
            status = item.get("status", "approved")
            if status == "pending":
                pending_count += 1
            elif status == "approved":
                approved_count += 1
            elif status == "mandatory":
                mandatory_count += 1
        result["pending_count"] = pending_count
        result["approved_count"] = approved_count
        result["mandatory_count"] = mandatory_count

    return result


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
    # Check governance mode restrictions
    governance_mode = get_governance_mode()
    if governance_mode == "mandatory_only":
        return False, "Voting is disabled in this governance mode"

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

    In legacy mode (no governance): returns all items the user has upvoted.
    In governance mode:
        - "hybrid": mandatory items (audience-checked) + user-upvoted approved items
        - "mandatory_only" / "admin_curated": only mandatory items (audience-checked)

    Args:
        username: The username.

    Returns:
        List of knowledge items to sync (deduplicated).
    """
    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    governance_mode = get_governance_mode()

    if governance_mode is None:
        # Legacy mode: upvoted items only (original behavior)
        votes_data = _read_json(VOTES_FILE)
        user_votes = votes_data.get(username, {})

        rules = []
        for item_id, vote_val in user_votes.items():
            if vote_val > 0 and item_id in items:
                rules.append(items[item_id])
        return rules

    # Governance mode: collect mandatory + optionally upvoted items
    seen_ids: set[str] = set()
    rules: list[dict] = []

    # Always include mandatory items that pass audience check
    for item_id, item in items.items():
        if item.get("status") == "mandatory" and _check_audience(item, username):
            rules.append(item)
            seen_ids.add(item_id)

    # In hybrid mode, also include user-upvoted approved items
    if governance_mode == "hybrid":
        votes_data = _read_json(VOTES_FILE)
        user_votes = votes_data.get(username, {})

        for item_id, vote_val in user_votes.items():
            if vote_val > 0 and item_id in items and item_id not in seen_ids:
                item = items[item_id]
                if item.get("status") == "approved":
                    rules.append(item)
                    seen_ids.add(item_id)

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


# ---------------------------------------------------------------------------
# Governance: admin action functions
# ---------------------------------------------------------------------------


def _regenerate_rules_for_audience(audience: str) -> None:
    """Regenerate rules for all users affected by an audience change.

    If audience is "all", regenerate for all users who have voted.
    If audience is "group:name", regenerate for group members only.
    """
    if audience == "all" or audience is None:
        regenerate_all_user_rules()
        return

    if audience.startswith("group:"):
        group_name = audience[len("group:"):]
        groups = _load_groups()
        group = groups.get(group_name)
        if group and isinstance(group, dict):
            for member_email in group.get("members", []):
                _regenerate_user_rules(member_email)


def approve_item(admin_email: str, item_id: str) -> tuple[bool, str]:
    """Approve a knowledge item.

    Args:
        admin_email: Email of the admin performing the action.
        item_id: The knowledge item ID to approve.

    Returns:
        Tuple of (success, error_or_success_message).
    """
    if not is_km_admin(admin_email):
        return False, "Permission denied: user is not a km_admin"

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    if item_id not in items:
        return False, f"Knowledge item {item_id} not found"

    item = items[item_id]
    current_status = item.get("status", "pending")

    if not _validate_transition(current_status, "approved"):
        return False, f"Cannot transition from '{current_status}' to 'approved'"

    now = datetime.now(timezone.utc).isoformat()
    item["status"] = "approved"
    item["approved_by"] = admin_email
    item["approved_at"] = now
    item["review_by"] = _default_review_by()
    item["updated_at"] = now

    _write_json(KNOWLEDGE_FILE, knowledge_data)
    _write_audit_log(admin_email, "approved", item_id, {
        "previous_status": current_status,
    })

    logger.info(f"Item {item_id} approved by {admin_email}")
    return True, "Item approved"


def reject_item(
    admin_email: str,
    item_id: str,
    reason: str | None = None,
) -> tuple[bool, str]:
    """Reject a knowledge item.

    Args:
        admin_email: Email of the admin performing the action.
        item_id: The knowledge item ID to reject.
        reason: Optional rejection reason.

    Returns:
        Tuple of (success, error_or_success_message).
    """
    if not is_km_admin(admin_email):
        return False, "Permission denied: user is not a km_admin"

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    if item_id not in items:
        return False, f"Knowledge item {item_id} not found"

    item = items[item_id]
    current_status = item.get("status", "pending")

    if not _validate_transition(current_status, "rejected"):
        return False, f"Cannot transition from '{current_status}' to 'rejected'"

    now = datetime.now(timezone.utc).isoformat()
    item["status"] = "rejected"
    item["rejected_by"] = admin_email
    item["rejected_at"] = now
    if reason:
        item["rejection_reason"] = reason
    item["updated_at"] = now

    _write_json(KNOWLEDGE_FILE, knowledge_data)
    _write_audit_log(admin_email, "rejected", item_id, {
        "previous_status": current_status,
        "reason": reason,
    })

    logger.info(f"Item {item_id} rejected by {admin_email}")
    return True, "Item rejected"


def mandate_item(
    admin_email: str,
    item_id: str,
    mandatory_reason: str,
    audience: str = "all",
) -> tuple[bool, str]:
    """Mark a knowledge item as mandatory for a target audience.

    Args:
        admin_email: Email of the admin performing the action.
        item_id: The knowledge item ID to mandate.
        mandatory_reason: Required reason for mandating (must be non-empty).
        audience: Target audience — "all" or "group:name".

    Returns:
        Tuple of (success, error_or_success_message).
    """
    if not is_km_admin(admin_email):
        return False, "Permission denied: user is not a km_admin"

    if not mandatory_reason or not mandatory_reason.strip():
        return False, "mandatory_reason is required and must be non-empty"

    # Validate audience format
    if audience != "all" and not audience.startswith("group:"):
        return False, f"Invalid audience format: '{audience}'. Use 'all' or 'group:<name>'"

    # Validate group exists if specified
    if audience.startswith("group:"):
        group_name = audience[len("group:"):]
        groups = _load_groups()
        if group_name not in groups:
            return False, f"Group '{group_name}' not found in config"

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    if item_id not in items:
        return False, f"Knowledge item {item_id} not found"

    item = items[item_id]
    current_status = item.get("status", "pending")

    if not _validate_transition(current_status, "mandatory"):
        return False, f"Cannot transition from '{current_status}' to 'mandatory'"

    now = datetime.now(timezone.utc).isoformat()
    item["status"] = "mandatory"
    item["mandatory_reason"] = mandatory_reason.strip()
    item["audience"] = audience
    item["approved_by"] = admin_email
    item["approved_at"] = now
    item["review_by"] = _default_review_by()
    item["updated_at"] = now

    _write_json(KNOWLEDGE_FILE, knowledge_data)
    _write_audit_log(admin_email, "mandated", item_id, {
        "previous_status": current_status,
        "mandatory_reason": mandatory_reason.strip(),
        "audience": audience,
    })

    # Regenerate rules for affected users
    _regenerate_rules_for_audience(audience)

    logger.info(f"Item {item_id} mandated by {admin_email} for audience={audience}")
    return True, "Item mandated"


def revoke_item(
    admin_email: str,
    item_id: str,
    reason: str | None = None,
) -> tuple[bool, str]:
    """Revoke a mandatory knowledge item.

    Only valid from "mandatory" status. Sets status to "revoked" and
    triggers rule regeneration for all users to remove the revoked item.

    Args:
        admin_email: Email of the admin performing the action.
        item_id: The knowledge item ID to revoke.
        reason: Optional revocation reason.

    Returns:
        Tuple of (success, error_or_success_message).
    """
    if not is_km_admin(admin_email):
        return False, "Permission denied: user is not a km_admin"

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    if item_id not in items:
        return False, f"Knowledge item {item_id} not found"

    item = items[item_id]
    current_status = item.get("status", "pending")

    if not _validate_transition(current_status, "revoked"):
        return False, f"Cannot transition from '{current_status}' to 'revoked'"

    now = datetime.now(timezone.utc).isoformat()
    item["status"] = "revoked"
    item["revoked_by"] = admin_email
    item["revoked_at"] = now
    if reason:
        item["revocation_reason"] = reason
    item["updated_at"] = now

    _write_json(KNOWLEDGE_FILE, knowledge_data)
    _write_audit_log(admin_email, "revoked", item_id, {
        "previous_status": current_status,
        "reason": reason,
    })

    # Regenerate rules for ALL users to remove revoked item
    regenerate_all_user_rules()

    logger.info(f"Item {item_id} revoked by {admin_email}")
    return True, "Item revoked"


def edit_item(
    admin_email: str,
    item_id: str,
    title: str | None = None,
    content: str | None = None,
) -> tuple[bool, str]:
    """Edit a knowledge item's title and/or content.

    At least one of title or content must be provided.

    Args:
        admin_email: Email of the admin performing the edit.
        item_id: The knowledge item ID to edit.
        title: New title (or None to keep existing).
        content: New content (or None to keep existing).

    Returns:
        Tuple of (success, error_or_success_message).
    """
    if not is_km_admin(admin_email):
        return False, "Permission denied: user is not a km_admin"

    if title is None and content is None:
        return False, "At least one of title or content must be provided"

    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})

    if item_id not in items:
        return False, f"Knowledge item {item_id} not found"

    item = items[item_id]
    now = datetime.now(timezone.utc).isoformat()

    audit_details: dict[str, Any] = {}

    if title is not None:
        audit_details["old_title"] = item.get("title")
        audit_details["new_title"] = title
        item["title"] = title

    if content is not None:
        audit_details["old_content"] = item.get("content")
        audit_details["new_content"] = content
        item["content"] = content

    item["edited_by"] = admin_email
    item["edited_at"] = now
    item["updated_at"] = now

    _write_json(KNOWLEDGE_FILE, knowledge_data)
    _write_audit_log(admin_email, "edited", item_id, audit_details)

    logger.info(f"Item {item_id} edited by {admin_email}")
    return True, "Item edited"


def batch_action(
    admin_email: str,
    item_ids: list[str],
    action: str,
    **kwargs: Any,
) -> dict:
    """Perform a governance action on multiple items.

    Not atomic — partial success is OK.

    Args:
        admin_email: Email of the admin performing the action.
        item_ids: List of knowledge item IDs.
        action: One of "approve", "reject", "mandate".
        **kwargs: Additional arguments passed to the action function.
            For "mandate": mandatory_reason (required), audience (default "all").

    Returns:
        Dict with "success" (list of IDs) and "failed" (list of {id, error}).
    """
    valid_actions = {"approve", "reject", "mandate"}
    if action not in valid_actions:
        return {
            "success": [],
            "failed": [{"id": "N/A", "error": f"Invalid action: '{action}'. Must be one of {valid_actions}"}],
        }

    action_map = {
        "approve": lambda item_id: approve_item(admin_email, item_id),
        "reject": lambda item_id: reject_item(
            admin_email, item_id, reason=kwargs.get("reason"),
        ),
        "mandate": lambda item_id: mandate_item(
            admin_email,
            item_id,
            mandatory_reason=kwargs.get("mandatory_reason", ""),
            audience=kwargs.get("audience", "all"),
        ),
    }

    action_fn = action_map[action]
    success: list[str] = []
    failed: list[dict] = []

    for item_id in item_ids:
        ok, message = action_fn(item_id)
        if ok:
            success.append(item_id)
        else:
            failed.append({"id": item_id, "error": message})

    return {"success": success, "failed": failed}


def get_pending_queue(
    category: str | None = None,
    page: int = 0,
    per_page: int = 20,
) -> dict:
    """Get pending knowledge items awaiting admin review.

    Args:
        category: Optional category filter.
        page: Page number (0-indexed).
        per_page: Items per page.

    Returns:
        Dict with items list, total count, and pagination info.
    """
    return get_knowledge(
        category=category,
        page=page,
        per_page=per_page,
        include_statuses={"pending"},
    )


def get_audit_log(
    page: int = 0,
    per_page: int = 50,
    admin: str | None = None,
    action: str | None = None,
) -> dict:
    """Read and paginate the audit log.

    Args:
        page: Page number (0-indexed).
        per_page: Entries per page.
        admin: Filter by admin email.
        action: Filter by action type.

    Returns:
        Dict with entries, total count, and pagination info.
    """
    entries: list[dict] = []

    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass

    # Apply filters
    if admin:
        entries = [e for e in entries if e.get("admin") == admin]
    if action:
        entries = [e for e in entries if e.get("action") == action]

    # Sort newest first
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    # Paginate
    total = len(entries)
    start = page * per_page
    end = start + per_page
    page_entries = entries[start:end]

    return {
        "entries": page_entries,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def migrate_existing_items() -> int:
    """Migrate existing knowledge items without a status field.

    Sets status="approved" with migration metadata for items that lack
    a status field. Idempotent — items that already have a status are skipped.

    Returns:
        Number of items migrated.
    """
    knowledge_data = _read_json(KNOWLEDGE_FILE)
    items = knowledge_data.get("items", {})
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for item_id, item in items.items():
        if "status" not in item:
            item["status"] = "approved"
            item["approved_by"] = "migration"
            item["approved_at"] = now
            item["review_by"] = _default_review_by()

            _write_audit_log("migration", "migration_auto_approved", item_id, {
                "reason": "Pre-governance item auto-approved during migration",
            })
            count += 1

    if count > 0:
        _write_json(KNOWLEDGE_FILE, knowledge_data)
        logger.info(f"Migrated {count} items to approved status")

    return count
