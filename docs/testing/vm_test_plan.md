# VM Test Plan - Self-Service Data Onboarding

End-to-end test of the full platform on a clean VM with a new GitHub repository.

## Prerequisites

- Clean Ubuntu 22.04+ VM (or Debian 12) with root access
- GitHub account with ability to create repositories
- Domain name pointing to the VM (or use IP + skip SSL)
- Keboola project with Storage API token (for discovery/sync testing)
- Google OAuth credentials (for login testing)

---

## Step 0: Create GitHub Repository & Push

**On your local machine:**

```bash
cd /path/to/agnes-the-ai-analyst

# Create repo on GitHub (pick org/name)
gh repo create YOUR_ORG/ai-data-analyst --private --source=. --push

# Verify
gh repo view YOUR_ORG/ai-data-analyst
```

**Expected:** Repo created, code pushed, visible on GitHub.

---

## Step 1: VM Initial Setup

**On the VM as root:**

```bash
# Clone the repo
REPO_URL="git@github.com:YOUR_ORG/ai-data-analyst.git"
APP_DIR="/opt/data-analyst"
mkdir -p $APP_DIR
ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N ""
# Add deploy key to GitHub repo (Settings -> Deploy keys)

sudo -u deploy git clone $REPO_URL $APP_DIR/repo

# Run setup
cd $APP_DIR/repo
REPO_URL=$REPO_URL bash server/setup.sh
```

### Checklist

| # | Check | Command |
|---|-------|---------|
| 1.1 | Groups created | `getent group data-ops dataread data-private` |
| 1.2 | Deploy user exists | `id deploy` |
| 1.3 | Directory structure | `ls -la /opt/data-analyst/` |
| 1.4 | Python venv works | `/opt/data-analyst/.venv/bin/python -c "import flask; print('OK')"` |
| 1.5 | Management scripts | `which add-analyst list-analysts` |

---

## Step 2: Webapp Setup

```bash
export SERVER_HOSTNAME="data.yourdomain.com"  # or skip SSL with IP
bash server/webapp-setup.sh
```

Then edit `/opt/data-analyst/.env`:

```bash
# Required
WEBAPP_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
GOOGLE_CLIENT_ID="your-google-client-id"
GOOGLE_CLIENT_SECRET="your-google-client-secret"
SERVER_HOST="YOUR_VM_IP"
SERVER_HOSTNAME="data.yourdomain.com"

# For Keboola discovery/sync
KEBOOLA_STORAGE_TOKEN="your-token"
KEBOOLA_STACK_URL="https://connection.keboola.com"
KEBOOLA_PROJECT_ID="your-project-id"
DATA_SOURCE="keboola"
DATA_DIR="/data/src_data"
```

### Checklist

| # | Check | Command |
|---|-------|---------|
| 2.1 | Nginx running | `systemctl status nginx` |
| 2.2 | Webapp running | `systemctl status webapp` |
| 2.3 | SSL cert (if domain) | `curl -I https://data.yourdomain.com/health` |
| 2.4 | Health endpoint | `curl http://localhost:5000/health` (or via nginx) |
| 2.5 | Login page loads | Browser: `https://data.yourdomain.com/login` |

---

## Step 3: Instance Configuration

```bash
cd /opt/data-analyst/repo
cp config/instance.yaml.example config/instance.yaml
```

Edit `config/instance.yaml` with:
- `instance.name` / `instance.subtitle`
- `server.hostname` / `server.host`
- `auth.allowed_domain` (your Google domain)
- `data_source.type: "keboola"` + keboola settings
- `catalog.categories` (at least one, e.g., `crm: {label: "CRM", icon: "crm"}`)

### Checklist

| # | Check | Command |
|---|-------|---------|
| 3.1 | Config loads | `cd /opt/data-analyst/repo && .venv/bin/python -c "from config.loader import load_instance_config; print(load_instance_config())"` |
| 3.2 | Webapp picks it up | Restart webapp, check login page shows instance name |

---

## Step 4: Create Admin Account & Login

1. Login via Google OAuth in browser
2. Register account with SSH key
3. Verify the user is admin:

```bash
id YOUR_USERNAME           # should be in data-ops or sudo group
# If not admin, manually add:
usermod -aG data-ops YOUR_USERNAME
```

### Checklist

