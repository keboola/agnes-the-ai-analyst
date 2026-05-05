# State directory layout

Agnes splits its persistent data into two tiers:

| Tier | Path | Contents | Backup posture |
|---|---|---|---|
| **data** | `/data` | analytics workspace, extracts, DuckDB caches | regenerable |
| **state** | `${STATE_DIR}` | `system.duckdb`, `.session_secret`, `.jwt_secret`, `certs/*` | irreplaceable |

`STATE_DIR` is an environment variable that selects the host path the state tier is mounted from. Two layouts are supported:

## Layout A — nested (legacy default)

```
sdb at /data
sdc at /data/state    (nested inside the data mount)
```

`STATE_DIR=/data/state` (or unset — that's the default). Used by the original deployment topology.

**Pros**: single bind mount per service (`/data:/data` recursive). Single env var defaults work.

**Cons**:
- Bind-mount propagation matters. Non-recursive bind silently shadows the nested sdc mount, causing the app to write to an invisible subdirectory of sdb. Recovery requires `docker volume rm` + manual data migration.
- Two writers (host's `tls-rotate.timer` running as root; container app running as uid 999) share `/data/state` with different mount-namespace views → ownership conflicts.
- Resizing sdb requires unmounting sdc first.

The 2026-05-05 incident in the Groupon FoundryAI deployment was a manifestation of the propagation gotcha. See PRs in this repo and the deployer's infra repo for full root-cause notes.

## Layout B — flat

```
sdb at /data         (analytics, regenerable)
sdc at /data-state   (state, irreplaceable — parallel to /data, not nested)
```

`STATE_DIR=/data-state`. Two parallel host binds per service: `/data:/data` and `/data-state:/data-state`. Use the `docker-compose.flat-mount.yml` overlay.

**Pros**:
- No nested-mount propagation class. Each disk is its own bind.
- Single writer per disk (host scripts → certs on sdc, container app → DuckDB on sdc; both at the same path).
- sdb resize doesn't touch sdc.
- Direct service binds default to recursive in modern Docker — no `driver_opts` immutability footgun.

**Cons**:
- One-time per-VM migration: tear down `/data/state` mount, mount sdc at `/data-state` instead, copy state contents.
- Two binds per service (slightly more compose YAML).

## Choosing

| Situation | Recommendation |
|---|---|
| Existing deployment, no plans to expand | stay on layout A |
| New deployment | layout B (cleaner, no shadow class) |
| Existing deployment hit by 2026-05-05 shadow class | migrate to layout B |
| CI / local dev | neither (use ephemeral compose volumes) |

## Migration A → B

Steps to move an existing VM from nested to flat:

```bash
# 1. Stop containers
sudo docker compose --env-file /opt/agnes/.env \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml \
  --profile tls down

# 2. Snapshot the existing state
sudo cp -a /data/state /tmp/state-backup-$(date -u +%Y%m%dT%H%M%SZ)

# 3. Unmount sdc from /data/state (its current nested location)
sudo umount /data/state
sudo rmdir /data/state  # remove the now-empty mount point on sdb

# 4. Create the new flat mount point and remount sdc there
sudo mkdir /data-state
echo "LABEL=agnes-state /data-state ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
# (also remove the old /data/state line from fstab)
sudo mount /data-state

# 5. Restore state from the backup
sudo cp -a /tmp/state-backup-*/. /data-state/

# 6. Set STATE_DIR in /opt/agnes/.env
echo "STATE_DIR=/data-state" | sudo tee -a /opt/agnes/.env

# 7. Bring the stack back up with the flat overlay
cd /opt/agnes
sudo docker compose --env-file /opt/agnes/.env \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.flat-mount.yml \
  --profile tls up -d
```

Verify: `sudo docker exec agnes-app-1 ls /data-state` should show `system.duckdb` etc.

## What reads `STATE_DIR`

App code:
- `src/db.py::_get_state_dir()` — the canonical helper. Used by `get_system_db()` and the schema migration snapshot.
- `app/secrets.py::_state_dir()` — for `.session_secret`, `.jwt_secret`. Mirrors the helper since `app/` shouldn't import from `src/`.
- `app/main.py` — for the `.env_overlay` startup file (loaded at process start).

Host scripts:
- `scripts/ops/agnes-auto-upgrade.sh` — mount-sanity check + cert detection.
- `scripts/ops/agnes-tls-rotate.sh` — `CERT_DIR=$STATE_DIR/certs`.

Both scripts source `/opt/agnes/.env` with `set -a`, so adding `STATE_DIR=/data-state` to that file propagates everywhere.

## Caddy cert mount

Caddy mounts the cert directory from the host at `/certs:ro`. The host-side path follows `STATE_DIR/certs`:

- Layout A: `/data/state/certs` (in `docker-compose.yml` directly).
- Layout B: `/data-state/certs` (overridden in `docker-compose.flat-mount.yml`).

Compose-time env substitution happens at `compose up`, not at runtime, so the overlay must be selected at deploy time — there's no single compose YAML that switches based on `STATE_DIR`.
