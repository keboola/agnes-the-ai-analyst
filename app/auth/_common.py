"""Shared helpers for auth providers (Google OAuth, password, email link).

Kept out of `dependencies.py` so it doesn't pull FastAPI auth machinery into
thin provider modules that only need these stdlib-only helpers.
"""

import os
from typing import Optional


def smtp_from_address() -> str:
    """Sender address for outgoing auth mail (magic link, reset, setup).

    ``SMTP_FROM`` is the canonical key. ``EMAIL_FROM_ADDRESS`` is honored as a
    backward-compatible fallback — it was the removed SendGrid-SDK branch's
    sender key, so a deployment that configured its sender through it keeps
    that sender when it switches to the SMTP relay.
    """
    return os.environ.get("SMTP_FROM") or os.environ.get("EMAIL_FROM_ADDRESS") or "noreply@example.com"


def send_smtp_email(to_email: str, subject: str, body_text: str) -> None:
    """Deliver a plaintext mail via the configured SMTP relay; raises on failure.

    SMTP is the only mail transport. Providers with an HTTP API (SendGrid,
    Mailgun, …) are used through their SMTP relay (e.g.
    ``SMTP_HOST=smtp.sendgrid.net``). The former SendGrid SDK branch was
    removed: the ``sendgrid`` package was never a declared dependency, so that
    path always died on import — while the endpoint still answered success.
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = smtp_from_address()
    msg["To"] = to_email
    with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587"))) as s:
        if os.environ.get("SMTP_USE_TLS", "true").lower() == "true":
            s.starttls()
        smtp_user = os.environ.get("SMTP_USER")
        if smtp_user:
            s.login(smtp_user, os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def safe_next_path(candidate: Optional[str], default: Optional[str] = None) -> str:
    """Return `candidate` if it's a same-origin absolute path, else `default`.

    Open-redirect guard: must start with a single `/` and must NOT start with
    `//` (which browsers treat as protocol-relative, i.e. cross-origin).
    Accepts plain paths like `/catalog` or `/foo?bar=baz`. Rejects
    `javascript:...`, `http://...`, `//evil/`, bare `dashboard`, empty/None, etc.

    When `default` is None, resolves to the operator-configured home route
    (`AGNES_HOME_ROUTE` env > `instance.home_route` YAML > `/dashboard`) so an
    instance with `AGNES_HOME_ROUTE=/home` lands users on /home after OAuth /
    magic-link / password login instead of the legacy /dashboard.

    Lazy-imported to keep this module dependency-free for thin provider
    modules that don't otherwise need `app.instance_config`.
    """
    if default is None:
        from app.instance_config import get_home_route

        default = get_home_route()
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    return candidate