| # | Check | Command |
|---|-------|---------|
| 4.1 | Google OAuth works | Login via browser |
| 4.2 | Account created | `list-analysts` shows your username |
| 4.3 | Dashboard loads | Browser: /dashboard shows data stats |
| 4.4 | Admin access | Browser: /admin/tables loads (no 403) |

---

## Step 5: Test Discovery API (Phase 1)

In browser, go to `/admin/tables` and click "Discover tables from source".

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 5.1 | Discovery button works | Loading spinner, then tables appear |
| 5.2 | Tables grouped by bucket | Buckets shown as collapsible sections |
| 5.3 | Table details shown | Name, columns, row count, size for each table |
| 5.4 | "Available" badge | All tables show "Available" (none registered yet) |
| 5.5 | API direct test | `curl -b cookies.txt https://HOST/api/admin/discover-tables \| jq .total` |

---

## Step 6: Test Table Registry (Phase 2)

### 6a: Register tables via Admin UI

1. Click "Register" on a table in discovery results
2. Fill in: sync_strategy=full_refresh, confirm primary key
3. Click "Register Table"
4. Repeat for 2-3 more tables (try incremental too)

### 6b: Verify registry

```bash
# On server
cat /data/src_data/metadata/table_registry.json | python3 -m json.tool | head -30

# Check generated data_description.md
head -10 /opt/data-analyst/repo/docs/data_description.md
# Should show: <!-- AUTO-GENERATED from table_registry.json -->

# Check audit log
cat /data/src_data/metadata/registry_audit.log
```

### 6c: Test via API

```bash
# List registry
curl -b cookies.txt https://HOST/api/admin/registry | jq '.tables | length'

# Update a table
curl -b cookies.txt -X PUT https://HOST/api/admin/registry/in.c-crm.company \
  -H "Content-Type: application/json" \
  -d '{"description": "Updated via API", "version": CURRENT_VERSION}'

# Delete a table
curl -b cookies.txt -X DELETE https://HOST/api/admin/registry/in.c-crm.company \
  -H "Content-Type: application/json" \
  -d '{"version": CURRENT_VERSION}'
```

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 6.1 | Register table | Success, table appears in registry panel |
| 6.2 | Badge changes | Registered tables show green "Registered" badge |
| 6.3 | data_description.md | Generated with AUTO-GENERATED header + checksum |
| 6.4 | Audit log written | Actions logged with timestamps and emails |
| 6.5 | Optimistic locking | Stale version POST returns 409 |
| 6.6 | Edit table | PUT changes description/strategy |
| 6.7 | Delete table | Table removed, badge reverts to "Available" |

---

## Step 7: Test Data Sync + Auto-Profiling (Phase 3)

```bash
cd /opt/data-analyst/repo
source .venv/bin/activate

# Run sync for registered tables
python -m src.data_sync
```

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 7.1 | Sync completes | Tables downloaded, Parquet created |
| 7.2 | Schema.yml generated | `cat docs/schema.yml \| head` |
| 7.3 | Auto-profiling ran | Log shows "Auto-profiling: N profiled" |
| 7.4 | profiles.json exists | `ls -la /data/src_data/metadata/profiles.json` |
| 7.5 | Catalog shows profiles | Browser: /catalog -> click table -> profile data loads |

---

## Step 8: Test Per-Table Subscriptions (Phase 4)

### 8a: Via API

```bash
# Get current subscriptions
curl -b cookies.txt https://HOST/api/table-subscriptions | jq .

# Switch to explicit mode, subscribe to specific tables
curl -b cookies.txt -X POST https://HOST/api/table-subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "table_mode": "explicit",
    "tables": {"company": true, "contact": true, "events": false}
  }'
```

### 8b: Via Catalog UI

1. Go to /catalog
2. Tables should show subscription status (all subscribed in "all" mode)
3. After switching to "explicit" mode via API, unsubscribed tables should be visually different

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 8.1 | Default is "all" mode | GET returns `table_mode: "all"` |
| 8.2 | Switch to explicit | POST succeeds, settings saved |
| 8.3 | Config YAML updated | `cat /home/USERNAME/.sync_settings.yaml` shows `table_mode: explicit` |
| 8.4 | Catalog reflects subs | Subscribed vs unsubscribed tables visually distinct |

---

## Step 9: Test Smart Sync (Phase 5)

### 9a: Check rsync filter generation

