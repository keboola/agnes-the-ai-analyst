"""
Jira webhook endpoints — FastAPI replacement for Flask Blueprint.

Receives Jira webhook notifications, verifies HMAC-SHA256 signatures,
and delegates processing to the Jira service.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from connectors.jira.service import Config, get_jira_service
from connectors.jira.validation import is_valid_issue_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["jira-webhooks"])

# Path for storing raw webhook events (debugging/audit)
WEBHOOK_LOG_DIR = Config.JIRA_DATA_DIR / "webhook_events"


def _verify_signature(payload: bytes, signature: str | None) -> bool:
    """Verify HMAC-SHA256 signature from Jira webhook.

    Fail-closed: callers must check ``Config.JIRA_WEBHOOK_SECRET`` is set
    before invoking. If it is not, this returns False (so a misconfigured
    deploy cannot accept unauthenticated webhooks). Issue #83.
    """
    secret = Config.JIRA_WEBHOOK_SECRET

    if not secret:
        return False

    if not signature:
        return False

    if signature.startswith("sha256="):
        signature = signature[7:]

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def _log_webhook_event(event_data: dict) -> None:
    """Log webhook event to file for debugging/audit."""
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


@router.post("/jira")
async def receive_jira_webhook(request: Request) -> Response:
    """Receive and process Jira webhook notifications."""
    # Refuse to process if the operator hasn't configured a webhook secret.
    # Returning 503 (not 401) signals "operator misconfiguration" rather
    # than "attacker guessed wrong". Issue #83.
    if not Config.JIRA_WEBHOOK_SECRET:
        logger.error("JIRA_WEBHOOK_SECRET not configured — refusing webhook")
        return JSONResponse(
            {"detail": "Webhook secret not configured"},
            status_code=503,
        )

    payload = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature")
    if not _verify_signature(payload, signature):
        logger.warning("Invalid webhook signature from %s", request.client.host if request.client else "unknown")
        return JSONResponse({"detail": "Invalid signature"}, status_code=401)

    # Parse JSON
    if not payload:
        return JSONResponse({"detail": "Empty payload"}, status_code=400)

    try:
        event_data = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        return JSONResponse({"detail": "Invalid JSON payload"}, status_code=400)

    if not event_data:
        return JSONResponse({"detail": "Empty payload"}, status_code=400)

    webhook_event = event_data.get("webhookEvent", "unknown")
    issue = event_data.get("issue", {})
    issue_key = issue.get("key", "")

    # Validate issue_key format BEFORE any filesystem operation. Jira issue
    # keys follow `[A-Z][A-Z0-9_]+-\d+`; anything else (path traversal,
    # SQL injection, control chars) is refused with 400. Issue #83.
    if not is_valid_issue_key(issue_key):
        logger.warning(
            "Webhook rejected: malformed issue key %r from %s",
            issue_key,
            request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {"detail": "Malformed or missing issue key"},
            status_code=400,
        )

    # Log event for debugging (after key validation so traversal attempts
    # don't end up named after attacker-supplied data).
    _log_webhook_event(event_data)

    logger.info(f"Received webhook: {webhook_event} for issue {issue_key}")

    jira_service = get_jira_service()

    if not jira_service.is_configured():
        logger.error("Jira service not configured, cannot process webhook")
        return JSONResponse(
            {"status": "error", "message": "Jira service not configured"},
            status_code=503,
        )

    success = jira_service.process_webhook_event(event_data)

    if success:
        return JSONResponse({"status": "ok", "event": webhook_event, "issue": issue_key})
    else:
        return JSONResponse(
            {"status": "error", "message": "Failed to process event", "event": webhook_event, "issue": issue_key},
            status_code=500,
        )


@router.get("/jira/health")
async def jira_webhook_health() -> dict:
    """Health check for Jira webhook endpoint."""
    jira_service = get_jira_service()

    return {
        "status": "ok",
        "configured": jira_service.is_configured(),
        "webhook_secret_set": bool(Config.JIRA_WEBHOOK_SECRET),
        "jira_domain": Config.JIRA_DOMAIN or "(not set)",
    }
