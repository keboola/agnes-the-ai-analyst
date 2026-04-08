"""
Jira webhook endpoint for receiving issue change notifications.

Handles incoming webhooks from Atlassian Jira, verifies HMAC signatures,
and triggers issue data fetching.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, request

from .service import Config, get_jira_service

logger = logging.getLogger(__name__)

jira_bp = Blueprint("jira", __name__, url_prefix="/webhooks")

# Path for storing raw webhook events (for debugging/audit)
WEBHOOK_LOG_DIR = Config.JIRA_DATA_DIR / "webhook_events"


def verify_signature(payload: bytes, signature: str | None) -> bool:
    """
    Verify HMAC-SHA256 signature from Jira webhook.

    Args:
        payload: Raw request body bytes
        signature: Signature from X-Hub-Signature header

    Returns:
        True if signature is valid or if no secret is configured (dev mode)
    """
    secret = Config.JIRA_WEBHOOK_SECRET

    # If no secret configured, skip verification (not recommended for production)
    if not secret:
        logger.warning("JIRA_WEBHOOK_SECRET not configured, skipping signature verification")
        return True

    if not signature:
        logger.warning("No signature provided in webhook request")
        return False

    # Jira may send signature with or without algorithm prefix
    if signature.startswith("sha256="):
        signature = signature[7:]

    # Compute expected signature
    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(signature, expected)


def log_webhook_event(event_data: dict) -> None:
    """
    Log webhook event to file for debugging/audit.

    Args:
        event_data: Webhook payload
    """
    try:
        WEBHOOK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        event_type = event_data.get("webhookEvent", "unknown").replace(":", "_")
        filename = f"{timestamp}_{event_type}.json"
        filepath = WEBHOOK_LOG_DIR / filename

        with open(filepath, "w") as f:
            json.dump(event_data, f, indent=2, default=str)

    except Exception as e:
        logger.warning(f"Failed to log webhook event: {e}")


@jira_bp.route("/jira", methods=["POST"])
def receive_jira_webhook():
    """
    Receive and process Jira webhook notifications.

    Jira sends POST requests with JSON payload containing:
    - webhookEvent: Event type (e.g., "jira:issue_created", "jira:issue_updated")
    - issue: Issue data (may be partial)
    - comment: Comment data (for comment events)
    - changelog: List of field changes (for update events)

    Returns:
        JSON response with processing status
    """
    # Get raw payload for signature verification
    payload = request.get_data()

    # Verify signature (Jira uses X-Hub-Signature or X-Hub-Signature-256)
    signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature")

    if not verify_signature(payload, signature):
        logger.warning(f"Invalid webhook signature from {request.remote_addr}")
        abort(401, "Invalid signature")

    # Parse JSON payload
    try:
        event_data = request.get_json(force=True)
    except Exception as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        abort(400, "Invalid JSON payload")

    if not event_data:
        abort(400, "Empty payload")

    # Log the event for debugging
    log_webhook_event(event_data)

    # Extract event info
    webhook_event = event_data.get("webhookEvent", "unknown")
    issue = event_data.get("issue", {})
    issue_key = issue.get("key", "unknown")

    logger.info(f"Received webhook: {webhook_event} for issue {issue_key}")

    # Process the event
    jira_service = get_jira_service()

    if not jira_service.is_configured():
        logger.error("Jira service not configured, cannot process webhook")
        return jsonify({
            "status": "error",
            "message": "Jira service not configured",
        }), 503

    # Process asynchronously would be better, but for now process synchronously
    success = jira_service.process_webhook_event(event_data)

    if success:
        return jsonify({
            "status": "ok",
            "event": webhook_event,
            "issue": issue_key,
        })
    else:
        return jsonify({
            "status": "error",
            "message": "Failed to process event",
            "event": webhook_event,
            "issue": issue_key,
        }), 500


@jira_bp.route("/jira/health", methods=["GET"])
def jira_webhook_health():
    """
    Health check for Jira webhook endpoint.

    Returns configuration status without exposing secrets.
    """
    jira_service = get_jira_service()

    return jsonify({
        "status": "ok",
        "configured": jira_service.is_configured(),
        "webhook_secret_set": bool(Config.JIRA_WEBHOOK_SECRET),
        "jira_domain": Config.JIRA_DOMAIN or "(not set)",
    })


@jira_bp.route("/jira/test", methods=["POST"])
def test_jira_fetch():
    """
    Test endpoint to manually fetch an issue (for debugging).

    Requires JSON body: {"issue_key": "KSP-123"}
    Only available if FLASK_DEBUG is true.
    """
    if not Config.DEBUG:
        abort(404)

    data = request.get_json(silent=True) or {}
    issue_key = data.get("issue_key")

    if not issue_key:
        return jsonify({"error": "issue_key is required"}), 400

    jira_service = get_jira_service()

    if not jira_service.is_configured():
        return jsonify({"error": "Jira service not configured"}), 503

    issue_data = jira_service.fetch_issue(issue_key)

    if issue_data:
        saved_path = jira_service.save_issue(issue_data)
        return jsonify({
            "status": "ok",
            "issue_key": issue_key,
            "saved_to": str(saved_path) if saved_path else None,
            "fields_count": len(issue_data.get("fields", {})),
        })
    else:
        return jsonify({"error": f"Failed to fetch issue {issue_key}"}), 500
