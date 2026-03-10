"""
Flask application for Google SSO user management.

Allows users to:
1. Log in with Google (allowed domain only)
2. View their account status if they exist
3. Create a new analyst account with their SSH key
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import yaml

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from .auth import admin_required, auth_bp, login_required
from .config import Config
from .desktop_auth import require_desktop_auth
from .notification_images import images_bp
from .account_service import get_account_details
from .sync_settings_service import get_sync_settings, update_sync_settings, get_table_subscriptions, update_table_subscriptions

# Jira connector is optional - only loaded if configured
try:
    from connectors.jira.webhook import jira_bp
    JIRA_AVAILABLE = True
except ImportError:
    JIRA_AVAILABLE = False
    jira_bp = None
from .telegram_service import get_telegram_status, link_telegram, unlink_telegram
from .corporate_memory_service import (
    get_knowledge,
    get_stats as get_memory_stats,
    get_user_stats as get_memory_user_stats,
    get_user_votes,
    vote as memory_vote,
)
from .user_service import (
    UserInfo,
    check_user_exists,
    create_user,
    get_username_from_email,
    is_username_available,
    validate_ssh_key,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.from_object(Config)

    # Validate configuration
    errors = Config.validate()
    if errors and not app.debug:
        for error in errors:
            logger.warning(f"Configuration warning: {error}")

    # Register core auth blueprint (login_required, login page, logout)
    app.register_blueprint(auth_bp)

    # Auto-discover and register auth providers
    from auth import discover_providers

    for provider_instance in discover_providers():
        provider_instance.init_app(app)
        app.register_blueprint(provider_instance.get_blueprint())

    # Register other blueprints
    app.register_blueprint(images_bp)
    if JIRA_AVAILABLE and jira_bp:
        app.register_blueprint(jira_bp)

    # Register main routes
    register_routes(app)

    # Add template context processor for current year and config
    @app.context_processor
    def inject_now():
        return {"now": datetime.now}

    @app.context_processor
    def inject_config():
        return {"config": Config}

    # Add cache busting for static files
    @app.context_processor
    def inject_static_cache_buster():
        def static_url(filename: str) -> str:
            """Generate static URL with cache-busting query parameter."""
            static_path = Path(app.static_folder) / filename
            if static_path.exists():
                mtime = int(static_path.stat().st_mtime)
                return url_for("static", filename=filename, v=mtime)
            return url_for("static", filename=filename)
        return {"static_url": static_url}

    return app


NOTIFY_SOCKET_PATH = "/data/notifications/bot.sock"

# Path to sync state (written by data sync process)
SYNC_STATE_PATH = Path("/data/src_data/metadata/sync_state.json")
# Local development: fall back to dev_data/metadata/ relative to project root
_DEV_METADATA_PATH = Path(__file__).parent.parent / "dev_data" / "metadata"


def _resolve_metadata_path(filename: str) -> Path:
    """Resolve metadata file path with dev fallback."""
    prod_path = SYNC_STATE_PATH.parent / filename
    if prod_path.exists():
        return prod_path
    dev_path = _DEV_METADATA_PATH / filename
    return dev_path

# Fallback stats (used when sync_state.json is unavailable)
FALLBACK_DATA_STATS = {
    "tables": 0,
    "columns": 0,
    "rows": 0,
    "rows_display": "-",
    "size_mb": 0,
    "size_display": "0 MB",
    "uncompressed_mb": 0,
    "unstructured_gb": 0,
    "unstructured_display": "",
    "last_updated": None,
    "highlights": {},
}


def _load_data_stats() -> dict:
    """Load aggregate data stats from sync_state.json, with hardcoded fallback."""
    try:
        sync_path = _resolve_metadata_path("sync_state.json")
        if sync_path.exists():
            with open(sync_path) as f:
                state = json.load(f)

            tables_data = state.get("tables", {})
            if not tables_data:
                return dict(FALLBACK_DATA_STATS)

            total_tables = len(tables_data)
            total_columns = sum(t.get("columns", 0) for t in tables_data.values())
            total_rows = sum(t.get("rows", 0) for t in tables_data.values())
            total_size_mb = sum(t.get("file_size_mb", 0) for t in tables_data.values())
            total_uncompressed_mb = sum(t.get("uncompressed_mb", 0) for t in tables_data.values())

            # Format rows for display
            if total_rows >= 1_000_000:
                rows_display = f"{total_rows / 1_000_000:.0f}M+"
            elif total_rows >= 1_000:
                rows_display = f"{total_rows / 1_000:.0f}K+"
            else:
                rows_display = str(total_rows)

            # Parse last_updated timestamp
            last_updated = state.get("last_updated")
            last_updated_display = None
            if last_updated:
                try:
                    dt = datetime.fromisoformat(last_updated)
                    last_updated_display = dt.strftime("%Y-%m-%d %H:%M") + " UTC"
                except (ValueError, TypeError):
                    last_updated_display = last_updated[:16] if last_updated else None

            # Format size for display
            size_mb = round(total_size_mb)
            if size_mb >= 1000:
                size_display = f"{size_mb / 1000:.1f} GB"
            else:
                size_display = f"{size_mb} MB"

            return {
                "tables": total_tables,
                "columns": total_columns if total_columns > 0 else FALLBACK_DATA_STATS["columns"],
                "rows": total_rows,
                "rows_display": rows_display,
                "size_mb": size_mb,
                "size_display": size_display,
                "uncompressed_mb": round(total_uncompressed_mb),
                "unstructured_gb": FALLBACK_DATA_STATS["unstructured_gb"],
                "unstructured_display": FALLBACK_DATA_STATS["unstructured_display"],
                "last_updated": last_updated_display,
                "highlights": FALLBACK_DATA_STATS["highlights"],
            }
    except Exception as e:
        logger.warning(f"Could not load data stats from sync_state.json: {e}")

    # Fallback: derive stats from profiles.json (covers sample data / no-sync setups)
    try:
        profiles_path = _resolve_metadata_path("profiles.json")
        if profiles_path.exists():
            with open(profiles_path) as f:
                profiles = json.load(f)
            tables_data = profiles.get("tables", {})
            if tables_data:
                total_tables = len(tables_data)
                total_rows = sum(t.get("row_count", 0) for t in tables_data.values())
                total_columns = sum(t.get("column_count", 0) for t in tables_data.values())
                total_size_mb = sum(t.get("file_size_mb", 0) or 0 for t in tables_data.values())
                if total_rows >= 1_000_000:
                    rows_display = f"{total_rows / 1_000_000:.0f}M+"
                elif total_rows >= 1_000:
                    rows_display = f"{total_rows / 1_000:.0f}K+"
                else:
                    rows_display = str(total_rows)
                size_mb = round(total_size_mb)
                size_display = f"{size_mb / 1000:.1f} GB" if size_mb >= 1000 else f"{size_mb} MB"
                return {
                    "tables": total_tables,
                    "columns": total_columns,
                    "rows": total_rows,
                    "rows_display": rows_display,
                    "size_mb": size_mb,
                    "size_display": size_display,
                    "uncompressed_mb": 0,
                    "unstructured_gb": 0,
                    "unstructured_display": "",
                    "last_updated": None,
                    "highlights": {},
                }
    except Exception as e:
        logger.warning(f"Could not load data stats from profiles.json: {e}")

    return dict(FALLBACK_DATA_STATS)


def _load_catalog_data() -> list:
    """Load catalog data by merging data_description.md (YAML) with sync_state.json.

    Returns list of category dicts: [{name, icon_type, tables: [{name, description, rows, rows_display, period}]}]
    """
    import re

    import yaml

    catalog = []

    try:
        # Parse data_description.md YAML block
        desc_path = Path(os.path.dirname(__file__)) / ".." / "docs" / "data_description.md"
        if not desc_path.exists():
            return catalog

        with open(desc_path) as f:
            content = f.read()

        # Extract YAML block between ```yaml and ```
        yaml_match = re.search(r'```yaml\s*\n(.*?)```', content, re.DOTALL)
        if not yaml_match:
            return catalog

        yaml_data = yaml.safe_load(yaml_match.group(1))
        if not yaml_data or "tables" not in yaml_data:
            return catalog

        # Load sync state for row counts
        sync_data = {}
        try:
            sync_path = _resolve_metadata_path("sync_state.json")
            if sync_path.exists():
                with open(sync_path) as f:
                    state = json.load(f)
                sync_data = state.get("tables", {})
        except Exception:
            pass

        # Get folder mapping
        folder_mapping = yaml_data.get("folder_mapping", {})

        # Load category mappings from instance config, with empty fallback
        try:
            from config.loader import load_instance_config, get_instance_value
            _catalog_config = load_instance_config()
            _catalog_categories = get_instance_value(_catalog_config, "catalog", "categories", default={})
            folder_to_category = {k: v.get("label", k) for k, v in _catalog_categories.items()}
            folder_to_icon = {k: v.get("icon", k) for k, v in _catalog_categories.items()}
        except Exception:
            folder_to_category = {}
            folder_to_icon = {}

        # Map bucket to folder
        bucket_to_folder = {}
        for bucket_id, folder_name in folder_mapping.items():
            bucket_to_folder[bucket_id] = folder_name

        # Group tables by category (folder)
        categories = {}
        for table in yaml_data["tables"]:
            table_id = table.get("id", "")
            # Extract bucket from table_id (e.g., "in.c-crm.company" -> "in.c-crm")
            parts = table_id.rsplit(".", 1)
            bucket_id = parts[0] if len(parts) > 1 else ""
            folder = bucket_to_folder.get(bucket_id, "other")

            if folder not in categories:
                categories[folder] = []

            # Get sync info
            sync_info = sync_data.get(table_id, {})
            rows = sync_info.get("rows", 0)

            # Format rows
            if rows >= 1_000_000:
                rows_display = f"{rows / 1_000_000:.1f}M"
            elif rows >= 1_000:
                rows_display = f"{rows:,}"
            else:
                rows_display = str(rows) if rows > 0 else "-"

            # Determine if "large" badge
            rows_large = rows >= 1_000_000

            categories[folder].append({
                "name": table.get("name", ""),
                "description": table.get("description", ""),
                "rows": rows,
                "rows_display": rows_display,
                "rows_large": rows_large,
            })

        # Build ordered catalog (from instance config or use discovered folders)
        try:
            category_order = get_instance_value(_catalog_config, "catalog", "order", default=list(folder_to_category.keys()))
        except Exception:
            category_order = list(folder_to_category.keys())
        for folder in category_order:
            if folder in categories:
                catalog.append({
                    "name": folder_to_category.get(folder, folder),
                    "icon_type": folder_to_icon.get(folder, folder),
                    "tables": categories[folder],
                    "count": len(categories[folder]),
                })

    except Exception as e:
        logger.warning(f"Could not load catalog data: {e}")

    return catalog


# Category metadata for Business Metrics card
METRIC_CATEGORY_META = {
    'revenue':    {'label': 'Revenue',    'css': 'sales',      'order': 1},
    'customers':  {'label': 'Customers',  'css': 'hr',         'order': 2},
    'marketing':  {'label': 'Marketing',  'css': 'telemetry',  'order': 3},
    'support':    {'label': 'Support',    'css': 'support',    'order': 4},
}


def _load_metrics_data():
    """Load business metric definitions for catalog display.

    Returns list of category dicts ordered by METRIC_CATEGORY_META:
    [{'key': 'revenue', 'label': 'Revenue', 'css': 'sales', 'metrics': [...]}, ...]
    """
    # Try production path first, fall back to local dev path
    metrics_dir = Path("/data/docs/metrics")
    if not metrics_dir.exists():
        metrics_dir = Path(__file__).parent.parent / "docs" / "metrics"

    if not metrics_dir.exists():
        return []

    categories = {}
    for yml_file in sorted(metrics_dir.glob("*/*.yml")):
        try:
            with open(yml_file, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f)

            if isinstance(raw, list) and raw:
                metric = raw[0]
            elif isinstance(raw, dict):
                metric = raw
            else:
                continue

            cat_key = yml_file.parent.name
            if cat_key not in categories:
                categories[cat_key] = []

            categories[cat_key].append({
                'name': metric.get('name', yml_file.stem),
                'display_name': metric.get('display_name', yml_file.stem),
                'description': metric.get('description', ''),
                'grain': metric.get('grain', ''),
                'path': f"{cat_key}/{yml_file.name}",
            })
        except Exception as e:
            logger.warning(f"Could not parse metric {yml_file}: {e}")

    # Build ordered result using METRIC_CATEGORY_META
    result = []
    for cat_key, meta in sorted(METRIC_CATEGORY_META.items(), key=lambda x: x[1]['order']):
        if cat_key in categories:
            result.append({
                'key': cat_key,
                'label': meta['label'],
                'css': meta['css'],
                'metrics': categories[cat_key],
            })

    # Add any unknown categories at the end
    for cat_key, metrics in sorted(categories.items()):
        if cat_key not in METRIC_CATEGORY_META:
            result.append({
                'key': cat_key,
                'label': cat_key.replace('_', ' ').title(),
                'css': cat_key,
                'metrics': metrics,
            })

    return result


def _send_welcome_message(username: str) -> None:
    """Send a welcome message to the user via bot socket after linking."""
    try:
        import httpx

        transport = httpx.HTTPTransport(uds=NOTIFY_SOCKET_PATH)
        with httpx.Client(transport=transport, timeout=10) as client:
            client.post(
                "http://localhost/send",
                json={
                    "user": username,
                    "text": (
                        f"Account linked!\n\n"
                        f"Your server login: *{username}*\n"
                        f"Notifications dir: `~/user/notifications/`\n\n"
                        f"To create notification scripts, ask your local AI assistant "
                        f"(Claude Code). It knows how to build them for you.\n\n"
                        f"You will receive alerts from your scripts here."
                    ),
                    "parse_mode": "Markdown",
                },
            )
    except Exception as e:
        logger.warning(f"Failed to send welcome message to {username}: {e}")


def register_routes(app: Flask) -> None:
    """Register main application routes."""

    @app.route("/")
    def index():
        """Redirect to dashboard or login."""
        if "user" in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("auth.login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        """Show user dashboard with account info or registration form."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        # Check if user exists on the system
        user_info = check_user_exists(username)

        # Check if username is available (for new registrations)
        username_available, username_error = is_username_available(username)

        # Read bootstrap YAML for Claude Code setup instructions
        bootstrap_yaml = ""
        try:
            bootstrap_path = os.path.join(os.path.dirname(__file__), "..", "docs", "setup", "bootstrap.yaml")
            with open(bootstrap_path, "r") as f:
                bootstrap_yaml_template = f.read()

            # Inject username and server info into template
            bootstrap_yaml = bootstrap_yaml_template.replace("{username}", username)
            bootstrap_yaml = bootstrap_yaml.replace("{server_host}", Config.SERVER_HOST)
            bootstrap_yaml = bootstrap_yaml.replace("{server_hostname}", Config.SERVER_HOSTNAME)
            webapp_url = f"https://{Config.SERVER_HOSTNAME}" if Config.SERVER_HOSTNAME else ""
            bootstrap_yaml = bootstrap_yaml.replace("{webapp_url}", webapp_url)

        except Exception as e:
            logger.warning(f"Could not read bootstrap.yaml: {e}")

        # Get Telegram link status
        telegram_status = get_telegram_status(username)

        # Get desktop app link status
        from .desktop_auth import get_desktop_status
        desktop_status = get_desktop_status(username)

        # Load data stats
        data_stats = _load_data_stats()
        catalog_data = _load_catalog_data()

        # Load sync settings (for existing users)
        sync_settings = get_sync_settings(username) if user_info.exists else None

        # Add subscription status to catalog tables
        if user_info.exists:
            subs = get_table_subscriptions(username)
            table_mode = subs.get("table_mode", "all")
            table_subs = subs.get("tables", {})
            for cat in catalog_data:
                for table in cat.get("tables", []):
                    if table_mode == "all":
                        table["subscribed"] = True
                    else:
                        table["subscribed"] = table_subs.get(table["name"], False)

        # Gather account widget details (notification scripts, cron, last sync)
        account_details = get_account_details(username) if user_info.exists else None

        # Activity Center summary for dashboard widget (empty fallback)
        activity_summary = {}

        # Load business metrics for dashboard widget
        metrics_data = _load_metrics_data()

        return render_template(
            "dashboard.html",
            user=user,
            username=username,
            user_info=user_info,
            username_available=username_available,
            username_error=username_error,
            server_host=Config.SERVER_HOST,
            server_hostname=Config.SERVER_HOSTNAME,
            bootstrap_yaml=bootstrap_yaml,
            telegram_status=telegram_status,
            desktop_status=desktop_status,
            data_stats=data_stats,
            catalog_data=catalog_data,
            sync_settings=sync_settings,
            account_details=account_details,
            activity_summary=activity_summary,
            metrics_data=metrics_data,
        )

    @app.route("/catalog")
    @login_required
    def catalog():
        """Data catalog page."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        data_stats = _load_data_stats()
        catalog_data = _load_catalog_data()
        sync_settings = get_sync_settings(username)

        # Add subscription status to catalog tables
        subs = get_table_subscriptions(username)
        table_mode = subs.get("table_mode", "all")
        table_subs = subs.get("tables", {})
        for cat in catalog_data:
            for table in cat.get("tables", []):
                if table_mode == "all":
                    table["subscribed"] = True
                else:
                    table["subscribed"] = table_subs.get(table["name"], False)

        metrics_data = _load_metrics_data()

        return render_template(
            "catalog.html",
            data_stats=data_stats,
            catalog_data=catalog_data,
            sync_settings=sync_settings,
            metrics_data=metrics_data,
        )

    @app.route("/api/catalog/profile/<table_name>")
    @login_required
    def catalog_profile(table_name):
        """Return profiler data for a single table."""
        profiles_path = _resolve_metadata_path("profiles.json")
        try:
            if not profiles_path.exists():
                return jsonify({"error": "Profiler data not available yet"}), 404

            with open(profiles_path) as f:
                profiles = json.load(f)

            table_profile = profiles.get("tables", {}).get(table_name)
            if not table_profile:
                return jsonify({"error": f"No profile for table '{table_name}'"}), 404

            return jsonify(table_profile)
        except Exception as e:
            logger.error(f"Error loading profile for {table_name}: {e}")
            return jsonify({"error": "Failed to load profile data"}), 500

    @app.route("/api/metrics/<path:metric_path>")
    @login_required
    def api_metric(metric_path):
        """API endpoint to serve metric definition as structured JSON."""
        import re

        # Validate path to prevent directory traversal (allow category/file.yml pattern)
        if not re.match(r"^[a-z_]+/[a-z_]+\.yml$", metric_path):
            return jsonify({"error": "Invalid metric path"}), 400

        # Try production path first, fall back to local dev path
        docs_dir = Path("/data/docs/metrics")
        if not docs_dir.exists():
            # Local development: use docs/metrics relative to project root
            docs_dir = Path(__file__).parent.parent / "docs" / "metrics"

        file_path = docs_dir / metric_path

        # Security check: ensure path stays within docs_dir
        try:
            if not file_path.is_file() or not file_path.resolve().is_relative_to(
                docs_dir.resolve()
            ):
                return jsonify({"error": "Metric file not found"}), 404
        except (ValueError, OSError):
            return jsonify({"error": "Invalid path"}), 400

        # Parse metric YAML and return structured JSON
        try:
            from webapp.utils.metric_parser import MetricParser

            parser = MetricParser(docs_dir)
            metric_data = parser.parse_metric(metric_path)
            return jsonify(metric_data)
        except Exception as e:
            logger.error(f"Error parsing metric {metric_path}: {e}")
            return jsonify({"error": f"Failed to parse metric: {str(e)}"}), 500

    @app.route("/docs/metrics/<path:metric_path>")
    @login_required
    def serve_metric(metric_path):
        """Serve metric definition YAML files (legacy endpoint for backward compatibility)."""
        import re

        # Validate path to prevent directory traversal (allow category/file.yml pattern)
        if not re.match(r"^[a-z_]+/[a-z_]+\.yml$", metric_path):
            return render_template("error.html", error="Invalid metric path", code=400), 400

        docs_dir = Path("/data/docs/metrics")
        file_path = docs_dir / metric_path

        # Security check: ensure path stays within docs_dir
        try:
            if not file_path.is_file() or not file_path.resolve().is_relative_to(
                docs_dir.resolve()
            ):
                return (
                    render_template("error.html", error="Metric file not found", code=404),
                    404,
                )
        except (ValueError, OSError):
            return render_template("error.html", error="Invalid path", code=400), 400

        from flask import send_file as flask_send_file

        return flask_send_file(file_path, mimetype="text/plain")

    @app.route("/register", methods=["POST"])
    @login_required
    def register():
        """Create a new analyst account."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        # Check if user already exists
        user_info = check_user_exists(username)
        if user_info.exists:
            flash("Your account already exists.", "info")
            return redirect(url_for("dashboard"))

        # Get and validate SSH key
        # Normalize whitespace: collapse newlines/tabs/multiple spaces to single spaces
        # Users often paste keys with line breaks from terminal wrapping
        ssh_key = " ".join(request.form.get("ssh_key", "").split())

        is_valid, error = validate_ssh_key(ssh_key)
        if not is_valid:
            flash(error, "error")
            return redirect(url_for("dashboard"))

        # Create the user
        success, message = create_user(username, ssh_key)

        if success:
            flash(message, "success")
            logger.info(f"Account created for {email} (username: {username})")
        else:
            flash(message, "error")
            logger.error(f"Failed to create account for {email}: {message}")

        return redirect(url_for("dashboard"))

    @app.route("/api/telegram/verify", methods=["POST"])
    @login_required
    def telegram_verify():
        """Verify a Telegram verification code and link the account."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        data = request.get_json(silent=True) or {}
        code = data.get("code", "").strip()

        if not code:
            return jsonify({"error": "Verification code is required"}), 400

        success, message = link_telegram(username, code)
        if success:
            logger.info(f"Telegram linked for {username}")
            # Send welcome message via bot socket
            _send_welcome_message(username)
            return jsonify({"ok": True, "message": message})
        return jsonify({"error": message}), 400

    @app.route("/api/telegram/unlink", methods=["POST"])
    @login_required
    def telegram_unlink():
        """Unlink Telegram from the account."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        success, message = unlink_telegram(username)
        if success:
            logger.info(f"Telegram unlinked for {username}")
            return jsonify({"ok": True, "message": message})
        return jsonify({"error": message}), 400

    @app.route("/api/telegram/status")
    @login_required
    def telegram_status():
        """Get Telegram link status."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)
        status = get_telegram_status(username)
        return jsonify(status)

    @app.route("/download/<filename>")
    @login_required
    def download(filename):
        """Serve downloadable files (e.g., desktop app)."""
        import re

        if not re.match(r"^[a-zA-Z0-9_\-]+\.(zip|dmg)$", filename):
            return render_template("error.html", error="Invalid filename", code=400), 400

        download_dir = Path("/data/downloads")
        file_path = download_dir / filename
        if not file_path.is_file():
            return render_template("error.html", error="File not found", code=404), 404

        from flask import send_file as flask_send_file

        return flask_send_file(file_path, as_attachment=True)

    @app.route("/api/desktop/scripts")
    def desktop_scripts():
        """List notification scripts for the authenticated desktop user."""
        username = require_desktop_auth()
        from services.telegram_bot.status import get_script_list_structured
        scripts = get_script_list_structured(username)
        return jsonify(scripts)

    @app.route("/api/desktop/scripts/run", methods=["POST"])
    def desktop_run_script():
        """Run a notification script on-demand for the authenticated desktop user."""
        username = require_desktop_auth()
        data = request.get_json(silent=True) or {}
        script_name = data.get("name", "").strip()
        if not script_name:
            return jsonify({"error": "Missing 'name' field"}), 400

        from services.telegram_bot.runner import run_user_script
        from services.telegram_bot.dispatch import dispatch_to_ws_gateway

        output = run_user_script(username, script_name)
        if output is None:
            return jsonify({"error": f"Script '{script_name}' failed or not found"}), 500

        if output.get("notify", False):
            dispatch_to_ws_gateway(username, output, script_name)

        return jsonify({"ok": True})

    @app.route("/api/sync-settings")
    @login_required
    def sync_settings_get():
        """Get sync settings for current user."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)
        settings = get_sync_settings(username)
        return jsonify(settings)

    @app.route("/api/sync-settings", methods=["POST"])
    @login_required
    def sync_settings_update():
        """Update sync settings for current user."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        data = request.get_json(silent=True) or {}
        datasets = data.get("datasets", {})

        if not datasets:
            return jsonify({"error": "Missing datasets field"}), 400

        success, message = update_sync_settings(username, datasets)
        if success:
            logger.info(f"Sync settings updated for {username}")
            return jsonify({"ok": True, "message": message})
        return jsonify({"error": message}), 400

    @app.route("/api/table-subscriptions")
    @login_required
    def table_subscriptions_get():
        """Get per-table subscriptions for current user."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)
        subs = get_table_subscriptions(username)
        return jsonify(subs)

    @app.route("/api/table-subscriptions", methods=["POST"])
    @login_required
    def table_subscriptions_update():
        """Update per-table subscriptions for current user."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        data = request.get_json(silent=True) or {}
        table_mode = data.get("table_mode", "all")
        tables = data.get("tables", {})

        if table_mode not in ("all", "explicit"):
            return jsonify({"error": "table_mode must be 'all' or 'explicit'"}), 400

        success, message = update_table_subscriptions(username, table_mode, tables)
        if success:
            logger.info(f"Table subscriptions updated for {username}")
            return jsonify({"ok": True, "message": message})
        return jsonify({"error": message}), 400

    # ─────────────────────────────────────────────────────────────────
    # Corporate Memory routes
    # ─────────────────────────────────────────────────────────────────

    @app.route("/corporate-memory")
    @login_required
    def corporate_memory():
        """Corporate Memory knowledge browser page."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        # Get stats for header
        stats = get_memory_stats()
        user_stats = get_memory_user_stats(username)

        # Get user's votes for highlighting
        user_votes = get_user_votes(username)

        # Get initial page of knowledge
        knowledge = get_knowledge(page=0, per_page=20)

        return render_template(
            "corporate_memory.html",
            stats=stats,
            user_stats=user_stats,
            user_votes=user_votes,
            knowledge=knowledge,
        )

    # ─────────────────────────────────────────────────────────────────
    # Activity Center routes
    # ─────────────────────────────────────────────────────────────────

    @app.route("/activity-center")
    @login_required
    def activity_center():
        """Activity Center page - enterprise data intelligence overview."""
        activity = {}
        return render_template("activity_center.html", activity=activity)

    @app.route("/api/corporate-memory/knowledge")
    @login_required
    def api_corporate_memory_knowledge():
        """Get knowledge items with optional filtering."""
        category = request.args.get("category")
        search = request.args.get("search")
        page = request.args.get("page", 0, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        sort = request.args.get("sort", "score")
        my_rules = request.args.get("my_rules", "").lower() == "true"

        # Get username for my_rules filter
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        # Limit per_page to reasonable maximum
        per_page = min(per_page, 100)

        result = get_knowledge(
            category=category,
            search=search,
            page=page,
            per_page=per_page,
            sort=sort,
            username=username,
            my_rules=my_rules,
        )
        return jsonify(result)

    @app.route("/api/corporate-memory/stats")
    @login_required
    def api_corporate_memory_stats():
        """Get corporate memory statistics for dashboard."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        stats = get_memory_stats()
        user_stats = get_memory_user_stats(username)

        return jsonify({
            **stats,
            **user_stats,
        })

    @app.route("/api/corporate-memory/vote", methods=["POST"])
    @login_required
    def api_corporate_memory_vote():
        """Vote on a knowledge item."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        data = request.get_json(silent=True) or {}
        item_id = data.get("item_id")
        vote_value = data.get("vote", 0)

        if not item_id:
            return jsonify({"error": "Missing item_id"}), 400

        try:
            vote_value = int(vote_value)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid vote value"}), 400

        success, message = memory_vote(username, item_id, vote_value)
        if success:
            return jsonify({"ok": True, "message": message})
        return jsonify({"error": message}), 400

    @app.route("/api/corporate-memory/my-votes")
    @login_required
    def api_corporate_memory_my_votes():
        """Get current user's votes."""
        user = session.get("user", {})
        email = user.get("email", "")
        username = get_username_from_email(email)

        votes = get_user_votes(username)
        return jsonify({"votes": votes})

    # ─────────────────────────────────────────────────────────────────
    # Admin pages
    # ─────────────────────────────────────────────────────────────────

    @app.route("/admin/tables")
    @login_required
    @admin_required
    def admin_tables():
        """Admin table management page."""
        return render_template("admin_tables.html")

    # ─────────────────────────────────────────────────────────────────
    # Admin API routes
    # ─────────────────────────────────────────────────────────────────

    @app.route("/api/admin/discover-tables")
    @login_required
    @admin_required
    def admin_discover_tables():
        """Discover all available tables from the data source."""
        try:
            from src.data_sync import create_data_source

            ds = create_data_source()
            raw_tables = ds.discover_tables()

            # Check which tables are already registered
            registered_ids = set()
            try:
                from src.table_registry import TableRegistry
                registry = TableRegistry.default()
                registered_ids = {t["id"] for t in registry.list_tables()}
            except Exception:
                pass

            # Group by bucket
            buckets: dict = {}
            for t in raw_tables:
                bid = t.get("bucket_id", "other")
                if bid not in buckets:
                    buckets[bid] = {
                        "bucket_id": bid,
                        "bucket_name": t.get("bucket_name", bid),
                        "tables": [],
                    }
                t["is_registered"] = t["id"] in registered_ids
                buckets[bid]["tables"].append(t)

            return jsonify({
                "ok": True,
                "total": len(raw_tables),
                "buckets": list(buckets.values()),
            })

        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/registry")
    @login_required
    @admin_required
    def admin_registry_list():
        """Return the full table registry."""
        try:
            from src.table_registry import TableRegistry

            registry = TableRegistry.default()
            return jsonify({
                "ok": True,
                "version": registry.version,
                "folder_mapping": registry.get_folder_mapping(),
                "tables": registry.list_tables(),
            })
        except Exception as e:
            logger.error(f"Registry list failed: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/register-table", methods=["POST"])
    @login_required
    @admin_required
    def admin_register_table():
        """Register a new table from discovery results."""
        from src.table_registry import ConflictError, TableRegistry

        user = session.get("user", {})
        email = user.get("email", "")

        data = request.get_json(silent=True) or {}
        if not data.get("id"):
            return jsonify({"error": "Missing table 'id'"}), 400

        try:
            registry = TableRegistry.default()
            registry.register_table(
                table_def=data,
                registered_by=email,
                expected_version=data.get("version"),
            )

            # Regenerate data_description.md
            docs_path = Path(os.path.dirname(__file__)) / ".." / "docs" / "data_description.md"
            registry.generate_data_description_md(docs_path.resolve())

            return jsonify({"ok": True, "version": registry.version})

        except ConflictError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Register table failed: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/registry/<path:table_id>", methods=["PUT"])
    @login_required
    @admin_required
    def admin_update_table(table_id):
        """Update configuration of a registered table."""
        from src.table_registry import ConflictError, TableRegistry

        user = session.get("user", {})
        email = user.get("email", "")

        data = request.get_json(silent=True) or {}

        try:
            registry = TableRegistry.default()
            registry.update_table(
                table_id=table_id,
                updates=data,
                updated_by=email,
                expected_version=data.pop("version", None),
            )

            # Regenerate data_description.md
            docs_path = Path(os.path.dirname(__file__)) / ".." / "docs" / "data_description.md"
            registry.generate_data_description_md(docs_path.resolve())

            return jsonify({"ok": True, "version": registry.version})

        except ConflictError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Update table failed: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/admin/registry/<path:table_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def admin_unregister_table(table_id):
        """Unregister a table and clean up subscriptions."""
        from src.table_registry import ConflictError, TableRegistry

        user = session.get("user", {})
        email = user.get("email", "")

        data = request.get_json(silent=True) or {}

        try:
            registry = TableRegistry.default()

            # Get table name before deletion (for subscription cleanup)
            table_info = registry.get_table(table_id)
            table_name = table_info["name"] if table_info else None

            registry.unregister_table(
                table_id=table_id,
                unregistered_by=email,
                expected_version=data.get("version"),
            )

            # Clean up per-user subscriptions for removed table
            if table_name:
                try:
                    _cleanup_table_subscriptions(table_name)
                except Exception as ce:
                    logger.warning(f"Subscription cleanup for {table_name} failed: {ce}")

            # Regenerate data_description.md
            docs_path = Path(os.path.dirname(__file__)) / ".." / "docs" / "data_description.md"
            registry.generate_data_description_md(docs_path.resolve())

            return jsonify({"ok": True, "version": registry.version})

        except ConflictError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Unregister table failed: {e}")
            return jsonify({"error": str(e)}), 500

    def _cleanup_table_subscriptions(table_name: str) -> None:
        """Remove a table from all users' per-table subscriptions."""
        from webapp.sync_settings_service import _read_json, _write_json, SYNC_SETTINGS_FILE

        all_settings = _read_json(SYNC_SETTINGS_FILE)
        changed = False
        for username, user_data in all_settings.items():
            tables = user_data.get("tables", {})
            if table_name in tables:
                del tables[table_name]
                changed = True
        if changed:
            _write_json(SYNC_SETTINGS_FILE, all_settings)
            logger.info(f"Cleaned up subscriptions for removed table: {table_name}")

    @app.route("/health")
    def health():
        """
        Health check endpoint for monitoring.

        Returns detailed status of services, disk, load, and recent activity.
        Returns 200 if healthy, 503 if degraded.
        """
        from webapp.health_service import health_check

        response, status_code = health_check()
        return response, status_code

    @app.errorhandler(404)
    def not_found(e):
        """Handle 404 errors."""
        return render_template("error.html", error="Page not found", code=404), 404

    @app.errorhandler(500)
    def server_error(e):
        """Handle 500 errors."""
        logger.exception("Server error")
        return render_template("error.html", error="Internal server error", code=500), 500


# Create the app instance for Gunicorn
app = create_app()


if __name__ == "__main__":
    # Development server
    app.run(debug=True, host="127.0.0.1", port=5000)
