# Disaster Recovery

Recovery procedures for the Data Broker Server (`data-broker-for-claude`).

## Overview

```
Disk Layout:
  sda (10 GB) /         System disk (instance) - EXPENDABLE
  sdb (30 GB) /data     Data disk - SNAPSHOTTED daily
  sdc (30 GB) /home     Home disk - SNAPSHOTTED daily
```

**Key principle**: sda is disposable. Everything on it is either in git or can be reinstalled. All unique data lives on sdb and sdc, which are independently snapshotted.

## What Lives Where

| Location | Content | Recovery Method |
|----------|---------|-----------------|
| sda: `/opt/data-analyst/repo/` | Application code | `git clone` from GitHub |
| sda: `/opt/data-analyst/.venv/` | Python packages | `pip install -r requirements.txt` |
| sda: `/opt/data-analyst/.env` | Application secrets | deploy.sh creates from GitHub secrets |
| sda: `/etc/sudoers.d/` | Permissions | deploy.sh copies from repo |
| sda: `/etc/security/limits.d/` | Resource limits | deploy.sh copies from repo |
| sda: `/etc/nginx/` | Nginx config | deploy.sh or manual copy from repo |
| sda: `/etc/letsencrypt/` | SSL certificate | `certbot` renews automatically |
| sdb: `/data/src_data/parquet/` | Parquet data | Regenerate from Keboola (`update.sh`) or restore snapshot |
| sdb: `/data/notifications/` | Notification state | Restore from snapshot |
| sdb: `/data/docs/`, `/data/scripts/` | Docs & scripts | deploy.sh copies from repo |
| sdc: `/home/*/` | User accounts, SSH keys, workspaces, scripts | Restore from snapshot |

## Scenario A: System Disk Failure (sda dies)

**Impact**: Server is down, but all user data is safe on sdb/sdc.

**Recovery time**: ~30 minutes

### Steps

1. **Create new VM** (same zone, attach existing disks):
   ```bash
   # Create new instance with existing disks
   gcloud compute instances create data-broker-for-claude \
     --project=kids-ai-data-analysis \
     --zone=europe-north1-a \
     --machine-type=e2-medium \
     --image-family=debian-12 \
     --image-project=debian-cloud \
     --boot-disk-size=10GB \
     --tags=http-server,https-server

   # Attach existing data disks
   gcloud compute instances attach-disk data-broker-for-claude \
     --project=kids-ai-data-analysis \
     --zone=europe-north1-a \
     --disk=data-disk

   gcloud compute instances attach-disk data-broker-for-claude \
     --project=kids-ai-data-analysis \
     --zone=europe-north1-a \
     --disk=home-disk
   ```

2. **SSH in and mount disks**:
   ```bash
   # Mount data disk
   mkdir -p /data
   mount /dev/sdb /data

   # Mount home disk
   mount /dev/sdc /home

   # Add to fstab (get UUIDs with blkid)
   echo "UUID=$(blkid -s UUID -o value /dev/sdb) /data ext4 discard,defaults,nofail 0 2" >> /etc/fstab
   echo "UUID=$(blkid -s UUID -o value /dev/sdc) /home ext4 discard,defaults,nofail 0 2" >> /etc/fstab
   ```

3. **Install prerequisites**:
   ```bash
   apt-get update
   apt-get install -y git python3.11-venv python3-pip nginx certbot python3-certbot-nginx
   ```

4. **Recreate deploy user and groups**:
   ```bash
   # Create groups
   groupadd dataread
   groupadd data-private
   groupadd data-ops

   # Create deploy user
   useradd -m -s /bin/bash deploy
   usermod -aG data-ops deploy

   # Restore deploy SSH key (generate new one)
   sudo -u deploy ssh-keygen -t ed25519 -f /home/deploy/.ssh/id_ed25519 -N '' -C 'deploy@data-broker'
   sudo -u deploy bash -c 'echo -e "Host github.com\n  IdentityFile ~/.ssh/id_ed25519\n  StrictHostKeyChecking accept-new" > /home/deploy/.ssh/config'
   chmod 600 /home/deploy/.ssh/config

   # Add new public key to GitHub as Deploy Key
   cat /home/deploy/.ssh/id_ed25519.pub
   ```

5. **Clone repo and run setup**:
   ```bash
   mkdir -p /opt/data-analyst
   chown deploy:data-ops /opt/data-analyst
   sudo -u deploy git clone git@github.com:keboola/internal_ai_data_analyst.git /opt/data-analyst/repo
   git config --global --add safe.directory /opt/data-analyst/repo
   /opt/data-analyst/repo/server/setup.sh
   ```

6. **Restore user accounts from /home**:
   ```bash
   # Users already exist on home-disk, just recreate /etc/passwd entries
   # For each directory in /home (except deploy):
   for dir in /home/*/; do
     username=$(basename "$dir")
     [[ "$username" == "deploy" ]] && continue
     # Create user if not exists
     if ! id "$username" &>/dev/null; then
       useradd -M -d "/home/$username" -s /bin/bash "$username"
       usermod -aG dataread "$username"
     fi
   done
   ```
   Note: Group memberships (data-private, sudo, data-ops) need manual review. Check the admin list in `server/limits-users.conf` for admin users.

