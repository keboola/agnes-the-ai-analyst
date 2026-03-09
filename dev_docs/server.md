# Data Broker Server

Central server for distributing data to AI analytical systems.

## Basic Information

| Parameter | Value |
|-----------|-------|
| Name | your-server |
| GCP Project | your-gcp-project |
| Zone | europe-north1-a |
| Type | e2-medium |
| OS | Debian 12 (bookworm) |
| External IP | YOUR_SERVER_IP |

## Hardware

| Resource | Size |
|----------|------|
| RAM | 3.8 GB |
| Swap | 2 GB (/mnt/swapfile) |
| System disk (sda) | 10 GB - OS, packages, app (expendable) |
| Data disk (sdb) | 30 GB - /data, pd-balanced (snapshotted) |
| Home disk (sdc) | 30 GB - /home, pd-balanced (snapshotted) |
| Temp disk (sdd) | 100 GB - /tmp, pd-standard (not snapshotted) |

## Access

### SSH connection (admin)

```bash
ssh kids
```

Requires SSH config:
```
Host kids
  HostName YOUR_SERVER_IP
  User admin1
  IdentityFile ~/.ssh/google_compute_engine
```

Or via gcloud:
```bash
gcloud compute ssh your-server --project=your-gcp-project --zone=europe-north1-a
```

## Data Structure

```
/data/                      # Data disk (30 GB, pd-balanced)
├── lost+found/             # System directory
├── src_data/               # Source data (group: dataread, 750)
│   ├── raw/                # Raw data from Keboola (reserved for future use)
│   ├── parquet/            # Converted data (parquet format)
│   │   ├── sales/          # CRM data (in.c-crm bucket) - group: dataread
│   │   └── private/        # Private data - group: data-private
│   ├── metadata/           # Sync state, cache, profiles
│   │   ├── sync_state.json # Per-table sync stats (rows, columns, size)
│   │   └── profiles.json   # Data profiler output (mode 644, ~900 KB)
│   └── staging/            # Temporary processing (reserved for future use)
├── docs/                   # Documentation (deployed from repo)
│   └── schema.yml          # Auto-generated table schemas (from data sync)
├── scripts/                # Helper scripts (deployed from repo)
├── examples/               # Example notification scripts (admin1:data-ops, 755)
│   └── notifications/      # Example notification scripts for analysts
├── notifications/          # Notification data (deploy:data-ops, 2770 setgid)
│   ├── telegram_users.json # username -> {chat_id, linked_at} mapping
│   ├── desktop_users.json  # username -> {linked_at} mapping (desktop app link state)
│   ├── pending_codes.json  # temporary verification codes
│   └── bot.log             # Bot service log
├── auth/                   # Password auth data (www-data:data-ops, 2770 setgid)
│   └── users.json          # Hashed passwords and metadata
├── corporate-memory/       # Knowledge base data (deploy:data-ops, 2770 setgid)
│   ├── knowledge.json      # Collected knowledge items from CLAUDE.local.md files
│   ├── votes.json          # User votes on knowledge items
│   └── user_hashes.json    # MD5 hashes for change detection
└── user_sessions/          # Session collector data (root:data-ops, 2770 setgid)
    └── *.jsonl             # User session logs collected every 6 hours

/run/notify-bot/                # Systemd RuntimeDirectory (mode 0755)
└── bot.sock                    # Unix socket for send API (mode 0666)

/tmp/data_analyst_staging/              # Keboola staging directory (root:data-ops, 2770 setgid)
└── *.parquet                   # Temporary Parquet files during Keboola data load
```

### Folder Mapping

Parquet subfolders are mapped from Keboola bucket names in `docs/data_description.md`:

```yaml
folder_mapping:
  in.c-crm: sales        # CRM/Salesforce data
  in.c-private: private  # Private/sensitive data
```

This mapping is used by `src/config.py` to determine where to save Parquet files.

## Access Control

Three-tier permission model:

| Role | Groups | Access |
|------|--------|--------|
| **Standard Analyst** | `dataread` | Public data read-only |
| **Privileged Analyst** | `dataread` + `data-private` | Public + private data read-only |
| **Admin** | `sudo` + `google-sudoers` + `dataread` + `data-private` + `data-ops` | Full server access (NOPASSWD) + all data read/write + deployment |

- **Standard Analyst** - can read public data, sync via rsync, run scripts in their workspace
- **Privileged Analyst** - same as standard + access to private/sensitive data (executives, management)
- **Admin** - server administration, can add/remove users, has sudo privileges, full data access with write permissions, can deploy application updates

### Data Directory Permissions

Data in `/data/src_data/` uses ACL for granular access:

```
/data/src_data/          owner: admin1, group: data-ops
├── raw/                 data-ops: rwx, dataread: r-x
├── parquet/             data-ops: rwx, dataread: r-x
│   └── private/         data-ops: rwx, data-private: r-x
└── staging/             data-ops: rwx, dataread: r-x
```

- **Admins (data-ops)**: Full read/write access to prepare data
- **Analysts (dataread)**: Read-only access to consume data
- **Private data (data-private)**: Additional group for sensitive data access

**Atomic writes and ACL — required pattern:**

Directories under `/data/` use default ACLs (e.g., `default:group:data-ops:rwx`). Files created with `open()` inherit these correctly. However, `tempfile.mkstemp()` explicitly sets mode `0600`, which overrides the ACL mask to `---` and silently breaks group access for all other services.

**Always use `os.fchmod()` immediately after `mkstemp()`:**

```python
fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
os.fchmod(fd, 0o660)  # REQUIRED: restore ACL mask for group access
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, str(target))
except Exception:
    os.unlink(tmp_path)
    raise
```

