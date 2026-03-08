# Pilot: PO Integrace pro Interního Analysta (bez lokálního ukládání klíčů)

## Shrnutí
Cíl je dodat produkční pilot pro **Purchase Order System** s tímto chováním:
1. Uživatel v dashboardu udělá jen `Connect` (1 klik + Google consent).
2. Server bezpečně uloží per-user credential (šifrovaně, ne v lokálním počítači).
3. Na klientovi se token načte **on-demand přes SSH** pouze pro jeden příkaz (ENV jen v child procesu).
4. Do `.claude/rules` se synchronizuje znalost, že PO operace mají používat PO wrapper/skill.
5. Dodáme zároveň **full skill beta** (vedle produkční varianty rules+wrapper).

## Scope a hranice
- In-scope:
  - Obecný service registry model (rozšiřitelný), implementace provideru jen pro `po`.
  - Connect/Disconnect/Test flow v dashboard katalogu.
  - Server-side per-user encrypted credential store.
  - Runtime wrapper přes SSH s krátkodobým tokenem.
  - Audit + revoke + 30denní rotace.
  - Sync rules + skill beta distribuce.
- Out-of-scope:
  - CRM/Revolut implementace (jen připravený framework).
  - Plná proxy architektura pro API calls (volání půjde přímo klient -> PO API).
  - Migrace na externí secret manager v pilotu.

## Návrh řešení (decision-complete)

### 1) Service registry a datový model
- Přidat nový server-side registry JSON: `/data/integrations/registry.json`.
- Schema položky služby:
  - `service_id` (`po`)
  - `display_name`
  - `connect_type` (`oauth_bootstrap_link`)
  - `scopes`
  - `api_base_url`
  - `enabled`
  - `skill_slug` (`po-system`)
  - `rule_template_id`
- Per-user credential metadata:
  - `/data/integrations/state/<service>/<username>.json`
  - Obsah: `status`, `connected_at`, `last_rotated_at`, `expires_at`, `token_ref`, `audit_last_use_at`
- Per-user encrypted secret:
  - `/data/integrations/secrets/<service>/<username>.enc`
  - Šifrování AES-GCM, key z `/etc/internal-analyst/integrations.key` (root:root, 600).

### 2) Dashboard UX (katalog zdrojů)
- Do `webapp/templates/catalog.html` přidat novou kartu “Purchase Order System”.
- Akce:
  - `Connect` -> redirect na bootstrap URL PO.
  - `Disconnect` -> revoke + smazání secretu.
  - `Test connection` -> ověření issuance short-lived tokenu.
- UI stav:
  - `Not connected`, `Connected`, `Error`, `Reauthorization required`.

### 3) Webapp API rozhraní
- Přidat nové endpointy (Flask):
  - `GET /api/integrations/catalog`
  - `POST /api/integrations/<service>/connect`
  - `GET /api/integrations/<service>/callback`
  - `POST /api/integrations/<service>/disconnect`
  - `POST /api/integrations/<service>/test`
- Interní service layer:
  - nový `webapp/integration_service.py`
  - provider interface:
    - `build_connect_url(user, state)`
    - `exchange_callback(code, state)`
    - `rotate(user)`
    - `issue_runtime_token(user, ttl_seconds)`
    - `revoke(user)`
- `po` provider implementace jako první konkrétní provider.

### 4) PO bootstrap link + token lifecycle
- Connect flow:
  1. Uživatel klikne Connect v dashboardu.
  2. Redirect na PO auth/bootstrap endpoint (Google auth + consent).
  3. Callback zpět na webapp.
  4. Webapp uloží refresh credential šifrovaně.
- Rotace:
  - server job (cron/systemd) denně kontroluje `last_rotated_at`.
  - rotuje každých 30 dní.
- Revoke:
  - Disconnect okamžitě revokuje token v PO a maže server secret.

### 5) Runtime token injection přes SSH (bez lokální persistence)
- Přidat server helper:
  - `/usr/local/bin/integration-runtime-env`
  - Vstup: `--service po --ttl 300 --format env`
  - Výstup: shell-safe `export` lines pro child proces.
- Přístup:
  - přes sudoers povolit pouze self-service issuance (uživatel jen pro svůj účet).
  - helper tvrdě validuje volajícího uživatele a service allowlist.
