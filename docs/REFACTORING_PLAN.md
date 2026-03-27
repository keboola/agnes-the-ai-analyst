# Refaktoring AI Data Analyst — Finální plán

## Kontext

Platforma vznikla iterativně pro interní Keboola a nyní se má stát produktem pro zákazníky (Groupon aj.). Klíčové problémy z transcriptu ZS+Padák: křehký filesystem stav (JSON soubory, permission konflikty), žádné API (vše SSH+skripty), bezpečnost přes Linux skupiny, složitá instalace (10+ kroků). Systém je navržen pro AI agenty — člověk diskutuje s AI, AI řeší vše (user, admin, dev operace).

**UX zůstává stejné.** Tooling: `uv` všude místo pip. Docker + Kamal pro server. CLI (`da`) jako primární rozhraní pro AI agenty.

---

## Architektura — cílový stav

```
SERVER (Docker + Kamal):
├── webapp        Flask UI (katalog, login, corporate memory)
├── api           FastAPI (CLI backend, sync manifest, data download)
├── scheduler     APScheduler (nahrazuje 7 systemd timerů)
├── telegram-bot  Telegram notifikace
├── ws-gateway    WebSocket pro desktop app
└── script-runner Sandboxovaný runner pro user skripty

LOKÁLNĚ (analytik):
├── da CLI        Python balíček (uv tool install)
├── DuckDB        Embedded (analytics.duckdb → views na parquety)
└── Parquety      Stažené ze serveru přes da sync

DVA DuckDB NA SERVERU:
├── /data/state/system.duckdb      Systémový stav (users, sync_state, knowledge...)
└── /data/analytics/server.duckdb  Views → /data/parquet/** (profiler, remote query, skripty)

JEDEN DuckDB LOKÁLNĚ:
└── user/duckdb/analytics.duckdb   Views → server/parquet/** + user tabulky
```

---

## Fáze 0: Základ — DuckDB state + repository vrstva

**Cíl:** Nahradit 10+ JSON souborů DuckDB databází. Eliminovat #1 zdroj outages (file permission konflikty).

**Proč DuckDB:** Už v stacku, agent může joinovat stav s analytickými daty, lepší než SQLite pro analytické dotazy nad stavem.

### Task 0A: DuckDB schema + repository vrstva [INDEPENDENT]

Nové soubory:
- `src/db.py` — DuckDB connection management, schema creation, migration system
- `src/repositories/__init__.py`
- `src/repositories/sync_state.py` — CRUD pro sync stav
- `src/repositories/users.py` — CRUD pro uživatele + role
- `src/repositories/knowledge.py` — CRUD pro corporate memory
- `src/repositories/table_registry.py` — CRUD pro registr tabulek
- `src/repositories/audit.py` — audit log
- `src/repositories/notifications.py` — telegram links, pending codes, script registry

Schema tabulky (mapování z JSON):

| Současný JSON | DuckDB tabulka | Zdroj soubor |
|---|---|---|
| `sync_state.json` | `sync_state` | `src/data_sync.py:37-138` |
| `sync_settings.json` | `user_sync_settings` | `webapp/sync_settings_service.py:20` |
| `knowledge.json` | `knowledge_items` | `webapp/corporate_memory_service.py` |
| `votes.json` | `knowledge_votes` | `webapp/corporate_memory_service.py` |
| `audit.jsonl` | `audit_log` | `webapp/corporate_memory_service.py` |
| `telegram_users.json` | `telegram_links` | `services/telegram_bot/storage.py` |
| `pending_codes.json` | `pending_codes` | `services/telegram_bot/storage.py` |
| `password_users.json` | `users` | `webapp/password_auth.py` |
| `table_registry.json` | `table_registry` | `src/table_registry.py` |
| `profiles.json` | `table_profiles` | `src/profiler.py` |

Přidat navíc: `sync_history` (posledních 10 syncí per tabulka, ne jen last), `script_registry` (deployed skripty).

### Task 0B: Migrace existujících service souborů na repository [DEPENDS ON 0A]

