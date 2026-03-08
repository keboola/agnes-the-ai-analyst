# Deployment Guide

## Server Requirements

- Debian 12 / Ubuntu 22.04+
- 2+ vCPUs, 2+ GB RAM
- 10+ GB data disk
- Public IP with DNS

## Initial Server Setup

1. Provision a VM (GCP, AWS, Azure, etc.)

2. Run the setup script:
   ```bash
   sudo bash server/setup.sh
   ```

   This creates:
   - System groups: `data-ops`, `dataread`, `data-private`
   - Deploy user with appropriate permissions
   - Directory structure under `/opt/data-analyst/`
   - Python virtual environment

3. Set up the webapp:
   ```bash
   sudo bash server/webapp-setup.sh
   ```

   This installs:
   - Gunicorn systemd service
   - Nginx reverse proxy with SSL
   - Log rotation

## CI/CD Pipeline

1. Copy the example workflow:
   ```bash
   cp .github/workflows/deploy.yml.example .github/workflows/deploy.yml
   ```

2. Configure GitHub Secrets:
   - `SERVER_HOST`: Server IP address
   - `SERVER_USER`: Deploy username
   - `SERVER_SSH_KEY`: Deploy SSH private key
   - All environment variables from `.env`

3. Push to `main` branch triggers automatic deployment.

## Directory Structure on Server

```
/opt/data-analyst/
├── repo/           # Git clone of this repository
├── .env            # Environment variables (secrets)
├── .venv/          # Python virtual environment
└── logs/           # Application logs

/data/
├── src_data/
│   ├── parquet/    # Converted data files
│   ├── metadata/   # Sync state, profiles
│   └── raw/        # Raw source data
├── docs/           # Documentation served to analysts
├── scripts/        # Scripts distributed to analysts
└── notifications/  # Notification system data
```

## Separate Config Repository

For production deployments, keep instance config in a separate private repository:

```
client-config-repo/
├── config/
│   ├── instance.yaml
│   └── data_description.md
├── .env.example
└── .github/workflows/deploy.yml
```

Set `CONFIG_DIR=/opt/data-analyst/client-config/config/` in the environment.

## SSL Setup

Use certbot for Let's Encrypt SSL:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d data.yourcompany.com
```

## Monitoring

- Health check: `GET /health`
- Logs: `journalctl -u webapp -f`
- Disk usage: `df -h /data`
