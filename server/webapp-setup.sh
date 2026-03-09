#!/bin/bash
# Setup script for Data Analyst Web App
# Run this ONCE on the server to set up the web application
# Must be run as root or with sudo

set -euo pipefail

# Server hostname - required for SSL, Nginx, and OAuth configuration
if [[ -z "${SERVER_HOSTNAME:-}" ]]; then
    read -p "Enter server hostname (e.g., data.example.com): " DOMAIN
    if [[ -z "$DOMAIN" ]]; then
        echo "ERROR: SERVER_HOSTNAME is required"
        exit 1
    fi
else
    DOMAIN="$SERVER_HOSTNAME"
fi
APP_DIR="/opt/data-analyst"
REPO_DIR="${APP_DIR}/repo"
VENV_DIR="${APP_DIR}/.venv"
LOG_DIR="${APP_DIR}/logs"
ENV_FILE="${APP_DIR}/.env"

echo "=== Data Analyst Web App Setup ==="
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

# Check if main setup has been run
if [[ ! -d "$REPO_DIR" ]]; then
    echo "ERROR: Repository not found at $REPO_DIR"
    echo "Please run server/setup.sh first."
    exit 1
fi

# Install system dependencies
echo "Installing system dependencies..."
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

# Install Python dependencies for webapp
echo "Installing Python dependencies..."
source "${VENV_DIR}/bin/activate"
pip install flask authlib gunicorn
deactivate

# Create log files for webapp
echo "Creating log files..."
touch "${LOG_DIR}/webapp-access.log"
touch "${LOG_DIR}/webapp-error.log"
chown www-data:www-data "${LOG_DIR}/webapp-access.log" "${LOG_DIR}/webapp-error.log"

# Check/create .env file
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Creating .env file template..."
    cat > "$ENV_FILE" << 'EOF'
# Web App Configuration
# Generate secret key with: python -c "import secrets; print(secrets.token_hex(32))"
WEBAPP_SECRET_KEY=CHANGE_ME_GENERATE_A_SECURE_KEY

# Google OAuth credentials (from Google Cloud Console)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Server info (set these to your server's IP and hostname)
SERVER_HOST=CHANGE_ME_SET_SERVER_IP
SERVER_HOSTNAME=CHANGE_ME_SET_SERVER_HOSTNAME
EOF
    chmod 600 "$ENV_FILE"
    chown root:data-ops "$ENV_FILE"
    echo ""
    echo "IMPORTANT: Edit ${ENV_FILE} and add your Google OAuth credentials!"
    echo ""
else
    echo ".env file already exists at ${ENV_FILE}"
fi

# Add www-data to data-ops group for static file access
echo "Adding www-data to data-ops group..."
usermod -aG data-ops www-data

# Install sudoers rules for www-data (from repo, includes all required rules)
# Validate BEFORE copying to prevent broken sudo if syntax is invalid
echo "Configuring sudoers..."
SUDOERS_FILE="/etc/sudoers.d/webapp"
SUDOERS_SRC="${REPO_DIR}/server/sudoers-webapp"
if ! visudo -cf "$SUDOERS_SRC"; then
    echo "ERROR: Invalid sudoers syntax in $SUDOERS_SRC"
    exit 1
fi
install -m 440 "$SUDOERS_SRC" "$SUDOERS_FILE"

# Install systemd service
echo "Installing systemd service..."
cp "${REPO_DIR}/server/webapp.service" /etc/systemd/system/webapp.service
systemctl daemon-reload

# Install Nginx configuration
echo "Installing Nginx configuration..."
cp "${REPO_DIR}/server/webapp-nginx.conf" /etc/nginx/sites-available/webapp

# Remove default site if it exists
rm -f /etc/nginx/sites-enabled/default

# Enable webapp site
ln -sf /etc/nginx/sites-available/webapp /etc/nginx/sites-enabled/webapp

# Test Nginx config
if ! nginx -t; then
    echo "ERROR: Nginx configuration test failed"
    exit 1
fi

# Check if SSL certificate exists
if [[ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    echo ""
    echo "SSL certificate not found. Obtaining certificate..."
    echo ""
    echo "IMPORTANT: Make sure DNS A record for ${DOMAIN} points to this server!"
    echo ""
    read -p "Press Enter to continue or Ctrl+C to abort..."

    # Temporarily disable HTTPS server block
    sed -i 's/listen 443/# listen 443/g' /etc/nginx/sites-available/webapp
    systemctl reload nginx

    # Get certificate
    certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --email "${CERTBOT_EMAIL:-admin@${DOMAIN}}" --redirect

    # Re-enable HTTPS
    sed -i 's/# listen 443/listen 443/g' /etc/nginx/sites-available/webapp
fi

# Start services
echo "Starting services..."
systemctl enable webapp
systemctl start webapp || true  # May fail if .env not configured

systemctl reload nginx

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo ""
echo "1. Configure Google OAuth:"
echo "   - Go to Google Cloud Console -> APIs & Services -> Credentials"
echo "   - Create OAuth 2.0 Client ID (Web application)"
echo "   - Set Authorized JavaScript origins: https://${DOMAIN}"
echo "   - Set Authorized redirect URIs: https://${DOMAIN}/authorize"
echo ""
echo "2. Edit ${ENV_FILE}:"
echo "   - Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
echo "   - Generate and set WEBAPP_SECRET_KEY"
echo ""
echo "3. Restart the webapp:"
echo "   systemctl restart webapp"
echo ""
echo "4. Check status:"
echo "   systemctl status webapp"
echo "   systemctl status nginx"
echo ""
echo "5. Test the site:"
echo "   curl -I https://${DOMAIN}"
echo ""