Soubory k úpravě (nahradit `_read_json`/`_write_json` za repository volání):
- `webapp/sync_settings_service.py` řádky 40-62
- `webapp/corporate_memory_service.py` — 31 JSON operací
- `webapp/telegram_service.py` řádky 22-45
- `src/data_sync.py` — třída `SyncState` řádky 37-138
- `src/table_registry.py` — `_load`, `_atomic_write_json`
- `src/profiler.py` — uložení profilů
- `services/corporate_memory/collector.py` — čtení/zápis knowledge
- `services/telegram_bot/storage.py` — 15 JSON operací

Pattern: dual-write (JSON + DuckDB) po přechodnou dobu → ověřit → smazat JSON zápisy.

### Task 0C: Migrační skript [DEPENDS ON 0A]

- `scripts/migrate_json_to_duckdb.py` — načte všechny JSON, vloží do DuckDB
- Idempotentní (safe to run multiple times)
- Validace po migraci (count porovnání)

### Co se NEMĚNÍ v Fázi 0
- Flask routes v `webapp/app.py`
- HTML šablony
- Konektory (`connectors/keboola/`, `connectors/bigquery/`, `connectors/jira/`)
- `src/config.py` (čte `data_description.md` — konfigurace, ne stav)
- `config/loader.py` (čte `instance.yaml`)
- `src/parquet_manager.py`

---

## Fáze 1: API vrstva (FastAPI)

**Cíl:** REST API pro CLI. Všechny operace co dnes vyžadují SSH.

### Task 1A: FastAPI základ + auth [INDEPENDENT od 0B, DEPENDS ON 0A]

Nové soubory:
```
api/
  __init__.py
  app.py              # FastAPI app, middleware, CORS
  auth.py             # JWT vydávání + validace
  dependencies.py     # DI pro DuckDB session, current_user
```

