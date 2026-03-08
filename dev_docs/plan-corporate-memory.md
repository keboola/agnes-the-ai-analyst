# Corporate Memory Module - Implementation Plan

## Overview

Server-side modul, který každých 30 minut sbírá znalosti z `CLAUDE.local.md` všech uživatelů, pomocí Claude HAIKU je extrahuje a filtruje, a vytváří sdílenou firemní knowledge base. Uživatelé mohou hlasovat a oblíbené znalosti se jim syncují do `.claude/rules/`.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Cron (*/30 * * * *)                                            │
│  └── /usr/local/bin/collect-knowledge                           │
│      1. Čte /home/*/CLAUDE.local.md                             │
│      2. Volá Claude HAIKU pro extrakci (AI filtering)           │
│      3. Ukládá do /data/corporate-memory/knowledge.json         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  /data/corporate-memory/                  (deploy:data-ops 2770)│
│  ├── knowledge.json      # {items: {id: {title, content, ...}}} │
│  ├── votes.json          # {username: {item_id: 1|-1}}          │
│  ├── user_hashes.json    # {username: {file_hash, last_proc}}   │
│  └── collection.log      # Logy z HAIKU procesingu              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Webapp                                                         │
│  ├── Dashboard widget: stats (contributors, items, your rules)  │
│  ├── /corporate-memory: sub-page s 👍/👎 hlasováním             │
│  └── API: /api/corporate-memory/*                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  sync_data.sh                                                   │
│  └── Stáhne upvoted items do .claude/rules/*.md                 │
└─────────────────────────────────────────────────────────────────┘
```

## Data Model

**knowledge.json:**
```json
{
  "items": {
    "km_abc123": {
      "id": "km_abc123",
      "title": "DuckDB query optimization",
      "content": "When querying large parquet files...",
      "category": "data_analysis",
      "tags": ["duckdb", "performance"],
      "source_users": ["john.doe", "jane.smith", "bob"],
      "votes_up": 5,
      "votes_down": 1,
      "score": 4,
      "extracted_at": "2026-02-05T10:30:00Z",
      "updated_at": "2026-02-05T12:00:00Z"
    }
  },
  "metadata": {
    "last_collection": "2026-02-05T10:30:00Z",
    "total_users": 8
  }
}
```

**votes.json** (kdo jak hlasoval - pro UI "already voted"):
```json
{
  "john.doe": {"km_abc123": 1, "km_def456": -1},
  "jane.smith": {"km_abc123": 1}
}
```

### Voting & Popularity

- `votes_up` / `votes_down` - počet palců nahoru/dolů (viditelné v UI)
- `score` = votes_up - votes_down (pro řazení)
- votes.json trackuje KDO hlasoval (pro zabránění dvojího hlasování)
- UI NEZOBRAZUJE jména hlasujících, jen počty

**Řazení v UI:**
- **Nejoblíbenější** (Most Popular): ORDER BY score DESC
- **Nejméně oblíbené** (Least Popular): ORDER BY score ASC
- **Nejnovější** (Recent): ORDER BY extracted_at DESC
- **Nejvíc přispěvatelů** (Most Contributors): ORDER BY len(source_users) DESC

**user_hashes.json** (tracking změn CLAUDE.local.md):
```json
{
  "john.doe": {
    "file_hash": "a1b2c3d4e5f6...",
    "last_processed": "2026-02-05T10:30:00Z"
  },
  "jane.smith": {
    "file_hash": "f6e5d4c3b2a1...",
    "last_processed": "2026-02-05T09:00:00Z"
  }
}
```

## Change Detection & Deduplication

### Change Detection (šetření tokenů)

**DŮLEŽITÉ**: Každých 30 minut se zpracovávají POUZE změněné soubory!

```
┌─────────────────────────────────────────────────────────────────┐
│  Cron běží každých 30 min                                       │
│                                                                 │
│  Pro každého uživatele:                                         │
│  1. Spočítej MD5 hash /home/$user/CLAUDE.local.md               │
│  2. Porovnej s uloženým hashem v user_hashes.json               │
│  3. Pokud STEJNÝ → PŘESKOČ (žádné API volání)                   │
│  4. Pokud ZMĚNĚNÝ → zpracuj pomocí HAIKU API                    │
│                                                                 │
│  Typicky: 8 uživatelů, 1-2 změny denně = 2-4 API volání/den     │
└─────────────────────────────────────────────────────────────────┘
```

```python
def collect_all():
    users_processed = 0
    users_skipped = 0

    for user_dir in Path("/home").iterdir():
        claude_file = user_dir / "CLAUDE.local.md"
        if not claude_file.exists():
            continue

        username = user_dir.name
        current_hash = hashlib.md5(claude_file.read_bytes()).hexdigest()
        stored_hash = user_hashes.get(username, {}).get("file_hash")

        if current_hash == stored_hash:
            users_skipped += 1
            continue  # ← PŘESKOČÍ - žádné API volání!

        # Pouze změněné soubory volají HAIKU
        new_knowledge = extract_knowledge(claude_file.read_text())
        deduplicate_and_merge(new_knowledge, username)
        update_user_hash(username, current_hash)
        users_processed += 1

    log(f"Processed: {users_processed}, Skipped: {users_skipped}")
```

### Deduplikace znalostí

Při extrakci nových znalostí od změněného uživatele:
1. HAIKU vrátí seznam znalostí
2. Pro každou novou znalost:
   - Hledej podobnou v CELÉ knowledge base (všichni uživatelé)
   - Pokud existuje podobná → přidej uživatele do `source_users[]`
   - Pokud neexistuje → vytvoř novou

```python
def deduplicate_and_merge(new_items: list, username: str):
    """Deduplikuje nové znalosti proti celé knowledge base."""
    for new_item in new_items:
        similar_id = find_similar_knowledge(new_item, knowledge_base)

        if similar_id:
            # Existující znalost - přidej uživatele
            knowledge_base["items"][similar_id]["source_users"].append(username)
            knowledge_base["items"][similar_id]["updated_at"] = now()
        else:
            # Nová znalost
            item_id = generate_id()
            knowledge_base["items"][item_id] = {
                **new_item,
                "id": item_id,
                "source_users": [username],
                "extracted_at": now()
            }
```

### Příklad deduplikace

5 uživatelů má podobnou znalost o DuckDB:
- User A: "Pro rychlé dotazy v DuckDB použij WHERE před JOIN"
- User B: "DuckDB je rychlejší když filtruješ před joinem"
- User C: "Filtruj data před JOIN operací v DuckDB"
- User D: "WHERE klauzule před JOIN zrychlí DuckDB"
- User E: "V DuckDB dej WHERE před JOIN"

→ HAIKU rozpozná jako JEDNU znalost → `source_users: ["user_a", "user_b", "user_c", "user_d", "user_e"]`

## Files to Create

### 1. Server-side Collector

**`server/corporate_memory/collector.py`** - Main collection logic:
- `collect_all()` - iteruje přes /home/*/CLAUDE.local.md
- `should_process_user(username)` - kontrola hash změn (šetří tokeny)
- `extract_knowledge(content)` - volá HAIKU API
- `find_similar_knowledge(new, existing)` - hledá duplicity
- `merge_knowledge(existing, new, username)` - sloučí a přidá uživatele
- `save_knowledge(data)` - atomic JSON write
- `update_user_hash(username, hash)` - uloží hash pro příští run