Use `0o660` for files accessed by services via data-ops group ACL, `0o644` for world-readable files (e.g., profiler output). See [#203](https://github.com/your-org/ai-data-analyst/issues/203) for a production incident caused by missing `fchmod`.

**Per-issue file locking for concurrent writers:**

When multiple services write to the same JSON file (e.g., SLA poll and webhook handler both updating `/data/src_data/raw/jira/issues/SUPPORT-1234.json`), use advisory file locking to prevent races:

```python
from connectors.jira.file_lock import issue_json_lock

with issue_json_lock(issues_dir, issue_key):
    # read JSON, modify, atomic write, transform to Parquet
    ...
```

- Uses `fcntl.flock()` (POSIX advisory, blocking, exclusive)
- Lock files stored in `{issues_dir}/.locks/{issue_key}.lock`
- Different issue keys don't block each other (fine-grained locking)
- The lock must cover the entire read-modify-write **and** the Parquet transform — otherwise another writer could overwrite the JSON between write and transform, causing the transform to read stale data

Currently used by:
- `connectors/jira/scripts/poll_sla.py` — wraps SLA+status update + `transform_single_issue()`
- `connectors/jira/service.py` — wraps `save_issue()` JSON write + `trigger_incremental_transform()`, and `_handle_deletion()` read-modify-write + transform

Attachment downloads in `save_issue()` intentionally run **outside** the lock (can take tens of seconds and don't modify JSON).

## User Management

Each user has:
- Own Linux account with home directory `/home/username/`
- Server symlinks: `/home/username/server/` (read-only links to `/data/`)
- User workspace: `/home/username/user/` (writable: duckdb, notifications, artifacts, scripts, parquet)
- Notification state: `/home/username/.notifications/{state,logs}`
- SSH key authentication

### Management Commands

```bash
# Add standard analyst (public data only)
sudo add-analyst username "ssh-rsa AAAA... comment"

# Add privileged analyst (public + private data)
sudo add-analyst username "ssh-rsa AAAA... comment" --private

# Add server admin (sudo + all data)
sudo add-admin username "ssh-rsa AAAA... comment"

# List all analysts
list-analysts

# Remove user (interactive)
sudo remove-analyst username

# Remove user (non-interactive, e.g., via SSH)
sudo remove-analyst username --force
```

### Examples

```bash
# Regular analyst
sudo add-analyst novak "ssh-rsa AAAAB3... jan.novak@example.com"

# Executive with private data access
sudo add-analyst ceo "ssh-rsa AAAAB3... ceo@example.com" --private

# Server administrator
sudo add-admin admin2 "ssh-rsa AAAAB3... admin2@example.com"
sudo add-admin admin3 "ssh-ed25519 AAAAC3... admin3@your-domain.com"
```

Output for admin:
```
Admin admin2 created successfully
  - Added to group: sudo (server administration)
  - Added to group: dataread (public data access)
  - Added to group: data-private (private data access)
  - Added to group: data-ops (application deployment)
  - Added to resource limits (unlimited)
  - Workspace: /home/admin2/workspace
  - Data link: /home/admin2/data -> /data/src_data
```

## SSH Configuration

- Passwords disabled (SSH keys only)
- Root login disabled
- MaxSessions: 20 (per user)
- MaxStartups: 30:50:100 (rate limiting for DDoS protection)
- ClientAliveInterval: 300s

## Resource Limits

Protection against fork bombs and resource abuse. Configuration is version-controlled in `server/limits-users.conf` and deployed automatically by `deploy.sh` to `/etc/security/limits.d/99-users.conf`:

| Resource | Analysts | Admins |
|----------|----------|--------|
| Max processes (nproc) | 100/150 | unlimited |
| Virtual memory (as) | 4 GB / 6 GB | unlimited |
| File size (fsize) | 2 GB / 4 GB | unlimited |
| Open files (nofile) | 1024/2048 | 65535 |
| Core dumps | disabled | unlimited |

- **Admins** (`data-ops` group members) are explicitly listed in the limits file with unlimited access
- New admins are automatically added to exceptions by `add-admin` script
- **All other users** get restricted limits via wildcard rule (protection against fork bombs)

## Data Sync Scripts

### Server: update.sh

Syncs data from Keboola to Parquet files. Run via cron 3x daily (6:00, 14:00, 19:00 UTC).

```bash
cd /opt/data-analyst/repo && ./scripts/update.sh
```

**What it does:**
1. Activates virtual environment (supports both local `./.venv` and server `/opt/data-analyst/.venv`)
2. Downloads data from Keboola Storage API, converts to Parquet format in `DATA_DIR/parquet/{folder}/`
3. Generates data profiles (`python -m src.profiler` → `profiles.json`) — non-fatal if it fails

**Cron setup:**
```bash
sudo crontab -u deploy -e
# Add:
# MAILTO=admin@your-domain.com
# 0 6,14,19 * * * cd /opt/data-analyst/repo && ./scripts/update.sh > /var/log/update.log 2>&1 || cat /var/log/update.log
```

### Client: sync_data.sh

Main sync script for analysts. Syncs docs, scripts, data, and regenerates CLAUDE.md:

```bash
bash server/scripts/sync_data.sh            # Full sync (pull server/ + push user/)
bash server/scripts/sync_data.sh --dry-run  # Preview only
bash server/scripts/sync_data.sh --push     # Only upload user/ to server
```

**What it does:**
1. Syncs `server/docs/`, `server/scripts/`, `server/examples/`, `server/metadata/` from server
2. Regenerates `CLAUDE.md` from latest template (preserves username, never touches `CLAUDE.local.md`)
3. Updates `.claude/settings.json` with project permissions from server
4. Syncs parquet data files to `server/parquet/` (incremental)
5. Uploads `user/` to server (backup + runtime for notifications)
6. Downloads corporate memory rules from `~/.claude_rules/` to `.claude/rules/`
7. Updates sync timestamp on server (`touch ~/server/`) - used by the webapp Account card "Last Sync" display. Each user's `~/server/` directory is per-user, so the timestamp is independent.
8. Reinitializes DuckDB in `user/duckdb/` (core tables via `duckdb_manager.py`, optional dataset views via `sync_jira.sh --views-only` etc.)

**Note:** Rsync uses `--delete` to remove obsolete files from client (e.g., old monthly partitions when switching to daily). Files are compared by mtime+size (no `--checksum` for better performance). If rsync is not available (Windows without WSL), scp is used as fallback with explicit dotfile handling.

**CLAUDE.md update mechanism:**
- `CLAUDE.md` is regenerated from `server/docs/setup/claude_md_template.txt` on every sync
- Template is maintained centrally and deployed to server via CI/CD
- User's personal `CLAUDE.local.md` is never overwritten (higher priority in Claude Code)
- New features added to template are automatically delivered to all analysts on next sync

**Claude Code settings.json:**
- `.claude/settings.json` is copied from `server/docs/setup/claude_settings.json` on every sync
- Contains project-wide permissions (allow/deny/ask rules for tools)
- Protects `server/` directory from accidental modifications by Claude
- Centrally managed - analysts cannot override these permissions locally

### Client: init.sh + setup_views.sh

**First time setup (init.sh):**
```bash
./scripts/init.sh
```
Creates virtual environment, installs dependencies, and creates data folders including `duckdb/`.

**After rsync (setup_views.sh):**
```bash
bash server/scripts/setup_views.sh
```
Initializes DuckDB views from synced Parquet files. DuckDB database is created at `user/duckdb/analytics.duckdb`.

Steps:
1. Activates virtual environment
2. Runs `duckdb_manager.py --reinit` for core Keboola tables (from `data_description.md`)
3. Calls optional dataset scripts with `--views-only` flag:
   - If `server/parquet/jira/` exists → `sync_jira.sh --views-only` (creates `jira_issues`, `jira_comments`, `jira_attachments`, `jira_changelog` views)
   - Future datasets follow the same pattern (e.g., `sync_github.sh --views-only`)

**Convention:** Each data source sync script (e.g., `sync_jira.sh`) manages its own DuckDB views. The `--views-only` flag creates/refreshes views without syncing data. This keeps `duckdb_manager.py` focused on core tables while optional datasets are self-contained.

## Server Purpose

1. **Sync from Keboola** - periodically pulls data from Keboola Storage
2. **Convert to Parquet** - transforms data to efficient format
3. **Chunking** - splits data by hour for incremental sync
4. **Distribution** - clients pull data via rsync to local machines
5. **On-server analysis** - analysts can run scripts directly on the server

## Usage Guide

### User Types

| Type | Groups | Data Access | Use Case |
|------|--------|-------------|----------|
| **Standard Analyst** | `dataread` | Public data | Regular analysts, data scientists |
| **Privileged Analyst** | `dataread` + `data-private` | Public + private | Executives, management |
| **Admin** | `sudo` + `data-ops` + all data groups | Everything + server + deployment | DevOps, IT team |

- **Standard analysts** see all company data except sensitive information stored in `private/`
- **Privileged analysts** have access to everything including executive reports and financial details
- **Admins** can manage the server, add/remove users, and have full sudo access

### What Each User Gets

Every analyst has their own Linux account with:

```
/home/username/
├── server/                         # Symlinks to shared read-only data on /data
│   ├── docs -> /data/docs
│   ├── scripts -> /data/scripts
│   ├── examples -> /data/examples
│   ├── parquet -> /data/src_data/parquet
│   └── metadata -> /data/src_data/metadata
├── user/                           # User's OWN writable directories
│   ├── duckdb/                     # Per-user DuckDB database
│   │   └── analytics.duckdb
│   ├── notifications/              # Notification scripts (*.py)
│   ├── artifacts/                  # Analysis outputs
│   ├── scripts/                    # Custom scripts
│   └── parquet/                    # Custom parquet files
├── .notifications/                 # Notification runner state
│   ├── state/                      # Cooldown tracking per script
│   └── logs/                       # Runner and cron logs
└── .ssh/authorized_keys            # SSH key for authentication
```

- **Home directory** (`/home/username/`) - private space for each user
- **Server data** (`~/server/`) - read-only symlinks to shared `/data/` on disk
- **User workspace** (`~/user/`) - writable directories for user's own files
- **DuckDB** (`~/user/duckdb/analytics.duckdb`) - per-user database built from shared parquet

### Typical Workflow

**Option A: Local analysis with rsync (recommended)**

1. Analyst syncs data to their local machine:
   ```bash
   # Recommended: use the sync script
   bash server/scripts/sync_data.sh

   # Or manual rsync
   rsync -avz data-analyst:server/parquet/ ./server/parquet/
   ```

2. Run analysis locally with Claude Code or other tools
3. Data stays on analyst's machine - they can do whatever they want with it

**Option B: Server-side analysis**

1. SSH into the server:
   ```bash
   ssh username@YOUR_SERVER_IP
   ```

2. Work in personal workspace:
   ```bash
   cd ~/user
   # Run scripts, analyze data from ~/server/parquet/
   ```

3. Copy results back to local machine if needed

### Data Access Examples

**Standard analyst (public data only):**
```bash
$ ls ~/server/parquet/
sales/  products/  customers/  orders/  private/

$ ls ~/server/parquet/private/
ls: cannot open directory 'private/': Permission denied
```

**Privileged analyst (public + private):**
```bash
$ ls ~/server/parquet/
sales/  products/  customers/  orders/  private/

$ ls ~/server/parquet/private/
executive_reports/  financial_details/  board_materials/
```

### Rsync Permissions

When syncing with rsync:
- Standard analysts will get "Permission denied" errors for `private/` folder (expected)
- Use `--exclude='private/'` to skip it cleanly:
  ```bash
  rsync -avz --exclude='private/' data-analyst:server/parquet/ ./server/parquet/
  ```
- Privileged analysts can sync everything including private data

## Monitoring

### Cloud Monitoring (GCP)

**Ops Agent** is installed and reports VM metrics to Cloud Monitoring, including disk space utilization.

**Installation** (already done):
```bash
curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
sudo bash add-google-cloud-ops-agent-repo.sh --also-install
```

**Check agent status:**
```bash
sudo systemctl status google-cloud-ops-agent
```

**Available metrics:**
- `agent.googleapis.com/disk/percent_used` - Disk utilization percentage
- `agent.googleapis.com/memory/percent_used` - Memory utilization
- `agent.googleapis.com/cpu/utilization` - CPU usage
- `agent.googleapis.com/network/traffic` - Network I/O

**View metrics in GCP Console:**
1. Go to [Cloud Console > Monitoring > Metrics Explorer](https://console.cloud.google.com/monitoring/metrics-explorer)
2. Select resource type: `VM Instance`
3. Select metric: `agent.googleapis.com/disk/percent_used`
4. Filter by device: `/dev/sdb` (data disk)

**Alert Policy for Disk Space:**

Alert triggers when `/data` partition exceeds 85% usage for 5 minutes.

To create the alert policy manually:

1. Go to [Cloud Console > Monitoring > Alerting](https://console.cloud.google.com/monitoring/alerting)
2. Click **Create Policy**
3. Click **Add Condition**:
   - **Resource type**: VM Instance
   - **Metric**: `agent.googleapis.com/disk/percent_used`
   - **Filter**: `metadata.system_labels.device="/dev/sdb"` AND `metadata.system_labels.state="used"`
   - **Threshold**: > 85
   - **Duration**: 5 minutes
4. Click **Next** > **Notifications** (add email/Slack channel)
5. Click **Next** > **Documentation**:
   ```
   Disk /data partition is above 85% full.

   Check /data/src_data/ for large files or run cleanup.

   Common causes:
   - Keboola data sync (check cron logs)
   - bot.log growth (check /data/notifications/bot.log)
   - Jira attachments (check /data/src_data/raw/jira/attachments/)
   ```
6. **Name**: "Disk Space Alert - /data partition"
7. Click **Create Policy**

**Cost:** Free tier (first 150 time series free, this VM uses ~25)

**Dashboard:** Available in GCP Console > Monitoring > Dashboards > "VM Instances"

### Local Monitoring

```bash
# Server status
ssh kids "uptime && free -h && df -h / /data /home"

# Active users
ssh kids "who"

# Recent logins
ssh kids "last | head -20"

# Check disk space for all partitions
ssh kids "df -h"

# Check disk usage by directory
ssh kids "du -sh /data/*"
```

## Backup & Disaster Recovery

### Disk Layout

| Disk | Mount | Size | Purpose | Backup |
|------|-------|------|---------|--------|
| `your-server` (sda) | `/` | 10 GB | OS, packages, app | Expendable (rebuild from git) |
| `data-disk` (sdb) | `/data` | 30 GB | Parquet data, docs, scripts | Daily GCP snapshots |
| `home-disk` (sdc) | `/home` | 30 GB | User homes, SSH keys, workspaces | Daily GCP snapshots |
| `tmp-disk` (sdd) | `/tmp` | 100 GB | Temporary files | Expendable (not snapshotted) |

### Automatic Snapshots

Both `data-disk` and `home-disk` have daily GCP snapshot schedules with 14-day retention. Setup via `server/setup-snapshot-schedule.sh`.

```bash
# Check snapshot schedule status
gcloud compute resource-policies describe daily-backup \
  --project=your-gcp-project --region=europe-north1

# List existing snapshots
gcloud compute snapshots list --project=your-gcp-project

# Manual snapshot (if needed)
gcloud compute disks snapshot data-disk home-disk \
  --project=your-gcp-project \
  --zone=europe-north1-a \
  --snapshot-names=data-disk-$(date +%Y%m%d),home-disk-$(date +%Y%m%d)
```

### Recovery

See `disaster-recovery.md` for detailed recovery procedures for each failure scenario.

## Application Deployment

### Directory Structure

```
/opt/data-analyst/          # Application directory (group: data-ops)
├── repo/                   # Git repository
│   ├── src/                # Python source code
│   ├── scripts/            # Data sync scripts
│   ├── server/             # Server management scripts
│   │   ├── bin/            # add-analyst, notify-runner, notify-scripts, etc.
│   │   └── telegram_bot/   # Telegram bot service
│   ├── webapp/             # Flask web application
│   └── examples/           # Example notification scripts
├── .venv/                  # Python virtual environment
├── .env                    # Webapp env (Google OAuth, secret key)
└── logs/                   # Application logs
```

### CI/CD Pipeline

Application is automatically deployed via GitHub Actions when changes are pushed to `main` branch.

**How it works:**
1. Push to `main` triggers GitHub Actions workflow
2. Action connects to server via SSH as `deploy` user
3. Runs `/opt/data-analyst/repo/server/deploy.sh`
4. Deploy script:
   - Pulls latest code from `origin/main`
   - Updates server management scripts in `/usr/local/bin/`
   - Updates sudoers configurations (`/etc/sudoers.d/`)
   - Updates resource limits (`/etc/security/limits.d/99-users.conf`)
   - Deploys `notify-runner` and `notify-scripts` to `/usr/local/bin/`
   - Creates data directories:
     - `/data/notifications/` (notification state)
     - `/data/src_data/raw/jira/` (Jira webhook data)
     - `/data/auth/` (password auth)
     - `/data/corporate-memory/` (knowledge base)
     - `/data/user_sessions/` (session logs)
     - `/data/examples/` (example scripts)
     - `/tmp/data_analyst_staging/` (Keboola staging)
   - Deploys systemd units:
     - `notify-bot.service` (Telegram bot)
     - `ws-gateway.service` (WebSocket gateway)
     - `corporate-memory.{service,timer}` (knowledge collector)
     - `jira-sla-poll.{service,timer}` (SLA refresh)
     - `jira-consistency.{service,timer,timer-deep}` (data integrity monitoring)
     - `session-collector.{service,timer}` (session logs)
   - Sets ACLs for Jira attachments (dataread group)
   - Creates/updates Keboola `.env` file (if secrets provided)
   - Sets correct permissions on `/opt/data-analyst/`
   - Restarts webapp, notify-bot, ws-gateway services
   - Enables/starts timers (if credentials configured)

**Deploy user permissions:**
The `deploy` user has limited sudo access defined in `/etc/sudoers.d/deploy`:

**Core Operations:**
- Can copy scripts to `/usr/local/bin/`
- Can update sudoers files in `/etc/sudoers.d/`
- Can manage permissions on `/opt/data-analyst/`
- Can update resource limits in `/etc/security/limits.d/`

**Service Management:**
- Can restart/reload webapp, nginx services
- Can manage notify-bot, ws-gateway services
- Can manage corporate-memory timer
- Can manage jira-sla-poll timer
- Can manage jira-consistency timers (incremental + deep)
- Can manage session-collector timer
- Can run `systemctl daemon-reload`

**Data Directories:**
- Can manage `/data/scripts/` (helper scripts for analysts)
- Can manage `/data/docs/` (documentation)
- Can manage `/data/notifications/` (notification state)
- Can manage `/data/examples/` (example scripts)
- Can manage `/data/src_data/raw/jira/` (Jira webhook data)
- Can manage `/data/auth/` (password auth state)
- Can manage `/data/corporate-memory/` (knowledge base)
- Can manage `/data/user_sessions/` (session collector data)
- Can manage `/tmp/data_analyst_staging/` (Keboola staging directory)

**Special Permissions:**
- Can run `notify-scripts` as any user (list/run notification scripts)
- Can set ACLs on Jira attachments (dataread group access)
- Can create log files in `/opt/data-analyst/logs/`

**Full sudoers reference:** `server/sudoers-deploy` in repository

Note: On Debian 12, core utils are in `/usr/bin/` (not `/bin/`). The sudoers file uses full paths like `/usr/bin/cp`, `/usr/bin/chmod`, etc.

### Initial Setup (one-time)

**1. Install prerequisites:**
```bash
sudo apt-get update
sudo apt-get install -y git python3.11-venv python3-pip
```

**2. Create deploy user and SSH key for GitHub:**
```bash
# Create deploy user
sudo useradd -m -s /bin/bash deploy
sudo groupadd data-ops 2>/dev/null || true
sudo usermod -aG data-ops deploy

# Generate SSH key for GitHub
sudo -u deploy ssh-keygen -t ed25519 -f /home/deploy/.ssh/id_ed25519 -N '' -C 'deploy@data-broker'

# Configure SSH for GitHub
sudo -u deploy bash -c 'echo -e "Host github.com\n  IdentityFile ~/.ssh/id_ed25519\n  StrictHostKeyChecking accept-new" > /home/deploy/.ssh/config'
sudo chmod 600 /home/deploy/.ssh/config

# Show public key (add this to GitHub as Deploy Key)
sudo cat /home/deploy/.ssh/id_ed25519.pub
```

**3. Add Deploy Key to GitHub:**
- Go to: https://github.com/your-org/ai-data-analyst/settings/keys
- Click "Add deploy key"
- Title: `data-broker-server`
- Key: (paste public key from previous step)
- Allow write access: NO

**4. Clone repository and run setup:**
```bash
sudo mkdir -p /opt/data-analyst
sudo chown deploy:data-ops /opt/data-analyst
sudo -u deploy git clone git@github.com:your-org/ai-data-analyst.git /opt/data-analyst/repo
sudo git config --global --add safe.directory /opt/data-analyst/repo
sudo -u deploy git config --global --add safe.directory /opt/data-analyst/repo
sudo /opt/data-analyst/repo/server/setup.sh
```

**5. Add existing admins to data-ops group:**
```bash
sudo usermod -aG data-ops admin1
sudo usermod -aG data-ops admin2
sudo usermod -aG data-ops admin3
```

### GitHub Secrets Required

Set these in GitHub repository settings (Settings > Secrets > Actions):

| Secret | Value |
|--------|-------|
| `SERVER_HOST` | `YOUR_SERVER_IP` |
| `SERVER_USER` | `deploy` |
| `SERVER_SSH_KEY` | Private SSH key (`/home/deploy/.ssh/id_ed25519`) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token (from @BotFather) |
| `SENDGRID_API_KEY` | SendGrid API key for password auth emails |
| `ALLOWED_EMAILS` | Comma-separated whitelisted emails for password auth |

### Manual Deployment

Admins can trigger deployment manually:

```bash
# Via GitHub Actions UI (Actions > Deploy to Server > Run workflow)
# Or via SSH:
ssh kids "cd /opt/data-analyst/repo && ./server/deploy.sh"
```

### Deployment Logs

```bash
# View deployment history
cat /opt/data-analyst/logs/deploy.log

# Follow live deployment
tail -f /opt/data-analyst/logs/deploy.log
```

### Troubleshooting CI/CD

**"sudo: a terminal is required to read the password"**
- Deploy user is missing NOPASSWD sudo permission for a specific command
- Check `/etc/sudoers.d/deploy` exists and has correct permissions (440)
- Verify the command path matches (Debian 12 uses `/usr/bin/`, not `/bin/`)
- **Fix:** Add missing permission to `server/sudoers-deploy` and redeploy:
  ```bash
  # Edit server/sudoers-deploy in repo
  # Add the missing command with full path
  deploy ALL=(ALL) NOPASSWD: /usr/bin/command-name args

  # Commit and push
  git add server/sudoers-deploy
  git commit -m "Add missing sudo permission"
  git push origin main

  # Manually update on server (one-time)
  ssh kids "sudo cp /opt/data-analyst/repo/server/sudoers-deploy /etc/sudoers.d/deploy"
  ssh kids "sudo chmod 440 /etc/sudoers.d/deploy"
  ```

**"Permission denied" on .env file**
- Deploy user cannot write directly to files owned by root
- Solution: Use `sudo /usr/bin/tee` instead of direct file write

**Deploy script changes not taking effect**
- The deploy script pulls new code AFTER it starts running
- Changes to `deploy.sh` itself require manual pull first:
  ```bash
  ssh kids "sudo -u deploy bash -c 'cd /opt/data-analyst/repo && git pull'"
  ```

**Verify sudoers configuration:**
```bash
# Check if sudoers file exists and has correct permissions
ssh kids "ls -la /etc/sudoers.d/deploy"

# Validate syntax (exit code 0 = OK)
ssh kids "sudo visudo -cf /etc/sudoers.d/deploy && echo 'Syntax OK'"

# View current sudoers rules
ssh kids "sudo cat /etc/sudoers.d/deploy"
```

**Test deploy locally as deploy user:**
```bash
ssh kids "sudo -u deploy bash -c 'cd /opt/data-analyst/repo && ./server/deploy.sh'"
```

## Web Application (Self-Service Portal)

A web application at `https://your-instance.example.com` allows team members to create their own analyst accounts via Google SSO.

### Features

- Google Sign-In (restricted to `@your-domain.com` emails only)
- Email/password login for external users (whitelisted emails)
- Self-service account creation for new users
- Dashboard showing account info for existing users (2-column layout)
- Dynamic data stats (tables, columns, rows, size) loaded from `sync_state.json`
- Data catalog page with dynamic table listings from `data_description.md` + `sync_state.json`
- Data profiler with per-column statistics, visualizations, and alerts (from `profiles.json`)
- SSH connection instructions
- Claude Code integration hints for AI-assisted setup
- Telegram notification linking
- macOS desktop app linking/unlinking with install instructions

### User Flow

1. User visits `https://your-instance.example.com`
2. Signs in with Google (@your-domain.com account)
3. Dashboard shows instructions and form for SSH key
4. User can ask Claude Code to generate SSH key and guide them
5. After pasting SSH key, account is created automatically
6. User syncs data and starts analyzing with Claude Code

### Dynamic Data Stats

Dashboard and catalog pages display live data statistics (table count, columns, rows, size). These are loaded dynamically from `sync_state.json` on every page request - no webapp restart needed.

**Data flow:**
```
Cron (update.sh) → data_sync.py → /data/src_data/metadata/sync_state.json
                                                    ↓
                              Flask reads on request → dashboard + catalog templates
```

- `sync_state.json` is updated by the data sync process with per-table stats (rows, columns, file size)
- Flask aggregates these into totals for display
- If `sync_state.json` is missing or unreadable, hardcoded fallback values are used
- Catalog page merges `data_description.md` (table names, descriptions, categories) with `sync_state.json` (row counts)

### Architecture

```
Browser -> Nginx (HTTPS/Let's Encrypt) -> Gunicorn -> Flask App
                                                         |
                                                         v
                                              sudo add-analyst (via sudoers)
```

### Setup

**1. Run webapp setup script:**
```bash
sudo /opt/data-analyst/repo/server/webapp-setup.sh
```

**2. Configure Google OAuth:**
- Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
- Create OAuth 2.0 Client ID (Web application)
- Authorized JavaScript origins: `https://your-instance.example.com`
- Authorized redirect URIs: `https://your-instance.example.com/authorize`

**3. Update environment file:**
```bash
sudo nano /opt/data-analyst/.env

# Add:
WEBAPP_SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
GOOGLE_CLIENT_ID=<from Google Console>
GOOGLE_CLIENT_SECRET=<from Google Console>
```

**4. Start/restart webapp:**
```bash
sudo systemctl restart webapp
```

### Monitoring

```bash
# Service status
sudo systemctl status webapp
sudo systemctl status nginx

# Logs
tail -f /opt/data-analyst/logs/webapp-access.log
tail -f /opt/data-analyst/logs/webapp-error.log

# Test endpoint
curl -I https://your-instance.example.com/health
```

### Security Notes

- Only `@your-domain.com` emails can log in via Google OAuth
- External users can log in via email/password if their email is whitelisted
- Self-service creates **standard analyst** accounts only (no --private flag)
- www-data is member of `data-ops` group (for access to /opt/data-analyst and static files)
- www-data can only run `add-analyst` via sudoers (not add-admin) - configured in `/etc/sudoers.d/webapp`
- HTTPS enforced with Let's Encrypt certificate
- SSH keys are validated before passing to add-analyst script
- Reserved system usernames (root, admin, deploy, etc.) are blocked from registration
- Username collision with existing system accounts shows error and requires admin intervention
- Password auth uses Argon2id hashing (state of the art) with rate limiting (5 attempts/minute)
- Magic links for password setup expire in 24 hours, reset links in 1 hour

### Technical Notes

**Sudoers configuration:**

The webapp needs sudo access to run `add-analyst` and `notify-scripts`. This is configured via `server/sudoers-webapp` file which is deployed to `/etc/sudoers.d/webapp`:

```
www-data ALL=(ALL) NOPASSWD: /usr/local/bin/add-analyst
www-data ALL=(ALL) NOPASSWD: /usr/local/bin/notify-scripts
```

**Absolute paths requirement:**

Gunicorn runs with a restricted PATH (only `/opt/data-analyst/.venv/bin`). Therefore, all system commands in Python code must use absolute paths:
- `/usr/bin/sudo` (not just `sudo`)
- `/usr/local/bin/add-analyst`
- `/usr/local/bin/notify-scripts`

This is handled in `webapp/user_service.py` and `server/telegram_bot/runner.py`.

### Username Generation

Username is generated from email address: the part before `@` converted to lowercase.

Examples:
- `John.Doe@your-domain.com` -> `john.doe`
- `john@your-domain.com` -> `john`

If a username conflicts with a reserved system name or existing non-analyst account, the user sees an error and must contact an admin to create the account manually with a different username.

### Prerequisites

**GCP Firewall:**
```bash
# Allow HTTP/HTTPS traffic (required for Let's Encrypt and webapp)
gcloud compute firewall-rules create allow-http-data-broker \
  --project=your-gcp-project \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=http-server,https-server

# Add tags to VM
gcloud compute instances add-tags your-server \
  --project=your-gcp-project \
  --zone=europe-north1-a \
  --tags=http-server,https-server
```

**DNS:**
- A record: `your-instance.example.com` -> `YOUR_SERVER_IP`

## Password Authentication for External Users

External users (investors, partners) who don't have @your-domain.com Google accounts can authenticate using email/password.

### How It Works

1. **Admin adds email to whitelist** (via GitHub Secrets):
   - Go to GitHub repo Settings > Secrets > Actions
   - Update `ALLOWED_EMAILS` secret (comma-separated list)
   - Push any change to trigger deploy, or manually restart webapp

2. **User visits login page and clicks "Sign in with Email"**

3. **First-time setup (Sign Up tab):**
   - User enters their whitelisted email
   - Clicks "Request Access"
   - Receives email with setup link (valid 24 hours)
   - Sets up password via the link

4. **Subsequent logins (Sign In tab):**
   - User enters email + password
   - Same session/dashboard as Google OAuth users

### Username Generation

Usernames are derived from email addresses differently for internal vs external users:

| Email | Username | Type |
|-------|----------|------|
| `john.doe@your-domain.com` | `john.doe` | Internal (Google OAuth) |
| `emily@investor.com` | `emily_investor_com` | External (password auth) |
| `partner@example.org` | `partner_example_org` | External (password auth) |

This prevents username collisions between internal and external users.

### Configuration

**GitHub Secrets (recommended):**

| Secret | Description |
|--------|-------------|
| `ALLOWED_EMAILS` | Comma-separated list of whitelisted emails |
| `SENDGRID_API_KEY` | SendGrid API key for sending emails |
| `EMAIL_FROM_ADDRESS` | Sender email address (e.g., `noreply@your-domain.com`) |
| `EMAIL_FROM_NAME` | Sender display name (e.g., `Data Analyst Platform`) |

**Data storage:**

```
/data/auth/                         # Password auth data (www-data:data-ops, 2770)
└── password_users.json             # User records (hashes, tokens, metadata)
```

### Security Features

- **Argon2id** password hashing (most secure algorithm)
- **Rate limiting**: 5 failed attempts per minute per email
- **Single-use tokens**: Setup/reset links invalidate after use
- **Token expiry**: Setup 24h, reset 1h
- **No email enumeration**: Reset endpoint always shows same message
- **Password requirements**: Min 8 chars, uppercase, lowercase, digit

### Password Reset

Users can reset their password via "Forgot Password?" link on the Sign In tab. They receive an email with a reset link valid for 1 hour.

## Telegram Notification Bot

A Telegram bot (`@YourBot`) allows analysts to receive alerts from their custom notification scripts.

### Architecture

```
Telegram Bot Service (systemd: notify-bot)
├── Telegram polling (handles /start, /test commands)
└── HTTP server on unix socket (/run/notify-bot/bot.sock)
        ▲
        │ POST /send, POST /send_photo
        │
notify-runner (user crontab, /usr/local/bin/notify-runner)
└── Executes ~/user/notifications/*.py
```

The webapp reads/writes shared JSON files in `/data/notifications/` for user-Telegram linking (verification codes, user mappings).

### Services

| Service | User | Description |
|---------|------|-------------|
| `notify-bot` | deploy:data-ops | Telegram polling + send API on unix socket |
| `webapp` | www-data:data-ops | Dashboard with Telegram link/unlink UI |

### Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Link account (or show status if already linked) |
| `/whoami` | Show username and email |
| `/status` | List notification scripts with Run buttons |
| `/test` | Send a demo graphical report |
| `/help` | Show available commands |

The `/status` command shows inline keyboard buttons to run scripts on demand. Scripts are executed as the owning user via `sudo -u` using the `notify-scripts` helper (see below).

### Data Files

```
/data/notifications/            # deploy:data-ops, mode 2770 (setgid, no others)
├── telegram_users.json         # username -> {chat_id, linked_at}
├── desktop_users.json          # username -> {linked_at} (desktop app link state)
├── pending_codes.json          # code -> {chat_id, created_at}
└── bot.log                     # Bot service log

/run/notify-bot/                # systemd RuntimeDirectory (mode 0755)
└── bot.sock                    # Unix socket for send API (mode 0666)
```

The setgid bit (`2770`) ensures all files created in `/data/notifications/` inherit the `data-ops` group, allowing both the bot service (deploy) and webapp (www-data) to read/write them. Analysts have no access to this directory.

The socket is in `/run/notify-bot/`, a systemd-managed directory with `0755` permissions, so any local user can connect to send notifications.

### Notification Runner

Users create Python scripts in `~/user/notifications/` that output JSON to stdout. The `notify-runner` script (installed at `/usr/local/bin/notify-runner`) executes these scripts and sends results via the bot's unix socket.

Per-user state is stored in `~/.notifications/state/` (cooldown tracking) and logs in `~/.notifications/logs/`.

Users configure their own crontab:
```bash
crontab -e
# Add:
*/5 * * * * ~/.venv/bin/python /usr/local/bin/notify-runner >> ~/.notifications/logs/cron.log 2>&1
```

### Notify-Scripts Helper

The `notify-scripts` helper (`/usr/local/bin/notify-scripts`) provides a secure way for services (webapp, Telegram bot) to list and run user notification scripts without needing filesystem access to user home directories.

**Why it exists:** User home directories are set to `750` permissions. Services like `www-data` and `deploy` cannot traverse `/home/{user}/` to read scripts or state files. The helper runs **as the target user** via `sudo -u`, so it has full access to `~/user/notifications/` and `~/.notifications/state/`.

**Usage:**
```bash
# List scripts with last_run metadata (returns JSON array)
sudo -u <username> /usr/local/bin/notify-scripts list

# Run a script and return its JSON output
sudo -u <username> /usr/local/bin/notify-scripts run <script_name.py>

# Get last sync time (returns JSON with elapsed_seconds, elapsed_display)
sudo -u <username> /usr/local/bin/notify-scripts sync-status
```

The `sync-status` command reads the mtime of `~/server/` directory. This is updated by `sync_data.sh` via `touch ~/server/` at the end of each sync. Each user has their own `~/server/` directory (containing symlinks to shared `/data/`), so timestamps are per-user.

**Callers:**
- `server/telegram_bot/status.py` - `/status` command and script list API
- `server/telegram_bot/runner.py` - on-demand script execution (Telegram "Run" button, webapp API)
- `webapp/account_service.py` - Account card "Last Sync" display

**Sudoers rules:**
```
# /etc/sudoers.d/webapp
www-data ALL=(ALL) NOPASSWD: /usr/local/bin/notify-scripts

# /etc/sudoers.d/deploy
deploy ALL=(ALL) NOPASSWD: /usr/local/bin/notify-scripts
```

### Monitoring

```bash
# Bot service
sudo systemctl status notify-bot
tail -f /data/notifications/bot.log

# Linked users
cat /data/notifications/telegram_users.json | python3 -m json.tool

# Runner logs (per user)
cat ~/.notifications/logs/runner.log
```

### Security

- Bot token is stored centrally in `/opt/data-analyst/repo/.env` (loaded via systemd EnvironmentFile)
- Users never see the token - they communicate via unix socket only
- Socket in `/run/notify-bot/bot.sock` (systemd RuntimeDirectory, mode `0755`), socket itself `0666`
- `/data/notifications/` is `2770` (only deploy + data-ops), no analyst access to logs or user mappings
- Notification scripts run under the user's own account (no sudo) when triggered by crontab
- On-demand runs (via /status button and webapp API) use `sudo -u <user> /usr/local/bin/notify-scripts` -- services never access user home directories directly
- Scripts have a 60-second timeout (enforced by `notify-scripts` helper)
- Verification codes expire after 10 minutes and are single-use

### Known Issues

**On-demand script execution security hardening (partially resolved):**
The `notify-scripts` helper replaced direct `sudo -H -u ... /usr/bin/env ...` calls with a single auditable entry point. Services no longer need filesystem access to user home directories (750 permissions are preserved). The bot still requires `NoNewPrivileges=false` and `/tmp` in `ReadWritePaths` for sudo execution. A queue-based approach ([#51](https://github.com/your-org/ai-data-analyst/issues/51)) could further improve this by having `notify-runner` pick up run requests from a queue instead of the bot calling sudo directly.

## Data Sync Settings (Web Portal)

Users can configure which optional datasets to sync via the web portal at `https://your-instance.example.com`. Settings are stored server-side and downloaded by `sync_data.sh` before each sync.

### Architecture

```
┌─────────────────────────────────────┐
│  Web Portal (Dashboard)             │
│  └── Data Settings widget           │
│      ├── Toggle: Jira (~50 MB)      │
│      └── Toggle: Jira Attachments   │
│                (~500 MB+)           │
└─────────────────────────────────────┘
              │ POST /api/sync-settings
              ▼
┌─────────────────────────────────────┐
│  Flask API                          │
│  ├── Save to sync_settings.json     │
│  └── Write ~/.sync_settings.yaml    │
│      (via sudo install)             │
└─────────────────────────────────────┘
              │
              ▼
/data/notifications/sync_settings.json  ← Central storage (all users)
/home/{user}/.sync_settings.yaml        ← Per-user config file
              │
              ▼ scp (analyst sync)
┌─────────────────────────────────────┐
│  sync_data.sh (client)              │
│  ├── Download ~/.sync_settings.yaml │
│  ├── Read dataset toggles           │
│  └── Conditionally run sync_jira.sh │
└─────────────────────────────────────┘
```

### Data Files

| File | Location | Purpose |
|------|----------|---------|
| `sync_settings.json` | `/data/notifications/` | Central storage for all users' settings |
| `.sync_settings.yaml` | `/home/{user}/` | Per-user config file (YAML format) |

**sync_settings.json format:**
```json
{
  "john.doe": {
    "datasets": {
      "jira": true,
      "jira_attachments": false
    },
    "updated_at": "2026-02-03T12:00:00Z"
  }
}
```

**Per-user .sync_settings.yaml format:**
```yaml
# Data Analyst - Sync Configuration
# Managed by web portal - changes here may be overwritten

datasets:
  jira: true
  jira_attachments: false
```

### Sudoers Configuration

The webapp needs sudo to write config files to user home directories. This is configured in `/etc/sudoers.d/webapp-sync`:

```
# Allow webapp to install sync settings to user home directories
www-data ALL=(ALL) NOPASSWD: /usr/bin/install -o * -g * -m 644 /tmp/*.yaml /home/*/.sync_settings.yaml
```

**Why this approach:**
- Webapp runs as `www-data` which cannot write to `/home/{user}/`
- Using `install` command allows setting ownership in one atomic operation
- Tempfile must be in `/tmp/` (Gunicorn has restricted PATH)
- Target is restricted to `.sync_settings.yaml` only

### Client Sync Flow

When `sync_data.sh` runs:

1. Downloads config from server:
   ```bash
   scp -q data-analyst:~/.sync_settings.yaml /tmp/.sync_settings_$(id -u).yaml
   ```

2. If no config exists on server, creates default (jira: false)

3. Reads config and conditionally runs dataset sync scripts:
   ```bash
   if grep -qE '^\s*jira:\s*true' "$SYNC_CONFIG_LOCAL"; then
       bash sync_jira.sh
   fi
   ```

4. `sync_jira.sh` syncs data AND creates DuckDB views automatically (no separate step needed)

5. `sync_jira.sh` checks `jira_attachments` setting for attachment sync

### Available Datasets

| Dataset | Size | Description |
|---------|------|-------------|
| `jira` | ~50 MB | Support tickets from SUPPORT project (issues, comments, changelog, attachment metadata) |
| `jira_attachments` | ~500 MB+ | Actual attachment files (images, logs, etc.). Requires `jira` to be enabled. |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sync-settings` | GET | Get current user's sync settings |
| `/api/sync-settings` | POST | Update settings and regenerate user config |

### Troubleshooting

**Settings not being saved to user home:**
- Check `/etc/sudoers.d/webapp-sync` exists
- Verify tempfile is created in `/tmp/` (not other directory)
- Check webapp logs: `tail -f /opt/data-analyst/logs/webapp-error.log`

**Old scripts on client after sync:**
- `sync_data.sh` downloads scripts from `/data/scripts/` on server
- Ensure `deploy.sh` copies all scripts including `sync_jira.sh`
- If scripts are missing from `/data/scripts/`, run manual deploy or CI/CD

## Jira Webhook Integration

Receives webhooks from Atlassian Jira to maintain a **real-time** copy of issue data for analysis.

### Architecture

```
Jira Cloud (your-org.atlassian.net)
        │
        │ POST /webhooks/jira (HTTPS)
        ▼
┌─────────────────────────────────────┐
│  Webapp (Flask)                     │
│  ├── Verify HMAC signature          │
│  ├── Fetch full issue via REST API  │
│  ├── Save JSON + download attachs   │
│  └── Trigger incremental transform  │
│            │                        │
│            ▼                        │
│  ┌─────────────────────────────┐    │
│  │ incremental_jira_transform  │    │
│  │ • Upsert to monthly Parquet │    │
│  │ • Copy to distribution dir  │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘
        │
        ▼ rsync (analyst sync)
┌─────────────────────────────────────┐
│  Analyst (local)                    │
│  • Only changed monthly files sync  │
│  • Data available within seconds    │
└─────────────────────────────────────┘
```

### Data Structure

```
/data/src_data/
├── raw/jira/                  # Raw Jira data from webhooks
│   ├── issues/                # Individual issue JSON files
│   │   ├── SUPPORT-1234.json
│   │   └── SUPPORT-1235.json
│   ├── attachments/           # Downloaded attachment files
│   │   └── SUPPORT-1234/
│   │       └── 56340_image.png
│   └── webhook_events/        # Raw webhook payloads (audit)
│       └── 20260203_120000_jira_issue_created.json
│
└── parquet/jira/              # Transformed data (monthly partitioned)
    ├── issues/
    │   ├── 2024-01.parquet
    │   └── 2024-02.parquet
    ├── comments/
    ├── attachments/           # Metadata only (not binary)
    └── changelog/

~/server/parquet/jira/         # Distribution directory (symlink or copy)
                               # This is what analysts sync via rsync
```

**Monthly partitioning:** Each issue belongs to the month of its `created_at` date. When an issue is updated, only that month's Parquet file changes. Rsync detects changed files by checksum and only transfers those (~50-100KB per month).

### Configuration

Add to `/opt/data-analyst/.env`:

```bash
# Jira Webhook Integration
JIRA_WEBHOOK_SECRET=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
JIRA_DOMAIN=your-org.atlassian.net
JIRA_EMAIL=integration-user@your-domain.com
JIRA_API_TOKEN=<API token from Atlassian account>

# SLA polling (JSM service account for elapsed_millis refresh)
JIRA_SLA_EMAIL=<JSM service account email>
JIRA_SLA_API_TOKEN=<JSM service account API token>
JIRA_CLOUD_ID=f0f7a244-4fb4-41f9-b1f0-b79e24a20f11
```

**Get Jira API token:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create API token
3. Store in `.env` as `JIRA_API_TOKEN`

### Jira Webhook Setup

1. Go to Jira Admin > System > WebHooks
2. Create new webhook:
   - **Name**: `Data Analyst Sync`
   - **URL**: `https://your-instance.example.com/webhooks/jira`
   - **Secret**: Same value as `JIRA_WEBHOOK_SECRET` in `.env`
   - **JQL Filter**: `project = "Your Project"` (or your project)
   - **Events**:
     - Issue: created, updated, deleted
     - Comment: created, updated
     - Attachment: created
     - Issue link: created

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhooks/jira` | POST | Receive Jira webhooks |
| `/webhooks/jira/health` | GET | Health check (shows config status) |
| `/webhooks/jira/test` | POST | Manual issue fetch (debug mode only) |

### Monitoring

```bash
# Check webhook health
curl https://your-instance.example.com/webhooks/jira/health

# View recent webhook events
ls -la /data/src_data/raw/jira/webhook_events/ | tail -20

# Check saved issues
ls /data/src_data/raw/jira/issues/ | wc -l

# View webapp logs for webhook processing
tail -f /opt/data-analyst/logs/webapp-error.log | grep -i jira
```

### SLA Polling

SLA elapsed values (`first_response_elapsed_millis`, `time_to_resolution_elapsed_millis`) only update when a webhook fires. For idle open tickets, these values go stale. The SLA polling timer refreshes them periodically and self-heals stale status data from missed webhooks.

| Component | Description |
|-----------|-------------|
| `jira-sla-poll.service` | Oneshot service that polls open tickets for fresh SLA + status data |
| `jira-sla-poll.timer` | Runs every 15 minutes (10min after boot, then every 15min) |
| `connectors/jira/scripts/poll_sla.py` | Reads Parquet to find open issues, fetches SLA + status via cloud API |
| `connectors/jira/file_lock.py` | Per-issue advisory file locking (shared with webhook handler) |

**How it works:**
1. Reads Parquet issues to find open tickets with SLA data (~49 tickets)
2. For each: fetches fresh SLA **and status** fields via JSM service account (cloud API)
3. Acquires per-issue advisory file lock (prevents concurrent webhook writes)
4. Updates raw JSON atomically (tempfile + `os.fchmod(0o660)` + os.replace)
5. If ticket is resolved in Jira but "open" locally: logs `Self-healing: SUPPORT-XXXX is resolved in Jira`
6. Calls `transform_single_issue()` to update Parquet + distribution (inside lock)
7. Releases lock

**Monitoring:**
```bash
# Check timer status
systemctl status jira-sla-poll.timer
systemctl list-timers | grep jira

# View last run logs
journalctl -u jira-sla-poll.service --since "1 hour ago"

# Manual dry run (count open issues)
cd /opt/data-analyst/repo
/opt/data-analyst/.venv/bin/python -m connectors.jira.scripts.poll_sla --dry-run
```

**Requires:** `JIRA_SLA_EMAIL`, `JIRA_SLA_API_TOKEN`, `JIRA_CLOUD_ID` in `.env`. Timer is auto-enabled by `deploy.sh` when `JIRA_SLA_API_TOKEN` is set.

### Consistency Monitoring

Automated check every 30 minutes to detect missing Jira issues caused by webhook losses, disk failures, or processing errors. Validates data integrity by comparing three sources: Jira API (ground truth), raw JSON files, and Parquet data.

| Component | Description |
|-----------|-------------|
| `jira-consistency.service` | Oneshot service that validates data consistency across all sources |
| `jira-consistency.timer` | Runs every 30 minutes (10min after boot) |
| `jira-consistency-deep.timer` | Weekly full history check (Sunday 3 AM) |
| `connectors/jira/scripts/consistency_check.py` | Validation script with auto-backfill capability |

**How it works:**
1. Queries Jira API for all issue keys (last 30 days by default)
2. Compares with raw JSON files in `/data/src_data/raw/jira/issues/`
3. Compares with Parquet data in `/data/src_data/parquet/jira/issues/`
4. Auto-backfills if 1-10 issues missing (downloads JSON + transforms to Parquet)
5. Alerts (ERROR log) if 11+ issues missing (requires manual investigation)
6. Re-transforms JSON to Parquet for issues with transform lag

**Grace period:** Ignores issues created in last 5 minutes to avoid false positives from webhook timing windows.

**Alert levels:**
- **INFO**: 1-5 missing issues, auto-backfilled successfully
- **WARNING**: 6-10 missing issues, auto-backfilled successfully
- **ERROR**: 11+ missing issues, manual review required (no auto-fix)

**Monitoring:**
```bash
# Check timer status
systemctl status jira-consistency.timer
systemctl list-timers | grep jira

# View last run logs
journalctl -u jira-consistency.service --since "1 hour ago"

# Manual check (dry run)
cd /opt/data-analyst/repo
/opt/data-analyst/.venv/bin/python -m connectors.jira.scripts.consistency_check --dry-run --max-age-days 7

# Manual check with auto-fix
/opt/data-analyst/.venv/bin/python -m connectors.jira.scripts.consistency_check --auto-fix --max-age-days 30

# View consistency report
cat /data/src_data/raw/jira/_consistency_report.json | python3 -m json.tool
```

**Manual recovery (if 11+ issues found):**
```bash
# List missing issues from report
jq -r '.discrepancies.missing_in_json[]' /data/src_data/raw/jira/_consistency_report.json

# Backfill specific issues
cd /opt/data-analyst/repo
/opt/data-analyst/.venv/bin/python -m connectors.jira.scripts.backfill --issue-keys SUPPORT-15307,SUPPORT-15308

# Verify in Parquet
/opt/data-analyst/.venv/bin/python -c "
import duckdb
con = duckdb.connect()
result = con.execute('''
  SELECT issue_key, created_at, summary
  FROM read_parquet('/data/src_data/parquet/jira/issues/*.parquet')
  WHERE issue_key IN ('SUPPORT-15307', 'SUPPORT-15308')
''').fetchall()
for row in result:
    print(row)
"
```

**Requires:** `JIRA_DOMAIN`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`. Timers are auto-enabled by `deploy.sh` when Jira credentials are configured.

### Security

- Webhooks are verified using HMAC-SHA256 signature
- API token has read-only access to Jira (no write permissions needed)
- Webhook events are logged for audit purposes
- Multiple services write to `/data/src_data/raw/jira/`: webapp (www-data), SLA poll (root), consistency check (root), backfill scripts (admin users)
- Concurrent writes to the same issue JSON are serialized via per-issue advisory file locking (`connectors/jira/file_lock.py`, `fcntl.flock`). Lock files in `issues/.locks/`. See [#203](https://github.com/your-org/ai-data-analyst/issues/203).

## Data Profiler

Generates YData-inspired statistical profiles for all tables in the data catalog, including Jira support tables. Profiles include per-column statistics, type-specific visualizations (histograms, top values, timelines), data quality alerts, and business context (relationships, metrics). Profiles are preserved across runs — if a table fails to profile, its previous valid data is retained.

### Architecture

```
Cron (update.sh, 3x daily)
  Step 2: python -m src.data_sync     → parquet + sync_state.json + schema.yml
  Step 3: python -m src.profiler      → profiles.json
                │
                ▼
/data/src_data/metadata/profiles.json  (mode 644, admin1:data-ops)
                │
                ▼
Webapp: GET /api/catalog/profile/<table_name>
                │
                ▼
Catalog page: profiler modal (Chart.js visualizations)
```

### How It Works

1. **Profiler runs as Step 4 in `scripts/update.sh`** after data sync and metadata generation
2. **Materializes Parquet into DuckDB** — `CREATE TEMP TABLE` loads each table once into DuckDB columnar storage (instead of re-reading Parquet files for every query)
3. **Batch statistics** — base stats (COUNT, COUNT DISTINCT) for all columns in one query; type-specific aggregates (numeric, string, date, boolean) batched per category
4. **Large tables** (>500K rows) are sampled: `USING SAMPLE 500000 ROWS`
5. **Merges metadata** from `data_description.md` (descriptions, foreign keys), `sync_state.json` (row counts, file sizes), and `docs/metrics/*.yml` (business metric mappings)
6. **Writes `profiles.json`** atomically (`tempfile.mkstemp()` + `os.chmod(0o644)` + `os.replace()`)
7. **Preserves existing profiles on failure** — if a table fails to profile, the previous valid profile is retained (marked `_stale: true`)
8. **Profiler failure is non-fatal** — if the entire profiler fails, the update pipeline continues
9. **Jira table relationships** — `issue_key` foreign keys are defined between all Jira tables (comments, attachments, changelog, issuelinks, remote_links → jira_issues), visible in the Relationships tab

### Output File

```
/data/src_data/metadata/profiles.json   # ~900 KB for ~29 tables
```

**Permissions:** File must be `644` (world-readable) so the webapp (`www-data`) can serve it. The profiler sets `os.chmod(tmp, 0o644)` before `os.replace()` because `mkstemp()` defaults to `600`.

### Per-Table Profile Structure

Each table profile contains:

| Field | Source | Description |
|-------|--------|-------------|
| `row_count`, `column_count` | DuckDB | Table dimensions |
| `file_size_mb` | sync_state.json | Parquet file size on disk |
| `description`, `primary_key` | data_description.md | Business context |
| `avg_completeness` | DuckDB | Average non-null percentage across columns |
| `missing_cells`, `missing_cells_pct` | DuckDB | Total NULL cells count and percentage |
| `duplicate_rows` | DuckDB | `COUNT(*) - COUNT(DISTINCT *)` |
| `date_range` | DuckDB | Earliest/latest date from date columns |
| `variable_types` | DuckDB | Breakdown by type (STRING, NUMERIC, DATE, BOOLEAN) |
| `alerts` | Computed | Auto-detected data quality issues (see below) |
| `related_tables` | data_description.md | Foreign key relationships (outgoing + incoming) |
| `used_by_metrics` | docs/metrics/*.yml | Which business metrics use this table |
| `sample_rows` | DuckDB | First 5 rows for preview |
| `columns` | DuckDB | Per-column detailed statistics |
| `_stale` | Profiler | `true` if this profile is from a previous run (current profiling failed) |

### Alert System

Auto-detection of data quality issues, displayed as colored badges:

| Alert | Condition | Severity |
|-------|-----------|----------|
| `constant` | `unique_count == 1` | warning (yellow) |
| `unique` | `unique_pct == 100%` | info (red) |
| `high_missing` | `missing_pct > 30%` | error (red) |
| `missing` | `missing_pct > 5%` | warning (yellow) |
| `imbalance` | `top_value_pct > 60%` (categorical) | info (blue) |
| `zeros` | `zero_pct > 50%` (numeric) | info (blue) |
| `high_cardinality` | `unique_count > 50` (text) | info (grey) |

### Type-Specific Column Statistics

| Column Type | Statistics | Visualization |
|-------------|-----------|---------------|
| **STRING** (low cardinality ≤50) | Top 10 values with counts/percentages | Horizontal bar chart |
| **STRING** (high cardinality >50) | min/max/avg length, sample values | Sample list |
| **NUMERIC** (FLOAT64, INT64, DECIMAL) | min, max, mean, median, p5/p25/p75/p95, stddev, zeros | Histogram (10-20 buckets) |
| **DATE/TIMESTAMP** | earliest, latest, span_days | Timeline histogram (quarterly) |
| **BOOLEAN** | true_count, false_count, true_pct | True/false ratio bar |

### Webapp Integration

**API endpoint:** `GET /api/catalog/profile/<table_name>` (requires login)
- Returns JSON profile for a single table from `profiles.json`
- 404 if profiler hasn't run yet or table not found
- 500 if file unreadable (check permissions)

**Catalog page:** Click any table row to open profiler modal with tabs:
- **Overview** — dataset statistics + variable type breakdown
- **Variables** — per-column cards with type-specific charts (Chart.js)
- **Alerts** — all detected issues with colored severity badges
- **Missing Values** — horizontal bar chart of completeness per column
- **Relationships** — foreign key links (clickable to open related table's profile)
- **Sample** — first 5 rows in table format

### Performance

- **Runtime:** ~1-2 minutes for ~29 tables (optimized from ~8min via TABLE materialization + batch queries)
- **Sampling:** Tables >500K rows use `USING SAMPLE 500000 ROWS` for consistent performance
- **Memory:** In-memory DuckDB with temporary tables (dropped after profiling)
- **Output size:** ~900 KB JSON for ~29 tables (including 6 Jira tables)

### Files

| File | Description |
|------|-------------|
| `src/profiler.py` | Profiler engine (~1220 lines) |
| `tests/test_profiler.py` | Unit + integration tests (24 tests) |
| `scripts/update.sh` | Pipeline integration (Step 4) |
| `webapp/app.py` | API route `/api/catalog/profile/<table_name>` |
| `webapp/templates/catalog.html` | Profiler modal UI + Chart.js |

### Monitoring

```bash
# Manual profiler run
ssh kids "cd /opt/data-analyst/repo && source /opt/data-analyst/.venv/bin/activate && python -m src.profiler"

# Check output
ssh kids "ls -la /data/src_data/metadata/profiles.json"
ssh kids "python3 -c \"import json; d=json.load(open('/data/src_data/metadata/profiles.json')); print(f'Tables: {len(d[\\\"tables\\\"])}')\""

# Check update.sh logs (profiler runs as Step 4)
ssh kids "cat /var/log/update.log | grep -A5 'Generating data profiles'"

# Test API endpoint
curl -s https://your-instance.example.com/api/catalog/profile/company | python3 -m json.tool | head -20
```

### Troubleshooting

**"Profile data not available for this table"**
- Profiler hasn't been run yet, or table name doesn't match
- Run manually: `python -m src.profiler` on server
- Note: Since v1.1, profiler preserves old profiles on failure — this should only appear for truly new tables

**HTTP 500 on `/api/catalog/profile/*`**
- Check file permissions: `ls -la /data/src_data/metadata/profiles.json` — must be `644`
- Fix: `sudo chmod 644 /data/src_data/metadata/profiles.json`
- Root cause: `mkstemp()` creates files with `600`; fixed in profiler.py with `os.chmod(0o644)`

**Profiler takes too long**
- Normal runtime is ~1-2 minutes; if significantly longer, check which tables are large in profiler logs
- Sampling threshold is 500K rows (configurable in `src/profiler.py` constant `SAMPLE_THRESHOLD`)
- TABLE materialization + batch queries keep it fast; if DuckDB runs out of memory, check server RAM

**Metrics not showing in profiler**
- Metrics are loaded from `docs/metrics/` directory (split by category: `docs/metrics/*/*.yml`)
- Legacy `docs/metrics.yml` path is still supported but the directory structure takes precedence
- Check that metric files exist: `ls docs/metrics/*/*.yml`

## Corporate Memory

A knowledge sharing system that extracts reusable insights from analysts' personal notes (`CLAUDE.local.md`), lets the team vote on them via a webapp, and syncs upvoted items back to each user's Claude Code rules.

### Architecture

```
┌─────────────────────────────────────┐
│  Analyst Workstations               │
│  ├── CLAUDE.local.md                │  ← Personal notes (synced to server)
│  └── .claude/rules/*.md             │  ← Synced rules from upvoted items
└─────────────────────────────────────┘
         │ sync_data.sh                    ▲ sync_data.sh
         │ (upload CLAUDE.local.md)        │ (download .claude_rules/*)
         ▼                                 │
┌─────────────────────────────────────┐   │
│  Server: /home/{user}/              │   │
│  ├── CLAUDE.local.md                │   │
│  └── .claude_rules/*.md             │───┘
└─────────────────────────────────────┘
         │ corporate-memory.timer (every 30 min)
         ▼
┌─────────────────────────────────────┐
│  Knowledge Collector (full refresh) │
│  ├── MD5 hash change detection      │
│  ├── ALL files + existing catalog   │
│  │   → single Claude Haiku 4.5 call │
│  │     (Structured Outputs)         │
│  ├── Sensitivity check (new items)  │
│  └── Save to knowledge.json        │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  /data/corporate-memory/            │
│  ├── knowledge.json                 │
│  ├── votes.json                     │
│  └── user_hashes.json               │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  Webapp: /corporate-memory          │
│  ├── Browse, search, filter         │
│  ├── Upvote / downvote items        │
│  └── On vote → regenerate user rules│
└─────────────────────────────────────┘
```

### How It Works

#### Collection (server-side, every 30 min)

1. **Analysts write notes** in `CLAUDE.local.md` during their work with Claude Code
2. **`sync_data.sh`** uploads `CLAUDE.local.md` to `/home/{user}/CLAUDE.local.md` on the server
3. **Collector checks for changes** by comparing MD5 hashes of all users' files against `user_hashes.json`
4. **If any file changed**, collector sends ALL users' files + the existing knowledge catalog to **Claude Haiku 4.5** in a single API call (full refresh approach)
5. **Haiku maps knowledge** to existing catalog items (preserving IDs for vote stability) or creates new items
6. **Sensitivity check** runs only on newly created items (existing items were already checked)
7. **Knowledge base** is updated atomically (`tempfile` + `os.replace`)

#### Voting and Rules Sync (webapp → analyst)

1. **Users browse** knowledge at `/corporate-memory` (search, filter by category, sort by score)
2. **Upvoting an item** records the vote in `votes.json` and immediately regenerates the user's rule files
3. **Rule files** are installed to `/home/{server_user}/.claude_rules/{item_id}.md` via the `install-user-rules` sudo helper (see below)
4. **Next `sync_data.sh` run** downloads `.claude_rules/*` to the analyst's `.claude/rules/` directory
5. **Claude Code** automatically reads files from `.claude/rules/` as project context

There is no threshold - any personal upvote syncs the item to that user's rules.

#### Rules Installation (sudo helper)

The webapp runs as `www-data` which cannot write to `/home/{user}/` directories (mode `drwxr-x---`). Rule files are installed using the established **sudo install pattern** (same approach as `sync_settings_service.py` for `.sync_settings.yaml`):

1. Webapp writes rule `.md` files to a temp directory
2. Calls `sudo -n /usr/local/bin/install-user-rules {username} {tmp_dir}`
3. Helper script creates `/home/{user}/.claude_rules/` (mode 700), removes old `km_*.md` files, installs new files with `/usr/bin/install -o {user} -g {user} -m 600`
4. Webapp cleans up the temp directory

**Files involved:**
- `server/bin/install-user-rules` → deployed to `/usr/local/bin/install-user-rules`
- `server/sudoers-webapp` → entry: `www-data ALL=(ALL) NOPASSWD: /usr/local/bin/install-user-rules`
- `webapp/corporate_memory_service.py` → `_regenerate_user_rules()` calls the helper via `subprocess.run()`

### Username Mapping

The webapp uses email-derived usernames (e.g., `john.doe`) while the server uses Linux home directory names (e.g., `john`). Most users match directly; add overrides when they differ.

Mapping is in `webapp/corporate_memory_service.py`:
```python
WEBAPP_TO_SERVER_USERNAME = {
    "john.doe": "john",
}
```

Display names for avatars (initials + tooltip):
```python
USER_DISPLAY_NAMES = {
    "john": {"name": "John Doe", "initials": "JD"},
    "jane.smith": {"name": "Jane Smith", "initials": "DD"},
    "mike.brown": {"name": "Mike Brown", "initials": "MM"},
    "tom.davis": {"name": "Tom Davis", "initials": "JM"},
    "alice.wilson": {"name": "Alice Wilson", "initials": "PD"},
}
```

### Data Files

```
/data/corporate-memory/               # deploy:data-ops, mode 2770
├── knowledge.json                    # Extracted knowledge items + metadata
├── votes.json                        # Per-user votes {username: {item_id: 1/-1}}
├── user_hashes.json                  # MD5 hashes for change detection
└── collection.log                    # Collection run history

/home/{user}/
├── CLAUDE.local.md                   # User's personal notes (source)
└── .claude_rules/                    # Generated rule files (mode 700, owner-only)
    ├── km_abc123.md                  # mode 600, owned by user
    └── km_def456.md
```

**knowledge.json structure:**
```json
{
  "items": {
    "km_abc123": {
      "id": "km_abc123",
      "title": "DuckDB Schema Reference Protocol",
      "content": "Always read schema before queries...",
      "category": "workflow",
      "tags": ["duckdb", "best-practices"],
      "source_users": ["john"],
      "extracted_at": "2026-02-05T21:54:18Z",
      "updated_at": "2026-02-05T21:54:18Z"
    }
  },
  "metadata": {
    "last_collection": "2026-02-05T21:54:18Z",
    "total_users": 3
  }
}
```

**votes.json structure:**
```json
{
  "john": {
    "km_abc123": 1,
    "km_def456": -1
  }
}
```

### Full Refresh Approach

The collector uses a **full refresh** strategy to avoid duplicates:

1. **Change detection**: MD5 hash of each user's `CLAUDE.local.md` is compared against `user_hashes.json`
2. **If no changes**: Skip the API call entirely (saves cost)
3. **If any file changed**: Load ALL user files and the existing catalog
4. **Single Haiku call**: The prompt includes the existing catalog with IDs, so Haiku can:
   - Map knowledge to existing items (preserving `existing_id` for vote stability)
   - Merge similar knowledge from different users into single items
   - Add genuinely new items (assigned new `km_*` IDs)
   - Preserve `source_users` from existing items even if a user removed their notes
5. **Sensitivity check**: Only NEW items (without `existing_id`) are checked - existing items passed the check previously

This approach ensures:
- No duplicates from non-deterministic AI output
- Stable item IDs across runs (votes are preserved)
- Cross-user knowledge merging in a single pass

### Systemd Services

| Service | Type | Schedule | Description |
|---------|------|----------|-------------|
| `corporate-memory.service` | oneshot | on-demand | Runs the knowledge collector |
| `corporate-memory.timer` | timer | every 30 min | Triggers the service |

**Service configuration:**
- Runs as `root` (needed to read `/home/*/CLAUDE.local.md`)
- Group: `data-ops`
- Timeout: 600 seconds (for API calls)
- Security hardening: `ProtectSystem=strict`, `PrivateTmp=true`

### Configuration

**Required GitHub Secret:**

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key for Haiku 4.5 extraction |

The API key is deployed to `/opt/data-analyst/.env` via CI/CD and loaded by the collector service.

**Model:** `claude-haiku-4-5-20251001` with Structured Outputs (`output_config.format.json_schema`)

### Knowledge Categories

| Category | Description |
|----------|-------------|
| `data_analysis` | DuckDB, Parquet, data processing techniques |
| `api_integration` | API usage, HTTP clients, authentication |
| `debugging` | Error diagnosis, troubleshooting techniques |
| `performance` | Optimization, caching, efficiency improvements |
| `workflow` | Best practices, processes, conventions |
| `infrastructure` | Server, deployment, configuration |
| `business_logic` | Domain knowledge, data relationships |

### Extraction Process

The collector uses **Claude Haiku 4.5** with **Structured Outputs** for guaranteed JSON schema compliance:

1. **Catalog refresh prompt** sends all user files + existing catalog to Haiku
2. **JSON Schema** enforces output format including `existing_id` (string or null) for ID preservation
3. **Sensitivity check** verifies only NEW items are safe to share
4. **ID assignment**: Existing items keep their IDs; new items get `km_{uuid[:8]}` format

**Filtering rules (in the prompt):**
- EXCLUDE: API keys, tokens, passwords, credentials
- EXCLUDE: Personal preferences, project-specific paths
- EXCLUDE: Basic knowledge any developer would know
- EXCLUDE: Incomplete or unclear notes
- EXCLUDE: Anything referencing specific people negatively

### Manual Reset

To recalculate the entire knowledge base from scratch (e.g., after fixing duplicates):

```bash
# Reset: clears knowledge.json, votes.json, user_hashes.json, and stale .claude_rules
sudo /usr/local/bin/collect-knowledge --reset --verbose
```

The `--reset` flag:
1. Clears `knowledge.json`, `user_hashes.json`, and `votes.json`
2. Removes stale `.claude_rules/km_*.md` files from all user home directories
3. Runs a fresh collection from all `CLAUDE.local.md` files

This is a manual operation, not part of the regular timer schedule.

### Monitoring

```bash
# Check timer status
sudo systemctl status corporate-memory.timer

# View last collection
sudo journalctl -u corporate-memory -n 50 --no-pager

# Manual collection run
sudo systemctl start corporate-memory.service

# Manual run with verbose output (shows API calls, items found)
sudo /usr/local/bin/collect-knowledge --verbose

# View knowledge base
cat /data/corporate-memory/knowledge.json | python3 -m json.tool

# Check item count
cat /data/corporate-memory/knowledge.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Items: {len(d.get(\"items\", {}))}')"

# Check votes
cat /data/corporate-memory/votes.json | python3 -m json.tool

# Check user hashes (change detection state)
cat /data/corporate-memory/user_hashes.json | python3 -m json.tool

# View a user's synced rules
ls -la /home/john/.claude_rules/
```

### Webapp Integration

The Corporate Memory page at `/corporate-memory` provides:
- **Dashboard stats**: Total items, contributors, categories, last collection time
- **Knowledge cards**: Title, content, category badge, tags, contributor avatars (initials + tooltip)
- **Voting**: Upvote/downvote buttons per item (instantly updates score, regenerates user rules)
- **Filtering**: By category dropdown, text search (title + content + tags)
- **Sorting**: By score (default), by date, by number of contributors
- **"My Rules" toggle**: Shows only items the current user has upvoted
- **User stats**: Number of votes cast, number of active rules

**API endpoints:**
- `GET /api/corporate-memory/knowledge` - List items (supports `category`, `search`, `sort`, `page`, `my_rules` params)
- `POST /api/corporate-memory/vote` - Cast vote `{item_id, vote: 1/-1/0}`
- `GET /api/corporate-memory/stats` - Dashboard statistics

### Security

- **Root access required**: Collector service runs as root to read `/home/*/CLAUDE.local.md`
- **Sudo helper for rules**: Webapp uses `install-user-rules` via sudo to write to user home dirs (same pattern as `sync_settings_service.py`). Each user's `.claude_rules/` is mode 700, files 600 - users cannot read each other's rules.
- **Sensitivity filtering**: Two-pass check (extraction prompt rules + dedicated sensitivity check on new items)
- **No credentials stored**: Knowledge items are filtered before storage
- **Source attribution**: Items track which users contributed (displayed as avatar initials)
- **Read-only for analysts**: `/data/corporate-memory/` is only writable by data-ops group
- **Atomic writes**: All JSON file updates use `tempfile.mkstemp()` + `os.replace()` to prevent corruption. **Critical:** always call `os.fchmod(fd, 0o660)` (or appropriate mode) immediately after `mkstemp()` — otherwise the default `0600` mode overrides the POSIX ACL mask to `---`, breaking group-based access for other services. See [#203](https://github.com/your-org/ai-data-analyst/issues/203).

## Session Collector

Collects Claude Code session transcripts from analyst home directories and stores them centrally.

### Architecture

```
/home/*/user/sessions/   (per-user session transcripts)
         │
         ▼
session-collector.timer  (every 6 hours)
         │
         ▼
/data/user_sessions/     (central storage, root:data-ops, mode 2770)
```

### Systemd Services

| Unit | Type | Schedule | Description |
|------|------|----------|-------------|
| `session-collector.service` | oneshot | on-demand | Runs the session collector |
| `session-collector.timer` | timer | every 6 hours | Triggers the service |

### Monitoring

```bash
sudo systemctl status session-collector.timer
sudo journalctl -u session-collector -n 50 --no-pager
```

### Security

- **Root access required**: Collector runs as root to read `/home/*/user/sessions/`
- **Central storage**: `/data/user_sessions/` is writable only by data-ops group

## WebSocket Gateway

Real-time WebSocket gateway for desktop app notifications and live updates.

### Architecture

```
Desktop App (WebSocket client)
         │
         ▼
ws-gateway.service  (deploy:data-ops)
         │
         ▼
/run/ws-gateway/ws.sock  (unix socket, mode 0755)
```

### Systemd Service

| Unit | Type | Description |
|------|------|-------------|
| `ws-gateway.service` | simple | WebSocket gateway for desktop clients |

### Monitoring

```bash
sudo systemctl status ws-gateway
sudo journalctl -u ws-gateway -n 50 --no-pager
```

### Security

- **JWT authentication**: Desktop clients authenticate via JWT tokens (DESKTOP_JWT_SECRET)
- **Read-only home**: Service has `ProtectHome=read-only`
- **Strict protection**: `ProtectSystem=strict` limits filesystem access

## Google Cloud Monitoring

The server uses **Google Cloud Ops Agent** for centralized logging and metrics collection. All logs and metrics are sent to Google Cloud for analysis, alerting, and debugging.

### What's Collected

**Logs (Fluent Bit → Cloud Logging):**
- All syslog messages (`/var/log/syslog`, `/var/log/messages`)
- systemd journal logs (including service failures, crashes)
- Application logs (if written to syslog/journal)
- Retention: 30 days (default)

**Metrics (OpenTelemetry → Cloud Monitoring):**
- CPU utilization (%)
- Memory usage (%)
- Disk usage (%) per device
- Network traffic (bytes sent/received)
- Load average
- Collection interval: 60 seconds
- Retention: 6 weeks (default)

### Configured Alerts

Alert notifications are sent to:
- **Admin 1** (admin1@your-domain.com)
- **Admin 2** (admin2@your-domain.com)
- **Admin 3** (admin3@your-domain.com)

| Alert | Threshold | Duration | Action |
|-------|-----------|----------|--------|
| **High CPU Usage** | >80% | 5 minutes | Check: `ssh kids 'ps aux --sort=-%cpu \| head -20'` |
| **High Memory Usage** | >90% | 5 minutes | Check: `ssh kids 'free -h && ps aux --sort=-%mem \| head -20'` |
| **High Disk Usage** | >85% | 1 minute | Check: `ssh kids 'df -h && du -sh /data/* \| sort -h'` |
| **Health Endpoint Down** | Uptime check fails | 3 minutes | Check: `ssh kids 'systemctl status webapp'` |
| **Health Endpoint Degraded** | /health returns 503 | 2 minutes | Check: `curl https://your-instance.example.com/health` and review service status |
| **Systemd Service/Timer Failures** | Any failure | 1 minute | Check: `ssh kids 'systemctl --failed && journalctl -xe'` |

### Log-Based Metrics

Custom metrics derived from logs for trend analysis:

| Metric | Description | Filter |
|--------|-------------|--------|
| `systemd_service_failures` | Count of systemd service/timer failures | `"Failed with result" OR "failed with result"` |
| `permission_denied_errors` | Count of Permission denied errors | `"Permission denied"` |
| `health_endpoint_degraded` | Count of /health returning 503 | `"/health" AND ("503" OR "degraded")` |

### Dashboard

**Server Overview Dashboard:**
- Real-time CPU, Memory, Disk, Network graphs
- Systemd service failures
- Health endpoint status
- URL: https://console.cloud.google.com/monitoring/dashboards/custom/09cdd94b-a0ed-4458-952f-3cca2bd5ba6e?project=your-gcp-project

### Health Endpoint & Uptime Monitoring

**Health Endpoint:** https://your-instance.example.com/health

Returns detailed server status in JSON format:
- **Services**: webapp.service, telegram-bot.service
- **Timers**: jira-consistency.timer, corporate-memory.timer, jira-sla-poll.timer
- **Disk usage**: All partitions (/, /data, /home, /tmp)
- **System load**: 1min, 5min, 15min averages
- **Jira webhook**: Last webhook timestamp and age

**Response format:**
```json
{
  "status": "healthy",  // or "degraded"
  "timestamp": "2026-02-13T18:50:33.825333Z",
  "services": [{"name": "webapp.service", "status": "active", "healthy": true}],
  "timers": [{"name": "jira-consistency.timer", "status": "active", "healthy": true}],
  "disk": [
    {"partition": "/", "used_percent": 79.4, "free_gb": 1.98, "healthy": true},
    {"partition": "/data", "used_percent": 39.0, "free_gb": 17.92, "healthy": true}
  ],
  "load": {"load_1min": 0.58, "load_5min": 1.82, "load_15min": 1.85, "healthy": true},
  "jira_webhook": {"last_webhook_hours_ago": 0.0, "healthy": true}
}
```

**HTTP Status Codes:**
- `200 OK` = all checks healthy
- `503 Service Unavailable` = one or more checks failed (status: "degraded")

**Uptime Check:**
- Monitors /health endpoint from 3 global locations (USA, Europe, Asia-Pacific)
- Check interval: 5 minutes
- Timeout: 10 seconds
- Validates response contains `"status": "healthy"`
- Alert triggered if check fails for 3+ minutes

### Viewing Logs

**Cloud Logging Console:**
https://console.cloud.google.com/logs?project=your-gcp-project

**Useful log queries:**

```
# All logs from the server (last 1 hour)
resource.type="gce_instance"
resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"

# systemd service failures
resource.type="gce_instance"
resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"
("Failed with result" OR "Main process exited")

# Permission denied errors
resource.type="gce_instance"
resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"
"Permission denied"

# Webapp errors
resource.type="gce_instance"
resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"
"gunicorn" AND ("ERROR" OR "WARNING")

# Jira webhook processing
resource.type="gce_instance"
resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"
"Received webhook"
```

### Viewing Metrics

**Cloud Monitoring Console:**
https://console.cloud.google.com/monitoring?project=your-gcp-project

**Metrics Explorer** - Useful metric queries:
- CPU: `compute.googleapis.com/instance/cpu/utilization`
- Memory: `agent.googleapis.com/memory/percent_used`
- Disk: `agent.googleapis.com/disk/percent_used`
- Network: `agent.googleapis.com/network/bytes_sent` / `bytes_recv`

### Cost

Google Cloud Monitoring pricing (as of 2026):
- **Logs ingestion**: First 50 GB/month free, then $0.50/GB
- **Metrics ingestion**: First 150 MB/month free, then $0.2580/MB
- **Log storage**: $0.01/GB/month (30-day retention)
- **Typical monthly cost for this server**: ~$5-10 (well within free tier)

Significantly cheaper than Datadog (~$15-31/host/month).

### Managing Alerts

**List alert policies:**
```bash
gcloud alpha monitoring policies list \
  --project=your-gcp-project \
  --format="table(displayName,enabled,conditions[0].conditionThreshold.thresholdValue)"
```

**Disable an alert:**
```bash
gcloud alpha monitoring policies update POLICY_ID \
  --project=your-gcp-project \
  --no-enabled
```

**Add notification channel:**
```bash
gcloud alpha monitoring channels create \
  --project=your-gcp-project \
  --display-name="New Person" \
  --type=email \
  --channel-labels=email_address=person@your-domain.com
```

### Debugging Server Crashes

When investigating server issues (like the 2026-02-13 systemd-journald crash):

1. **View logs around the crash time:**
   - Go to Cloud Logging Console
   - Filter: `resource.labels.instance_id="656c1763-11a1-49bb-bbc3-9782acf15aef"`
   - Set time range to include the crash
   - Look for ERROR/WARNING severity

2. **Check metrics before the crash:**
   - Go to Dashboard or Metrics Explorer
   - View CPU/Memory/Disk graphs for the time period
   - Look for spikes or anomalies

3. **Correlate logs with metrics:**
   - High CPU spike at 15:20? Check logs from that time
   - Memory growth over time? Look for memory leaks in logs

4. **Export for analysis:**
   ```bash
   # Export logs to file
   gcloud logging read "resource.labels.instance_id=\"656c1763-11a1-49bb-bbc3-9782acf15aef\"" \
     --project=your-gcp-project \
     --limit=1000 \
     --format=json \
     --freshness=1d > server_logs.json
   ```

### Best Practices

1. **Structured logging**: Applications should log in JSON format for better searchability
2. **Log levels**: Use appropriate levels (ERROR for problems, INFO for events, DEBUG for details)
3. **Alert fatigue**: Only alert on actionable issues, not informational events
4. **Regular review**: Check dashboard weekly to spot trends before they become problems
5. **Cost monitoring**: If ingestion grows, consider log sampling or exclusion filters