```bash
# After setting explicit subscriptions:
cat /home/USERNAME/.sync_rsync_filter
# Should show include/exclude rules
```

### 9b: Test from analyst machine

```bash
# On analyst machine (or simulate):
bash server/scripts/sync_data.sh --dry-run
# Should show filter-based sync when explicit mode is active
```

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 9.1 | Filter file exists | `.sync_rsync_filter` created in user home |
| 9.2 | Correct include/exclude | Subscribed tables included, others excluded |
| 9.3 | Dry-run uses filter | `--filter="merge ..."` in rsync output |
| 9.4 | Fallback works | Without filter file, syncs everything (backwards compat) |

---

## Step 10: Migration Test (One-Time Bootstrap)

If you already have a `docs/data_description.md` with tables defined:

```bash
python3 -c "
from src.table_registry import TableRegistry
from pathlib import Path

registry = TableRegistry.import_from_data_description(
    Path('docs/data_description.md'),
    Path('/data/src_data/metadata/table_registry.json'),
    registered_by='migration@test.com'
)
print(f'Migrated {len(registry.list_tables())} tables')
print(f'Version: {registry.version}')
"
```

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 10.1 | Migration succeeds | All tables imported |
| 10.2 | Registry JSON valid | `cat table_registry.json \| python3 -m json.tool` |
| 10.3 | migrated_from marker | `"migrated_from": "docs/data_description.md"` in metadata |
| 10.4 | Admin UI shows tables | /admin/tables lists all migrated tables |

---

## Step 11: Regression Tests

```bash
cd /opt/data-analyst/repo
source .venv/bin/activate
python -m pytest tests/ -v
```

### Checklist

| # | Check | Expected |
|---|-------|----------|
| 11.1 | All tests pass | 132+ tests, 0 failures |
| 11.2 | No import errors | All modules load cleanly |

---

## Quick Smoke Test Script

Run this after full setup to verify the critical path:

```bash
#!/bin/bash
# smoke_test.sh - Quick verification of self-service onboarding
set -e

APP_DIR="/opt/data-analyst/repo"
cd "$APP_DIR"
source .venv/bin/activate

echo "=== Smoke Test ==="

# 1. Tests
echo "[1/5] Running tests..."
python -m pytest tests/ -q --tb=short
echo "  PASS"

# 2. Registry module
echo "[2/5] Testing Table Registry..."
python -c "
from src.table_registry import TableRegistry
from pathlib import Path
import tempfile
r = TableRegistry(Path(tempfile.mktemp(suffix='.json')))
r.register_table({'id': 'test.t', 'name': 't', 'primary_key': 'id', 'sync_strategy': 'full_refresh'}, 'test')
assert r.is_registered('test.t')
r.unregister_table('test.t')
assert not r.is_registered('test.t')
print('  PASS')
"

# 3. Discovery (needs Keboola credentials)
echo "[3/5] Testing Discovery API..."
python -c "
try:
    from src.data_sync import create_data_source
    ds = create_data_source()
    tables = ds.discover_tables()
    print(f'  PASS - Discovered {len(tables)} tables')
except Exception as e:
    print(f'  SKIP - {e}')
"

# 4. Profiler API
echo "[4/5] Testing Profiler API..."
python -c "
from src.profiler import profile_changed_tables
result = profile_changed_tables([])
assert result == {'success': 0, 'errors': 0, 'skipped': 0}
print('  PASS')
"

# 5. Webapp imports
echo "[5/5] Testing Webapp imports..."
python -c "
from webapp.auth import admin_required, login_required
from webapp.sync_settings_service import get_table_subscriptions, generate_rsync_filter
from src.table_registry import TableRegistry, ConflictError
print('  PASS')
"

echo ""
echo "=== All smoke tests passed ==="
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `/admin/tables` returns 403 | User not in `data-ops` group. Run `usermod -aG data-ops USERNAME` |
| Discovery returns empty | Check `KEBOOLA_STORAGE_TOKEN` in `.env`, verify `DATA_SOURCE=keboola` |
| Profiles not generated | Check `/data/src_data/parquet/` has parquet files, check DuckDB installed |
| Rsync filter not created | Check `sudo` permissions for `www-data` in sudoers-webapp |
| `data_description.md` not updating | Check write permissions on `docs/` directory |
| Webapp won't start | Check `journalctl -u webapp -n 50` for errors |
