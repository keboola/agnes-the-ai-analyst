#!/bin/bash
# Deploy script for Data Analyst application
# This script is called by GitHub Actions or manually to deploy updates.
#
# Works with any data source (Keboola, BigQuery, etc.) — instance-specific
# configuration comes from instance.yaml and GHA secrets, not from this script.
#
# Usage:
#   bash server/deploy.sh                    # Full deploy (from GHA or manually)
#   bash server/deploy.sh --scripts-only     # Only update /data/scripts and /data/docs

set -euo pipefail

APP_DIR="/opt/data-analyst"
REPO_DIR="${APP_DIR}/repo"
VENV_DIR="${APP_DIR}/.venv"
LOG_DIR="${APP_DIR}/logs"
DEPLOY_LOG="${LOG_DIR}/deploy.log"

# Parse arguments
SCRIPTS_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --scripts-only) SCRIPTS_ONLY=true ;;
    esac
done

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$DEPLOY_LOG"
}

error() {
    log "ERROR: $*"
    exit 1
}

# Check if running as user with access to APP_DIR
if [[ ! -w "$APP_DIR" ]]; then
    error "No write access to $APP_DIR. Are you in the data-ops group?"
fi

log "Starting deployment..."

cd "$REPO_DIR" || error "Cannot cd to $REPO_DIR"

# Ensure git trusts this directory
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true

if [[ "$SCRIPTS_ONLY" == false ]]; then
    # Pull latest changes
    log "Pulling latest changes from origin/main..."
    git fetch origin
    git reset --hard origin/main

    # Update Python dependencies if requirements.txt changed
    if git diff HEAD@{1} --name-only 2>/dev/null | grep -q "requirements.txt"; then
        log "requirements.txt changed, updating dependencies..."
        source "${VENV_DIR}/bin/activate"
        pip install -q -r requirements.txt
        deactivate
    fi
fi

# --- Core: scripts and docs (always runs) ---

# Update server management scripts (add-analyst, list-analysts, etc.)
log "Updating server management scripts..."
if compgen -G "${REPO_DIR}/server/bin/*" > /dev/null 2>&1; then
    for script in "${REPO_DIR}"/server/bin/*; do
        if [[ -f "$script" ]]; then
            script_name=$(basename "$script")
            sudo /usr/bin/cp "$script" "/usr/local/bin/${script_name}"
            sudo /usr/bin/chmod 755 "/usr/local/bin/${script_name}"
            log "  Updated /usr/local/bin/${script_name}"
        fi
    done
fi

# Update sudoers configurations
log "Updating sudoers configurations..."
for sudoers_file in "${REPO_DIR}"/server/sudoers-*; do
    if [[ -f "$sudoers_file" ]]; then
        sudoers_name=$(basename "$sudoers_file" | sed 's/sudoers-//')
        if sudo /usr/sbin/visudo -cf "$sudoers_file" 2>/dev/null; then
            sudo /usr/bin/cp "$sudoers_file" "/etc/sudoers.d/${sudoers_name}"
            sudo /usr/bin/chmod 440 "/etc/sudoers.d/${sudoers_name}"
            log "  Updated /etc/sudoers.d/${sudoers_name}"
        else
            log "  WARNING: Invalid sudoers syntax in $sudoers_file, skipping"
        fi
    fi
done

# Update user-facing scripts in /data/scripts
# These are synced to analyst machines via sync_data.sh
log "Updating scripts in /data/scripts/..."
sudo /usr/bin/mkdir -p /data/scripts
for script_file in setup_views.sh duckdb_manager.py sync_data.sh activate_venv.sh \
                   README.md generate_user_sync_configs.py collect_session.py; do
    if [[ -f "${REPO_DIR}/scripts/${script_file}" ]]; then
        sudo /usr/bin/cp "${REPO_DIR}/scripts/${script_file}" /data/scripts/
    fi
done
# Copy connector-specific sync scripts (e.g. sync_jira.sh) if they exist
for sync_script in "${REPO_DIR}"/connectors/*/scripts/sync_*.sh "${REPO_DIR}"/scripts/sync_*.sh; do
    if [[ -f "$sync_script" ]]; then
        sudo /usr/bin/cp "$sync_script" /data/scripts/
    fi
