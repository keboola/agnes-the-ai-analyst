# Disaster Recovery

Recovery procedures for the AI Data Analyst Docker deployment.

## Overview

```
What lives where:
  Docker volumes  /data        DuckDB files, parquet extracts, state
  Git             repo/        Application code — rebuild from GitHub
  .env            secrets      Recreate from GitHub Secrets / 1Password
```

**Key principle**: the container is disposable. All unique data lives in the `/data`
Docker volume (or a GCP persistent disk mounted at `/data`). Re-pulling the image
and restoring `/data` brings the service back to full operation.

## Data Layout

| Path | Content | Backup |
|------|---------|--------|
| `/data/state/system.duckdb` | Table registry, users, sync state | Daily snapshot |
| `/data/analytics/server.duckdb` | Master analytics DB (views) | Regenerated on start |
| `/data/extracts/*/extract.duckdb` | Per-source extract DBs | Daily snapshot |
| `/data/extracts/*/data/*.parquet` | Parquet files (local sources) | Daily snapshot |

`analytics/server.duckdb` is rebuilt automatically by `SyncOrchestrator.rebuild()`
on every startup, so it does not need to be backed up separately.

## Scenario A: Container Crash / Bad Deploy

**Impact**: Service down, data intact.

**Recovery time**: ~2 minutes

```bash
# Pull latest image and restart
docker compose pull
docker compose up -d

# Check health
curl https://your-instance.example.com/health
```

If a bad image was pushed, roll back to the previous tag:
```bash
docker compose down
# Edit docker-compose.yml to pin the previous image tag
docker compose up -d
```

## Scenario B: /data Volume Corruption or Loss

**Impact**: All DuckDB state and parquet data lost.

**Recovery time**: ~10 minutes (from snapshot) or ~30 minutes (regenerate from source)

### Option 1: Restore from GCP disk snapshot (faster)

```bash
# Find latest snapshot
gcloud compute snapshots list --project=your-gcp-project \
  --filter="sourceDisk:data-disk" --sort-by=~creationTimestamp --limit=5

# Create new disk from snapshot
gcloud compute disks create data-disk \
  --project=your-gcp-project \
  --zone=europe-north1-a \
  --source-snapshot=SNAPSHOT_NAME \
  --type=pd-balanced

# Attach to VM and mount
gcloud compute instances attach-disk your-server \
  --project=your-gcp-project \
  --zone=europe-north1-a \
  --disk=data-disk

# Restart containers
docker compose up -d
```

### Option 2: Regenerate from source

```bash
# Start with empty /data volume
docker compose up -d

# Trigger a full sync from the data source
curl -X POST http://localhost:8000/api/sync/trigger
# Or via CLI:
docker compose exec app da sync
```

DuckDB extract files and parquet will be repopulated from Keboola / BigQuery.
`system.duckdb` (table registry, users) must be restored from snapshot if
not regenerated — user accounts and table definitions are not recreated by sync.

## Scenario C: Complete VM Loss

**Recovery time**: ~20 minutes

1. **Create new VM** (or use managed instance group):
   ```bash
   gcloud compute instances create your-server \
     --project=your-gcp-project \
     --zone=europe-north1-a \
     --machine-type=e2-medium \
     --image-family=debian-12 \
     --image-project=debian-cloud
   ```

2. **Install Docker**:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. **Attach and mount the data disk** (or restore from snapshot per Scenario B):
   ```bash
   gcloud compute instances attach-disk your-server \
     --project=your-gcp-project --zone=europe-north1-a --disk=data-disk
   # Add mount to /etc/fstab and mount /data
   ```

4. **Clone repo and create .env**:
   ```bash
   git clone git@github.com:your-org/ai-data-analyst.git /opt/data-analyst
   cd /opt/data-analyst
   cp config/.env.template .env
   # Fill in secrets from GitHub Secrets / 1Password
   ```

5. **Start the stack**:
   ```bash
   docker compose up -d
   ```

6. **Update DNS** if the external IP changed:
   - A record for `your-instance.example.com`

## Verification Checklist

After any recovery, verify:

- [ ] `docker compose ps` — all services `Up`
- [ ] `https://your-instance.example.com/health` returns `{"status": "ok"}`
- [ ] Login works (Google OAuth or email magic link)
- [ ] At least one table appears in the data catalog
- [ ] `docker compose logs app` — no ERROR lines at startup

## Preventive Measures

- **GCP snapshots**: Daily automatic snapshots of the `/data` persistent disk
  (14-day retention). Configure via:
  ```bash
  gcloud compute resource-policies create snapshot-schedule daily-backup \
    --project=your-gcp-project \
    --region=europe-north1 \
    --max-retention-days=14 \
    --on-source-disk-delete=keep-auto-snapshots \
    --daily-schedule \
    --start-time=03:00
  gcloud compute disks add-resource-policies data-disk \
    --project=your-gcp-project --zone=europe-north1-a \
    --resource-policies=daily-backup
  ```
- **Secrets in GitHub / 1Password**: `.env` is never committed; recreate from stored secrets
- **Image tags**: Pin a known-good image tag in `docker-compose.yml` before each deploy
