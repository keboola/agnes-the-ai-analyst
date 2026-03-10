"""
Email magic link authentication provider.

Users enter their email, receive a magic link, click it and they're logged in.
No passwords needed. Domain restriction ensures only allowed users can access.

Email delivery modes:
1. SMTP relay (recommended) - configure SMTP_HOST, SMTP_PORT, etc. in .env
2. Console mode (development) - link printed to server log, shown in browser
"""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from auth import AuthProvider
from webapp.auth import validate_email_domain
from webapp.config import Config

logger = logging.getLogger(__name__)

email_bp = Blueprint("email_auth", __name__)

# SVG envelope icon for the login button
_EMAIL_ICON_HTML = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round">'
    '<rect x="2" y="4" width="20" height="16" rx="2"/>'
    '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>'
    "</svg>"
)


def _get_serializer() -> URLSafeTimedSerializer:
    """Create token serializer using the app secret key."""
    return URLSafeTimedSerializer(Config.SECRET_KEY, salt="email-magic-link")


def _generate_magic_token(email: str) -> str:
    """Generate a signed, time-limited token containing the email."""
    s = _get_serializer()
    return s.dumps({"email": email.lower(), "t": int(time.time())})


def _verify_magic_token(token: str, max_age_seconds: int = 900) -> str | None:
    """Verify magic link token. Returns email if valid, None otherwise.

    Args:
        token: The signed token from the magic link URL.
        max_age_seconds: Token validity period (default 15 minutes).

    Returns:
        Email address if token is valid, None otherwise.
    """
    s = _get_serializer()
    try:
        data = s.loads(token, max_age=max_age_seconds)
        return data.get("email")
    except SignatureExpired:
        logger.warning("Magic link token expired")
        return None
    except BadSignature:
        logger.warning("Invalid magic link token")
        return None


def _send_magic_email(email: str, magic_url: str) -> bool:
    """Send magic link email via SMTP relay.

    Returns True if sent successfully, False otherwise.
    """
    smtp_host = Config.SMTP_HOST
    if not smtp_host:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Sign in to {Config.INSTANCE_NAME}"
    msg["From"] = Config.SMTP_FROM
    msg["To"] = email

    text_body = (
        f"Sign in to {Config.INSTANCE_NAME}\n\n"
        f"Click the link below to sign in:\n{magic_url}\n\n"
        f"This link expires in 15 minutes.\n"
        f"If you didn't request this, ignore this email."
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #1a1a2e;">Sign in to {Config.INSTANCE_NAME}</h2>
    <p>Click the button below to sign in:</p>
    <p style="text-align: center; margin: 30px 0;">
        <a href="{magic_url}"
           style="background: #4361ee; color: white; padding: 12px 32px;
                  text-decoration: none; border-radius: 6px; font-weight: 500;">
            Sign In
        </a>
    </p>
    <p style="color: #666; font-size: 14px;">
        This link expires in 15 minutes.<br>
        If you didn't request this, ignore this email.
    </p>
    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
    <p style="color: #999; font-size: 12px;">
        Or copy and paste this URL into your browser:<br>
        <code style="word-break: break-all;">{magic_url}</code>
    </p>
</body>
</html>"""

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        smtp_port = Config.SMTP_PORT
        use_tls = Config.SMTP_USE_TLS

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            if use_tls:
                server.starttls()

        smtp_user = Config.SMTP_USER
        smtp_password = Config.SMTP_PASSWORD
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)

        server.sendmail(Config.SMTP_FROM, [email], msg.as_string())
        server.quit()
        logger.info("Magic link email sent to %s via SMTP", email)
        return True

    except Exception as e:
        logger.error("Failed to send magic link email to %s: %s", email, e)
        return False


# --- Routes ---


@email_bp.route("/login/email")
def login_email_form():
    """Show email input form."""
    return render_template(
        "login_magic_link.html",
        allowed_domains=Config.ALLOWED_DOMAINS,
    )


@email_bp.route("/login/email/send", methods=["POST"])
def send_magic_link():
    """Validate email domain and send magic link."""
    email = request.form.get("email", "").strip().lower()

    if not email:
        flash("Please enter your email address.", "error")
        return redirect(url_for("email_auth.login_email_form"))

    if not validate_email_domain(email):
        domains_str = ", ".join(f"@{d}" for d in Config.ALLOWED_DOMAINS)
        flash(
            f"Only {domains_str} email addresses are allowed.",
            "error",
        )
        return redirect(url_for("email_auth.login_email_form"))

    # Generate magic link
    token = _generate_magic_token(email)
    magic_url = url_for("email_auth.verify_magic_link", token=token, _external=True)

    # Try SMTP first, fall back to console mode
    smtp_sent = _send_magic_email(email, magic_url)

    if smtp_sent:
        flash("Check your email for the sign-in link.", "info")
        return render_template(
            "login_magic_link_sent.html",
            email=email,
            console_mode=False,
        )
    else:
        # Console/development mode - show link directly
        logger.info("MAGIC LINK for %s: %s", email, magic_url)
        return render_template(
            "login_magic_link_sent.html",
            email=email,
            magic_url=magic_url,
            console_mode=True,
        )


@email_bp.route("/login/email/verify/<token>")
def verify_magic_link(token: str):
    """Verify magic link token and log user in."""
    email = _verify_magic_token(token)

    if not email:
        flash("This sign-in link has expired or is invalid. Please try again.", "error")
        return redirect(url_for("email_auth.login_email_form"))

    # Double-check domain (in case config changed since token was issued)
    if not validate_email_domain(email):
        flash("Your email is no longer authorized.", "error")
        return redirect(url_for("auth.login"))

    # Set session (shared contract across all auth providers)
    name = email.split("@")[0].replace(".", " ").title()
    session["user"] = {
        "email": email,
        "name": name,
        "picture": "",
    }

    logger.info("User logged in via magic link: %s", email)
    return redirect(url_for("dashboard"))


# --- Provider class ---


class EmailAuthProvider(AuthProvider):
    """Email magic link authentication provider."""

    def get_name(self) -> str:
        return "email"

    def get_display_name(self) -> str:
        return "Email"

    def get_blueprint(self) -> Blueprint:
        return email_bp

    def get_login_button(self) -> dict:
        domains = Config.ALLOWED_DOMAINS
        if len(domains) > 1:
            domain_str = ", ".join(f"@{d}" for d in domains)
        elif domains:
            domain_str = f"@{domains[0]}"
        else:
            domain_str = ""
        return {
            "text": "Sign in with Email",
            "url": "/login/email",
            "icon_html": _EMAIL_ICON_HTML,
            "subtitle": f'For <strong>{domain_str}</strong> email addresses.' if domain_str else "",
            "order": 20,
            "css_class": "btn-email",
            "visible": True,
        }

    def is_available(self) -> bool:
        """Available when at least one allowed domain is configured."""
        return len(Config.ALLOWED_DOMAINS) > 0

    def init_app(self, app) -> None:
        """No additional initialization needed."""
        pass


# Module-level provider instance for auto-discovery
provider = EmailAuthProvider()
