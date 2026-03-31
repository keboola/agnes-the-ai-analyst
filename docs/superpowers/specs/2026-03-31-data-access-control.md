# Data Access Control — Spec

**Date:** 2026-03-31
**Status:** Draft

## 1. Problem

V novém systému (API místo rsync) nemáme ekvivalent rsync filtru. Každý přihlášený uživatel vidí a stáhne všechny tabulky. V produkci to řeší filesystem permissions + per-user rsync filter.

## 2. Současný model (produkce, rsync)

```
Server: /data/src_data/parquet/
├── crm/orders.parquet          ← dataread group
├── crm/customers.parquet       ← dataread group
├── private/salaries.parquet    ← data-private group only
└── jira/issues/2026-03.parquet ← dataread group

Analytik (sync_data.sh):
1. Webapp generuje ~/.sync_rsync_filter (include/exclude per tabulka)
2. rsync --filter="merge ~/.sync_rsync_filter" stáhne jen povolené
3. AI agent pracuje s lokálními soubory → vidí jen to co se stáhlo
```

Tři vrstvy:
- **Linux skupiny** (dataread, data-private) → hrubé řízení
- **Datasety** (opt-in v instance.yaml) → celé skupiny tabulek
- **Per-table subscription** (explicit mode) → jednotlivé tabulky

## 3. Nový model (API)

Princip zůstává: **uživatel vidí jen to, k čemu má explicitní přístup**.

### 3.1 Datový model

Stávající tabulka `dataset_permissions` v DuckDB:

```sql
CREATE TABLE dataset_permissions (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,    -- table_id nebo dataset group name
    access VARCHAR DEFAULT 'read',  -- 'read', 'none'
    PRIMARY KEY (user_id, dataset)
);
```

`dataset` může být:
- **Table ID** (`circle`, `chart_of_accounts`) — přístup k jedné tabulce
- **Wildcard/group** (`in.c-finance.*`) — přístup ke všem tabulkám v bucketu
- **Dataset name** (`jira`, `finance`) — pojmenovaná skupina z instance.yaml

### 3.2 Pravidla přístupu

```
Admin → vidí vše (bypass permissions)
Ostatní → vidí jen tabulky kde:
  1. Existuje explicitní permission (dataset_permissions.access = 'read')
  2. NEBO tabulka patří do povoleného datasetu/bucketu
  3. NEBO je tabulka public (nový flag v table_registry)
```

### 3.3 Nový sloupec v table_registry

```sql
ALTER TABLE table_registry ADD COLUMN is_public BOOLEAN DEFAULT true;
```

- `is_public = true` → každý přihlášený uživatel vidí (default, zpětně kompatibilní)
- `is_public = false` → vyžaduje explicitní permission

## 4. Kde se kontroluje

### 4.1 Manifest (`GET /api/sync/manifest`)

```python
# Současný kód (NEFUNGUJE):
accessible = set(perm_repo.get_accessible_datasets(user["id"]))
# ... ale nikdy nefiltruje

# Nový kód:
all_states = repo.get_all_states()
if user["role"] != "admin":
    all_states = [s for s in all_states if _user_can_access(user, s["table_id"])]
```

### 4.2 Download (`GET /api/data/{table}/download`)

```python
# Současný kód (ŽÁDNÁ KONTROLA):
return FileResponse(path=file_path)

# Nový kód:
if not _user_can_access(user, table_id):
    raise HTTPException(403, "Access denied")
return FileResponse(path=file_path)
```

### 4.3 Query (`POST /api/query`)

```python
# Současný kód: otevře analytics.duckdb s VŠEMI views

# Nový kód: vytvořit per-user filtered connection
# Varianta A: CREATE TEMP VIEW pro povolené tabulky
# Varianta B: Dynamicky generovat allowed list, validovat SQL against it
```

Query je nejtěžší — uživatel může napsat `SELECT * FROM salaries` a pokud view existuje v analytics.duckdb, data se vrátí. Řešení:

**Varianta A — Filtered views (doporučeno):**
Per-request vytvoření in-memory DuckDB, ATTACH analytics.duckdb, vytvořit views jen pro povolené tabulky. Overhead ~10ms.

**Varianta B — SQL validation:**
Parsovat SQL, extrahovat referenced tables, ověřit proti allowed list. Křehké (sub-queries, CTEs, aliasy).

### 4.4 Catalog (`GET /api/catalog/tables`)

```python
# Filtrovat jako manifest — uživatel vidí metadata jen povolených tabulek
if user["role"] != "admin":
    tables = [t for t in tables if _user_can_access(user, t["id"])]
```

## 5. Shared helper

```python
# src/rbac.py — rozšíření

def can_access_table(user: dict, table_id: str) -> bool:
    """Check if user can access a specific table."""
    # Admin bypass
    if user.get("role") == "admin":
        return True

    # Check if table is public
    table = TableRegistryRepository(conn).get(table_id)
    if table and table.get("is_public", True):
        return True

    # Check explicit permission
    user_id = user["id"]
    if DatasetPermissionRepository(conn).has_access(user_id, table_id):
        return True

    # Check wildcard/bucket permission (e.g., "in.c-finance.*")
    bucket = table.get("bucket", "") if table else ""
    if bucket and DatasetPermissionRepository(conn).has_access(user_id, f"{bucket}.*"):
        return True

    return False
```

## 6. Admin API pro permissions

```
POST   /api/admin/permissions          — grant access
DELETE /api/admin/permissions           — revoke access
GET    /api/admin/permissions/{user_id} — list user's permissions
GET    /api/admin/permissions           — list all (admin only)

POST body: {"user_id": "...", "dataset": "circle", "access": "read"}
```

## 7. Migrace

### Pro existující instance:
1. Všechny stávající tabulky: `is_public = true` (zachová současné chování)
2. Admin nastaví `is_public = false` pro citlivé tabulky
3. Přidá explicitní permissions pro uživatele

### Pro nové instance:
- Default `is_public = true` → otevřený model (jako teď)
- Admin může přepnout na uzavřený: `is_public = false` per tabulka

## 8. CLI (`da sync`)

```
da sync
  → GET /api/sync/manifest (vrátí jen povolené tabulky)
  → pro každou tabulku: GET /api/data/{table}/download
  → rebuild lokální DuckDB jen z povolených parquetů
  → AI agent vidí jen to co se stáhlo
```

Identický princip jako rsync filter — ale filtr je server-side v API, ne v souboru.

## 9. Co se NEMĚNÍ

- Role hierarchy (viewer < analyst < km_admin < admin)
- Admin vidí vše
- JWT auth flow
- Orchestrator + extractory (server-side, vidí vše)
- Sync trigger (admin-only, stahuje vše na server)

## 10. Implementační pořadí

1. `is_public` sloupec v table_registry (schema v3)
2. `can_access_table()` helper v src/rbac.py
3. Filtrování v manifest + download + catalog
4. Admin permissions API
5. Query endpoint — filtered views
6. Testy
