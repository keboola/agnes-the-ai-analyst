"""
Desktop app authentication module.

Handles JWT-based authentication for the native desktop application.
Users authorize via Google SSO in the webapp, then receive a JWT token
that the desktop app uses for API access.

Link state is persisted in /data/notifications/desktop_users.json
so the dashboard can show linked/unlinked status and allow unlinking.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import jwt
from flask import Blueprint, abort, jsonify, render_template, request, session

from .auth import login_required
from .config import Config
from .user_service import get_webapp_username

logger = logging.getLogger(__name__)

desktop_bp = Blueprint("desktop", __name__)

NOTIFICATIONS_DIR = "/data/notifications"
DESKTOP_USERS_FILE = os.path.join(NOTIFICATIONS_DIR, "desktop_users.json")


def _read_json(path: str) -> dict:
    """Read a JSON file, return empty dict if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    """Write JSON data to file atomically."""
    import tempfile

    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def get_desktop_status(username: str) -> dict:
    """Get desktop app link status for a user."""
    users = _read_json(DESKTOP_USERS_FILE)
    entry = users.get(username)
    if entry:
        return {
            "linked": True,
            "linked_at": entry.get("linked_at", ""),
        }
    return {"linked": False}


def _mark_desktop_linked(username: str) -> None:
    """Record that the desktop app has been linked for this user."""
    users = _read_json(DESKTOP_USERS_FILE)
    users[username] = {
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(DESKTOP_USERS_FILE, users)


def unlink_desktop(username: str) -> tuple[bool, str]:
    """Unlink the desktop app from a user account."""
    users = _read_json(DESKTOP_USERS_FILE)
    if username not in users:
        return False, "Desktop app is not linked."

    del users[username]
    _write_json(DESKTOP_USERS_FILE, users)

    logger.info(f"Unlinked desktop app for user '{username}'")
    return True, "Desktop app unlinked."


def _create_desktop_token(username: str) -> str:
    """Create a JWT token for desktop app authentication."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "exp": now + timedelta(days=Config.DESKTOP_JWT_EXPIRY_DAYS),
        "iat": now,
        "iss": Config.DESKTOP_JWT_ISSUER,
    }
    return jwt.encode(payload, Config.DESKTOP_JWT_SECRET, algorithm="HS256")


def _decode_desktop_token(token: str, *, allow_expired_seconds: int = 0) -> dict | None:
    """Decode and validate a desktop JWT token.

    Args:
        token: The JWT token string.
        allow_expired_seconds: If > 0, accept tokens expired within this grace period.

    Returns:
        Decoded payload dict or None if invalid.
    """
    try:
        return jwt.decode(
            token,
            Config.DESKTOP_JWT_SECRET,
            algorithms=["HS256"],
            issuer=Config.DESKTOP_JWT_ISSUER,
            leeway=timedelta(seconds=allow_expired_seconds),
        )
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid desktop token: {e}")
        return None


@desktop_bp.route("/desktop/link")
@login_required
def desktop_link():
    """Render the desktop app authorization page."""
    user = session.get("user", {})
    email = user.get("email", "")
    username = get_webapp_username(email)
    return render_template("desktop_link.html", username=username)


@desktop_bp.route("/api/desktop/authorize", methods=["POST"])
@login_required
def desktop_authorize():
    """Generate a JWT token for the desktop app and return a redirect URL."""
    user = session.get("user", {})
    email = user.get("email", "")
    username = get_webapp_username(email)

    token = _create_desktop_token(username)
    redirect_url = f"{Config.DESKTOP_URL_SCHEME}://auth?token={token}"

    _mark_desktop_linked(username)
    logger.info(f"Desktop token issued for {username}")
    return jsonify({"url": redirect_url})


@desktop_bp.route("/api/desktop/refresh", methods=["POST"])
def desktop_refresh():
    """Refresh an existing desktop JWT token.

    Accepts tokens expired within a 7-day grace period.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    old_token = auth_header[len("Bearer "):]
    grace_seconds = Config.DESKTOP_JWT_REFRESH_GRACE_DAYS * 86400

    payload = _decode_desktop_token(old_token, allow_expired_seconds=grace_seconds)
    if payload is None:
        return jsonify({"error": "Invalid or expired token"}), 401

    username = payload.get("sub", "")
    if not username:
        return jsonify({"error": "Invalid token payload"}), 401

    new_token = _create_desktop_token(username)
    logger.info(f"Desktop token refreshed for {username}")
    return jsonify({"token": new_token})


@desktop_bp.route("/api/desktop/unlink", methods=["POST"])
@login_required
def desktop_unlink():
    """Unlink desktop app from the account."""
    user = session.get("user", {})
    email = user.get("email", "")
    username = get_webapp_username(email)

    success, message = unlink_desktop(username)
    if success:
        return jsonify({"ok": True, "message": message})
    return jsonify({"error": message}), 400


def require_desktop_auth() -> str:
    """Extract and validate JWT from Authorization Bearer header.

    Returns the username from the token payload.
    Aborts with 401 if token is missing or invalid.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        abort(401, description="Missing or invalid Authorization header")

    token = auth_header[len("Bearer "):]
    payload = _decode_desktop_token(token)
    if payload is None:
        abort(401, description="Invalid or expired token")

    username = payload.get("sub", "")
    if not username:
        abort(401, description="Invalid token payload")

    # Auto-mark as linked on any successful API call
    status = get_desktop_status(username)
    if not status["linked"]:
        _mark_desktop_linked(username)
        logger.info(f"Auto-linked desktop app for '{username}' via API call")

    return username