7. **Trigger deploy via GitHub Actions** (or manually):
   ```bash
   sudo -u deploy bash -c 'cd /opt/data-analyst/repo && ./server/deploy.sh'
   ```

8. **Set up SSL certificate**:
   ```bash
   certbot --nginx -d your-instance.example.com
   ```

9. **Restore crontab**:
   ```bash
   sudo -u deploy crontab -e
   # Add:
   # MAILTO=admin@your-domain.com
   # 0 6,14,19 * * * cd /opt/data-analyst/repo && ./scripts/update.sh > /var/log/update.log 2>&1 || cat /var/log/update.log
   ```

10. **Update external IP** if it changed:
    - DNS: `your-instance.example.com` A record
    - GitHub secrets: `SERVER_HOST`
    - SSH configs of all users

## Scenario B: Data Disk Failure (sdb/data-disk dies)

**Impact**: Parquet data lost, users unaffected.

**Recovery time**: ~10 minutes (from snapshot) or ~30 minutes (from Keboola)

### Option 1: Restore from snapshot (faster)

```bash
# Find latest snapshot
gcloud compute snapshots list --project=kids-ai-data-analysis \
  --filter="sourceDisk:data-disk" --sort-by=~creationTimestamp --limit=5

# Create new disk from snapshot
gcloud compute disks create data-disk \
  --project=kids-ai-data-analysis \
  --zone=europe-north1-a \
  --source-snapshot=SNAPSHOT_NAME \
  --type=pd-balanced

# Attach to VM (may need to stop VM first)
gcloud compute instances attach-disk data-broker-for-claude \
  --project=kids-ai-data-analysis \
  --zone=europe-north1-a \
  --disk=data-disk

# Mount
ssh kids "sudo mount /dev/sdb /data"
```

### Option 2: Regenerate from Keboola

```bash
# Create fresh disk
gcloud compute disks create data-disk \
  --project=kids-ai-data-analysis \
  --zone=europe-north1-a \
  --size=30GB \
  --type=pd-balanced

# Attach, format, mount
ssh kids "sudo mkfs.ext4 /dev/sdb && sudo mount /dev/sdb /data"

# Run deploy to recreate directory structure
ssh kids "sudo -u deploy bash -c 'cd /opt/data-analyst/repo && ./server/deploy.sh'"

# Regenerate parquet data from Keboola
ssh kids "cd /opt/data-analyst/repo && ./scripts/update.sh"
```

## Scenario C: Home Disk Failure (sdc/home-disk dies)

**Impact**: All user accounts, SSH keys, and personal workspaces lost.

**Recovery time**: ~10 minutes (from snapshot)

### Restore from snapshot

```bash
# Find latest snapshot
gcloud compute snapshots list --project=kids-ai-data-analysis \
  --filter="sourceDisk:home-disk" --sort-by=~creationTimestamp --limit=5

# Create new disk from snapshot
gcloud compute disks create home-disk \
  --project=kids-ai-data-analysis \
  --zone=europe-north1-a \
  --source-snapshot=SNAPSHOT_NAME \
  --type=pd-balanced

# Attach to VM
gcloud compute instances attach-disk data-broker-for-claude \
  --project=kids-ai-data-analysis \
  --zone=europe-north1-a \
  --disk=home-disk

# Mount
ssh kids "sudo mount /dev/sdc /home"
```

If no snapshot exists, users must re-register via https://your-instance.example.com.

## Scenario D: Complete Server Loss (VM + all disks)

**Recovery time**: ~45 minutes

1. Follow **Scenario A** steps 1-5 (new VM, prerequisites, deploy user)
2. Restore `data-disk` from snapshot (Scenario B, Option 1)
3. Restore `home-disk` from snapshot (Scenario C)
4. Follow **Scenario A** steps 6-10 (user accounts, deploy, SSL, cron, IP)

## Verification Checklist

After any recovery, verify:

- [ ] `ssh kids` works (admin access)
- [ ] `https://your-instance.example.com` loads (webapp)
- [ ] `https://your-instance.example.com/health` returns OK
- [ ] At least one analyst can SSH in
- [ ] `ls /data/src_data/parquet/` shows data
- [ ] `ls /home/` shows user directories
- [ ] `systemctl status webapp` is active
- [ ] `systemctl status notify-bot` is active
- [ ] `sudo crontab -u deploy -l` shows data sync cron

## Preventive Measures

- **GCP snapshots**: Daily automatic snapshots of `data-disk` and `home-disk` (14-day retention)
- **Setup script**: `server/setup-snapshot-schedule.sh` configures snapshot policy
- **Limits in git**: `server/limits-users.conf` is version-controlled and deployed automatically
- **All configs in git**: sudoers, nginx, systemd services, management scripts
- **Secrets in GitHub**: `.env` is recreated by deploy.sh from GitHub Actions secrets