Auth flow:
1. `POST /api/auth/login` — přijme OAuth token z webappu, vydá JWT
2. `POST /api/auth/token` — přijme API key, vydá JWT
3. JWT obsahuje: user_id, email, role, expiry
4. Middleware validuje JWT na všech /api/* endpoints

### Task 1B: Sync + Data endpointy [DEPENDS ON 1A, 0A]

```
api/routers/
  sync.py             # GET /api/sync/manifest, POST /api/sync/trigger
  data.py             # GET /api/data/{table}/download (parquet stream)
```

- `/api/sync/manifest` — vrátí hashe všech parquetů, docs, rules, profilů (filtrované per-user dle subscription)
- `/api/data/{table}/download` — streaming parquet souboru s ETag/If-None-Match
- `/api/sync/trigger` — spustí DataSyncManager (reuse `src/data_sync.py`)

### Task 1C: Query + Scripts endpointy [DEPENDS ON 1A, 0A]

```
api/routers/
  query.py            # POST /api/query (remote query)
  scripts.py          # POST /api/scripts/run, /deploy, /list
```

- `/api/query` — reuse `src/remote_query.py`, výsledek jako JSON/parquet
- `/api/scripts/run` — spustí Python skript v sandboxu na serveru
- `/api/scripts/deploy` — nahraje skript + registruje v scheduleru
- `/api/scripts/list` — deployed skripty s jejich schedules

### Task 1D: User management + Corporate memory endpointy [DEPENDS ON 1A, 0A]

```
api/routers/
  users.py            # CRUD uživatelů, role, permissions
  settings.py         # GET/PUT sync settings per user
  memory.py           # Corporate memory CRUD, voting, governance
  health.py           # GET /api/health (strukturovaná diagnostika)
  upload.py           # POST sessions, artifacts, CLAUDE.local.md
```

### Task 1E: Odstranění SSH/sudo závislostí [DEPENDS ON 1B, 1D]

Smazat/přepsat:
- `webapp/sync_settings_service.py` řádky 128-240 (sudo/rsync-filter kód)
- `webapp/user_service.py` — Linux user management (`pwd.getpwnam`, `sudo add-analyst`)
- SSH key validace workflow
- `server/sudoers-webapp`, `server/sudoers-deploy`
- `server/bin/add-analyst`

---

## Fáze 2: CLI nástroj (`da`)

**Cíl:** Jediné rozhraní pro AI agenty. Nahrazuje SSH+skripty. `uv tool install`.

### Task 2A: CLI základ + auth [INDEPENDENT od 1B-1E, DEPENDS ON 1A]

```
cli/
  __init__.py
  main.py             # Typer app, global options (--server, --json)
  config.py           # ~/.config/da/ management
  client.py           # HTTP client wrapper (auth, retry, streaming)
  commands/
    auth.py           # da login, da logout, da whoami
```

- `da login` → otevře browser pro OAuth → server vydá JWT → uloží do `~/.config/da/token.json`
- `da --json` flag na všech příkazech pro strukturovaný output
- `da --server URL` override (default z config.yaml)

### Task 2B: Sync příkazy [DEPENDS ON 2A, 1B]

```
cli/commands/
  sync.py             # da sync, da sync --table X, da sync --upload-only
```

Flow:
1. `GET /api/sync/manifest` → porovnej s `~/.config/da/sync_state.json`
2. Download změněné parquety (HTTP streaming s progress barem)
3. Download docs, rules, profily
4. Upload sessions, artifacts, CLAUDE.local.md
5. Rebuild DuckDB views (DROP views, CREATE VIEW per tabulka, zachovej user tabulky)
6. Update lokální manifest

Přepíše funkci `scripts/sync_data.sh` (475 řádků).

### Task 2C: Query + Scripts příkazy [DEPENDS ON 2A, 1C]

```
cli/commands/
  query.py            # da query "SQL" [--remote] [--json]
  scripts.py          # da scripts list/run/deploy/undeploy
  explore.py          # da explore {table} — profil tabulky
```

- `da query` — lokální DuckDB default, `--remote` přes server API
- `da scripts run X` — lokálně default, `--remote` přes server
- `da scripts deploy X --schedule "cron"` — upload + registrace na serveru
- `da explore orders` — profil z lokálních dat (nebo `--remote` ze serveru)

### Task 2D: Admin + Server příkazy [DEPENDS ON 2A, 1D]

```
cli/commands/
  admin.py            # da admin add-user/remove-user/list-users
  status.py           # da status [--local] — zdraví systému
  server.py           # da server deploy/rollback/logs/status
  diagnose.py         # da diagnose — AI-friendly diagnostika
```

- `da status` — strukturovaný health report (tabulky, sync stav, služby)
- `da status --local` — offline: kdy jsem synkoval, kolik dat mám
- `da diagnose` — projde logy, sync stav, konektivitu → root cause
- `da server deploy` — wrapper kolem `kamal deploy`
- `da server logs webapp` — wrapper kolem `kamal app logs`

### Task 2E: PyPI distribuce [DEPENDS ON 2A]

- `pyproject.toml` pro CLI balíček
- `uv tool install data-analyst` nebo `uv pip install data-analyst`
- Entry point: `[project.scripts] da = "cli.main:app"`
- Minimální dependencies: typer, httpx, duckdb, rich (progress bars)

---

## Fáze 3: Docker + Kamal

**Cíl:** `docker compose up` pro dev, `kamal deploy` pro produkci. Nahrazuje 10+ manuálních kroků.

### Task 3A: Dockerfile + docker-compose.yml [INDEPENDENT]

```
Dockerfile              # python:3.13-slim, uv install, jeden image
docker-compose.yml      # webapp, api, scheduler, telegram-bot, ws-gateway
docker-compose.test.yml # api + test-runner pro integrační testy
```

- Jeden image, různý CMD per služba
- Volume `/data` sdílený mezi kontejnery
- `profiles: ["full"]` pro volitelné služby (telegram, ws-gateway)
- `uv sync` místo `pip install` v Dockerfile

### Task 3B: Scheduler služba [DEPENDS ON 0A]

Nový soubor: `services/scheduler/__main__.py`
- APScheduler (nebo jednoduchý custom) nahrazuje 7 systemd timerů:

| Timer | Schedule | Funkce |
|---|---|---|
| data-refresh | 15 min | `DataSyncManager.sync_scheduled()` |
| catalog-refresh | 15 min | Catalog refresh |
| corporate-memory | 30 min | Knowledge collector |
| session-collector | 6h | Session collection (z uploaded dat) |
| user-scripts | per-script cron | Script runner |
| profiler | po data-refresh | Auto-profile nových dat |

### Task 3C: Kamal konfigurace [DEPENDS ON 3A]

```
config/
  deploy.yml           # produkční Kamal config
  deploy.staging.yml   # staging override
```

- Kamal Proxy pro auto-SSL (Let's Encrypt)
- Healthcheck na `/api/health`
- Zero-downtime deploy
- Accessories: scheduler, telegram-bot, ws-gateway, script-runner
- Environment secrets přes Kamal env management

### Task 3D: GitHub Actions CI/CD [DEPENDS ON 3A, 3C]

```
.github/workflows/
  ci.yml               # test + build na každém push
  deploy.yml           # staging na PR, production na merge do main
```

Flow: push → pytest → integrační testy (docker compose) → build image → push GHCR → kamal deploy staging (PR) / production (merge)

### Task 3E: Smazání starého server infra [DEPENDS ON 3A-3D, ověřeno že nové funguje]

Smazat:
- `server/setup.sh` (103 řádků)
- `server/webapp-setup.sh` (171 řádků)
- `server/deploy.sh` (395 řádků)
- `server/migrate-to-v2.sh` (146 řádků)
- Všechny systemd unit soubory (`services/*/systemd/`)
- `server/sudoers-*`
- `server/bin/add-analyst` a related skripty
- `scripts/sync_data.sh` (475 řádků)
- `server/webapp.service`, `server/webapp-nginx.conf`

---

## Fáze 4: RBAC + bezpečnost

**Cíl:** Aplikační RBAC místo Linux skupin. Audit trail. Script sandboxing.

### Task 4A: Role + permissions v DuckDB [DEPENDS ON 0A]

Nový soubor: `src/rbac.py`

```python
class Role(Enum):
    VIEWER = "viewer"       # Katalog, čtení dat
    ANALYST = "analyst"     # Sync, queries, voting, skripty
    ADMIN = "admin"         # Správa uživatelů, schvalování knowledge
    KM_ADMIN = "km_admin"  # Corporate memory governance
