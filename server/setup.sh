#!/bin/bash
# Initial setup script for Data Analyst server
# Run this ONCE on the server to set up the environment
# Must be run as root or with sudo

set -euo pipefail

APP_DIR="/opt/data-analyst"
REPO_URL="${REPO_URL:-https://github.com/your-org/ai-data-analyst.git}"

echo "=== Data Analyst Server Setup ==="
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

# Install required system packages
echo "Installing required system packages..."
apt-get update -qq
apt-get install -y rsync
echo "  rsync installed"

# Create data-ops group if it doesn't exist
if ! getent group data-ops > /dev/null 2>&1; then
    echo "Creating data-ops group..."
    groupadd data-ops
fi

# Create directory structure
echo "Creating directory structure..."
mkdir -p "${APP_DIR}"/{repo,.venv,logs}

# Check repository
if [[ ! -d "${APP_DIR}/repo/.git" ]]; then
    echo "ERROR: Repository not found at ${APP_DIR}/repo"
    echo "Please clone it first as deploy user:"
    echo "  sudo -u deploy git clone \${REPO_URL} ${APP_DIR}/repo"
    exit 1
else
    echo "Repository found at ${APP_DIR}/repo"
fi

# Create Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv "${APP_DIR}/.venv"
source "${APP_DIR}/.venv/bin/activate"
pip install --upgrade pip
pip install -r "${APP_DIR}/repo/requirements.txt"
deactivate

# Install server management scripts
echo "Installing management scripts..."
for script in "${APP_DIR}/repo/server/bin"/*; do
    if [[ -f "$script" ]]; then
        script_name=$(basename "$script")
        cp "$script" "/usr/local/bin/${script_name}"
        chmod 755 "/usr/local/bin/${script_name}"
        echo "  Installed /usr/local/bin/${script_name}"
    fi
done

# Set permissions
echo "Setting permissions..."
chown -R root:data-ops "$APP_DIR"
chmod -R 775 "$APP_DIR"
chmod -R g+s "$APP_DIR"  # setgid so new files inherit group

# Create deploy log
touch "${APP_DIR}/logs/deploy.log"
chmod 664 "${APP_DIR}/logs/deploy.log"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Directory structure:"
echo "  ${APP_DIR}/repo/    - Git repository"
echo "  ${APP_DIR}/.venv/   - Python virtual environment"
echo "  ${APP_DIR}/logs/    - Application logs"
echo ""
echo "Management commands installed:"
echo "  add-admin     - Add server administrator"
echo "  add-analyst   - Add data analyst"
echo "  remove-analyst - Remove user"
echo "  list-analysts - List all analysts"
echo ""
echo "Next steps:"
echo "  1. Add existing admins to data-ops group:"
echo "     usermod -aG data-ops padak"
echo "     usermod -aG data-ops matejkys"
echo ""
echo "  2. Set up GitHub Actions deploy key (see .github/workflows/deploy.yml)"
echo ""
