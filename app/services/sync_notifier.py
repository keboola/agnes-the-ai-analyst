"""Sync-completion notifications: operator alert on failure, analyst desktop
notification on success.

Channel-agnostic webhook: when ``notifications.alert_webhook_url`` is set in
instance.yaml, a concise ``{"text": ...}`` payload is POSTed to it on sync
failure. The payload shape is the lowest common denominator across Slack,
Google Chat, Mattermost and Discord incoming webhooks, so operators point the
URL at whichever channel they already run.

Best-effort by contract: a webhook failure (unreachable host, non-2xx, raised
exception) MUST NEVER break the sync that triggered the alert. The actual
``httpx.post`` lives in :func:`services.telegram_bot.sender.post_webhook`, which
swallows everything; the hook in :func:`app.api.sync._run_sync` is itself
wrapped defensively as a second line of defence.

``httpx`` is imported at module scope (not only inside ``post_webhook``) so
tests can monkeypatch ``sync_notifier.httpx.post`` — the same module object the
shared sender references — to assert on the outbound payload without a network.

``notify_sync_completed`` (#412: ``agnes watch``) rides the same best-effort
contract for the SUCCESS path, but a different transport: the desktop
notification channel (``app.notifications.publish_notification`` →
``notify:{user}`` pub/sub → ``app/api/notifications_ws.py``), not the webhook.
That channel only ever addresses one user at a time — there is no
instance-wide broadcast primitive — and the only two existing producers
(the Telegram bot's script-run result, ``app.api.store._notify_author``)
address a single already-known user (the requester / the entity owner). A
sync has no such single owner, so this fans out by calling the same per-user
primitive once per active user; delivery still degrades to a silent no-op
for anyone with no live desktop socket, exactly like a single-user call
would. ``publish_notification`` and ``users_repo`` are imported at module
scope for the same reason ``httpx`` is: so tests can monkeypatch them here.
"""

import logging

import httpx  # noqa: F401  — re-exported so tests patch sync_notifier.httpx.post

from app.notifications import publish_notification  # noqa: F401  — re-exported for monkeypatching
from src.repositories import users_repo  # noqa: F401  — re-exported for monkeypatching

logger = logging.getLogger(__name__)


def _alert_webhook_url() -> str:
    """Resolve the configured alert webhook URL (empty string when unset).

    ``AGNES_ALERT_WEBHOOK_URL`` env > ``notifications.alert_webhook_url`` YAML >
    ``""``. The env-overrides-yaml shape mirrors the other instance_config
    getters so Terraform can wire the URL per-deployment without forking YAML.
    """
    import os

    from app.instance_config import get_value

    raw = os.environ.get("AGNES_ALERT_WEBHOOK_URL")
    if raw is None:
        raw = get_value("notifications", "alert_webhook_url", default="")
    return (raw or "").strip()


def _build_message(failed_tables: list[dict], fatal: "Exception | None") -> str:
    """Render a concise, human-readable alert body.

    Names the fatal exception (when any) and lists up to a handful of failed
    tables with their error strings so an operator can triage from the
    notification alone, without opening the dashboard.
    """
    lines = ["Agnes scheduled sync failed."]
    if fatal is not None:
        lines.append(f"Fatal error: {type(fatal).__name__}: {fatal}")
    if failed_tables:
        lines.append(f"{len(failed_tables)} table(s) failed to sync:")
        # Cap the listing so a wholesale failure doesn't blow past the webhook
        # body limit; the count above always reflects the true total.
        for err in failed_tables[:10]:
            table = err.get("table", "?")
            detail = err.get("error", "")
            lines.append(f"  - {table}: {detail}")
        if len(failed_tables) > 10:
            lines.append(f"  … and {len(failed_tables) - 10} more")
    return "\n".join(lines)


def notify_sync_failure(
    *,
    failed_tables: list[dict],
    fatal: "Exception | None",
) -> None:
    """Best-effort operator alert on scheduled-sync failure.

    No-op when there's nothing to report (no fatal exception AND no per-table
    errors) or when ``notifications.alert_webhook_url`` is unset. Otherwise
    POSTs a single ``{"text": ...}`` payload to the configured webhook.

    Never raises — every failure mode (unset URL, empty input, webhook error)
    degrades to a silent no-op / logged warning so the caller on the sync
    critical path is unaffected.
    """
    try:
        if not failed_tables and fatal is None:
            return
        url = _alert_webhook_url()
        if not url:
            return
        text = _build_message(failed_tables, fatal)
        from services.telegram_bot.sender import post_webhook

        post_webhook(url, {"text": text})
    except Exception:
        # Defence in depth on top of post_webhook's own swallow — a failure
        # building the message or resolving config must not bubble into the
        # sync error handler.
        logger.exception("sync-failure webhook notification failed")


def notify_sync_completed(views: dict[str, list[str]]) -> None:
    """Best-effort desktop notification when a sync completes successfully.

    ``views`` is the orchestrator's own rebuild result — ``{source_name:
    [table_names]}`` (see ``src.orchestrator.SyncOrchestrator.rebuild``).
    No-op on an empty rebuild (nothing to report). Otherwise publishes one
    ``sync_completed`` event per source to every active user's desktop
    notification channel (see the module docstring for why "every active
    user" rather than a narrower RBAC-scoped audience: today's
    ``publish_notification`` has no reverse "who can see this table" lookup,
    and the payload itself carries only a source name + table count, never
    table names or data).

    Never raises — a dropped desktop notification is an acceptable
    degradation (the analyst still gets fresh data on their next ``agnes
    pull``), so a coordination-backend outage or a bad user row must not
    fail the sync that triggered it.
    """
    try:
        if not views:
            return
        for user in users_repo().list_all():
            if not user.get("active", True):
                continue
            for source_name, table_names in views.items():
                try:
                    publish_notification(
                        user["id"],
                        {
                            "type": "sync_completed",
                            "source": source_name,
                            "table_count": len(table_names),
                        },
                    )
                except Exception:
                    logger.warning(
                        "sync-completed notification dropped for user %s, source %s",
                        user.get("id"),
                        source_name,
                    )
    except Exception:
        logger.exception("sync-completed notifier failed")