```

- Dataset-level permissions (kdo má přístup ke kterým datům)
- Přepsat `webapp/auth.py` řádky 37-65 (admin_required/km_admin_required)
- Přepsat `webapp/user_service.py` celý — DB místo `pwd.getpwnam()` + `sudo`

### Task 4B: Audit trail [DEPENDS ON 0A]

- Každý API call logován do `audit_log` tabulky
- Struktura: timestamp, user_id, action, resource, params, result, duration
- Agent může: `da query "SELECT * FROM system.audit_log WHERE action='sync_trigger' ORDER BY timestamp DESC LIMIT 10"`

### Task 4C: Script sandboxing [DEPENDS ON 3A]

- Script-runner jako izolovaný Docker kontejner
- Read-only přístup k DuckDB
- Omezená paměť (512MB), čas (5min), žádný network (kromě notification dispatch)
- Explicitní whitelist Python balíčků (pandas, duckdb, matplotlib)

### Task 4D: Corporate memory push model [DEPENDS ON 1D]

- Uživatelé pushují CLAUDE.local.md přes `da sync --upload-only`
- Server nikdy nečte `/home/*/` jako root
- Corporate memory collector zpracovává uploaded data z DB

---

## Dependency graf pro multi-agenty

```
Fáze 0:
  0A (DuckDB schema) ─────────────────────┐
  0C (migrační skript) ← závisí na 0A     │
  0B (migrace services) ← závisí na 0A    │
                                           │
Fáze 1:                                   │
  1A (FastAPI základ) ← závisí na 0A ─────┤
  1B (sync/data EP) ← závisí na 1A, 0A    │
  1C (query/scripts EP) ← závisí na 1A    │
  1D (users/memory EP) ← závisí na 1A     │
  1E (remove SSH) ← závisí na 1B, 1D      │
                                           │
Fáze 2:                                   │
  2A (CLI základ) ← závisí na 1A          │
  2B (sync cmd) ← závisí na 2A, 1B        │
  2C (query/scripts cmd) ← závisí na 2A   │
  2D (admin/server cmd) ← závisí na 2A    │
  2E (PyPI) ← závisí na 2A               │
                                           │
Fáze 3:                                   │
  3A (Dockerfile) ← INDEPENDENT ──────────┘
  3B (scheduler) ← závisí na 0A
  3C (Kamal) ← závisí na 3A
  3D (CI/CD) ← závisí na 3A, 3C
  3E (cleanup) ← závisí na 3A-3D verified

Fáze 4:
  4A (RBAC) ← závisí na 0A
  4B (audit) ← závisí na 0A
  4C (sandbox) ← závisí na 3A
  4D (push model) ← závisí na 1D
```

### Paralelní agenty — optimální rozložení

```
AGENT 1: DuckDB + Repositories    AGENT 2: FastAPI           AGENT 3: Docker + Kamal
─────────────────────────────      ─────────────────          ──────────────────────
0A: DuckDB schema                  (čeká na 0A)               3A: Dockerfile + compose
0C: migrační skript                1A: FastAPI základ          3B: scheduler služba
0B: migrace services               1B: sync/data EP           3C: Kamal konfigurace
4A: RBAC                           1C: query/scripts EP       3D: CI/CD workflow
4B: audit trail                    1D: users/memory EP        4C: script sandbox
                                   1E: remove SSH deps

AGENT 4: CLI + Skills              AGENT 5: Integrace + Cleanup
─────────────────────               ───────────────────────────
(čeká na 1A)                        (čeká na agents 1-4)
2A: CLI základ + auth               End-to-end testování
2B: sync příkazy                    3E: smazání starého infra
2C: query/scripts příkazy           4D: corporate memory push
2D: admin/server příkazy            5A: CLAUDE.md template update
2E: PyPI distribuce                 Dokumentace update
5B: CLI skills (help/docs)
5C: da setup (interactive)
5D: da diagnose
5E: da infra (multi-customer)
```

---

## Znovupoužité vs. přepsané soubory

### Beze změny (business logika zachována)
- `src/config.py` — TableConfig, Config parsing (625 řádků)
- `src/parquet_manager.py` — Parquet conversion engine
- `connectors/keboola/adapter.py` + `client.py`
- `connectors/bigquery/adapter.py` + `client.py`
- `connectors/jira/` — celý connector
- `connectors/llm/` — LLM abstrakce
- `connectors/openmetadata/` — katalog enrichment
- `webapp/config.py`, `config/loader.py`
- `webapp/templates/` — všechny HTML šablony
- `src/remote_query.py` — query logika (zabalená API)
- `src/profiler.py` — profiling logika (output do DuckDB)

### Přepojené na DuckDB (logika zachována, I/O vrstva vyměněna)
- `webapp/corporate_memory_service.py`
- `webapp/sync_settings_service.py`
- `webapp/telegram_service.py`
- `src/data_sync.py` (SyncState třída)
- `src/table_registry.py`
- `services/corporate_memory/collector.py`
- `services/telegram_bot/storage.py`

### Přepsané
- `webapp/user_service.py` — DB místo Linux users
- `webapp/auth.py` řádky 37-65 — RBAC místo Linux skupin

### Nové
- `src/db.py`, `src/repositories/`, `src/rbac.py`
- `api/` — celý FastAPI server
- `cli/` — celý CLI nástroj
- `Dockerfile`, `docker-compose*.yml`, `config/deploy*.yml`
- `services/scheduler/__main__.py`
- `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`

### Smazané
- `server/setup.sh`, `server/webapp-setup.sh`, `server/deploy.sh`
- `server/migrate-to-v2.sh`
- `server/sudoers-*`, `server/bin/add-analyst`
- `scripts/sync_data.sh`
- Všechny `services/*/systemd/` soubory
- `server/webapp.service`, `server/webapp-nginx.conf`

---

## Fáze 5: Agent Skills (CLAUDE.md + CLI skills)

**Cíl:** AI agent má vestavěné znalosti pro nasazení, administraci, diagnostiku a vývoj. Nemusí nic googlit — vše je v skills.

### Task 5A: CLAUDE.md template pro analytiky [INDEPENDENT]

Aktualizovat `docs/setup/claude_md_template.md`:
- Instrukce pro `da` CLI místo SSH/rsync
- `da sync` jako povinný start session
- Jak pracovat s lokálním DuckDB
- Jak vytvářet a deployovat skripty
- Jak používat corporate memory
- Notifikační vzory (lokální vs serverové)

### Task 5B: Admin/Deploy skills v CLI [DEPENDS ON 2D]

`da` CLI bude obsahovat vestavěné skills — dlouhé help texty s domain knowledge, které AI agent přečte přes `da <command> --help` nebo `da skills <topic>`:

```bash
da skills list                    # seznam všech dostupných skills
da skills setup                   # kompletní průvodce setup nové instance
da skills troubleshoot            # diagnostické postupy
da skills connectors              # jak přidat nový data source
da skills notifications           # jak fungují notifikace
da skills corporate-memory        # governance, approval flow
da skills security                # RBAC, permissions, audit
da skills backup-restore          # disaster recovery
da skills upgrade                 # jak upgradovat verzi
```

Každý skill = markdown soubor v `cli/skills/` který se zobrazí přes `da skills <name>`.

### Task 5C: Interaktivní setup skill [DEPENDS ON 2D, 1A]

```bash
da setup                          # AI agent spustí interaktivní setup
```

Flow (agent řídí):
1. `da setup init` → vygeneruje `instance.yaml` z konverzace s uživatelem
2. `da setup test-connection` → ověří credentials (Keboola/BigQuery)
3. `da setup deploy` → `docker compose up` nebo `kamal deploy`
4. `da setup first-sync` → triggeruje první data sync
5. `da setup verify` → healthcheck, počet tabulek, sample query
6. `da setup add-user` → přidá prvního analytika

Každý krok vrací strukturovaný JSON → agent ví co dělat dál.

### Task 5D: Diagnose skill [DEPENDS ON 2D, 1D]

```bash
da diagnose                       # kompletní diagnostika
da diagnose --symptom "data not updating"    # cílená diagnostika
da diagnose --component scheduler            # diagnostika jedné služby
```

Output (strukturovaný pro agenta):
```json
{
  "overall": "degraded",
  "checks": [
    {"name": "api", "status": "ok", "latency_ms": 12},
    {"name": "scheduler", "status": "ok", "last_run": "2026-03-27T08:00"},
    {"name": "data_freshness", "status": "warning",
     "detail": "table 'orders' last synced 26h ago, expected 15min",
     "suggested_action": "da server logs scheduler | grep orders"},
    {"name": "disk", "status": "ok", "usage": "45%"},
    {"name": "duckdb", "status": "ok", "tables": 47, "total_rows": "12.3M"}
  ],
  "suggested_actions": [
    "Check scheduler logs for 'orders' sync failures",
    "Run: da server logs scheduler --since 24h | grep -i error"
  ]
}
```

### Task 5E: Operační skills pro multi-customer [DEPENDS ON 3C]

```bash
da infra list                     # seznam zákaznických instancí
da infra provision --customer acme --cloud gcp --region europe-west1
da infra status acme              # zdraví zákaznické instance
da infra deploy acme              # deploy na zákaznický server
da infra backup acme              # snapshot dat
```

Budoucí rozšíření — Terraform pod kapotou pro provision, Kamal pro deploy.

---

## Verifikace

### Per-fáze
1. **Fáze 0:** `pytest tests/` zelený, webapp funguje identicky s DuckDB backendem
2. **Fáze 1:** `curl /api/health` → ok, `curl /api/sync/manifest` → manifest, parquet download funguje
3. **Fáze 2:** `da login && da sync` vytvoří identickou strukturu jako `sync_data.sh`, `da query` funguje offline
4. **Fáze 3:** `docker compose up` → všechny služby běží, `kamal deploy -d staging` → staging funguje
5. **Fáze 4:** viewer nemůže triggerovat sync, admin může spravovat uživatele, skripty běží v sandboxu

### End-to-end test (celý flow)
1. `docker compose up -d` (nebo `kamal deploy`)
2. Přes webapp: přihlásit se, vybrat datasety
3. `da login && da sync` → parquety lokálně
4. `da query "SELECT count(*) FROM orders"` → výsledek offline
5. `da scripts run sales_alert` → lokální exekuce
6. `da scripts deploy sales_alert --schedule "0 8 * * MON"` → serverová exekuce
7. `da sync --upload-only` → sessions/artifacts na serveru
8. Corporate memory: knowledge items viditelné ve webappu
9. Telegram notifikace doručeny
10. `da diagnose` → strukturovaný health report
