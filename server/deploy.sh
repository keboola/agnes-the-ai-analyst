#!/bin/bash
# Deploy script for Data Analyst application
# This script is called by GitHub Actions or manually to deploy updates

set -euo pipefail

APP_DIR="/opt/data-analyst"
REPO_DIR="${APP_DIR}/repo"
VENV_DIR="${APP_DIR}/.venv"
LOG_DIR="${APP_DIR}/logs"
DEPLOY_LOG="${LOG_DIR}/deploy.log"

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

# Update server management scripts
log "Updating server management scripts..."
for script in "${REPO_DIR}"/server/bin/*; do
    if [[ -f "$script" ]]; then
        script_name=$(basename "$script")
        sudo /usr/bin/cp "$script" "/usr/local/bin/${script_name}"
        sudo /usr/bin/chmod 755 "/usr/local/bin/${script_name}"
        log "  Updated /usr/local/bin/${script_name}"
    fi
done

# Update sudoers configurations
log "Updating sudoers configurations..."
for sudoers_file in "${REPO_DIR}"/server/sudoers-*; do
    if [[ -f "$sudoers_file" ]]; then
        sudoers_name=$(basename "$sudoers_file" | sed 's/sudoers-//')
        # Validate before installing
        if sudo /usr/sbin/visudo -cf "$sudoers_file" 2>/dev/null; then
            sudo /usr/bin/cp "$sudoers_file" "/etc/sudoers.d/${sudoers_name}"
            sudo /usr/bin/chmod 440 "/etc/sudoers.d/${sudoers_name}"
            log "  Updated /etc/sudoers.d/${sudoers_name}"
        else
            log "  WARNING: Invalid sudoers syntax in $sudoers_file, skipping"
        fi
    fi
done

# Update user scripts in /data/scripts
log "Updating scripts in /data/scripts/..."
sudo /usr/bin/mkdir -p /data/scripts
sudo /usr/bin/cp "${REPO_DIR}"/scripts/setup_views.sh /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/duckdb_manager.py /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/sync_data.sh /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/activate_venv.sh /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/README.md /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/sync_jira.sh /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/generate_user_sync_configs.py /data/scripts/
sudo /usr/bin/cp "${REPO_DIR}"/scripts/collect_session.py /data/scripts/
sudo /usr/bin/chmod -R 755 /data/scripts
sudo /usr/bin/chown -R deploy:data-ops /data/scripts
log "  Scripts updated in /data/scripts/"

# Update documentation in /data/docs
log "Updating documentation..."
sudo /usr/bin/mkdir -p /data/docs/setup
if [[ -f "${REPO_DIR}/docs/data_description.md" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}"/docs/data_description.md /data/docs/
fi
sudo /usr/bin/cp "${REPO_DIR}"/docs/GETTING_STARTED.md /data/docs/
if [[ -f "${REPO_DIR}/docs/notifications.md" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}"/docs/notifications.md /data/docs/
fi
if [[ -f "${REPO_DIR}/docs/jira_schema.md" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}"/docs/jira_schema.md /data/docs/
fi
sudo /usr/bin/cp "${REPO_DIR}"/docs/setup/bootstrap.yaml /data/docs/setup/
sudo /usr/bin/cp "${REPO_DIR}"/docs/setup/claude_md_template.txt /data/docs/setup/
sudo /usr/bin/cp "${REPO_DIR}"/docs/setup/claude_settings.json /data/docs/setup/
if [[ -d "${REPO_DIR}/docs/metrics" ]]; then
    sudo /usr/bin/cp -r "${REPO_DIR}"/docs/metrics /data/docs/
fi
# Note: schema.yml files are generated directly to DOCS_OUTPUT_DIR by data_sync.py
# Here we only copy static *.md files from datasets/
if [[ -d "${REPO_DIR}/docs/datasets" ]]; then
    sudo /usr/bin/mkdir -p /data/docs/datasets
    # Copy only .md files (glob expands before sudo)
    if compgen -G "${REPO_DIR}/docs/datasets/*.md" > /dev/null; then
        sudo /usr/bin/cp "${REPO_DIR}"/docs/datasets/*.md /data/docs/datasets/
    fi
    log "  Dataset docs (*.md) copied to /data/docs/datasets/"
fi
sudo /usr/bin/chmod -R 775 /data/docs
sudo /usr/bin/chown -R deploy:data-ops /data/docs
log "  Documentation updated in /data/docs/"

# Deploy notify-runner to /usr/local/bin
log "Deploying notify-runner..."
if [[ -f "${REPO_DIR}/server/bin/notify-runner" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/bin/notify-runner" /usr/local/bin/notify-runner
    sudo /usr/bin/chmod 755 /usr/local/bin/notify-runner
    log "  Updated /usr/local/bin/notify-runner"
fi

# Deploy notify-scripts helper to /usr/local/bin
log "Deploying notify-scripts..."
if [[ -f "${REPO_DIR}/server/bin/notify-scripts" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/bin/notify-scripts" /usr/local/bin/notify-scripts
    sudo /usr/bin/chmod 755 /usr/local/bin/notify-scripts
    log "  Updated /usr/local/bin/notify-scripts"
fi

# Create notifications data directory
log "Setting up notifications directory..."
sudo /usr/bin/mkdir -p /data/notifications
sudo /usr/bin/chown deploy:data-ops /data/notifications
sudo /usr/bin/chmod 2770 /data/notifications  # setgid, no others access (socket is in /run/notify-bot/)

# Ensure deploy user is in dataread group (needed for notify-bot socket group ownership)
if ! id -nG deploy | grep -qw dataread; then
    sudo /usr/sbin/usermod -a -G dataread deploy
    log "  Added deploy user to dataread group"
fi

# Create Jira webhook data directory (raw data, will be processed to parquet later)
log "Setting up Jira data directory..."
sudo /usr/bin/mkdir -p /data/src_data/raw/jira/issues
sudo /usr/bin/mkdir -p /data/src_data/raw/jira/webhook_events
sudo /usr/bin/mkdir -p /data/src_data/raw/jira/attachments
sudo /usr/bin/chown -R root:data-ops /data/src_data/raw/jira
sudo /usr/bin/chmod -R 2770 /data/src_data/raw/jira  # setgid, www-data (data-ops member) can write

# Create password auth data directory
log "Setting up password auth directory..."
sudo /usr/bin/mkdir -p /data/auth
sudo /usr/bin/chown www-data:data-ops /data/auth
sudo /usr/bin/chmod 2770 /data/auth  # setgid, www-data can write, no others access

# Create corporate memory data directory
log "Setting up corporate memory directory..."
sudo /usr/bin/mkdir -p /data/corporate-memory
sudo /usr/bin/chown deploy:data-ops /data/corporate-memory
sudo /usr/bin/chmod 2770 /data/corporate-memory  # setgid, deploy can write

# Create user sessions data directory
log "Setting up user sessions directory..."
sudo /usr/bin/mkdir -p /data/user_sessions
sudo /usr/bin/chown root:data-ops /data/user_sessions
sudo /usr/bin/chmod 2770 /data/user_sessions  # setgid, root writes, admins only

# Create staging directory for data sync (uses /tmp for faster I/O)
log "Setting up staging directory..."
sudo /usr/bin/mkdir -p /tmp/data_analyst_staging
sudo /usr/bin/chown root:data-ops /tmp/data_analyst_staging
sudo /usr/bin/chmod 2770 /tmp/data_analyst_staging  # setgid, data-ops can write

# Add read access to Jira attachments for analysts (dataread group)
if command -v setfacl &>/dev/null; then
    sudo /usr/bin/setfacl -R -m g:dataread:rx /data/src_data/raw/jira/attachments 2>/dev/null || true
    sudo /usr/bin/setfacl -R -d -m g:dataread:rx /data/src_data/raw/jira/attachments 2>/dev/null || true
    log "  ACL set for dataread group on Jira attachments"
fi

# Set ACL for private data directory (data-private group only, remove dataread)
if command -v setfacl &>/dev/null; then
    sudo /usr/bin/setfacl -R -m g:data-private:rx /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -d -m g:data-private:rx /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -x g:dataread /data/src_data/parquet/private/ 2>/dev/null || true
    sudo /usr/bin/setfacl -R -d -x g:dataread /data/src_data/parquet/private/ 2>/dev/null || true
    log "  ACL set for data-private group on private parquet directory"
fi

# Deploy notification bot systemd service
log "Deploying notify-bot service..."
if [[ -f "${REPO_DIR}/server/notify-bot.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/notify-bot.service" /etc/systemd/system/notify-bot.service
    sudo /usr/bin/systemctl daemon-reload
fi

# Deploy WebSocket gateway systemd service
log "Deploying ws-gateway service..."
if [[ -f "${REPO_DIR}/server/ws-gateway.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/ws-gateway.service" /etc/systemd/system/ws-gateway.service
    sudo /usr/bin/systemctl daemon-reload
fi

# Deploy corporate memory systemd service and timer
log "Deploying corporate-memory service and timer..."
if [[ -f "${REPO_DIR}/server/corporate-memory.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/corporate-memory.service" /etc/systemd/system/corporate-memory.service
    sudo /usr/bin/cp "${REPO_DIR}/server/corporate-memory.timer" /etc/systemd/system/corporate-memory.timer
    sudo /usr/bin/systemctl daemon-reload
fi

# Deploy Jira SLA polling systemd service and timer
log "Deploying jira-sla-poll service and timer..."
if [[ -f "${REPO_DIR}/server/jira-sla-poll.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/jira-sla-poll.service" /etc/systemd/system/jira-sla-poll.service
    sudo /usr/bin/cp "${REPO_DIR}/server/jira-sla-poll.timer" /etc/systemd/system/jira-sla-poll.timer
    sudo /usr/bin/systemctl daemon-reload
fi

# Deploy Jira consistency monitoring systemd service and timers
log "Deploying jira-consistency service and timers..."
if [[ -f "${REPO_DIR}/server/jira-consistency.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/jira-consistency.service" /etc/systemd/system/jira-consistency.service
    sudo /usr/bin/cp "${REPO_DIR}/server/jira-consistency.timer" /etc/systemd/system/jira-consistency.timer
    sudo /usr/bin/cp "${REPO_DIR}/server/jira-consistency-deep.timer" /etc/systemd/system/jira-consistency-deep.timer
    sudo /usr/bin/systemctl daemon-reload

    # Create log file with correct permissions
    sudo /usr/bin/touch /opt/data-analyst/logs/jira-consistency.log
    sudo /usr/bin/chown root:data-ops /opt/data-analyst/logs/jira-consistency.log
    sudo /usr/bin/chmod 664 /opt/data-analyst/logs/jira-consistency.log
fi

# Deploy session collector systemd service and timer
log "Deploying session-collector service and timer..."
if [[ -f "${REPO_DIR}/server/session-collector.service" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/session-collector.service" /etc/systemd/system/session-collector.service
    sudo /usr/bin/cp "${REPO_DIR}/server/session-collector.timer" /etc/systemd/system/session-collector.timer
    sudo /usr/bin/systemctl daemon-reload
fi

# Deploy example notification scripts to /data/examples
log "Deploying example notification scripts..."
sudo /usr/bin/mkdir -p /data/examples/notifications
for example in "${REPO_DIR}"/examples/notifications/*.py; do
    if [[ -f "$example" ]]; then
        sudo /usr/bin/cp "$example" /data/examples/notifications/
    fi
done
sudo /usr/bin/chmod -R 755 /data/examples
sudo /usr/bin/chown -R deploy:data-ops /data/examples

# Update resource limits configuration
log "Updating resource limits..."
if [[ -f "${REPO_DIR}/server/limits-users.conf" ]]; then
    sudo /usr/bin/cp "${REPO_DIR}/server/limits-users.conf" /etc/security/limits.d/99-users.conf
    sudo /usr/bin/chmod 644 /etc/security/limits.d/99-users.conf
    log "  Updated /etc/security/limits.d/99-users.conf"
fi

# Create data sync .env file from environment variables (passed from GitHub Actions)
KEBOOLA_ENV_FILE="${REPO_DIR}/.env"
if [[ -n "${KEBOOLA_STORAGE_TOKEN:-}" ]]; then
    log "Creating data sync .env file..."
    {
        echo "KEBOOLA_STORAGE_TOKEN=${KEBOOLA_STORAGE_TOKEN}"
        echo "KEBOOLA_STACK_URL=${KEBOOLA_STACK_URL}"
        echo "KEBOOLA_PROJECT_ID=${KEBOOLA_PROJECT_ID}"
        echo "DATA_DIR=${DATA_DIR}"
        echo "DATA_SOURCE=${DATA_SOURCE}"
        echo "LOG_LEVEL=${LOG_LEVEL}"
        if [[ -n "${DOCS_OUTPUT_DIR:-}" ]]; then
            echo "DOCS_OUTPUT_DIR=${DOCS_OUTPUT_DIR}"
        fi
        if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
            echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        fi
        if [[ -n "${DESKTOP_JWT_SECRET:-}" ]]; then
            echo "DESKTOP_JWT_SECRET=${DESKTOP_JWT_SECRET}"
        fi
        if [[ -n "${SENDGRID_API_KEY:-}" ]]; then
            echo "SENDGRID_API_KEY=${SENDGRID_API_KEY}"
        fi
        if [[ -n "${JIRA_SLA_EMAIL:-}" ]]; then
            echo "JIRA_SLA_EMAIL=${JIRA_SLA_EMAIL}"
        fi
        if [[ -n "${JIRA_SLA_API_TOKEN:-}" ]]; then
            echo "JIRA_SLA_API_TOKEN=${JIRA_SLA_API_TOKEN}"
        fi
        if [[ -n "${JIRA_CLOUD_ID:-}" ]]; then
            echo "JIRA_CLOUD_ID=${JIRA_CLOUD_ID}"
        fi
        if [[ -n "${EMAIL_FROM_ADDRESS:-}" ]]; then
            echo "EMAIL_FROM_ADDRESS=${EMAIL_FROM_ADDRESS}"
        fi
        if [[ -n "${EMAIL_FROM_NAME:-}" ]]; then
            echo "EMAIL_FROM_NAME=${EMAIL_FROM_NAME}"
        fi
        if [[ -n "${ALLOWED_EMAILS:-}" ]]; then
            echo "ALLOWED_EMAILS=${ALLOWED_EMAILS}"
        fi
        if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
            echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
        fi
    } | sudo /usr/bin/tee "$KEBOOLA_ENV_FILE" > /dev/null
    sudo /usr/bin/chown root:data-ops "$KEBOOLA_ENV_FILE"
    sudo /usr/bin/chmod 640 "$KEBOOLA_ENV_FILE"
    log "  Data sync .env created with secure permissions (640)"
else
    log "  Skipping data sync .env creation (no KEBOOLA_STORAGE_TOKEN provided)"
fi

# Set correct permissions
log "Setting permissions..."
sudo /usr/bin/chown -R root:data-ops "$APP_DIR"
sudo /usr/bin/chmod -R 770 "$APP_DIR"  # owner+group rwx, others none
sudo /usr/bin/chmod -R g+s "$APP_DIR"  # setgid for new files

# Restore .env permissions (may have been overwritten by chmod -R)
if [[ -f "$KEBOOLA_ENV_FILE" ]]; then
    sudo /usr/bin/chmod 640 "$KEBOOLA_ENV_FILE"
fi

# Update and restart webapp if running
if systemctl is-active --quiet webapp 2>/dev/null || systemctl is-enabled --quiet webapp 2>/dev/null; then
    log "Updating webapp service..."
    sudo /usr/bin/cp "${REPO_DIR}/server/webapp.service" /etc/systemd/system/webapp.service
    sudo /usr/bin/systemctl daemon-reload
    log "Restarting webapp..."
    sudo /usr/bin/systemctl restart webapp
fi

# Restart notify-bot if running
if systemctl is-active --quiet notify-bot 2>/dev/null; then
    log "Restarting notify-bot..."
    sudo /usr/bin/systemctl restart notify-bot
elif [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    log "Starting notify-bot service..."
    sudo /usr/bin/systemctl enable notify-bot
    sudo /usr/bin/systemctl start notify-bot
fi

# Restart ws-gateway if running
if systemctl is-active --quiet ws-gateway 2>/dev/null; then
    log "Restarting ws-gateway..."
    sudo /usr/bin/systemctl restart ws-gateway
elif [[ -n "${DESKTOP_JWT_SECRET:-}" ]]; then
    log "Starting ws-gateway service..."
    sudo /usr/bin/systemctl enable ws-gateway
    sudo /usr/bin/systemctl start ws-gateway
fi

# Enable corporate-memory timer if ANTHROPIC_API_KEY is set
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    if ! systemctl is-enabled --quiet corporate-memory.timer 2>/dev/null; then
        log "Enabling corporate-memory timer..."
        sudo /usr/bin/systemctl enable corporate-memory.timer
        sudo /usr/bin/systemctl start corporate-memory.timer
    fi
fi

# Enable jira-sla-poll timer if JIRA_SLA_API_TOKEN is set
if [[ -n "${JIRA_SLA_API_TOKEN:-}" ]]; then
    if ! systemctl is-enabled --quiet jira-sla-poll.timer 2>/dev/null; then
        log "Enabling jira-sla-poll timer..."
        sudo /usr/bin/systemctl enable jira-sla-poll.timer
        sudo /usr/bin/systemctl start jira-sla-poll.timer
    fi
fi

# Enable jira-consistency timers (always enabled if Jira credentials are configured)
if [[ -f "/opt/data-analyst/.env" ]] && grep -q "JIRA_API_TOKEN" /opt/data-analyst/.env 2>/dev/null; then
    if ! systemctl is-enabled --quiet jira-consistency.timer 2>/dev/null; then
        log "Enabling jira-consistency timer..."
        sudo /usr/bin/systemctl enable jira-consistency.timer
        sudo /usr/bin/systemctl start jira-consistency.timer
    fi
    if ! systemctl is-enabled --quiet jira-consistency-deep.timer 2>/dev/null; then
        log "Enabling jira-consistency-deep timer..."
        sudo /usr/bin/systemctl enable jira-consistency-deep.timer
        sudo /usr/bin/systemctl start jira-consistency-deep.timer
    fi
fi

# Enable session-collector timer
if ! systemctl is-enabled --quiet session-collector.timer 2>/dev/null; then
    log "Enabling session-collector timer..."
    sudo /usr/bin/systemctl enable session-collector.timer
    sudo /usr/bin/systemctl start session-collector.timer
fi

log "Deployment completed successfully! (v4)"