**`server/corporate_memory/prompts.py`** - HAIKU prompts:
- Prompt pro extrakci znalostí
- Prompt pro AI filtering citlivých dat
- Prompt pro merge podobných znalostí (optional)

**`server/bin/collect-knowledge`** - Shell wrapper:
```bash
#!/bin/bash
cd /opt/data-analyst/repo
/opt/data-analyst/.venv/bin/python -m server.corporate_memory.collector
```

**`server/corporate-memory.timer`** + **`server/corporate-memory.service`** - Systemd timer (30 min)

### 2. Webapp Backend

**`webapp/corporate_memory_service.py`** (pattern: telegram_service.py):
- `get_knowledge(filter, page)` - seznam znalostí
- `get_stats()` - pro dashboard widget
- `vote(username, item_id, vote)` - +1/-1
- `get_user_rules(username)` - upvoted items pro sync
- `generate_user_rules_file(username)` - vytvoří JSON pro sync

**`webapp/app.py`** - nové routes:
- `GET /corporate-memory` - sub-page
- `GET /api/corporate-memory/knowledge` - list items
- `GET /api/corporate-memory/stats` - dashboard stats
- `POST /api/corporate-memory/vote` - hlasování
- `GET /api/corporate-memory/my-rules` - user's upvoted

### 3. Webapp Frontend