- Lokální wrapper skript v syncovaných skriptech:
  - `server/scripts/po-run`
  - Spuštění:
    - `ssh data-analyst "/usr/local/bin/integration-runtime-env --service po --ttl 300 --format env"`
    - spustí cílový příkaz v subshell s ENV
    - po skončení ENV zahodí (bez zápisu do souboru)
- Přímá API volání:
  - klientský skript/skill volá PO API přímo s runtime ENV tokenem.

### 6) Rules + skill beta distribuce
- Produkčně:
  - generovat service rule soubor na serveru: `/home/<user>/.claude_rules/svc_po.md`
  - `scripts/sync_data.sh` už pravidla stahuje do `.claude/rules/`; upravit jen reporting, aby počítal i `svc_*.md`.
- Skill beta:
  - přidat sync složku `server/skills/po-system/` (SKILL.md + scripts + references).
  - přidat skript `server/scripts/install_skills.sh`:
    - instaluje do uživatelova Codex skill home.
  - `sync_data.sh` po syncu volá `install_skills.sh` (best-effort, neblokuje datový sync).

### 7) Audit, observability, bezpečnost
- Audit log soubor:
  - `/data/integrations/audit.log` (append-only)
  - eventy: `connect`, `callback_ok`, `runtime_issue`, `rotate`, `revoke`, `error`
- Bezpečnostní guardrails:
  - token nikdy nelogovat
  - helper vrací jen krátkodobý access token
  - TTL runtime tokenu default 5 min
  - strict input validation service/usernames
- Monitoring:
  - dashboard health check pro integrations service
  - metriky: connect success rate, runtime issuance latency, rotate failures.

## Důležité změny veřejných API/rozhraní/typů
- Nové REST endpointy pod `/api/integrations/*`.
- Nový datový kontrakt `registry.json` pro katalog služeb.
- Nový provider interface v `webapp/integration_service.py`.
- Nový CLI kontrakt helperu `integration-runtime-env`.
- Nový lokální wrapper command `po-run`.

## Testy a validační scénáře

### Unit testy
- `webapp/integration_service.py`:
  - validace state/nonce
  - encrypt/decrypt secretu
  - rotace pravidla 30 dní
- provider `po`:
  - connect URL tvorba
  - callback exchange error handling
- helper `integration-runtime-env`:
  - self-user enforcement
  - invalid service rejection
  - TTL bounds

### Integration testy
- E2E Connect flow:
  - dashboard -> PO consent -> callback -> connected status.
- E2E runtime:
  - `po-run "curl ..."` získá token přes SSH a provede volání.
- E2E Disconnect:
  - revoke v PO + odstranění state/secrets + UI přepnutí na not connected.
- E2E rotation:
  - forced rotation scenario + audit log verification.

### Security testy
- Pokus o issuance tokenu pro jiného uživatele (musí failnout).
- Pokus o command injection přes service parameter (musí failnout).
- Ověření, že token není v logu ani na disku klienta po dokončení příkazu.

### Acceptance kritéria
- Uživatel připojí PO službu 1 klikem + consent.
- Žádný persistentní API klíč na klientově disku.
- `po-run` funguje bez ručního exportu tokenu.
- Disconnect do 1 min deaktivuje další runtime issuance.
- Rotace probíhá automaticky po 30 dnech.

## Rollout plán
1. Backend + helper + audit nasadit behind feature flag `INTEGRATIONS_PO_ENABLED=false`.
2. Zapnout interně pro 1–2 test uživatele.
3. Ověřit E2E + security checklist.
4. Zapnout pro všechny analytiky.
5. Po stabilizaci přidat další provider (CRM) přes stejný interface.

## Assumptions a zvolené defaulty
- PO systém je plně pod kontrolou týmu a lze doplnit endpointy pro issuance/rotate/revoke.
- Identita uživatele je mapovatelná mezi dashboardem a server účtem.
- Runtime přenos credentialů má být pouze přes SSH mechanismus (ne lokální secret file).
- Default runtime token TTL: 300 sekund.
- Default rotace: 30 dní.
- Pilot storage není `.env`; používá per-user encrypted store.
- Produkční path v pilotu je rules+wrapper, full skill je beta paralelně.