done
sudo /usr/bin/chmod -R 755 /data/scripts
sudo /usr/bin/chown -R root:data-ops /data/scripts
log "  Scripts updated in /data/scripts/"

# Update documentation in /data/docs
log "Updating documentation..."
sudo /usr/bin/mkdir -p /data/docs/setup
# Core docs (copy if they exist)
for doc_file in data_description.md GETTING_STARTED.md notifications.md jira_schema.md schema.yml; do
    if [[ -f "${REPO_DIR}/docs/${doc_file}" ]]; then
        sudo /usr/bin/cp "${REPO_DIR}/docs/${doc_file}" /data/docs/
    fi
done
# Setup docs
for setup_file in bootstrap.yaml claude_md_template.txt claude_settings.json; do
    if [[ -f "${REPO_DIR}/docs/setup/${setup_file}" ]]; then
        sudo /usr/bin/cp "${REPO_DIR}/docs/setup/${setup_file}" /data/docs/setup/
    fi
done
# Metrics definitions
if [[ -d "${REPO_DIR}/docs/metrics" ]]; then
    sudo /usr/bin/cp -r "${REPO_DIR}"/docs/metrics /data/docs/
fi
# Dataset documentation
if [[ -d "${REPO_DIR}/docs/datasets" ]]; then
    sudo /usr/bin/mkdir -p /data/docs/datasets
    if compgen -G "${REPO_DIR}/docs/datasets/*.md" > /dev/null; then
        sudo /usr/bin/cp "${REPO_DIR}"/docs/datasets/*.md /data/docs/datasets/
    fi
    log "  Dataset docs copied to /data/docs/datasets/"
fi
sudo /usr/bin/chmod -R 775 /data/docs
sudo /usr/bin/chown -R root:data-ops /data/docs
log "  Documentation updated in /data/docs/"

# Deploy examples (notifications, queries, etc.)
log "Deploying examples..."
if [[ -d "${REPO_DIR}/examples" ]]; then
    sudo /usr/bin/mkdir -p /data/examples
    sudo /usr/bin/cp -r "${REPO_DIR}"/examples/* /data/examples/ 2>/dev/null || true
    sudo /usr/bin/chmod -R 755 /data/examples
    sudo /usr/bin/chown -R root:data-ops /data/examples
fi

if [[ "$SCRIPTS_ONLY" == true ]]; then
    log "Scripts-only deployment completed successfully!"
    exit 0
fi

# --- Optional: server management scripts ---

# Deploy helper binaries to /usr/local/bin (notify-runner, notify-scripts, etc.)
for bin_file in "${REPO_DIR}"/server/bin/*; do
    if [[ -f "$bin_file" ]]; then
        bin_name=$(basename "$bin_file")
        sudo /usr/bin/cp "$bin_file" "/usr/local/bin/${bin_name}"
        sudo /usr/bin/chmod 755 "/usr/local/bin/${bin_name}"
        log "  Updated /usr/local/bin/${bin_name}"
    fi
done

# --- Optional: data directories (created only if relevant features exist) ---

# Notifications directory
if [[ -f "${REPO_DIR}/server/bin/notify-runner" ]] || [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    log "Setting up notifications directory..."
    sudo /usr/bin/mkdir -p /data/notifications
    sudo /usr/bin/chown root:data-ops /data/notifications
    sudo /usr/bin/chmod 2770 /data/notifications
fi

# Jira data directory (only if Jira connector exists)
if [[ -d "${REPO_DIR}/connectors/jira" ]]; then
    log "Setting up Jira data directory..."
    sudo /usr/bin/mkdir -p /data/src_data/raw/jira/issues
    sudo /usr/bin/mkdir -p /data/src_data/raw/jira/webhook_events
    sudo /usr/bin/mkdir -p /data/src_data/raw/jira/attachments
    sudo /usr/bin/chown -R root:data-ops /data/src_data/raw/jira
    sudo /usr/bin/chmod -R 2770 /data/src_data/raw/jira
    # ACL for read access by analysts
    if command -v setfacl &>/dev/null; then
        sudo /usr/bin/setfacl -R -m g:dataread:rx /data/src_data/raw/jira/attachments 2>/dev/null || true
        sudo /usr/bin/setfacl -R -d -m g:dataread:rx /data/src_data/raw/jira/attachments 2>/dev/null || true
    fi
fi

# Password auth directory (only if password auth module exists)
if [[ -f "${REPO_DIR}/auth/password.py" ]]; then
    log "Setting up password auth directory..."
    sudo /usr/bin/mkdir -p /data/auth
    sudo /usr/bin/chown www-data:data-ops /data/auth
    sudo /usr/bin/chmod 2770 /data/auth
fi

# Corporate memory directory
if [[ -d "${REPO_DIR}/services/corporate-memory" ]]; then
    log "Setting up corporate memory directory..."
    sudo /usr/bin/mkdir -p /data/corporate-memory
    sudo /usr/bin/chown root:data-ops /data/corporate-memory
    sudo /usr/bin/chmod 2770 /data/corporate-memory
fi

# User sessions directory
log "Setting up user sessions directory..."
sudo /usr/bin/mkdir -p /data/user_sessions
sudo /usr/bin/chown root:data-ops /data/user_sessions
sudo /usr/bin/chmod 2770 /data/user_sessions

# Private data ACL (only if private directory exists)
if [[ -d /data/src_data/parquet/private ]] && command -v setfacl &>/dev/null; then
    sudo /usr/bin/setfacl -R -m g:data-private:rx /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -d -m g:data-private:rx /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -x g:dataread /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -d -x g:dataread /data/src_data/parquet/private/ 2>/dev/null || true
    log "  ACL set for data-private group on private parquet directory"
fi

# --- Deploy systemd services and timers ---

log "Deploying systemd service and timer files..."
SYSTEMD_CHANGED=false
for unit_file in "${REPO_DIR}"/services/*/systemd/*.service "${REPO_DIR}"/services/*/systemd/*.timer \
                 "${REPO_DIR}"/connectors/*/systemd/*.service "${REPO_DIR}"/connectors/*/systemd/*.timer; do
    if [[ -f "$unit_file" ]]; then
        unit_name=$(basename "$unit_file")
        sudo /usr/bin/cp "$unit_file" "/etc/systemd/system/${unit_name}"
        log "  Installed /etc/systemd/system/${unit_name}"
        SYSTEMD_CHANGED=true
    fi
done
if [[ "$SYSTEMD_CHANGED" == "true" ]]; then
    sudo /usr/bin/systemctl daemon-reload
    log "  systemd daemon-reload completed"
fi

# Post-install hooks for specific services
if [[ -f "/etc/systemd/system/jira-consistency.service" ]]; then
    sudo /usr/bin/touch /opt/data-analyst/logs/jira-consistency.log
    sudo /usr/bin/chown root:data-ops /opt/data-analyst/logs/jira-consistency.log
    sudo /usr/bin/chmod 664 /opt/data-analyst/logs/jira-consistency.log
fi

# Update resource limits configuration
if [[ -f "${REPO_DIR}/server/limits-users.conf" ]]; then
    log "Updating resource limits..."
    sudo /usr/bin/cp "${REPO_DIR}/server/limits-users.conf" /etc/security/limits.d/99-users.conf
    sudo /usr/bin/chmod 644 /etc/security/limits.d/99-users.conf
fi

# --- Create .env for data sync (data-source agnostic) ---

SYNC_ENV_FILE="${REPO_DIR}/.env"

# Write all known env vars that are set (works for any data source)
log "Creating data sync .env file..."
{
    # Core settings (always written if set)
    for var in DATA_DIR DATA_SOURCE DOCS_OUTPUT_DIR LOG_LEVEL; do
        if [[ -n "${!var:-}" ]]; then
            echo "${var}=${!var}"
        fi
    done

    # Keboola data source
    for var in KEBOOLA_STORAGE_TOKEN KEBOOLA_STACK_URL KEBOOLA_PROJECT_ID; do
        if [[ -n "${!var:-}" ]]; then
            echo "${var}=${!var}"
        fi
    done

    # BigQuery data source
    for var in BIGQUERY_PROJECT BIGQUERY_LOCATION; do
        if [[ -n "${!var:-}" ]]; then
            echo "${var}=${!var}"
        fi
    done

    # OpenMetadata catalog
    for var in OPENMETADATA_TOKEN; do
        if [[ -n "${!var:-}" ]]; then
            echo "${var}=${!var}"
        fi
    done

    # Optional services (written only if set)
    for var in TELEGRAM_BOT_TOKEN DESKTOP_JWT_SECRET SENDGRID_API_KEY \
               JIRA_SLA_EMAIL JIRA_SLA_API_TOKEN JIRA_CLOUD_ID \
               EMAIL_FROM_ADDRESS EMAIL_FROM_NAME ALLOWED_EMAILS \
               ANTHROPIC_API_KEY; do
        if [[ -n "${!var:-}" ]]; then
            echo "${var}=${!var}"
        fi
    done
} | sudo /usr/bin/tee "$SYNC_ENV_FILE" > /dev/null

# Only set permissions if file has content
if [[ -s "$SYNC_ENV_FILE" ]]; then
    sudo /usr/bin/chown root:data-ops "$SYNC_ENV_FILE"
    sudo /usr/bin/chmod 640 "$SYNC_ENV_FILE"
    log "  Data sync .env created with secure permissions (640)"
else
    log "  No environment variables provided, .env is empty"
fi

# --- Set correct permissions ---

log "Setting permissions..."
sudo /usr/bin/chown -R root:data-ops "$APP_DIR"
sudo /usr/bin/chmod -R 770 "$APP_DIR"
sudo /usr/bin/chmod -R g+s "$APP_DIR"

# Restore .env permissions (may have been overwritten by chmod -R)
if [[ -f "$SYNC_ENV_FILE" ]]; then
    sudo /usr/bin/chmod 640 "$SYNC_ENV_FILE"
fi

# --- Restart services ---

# Webapp (always restart if running)
if systemctl is-active --quiet webapp 2>/dev/null || systemctl is-enabled --quiet webapp 2>/dev/null; then
    log "Updating webapp service..."
    if [[ -f "${REPO_DIR}/server/webapp.service" ]]; then
        sudo /usr/bin/cp "${REPO_DIR}/server/webapp.service" /etc/systemd/system/webapp.service
        sudo /usr/bin/systemctl daemon-reload
    fi
    log "Restarting webapp..."
    sudo /usr/bin/systemctl restart webapp
fi

# Optional services (restart only if already running or newly configured)
for svc in notify-bot ws-gateway; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        log "Restarting ${svc}..."
        sudo /usr/bin/systemctl restart "$svc"
    fi
done

# Enable notify-bot if Telegram token is newly provided
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && ! systemctl is-active --quiet notify-bot 2>/dev/null; then
    if [[ -f "/etc/systemd/system/notify-bot.service" ]]; then
        log "Starting notify-bot service..."
        sudo /usr/bin/systemctl enable notify-bot
        sudo /usr/bin/systemctl start notify-bot
    fi
fi

# Enable ws-gateway if JWT secret is newly provided
if [[ -n "${DESKTOP_JWT_SECRET:-}" ]] && ! systemctl is-active --quiet ws-gateway 2>/dev/null; then
    if [[ -f "/etc/systemd/system/ws-gateway.service" ]]; then
        log "Starting ws-gateway service..."
        sudo /usr/bin/systemctl enable ws-gateway
        sudo /usr/bin/systemctl start ws-gateway
    fi
fi

# Enable timers (only if service files exist)
for timer in corporate-memory session-collector jira-sla-poll jira-consistency jira-consistency-deep data-refresh catalog-refresh; do
    if [[ -f "/etc/systemd/system/${timer}.timer" ]]; then
        if ! systemctl is-enabled --quiet "${timer}.timer" 2>/dev/null; then
            log "Enabling ${timer} timer..."
            sudo /usr/bin/systemctl enable "${timer}.timer"
            sudo /usr/bin/systemctl start "${timer}.timer"
        fi
    fi
done

log "Deployment completed successfully! (v5)"