**`webapp/templates/corporate_memory.html`** - sub-page:
- Header se stats
- Řazení: Most Popular | Least Popular | Recent | Most Contributors
- Seznam znalostí s filtry (category, search)
- Každá položka:
  - Title + content preview
  - Tags (category badges)
  - 👍 (count) / 👎 (count) buttons
  - "From X contributors" badge
  - "Synced to your rules" indicator (if user upvoted)

```
┌─────────────────────────────────────────────────────────────────┐
│ DuckDB query optimization                      [data_analysis]  │
│ When querying large parquet files, filter before JOIN...        │
│                                                                 │
│ 👍 5   👎 1   |   From 3 contributors   |   ✓ In your rules     │
└─────────────────────────────────────────────────────────────────┘
```

**`webapp/templates/dashboard.html`** - přidat widget:
```html
<div class="card-v2 memory-card">
  <h3>Corporate Memory</h3>
  <div class="memory-stats">
    <div class="stat">{{ stats.contributors }} contributors</div>
    <div class="stat">{{ stats.knowledge_count }} knowledge items</div>
    <div class="stat">{{ stats.your_rules }} your rules</div>
  </div>
  <a href="/corporate-memory">Browse Knowledge →</a>
</div>
```

### 4. Client Sync

Server generuje .md soubory přímo do `/home/$user/.claude_rules/` pro každého uživatele.
Klient si je jen stáhne.

**`scripts/sync_data.sh`** - přidat:
```bash
# --- Sync corporate memory rules ---
echo "📚 Syncing corporate memory rules..."
mkdir -p .claude/rules
rsync -avz data-analyst:~/.claude_rules/ .claude/rules/ 2>/dev/null || \
    scp -r data-analyst:~/.claude_rules/* .claude/rules/ 2>/dev/null || true
```

**Server-side** (v `corporate_memory_service.py`):
- `generate_user_rules(username)` - při hlasování regeneruje .md soubory
- Zapisuje do `/home/$user/.claude_rules/km_*.md`
- Webapp volá po každém vote změně

### 5. Deployment

**`server/deploy.sh`** - additions:
- Vytvořit /data/corporate-memory/ (2770 setgid)
- Nainstalovat collect-knowledge do /usr/local/bin/
- Deploy systemd timer
- Přidat ANTHROPIC_API_KEY do .env

**`server/sudoers-deploy`** - přidat práva pro /data/corporate-memory/

**GitHub Secrets**: `ANTHROPIC_API_KEY`

## Implementation Phases

### Phase 1: Server Infrastructure
1. `server/corporate_memory/` Python modul
2. `server/bin/collect-knowledge` wrapper
3. `server/corporate-memory.service` + `.timer`
4. Update `server/deploy.sh`
5. Update sudoers

### Phase 2: Webapp Backend
1. `webapp/corporate_memory_service.py`
2. API endpoints v `webapp/app.py`
3. Update `webapp/config.py` (ANTHROPIC_API_KEY, paths)

### Phase 3: Webapp Frontend
1. Dashboard widget v `dashboard.html`
2. Sub-page `corporate_memory.html`
3. CSS styles

### Phase 4: Client Sync
1. Update `scripts/sync_data.sh` - přidat rsync pro `.claude_rules/`

## Key Files to Modify

| File | Changes |
|------|---------|
| `server/deploy.sh` | Add /data/corporate-memory/, ANTHROPIC_API_KEY, systemd timer |
| `server/sudoers-deploy` | Add permissions for corporate-memory dir |
| `webapp/app.py` | Add routes and API endpoints |
| `webapp/config.py` | Add ANTHROPIC_API_KEY, CORPORATE_MEMORY_DIR |
| `webapp/templates/dashboard.html` | Add Corporate Memory widget |
| `scripts/sync_data.sh` | Add corporate rules sync step |

## Reusable Patterns (from codebase)

- **JSON I/O**: `webapp/telegram_service.py` - atomic writes with tempfile
- **Dashboard widget**: `webapp/templates/dashboard.html` - KPI card pattern
- **Sub-page**: `webapp/templates/catalog.html` - route + template pattern
- **Systemd service**: `server/notify-bot.service` - timer pattern
- **Deploy**: `server/deploy.sh` - directory setup, script installation

## Verification

1. **Collector**: `ssh kids "/usr/local/bin/collect-knowledge --dry-run"`
2. **API**: `curl https://your-instance.example.com/api/corporate-memory/stats`
3. **Widget**: Check dashboard shows stats
4. **Voting**: Click 👍/👎, verify votes.json updated
5. **Sync**: Run `sync_data.sh`, check `.claude/rules/` has .md files
