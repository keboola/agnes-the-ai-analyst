# Agnes HA / DR plan — split state to Postgres, keep DuckDB for analytics

> **Source.** Extracted from Claude Code session `b1b2b33a-0dc5-487b-9d99-1a013d6560a4`
> (2026-05-21 07:49 → 21:03 UTC), worktree
> `/Users/vrysanek/foundry-ai/agnes-the-ai-analyst/.worktrees/vr-marker-io-integration`.
> Discussion was prompted by a ~10-minute outage during a `terraform apply -replace`
> on `foundryai-production`. The on-disk decision: **Option C — move app-state
> repositories from DuckDB to Cloud SQL Postgres; keep DuckDB in-process per-VM
> for analytics over parquet.**
>
> Plan today is *paper only* — no code written, no TF changes filed.

---

## 1. Core constraint

DuckDB is **embed-in-process, single-writer file lock**. Two processes opening the
same `.duckdb` file in write mode = corruption. `system.duckdb` (users, registry,
sessions, audit_log, metric_definitions, knowledge_items, data_packages, ...) and
`analytics.duckdb` (views over parquet, ATTACH'd `extract.duckdb` files) both
satisfy this constraint today because a single uvicorn process owns both.

That single process is the entire HA problem. Two workloads sharing one file
lock:

| Workload | Tables / role | Single-writer required? | Multi-VM path |
|---|---|---|---|
| **State** | `users`, `user_groups`, `resource_grants`, `table_registry`, `sync_state`, `sync_history`, `audit_log`, `metric_definitions`, `knowledge_items`, `data_packages`, `marketplace_*`, `store_*` | **Yes** — every API request mutates | **Migrate to Postgres** (Cloud SQL HA) |
| **Analytics** | `analytics.duckdb` + ATTACH'd `extract.duckdb` files, `read_parquet`, BQ extension | Read-only across replicas (each opens its own connection in read-only) | **Per-VM local copy over shared parquet mount** (no contention) |

The state workload is the only thing forcing single-writer across instances.
The analytics layer is already perfectly shardable — every VM can run its own
`analytics.duckdb` over a shared `/data/extracts` GCS-fuse mount.

## 2. Options evaluated

### A. Blue-green disk swap (user-proposed)

Snapshot active boot/config disk → restore to clone → attach to standby → DNS cutover.

- RPO ≈ snapshot age (5-10s incremental); RTO ≈ 2-3 min.
- `pd-ssd multiWriter` does NOT solve this (only ext4 cluster-FS workloads, not DuckDB file-lock).
- Overlap risk: both VMs hold disk lock at cutover → corruption. Hard-fence required.
- **Verdict:** faster planned cutover. Doesn't help unplanned failure. Buys faster RTO, not zero-downtime. Treat as a Phase-1 stopgap, not a destination.

### B. Managed Instance Group (MIG) + rolling update

- `update_policy.type = PROACTIVE`, `max_unavailable = 0`, `max_surge = 1`.
- **Problem:** new + old both attach the same config-disk → DuckDB lock fight unless detach/reattach during cutover, which negates rolling.
- **Verdict:** doesn't solve stateful constraint. Only useful combined with Option C (state in Postgres) so the config-disk no longer holds the lock-critical content.

### C. State → Cloud SQL Postgres (architectural) ← **WINNER**

Refactor `system.duckdb` repositories → Postgres (or AlloyDB). Keep
`analytics.duckdb` as read-only over parquet on each VM.

```
                ┌─────────────────┐
                │ Internal LB     │   (regional)
                └────────┬────────┘
                         │
              ┌──────────┴──────────┐
       ┌─────▼─────┐         ┌─────▼─────┐
       │ agnes-vm1 │         │ agnes-vm2 │     ← MIG, autoscale 2..N
       │ (Active)  │         │ (Active)  │
       └─────┬─────┘         └─────┬─────┘
             │                     │
             ├─ GCS-fuse mount: /data/extracts (RO, shared) ──┐
             │  /data/parquets (RO, shared)                   │
             │                                                 │
             └─ Cloud SQL Postgres ────────────────────────────┘
                ↑ regional HA, sync replicas, auto-failover ≤60s
```

- Multiple agnes VMs concurrently — each reads parquets from GCS-fuse,
  talks to shared Postgres for state.
- Cloud SQL HA: regional, auto-failover ≤ 60s, sync replicas.
- Internal LB round-robins.
- Deploys: rolling — drain, swap image, healthcheck, next.
- Effort: medium-large. Every repo class in `src/repositories/*.py` gets
  a Postgres driver. ~18 files / ~340 callsites in current app.

**Why C wins (vs the other shortlisted options below)**: it removes the
single-writer constraint *only* on the layer that has it
(`system.duckdb`), leaves DuckDB-in-process intact for analytics (where
DuckDB's value lives — `read_parquet`, BQ extension, `_remote_attach`,
ATTACH'd extracts). No need to re-implement Agnes's five DuckDB usage
patterns (see §4) on top of a network protocol.

### D. GKE + leader election

- Two pods, K8s lease primitive elects leader holding file lock; standby
  watches lease; on lease loss/timeout it grabs and starts agnes.
- Shared storage: Filestore (NFS) for parquets + config-disk path.
- Failover: ~30s.
- **Verdict:** active-passive HA, keeps DuckDB shape. But NFS for DuckDB is
  a known perf footgun (analytics queries hit NFS instead of local SSD).
  Operational cost (GKE) high for the team size. Rejected.

### E. Cloud Run + GCS parquets + Cloud SQL state

- Stateless container, 0→N autoscale.
- Parquets via `read_parquet('gs://...')` (DuckDB native).
- State: Cloud SQL. Sessions: Memorystore Redis.
- **Verdict:** most "cloud-native" but largest refactor. Cold start 5-10s.
  Every parquet read hits GCS (10-100× slower than local SSD; caching
  mitigates but doesn't eliminate). WebSocket support constrains scaling.
  Same prereq as C (state in Postgres) plus parquet caching strategy.
  Defer until C is in place — then re-evaluate.

### F. Snapshot-based DR (lighter-weight than HA)

- Active VM, regional pd-ssd snapshot every 5 min. Standby VM template
  ready but not running. Cloud Monitoring health-check → Cloud Function →
  standby VM creation from latest snapshot on failure.
- Cheap (only one VM running). RTO ≈ 5-10 min, RPO ≈ 5 min. Manual DNS
  cutover.
- **Verdict:** disaster recovery, not HA. Partially in place already —
  `backup.tf` snapshot schedule for boot + config disks. Keep as-is; don't
  treat as the HA answer.

### I. DuckDB-as-a-service tier (extracted to dedicated DB VM)

Single DuckDB writer behind RPC. App tier stateless. Adapter pretends to
be `duckdb.DuckDBPyConnection`. ~500-1000 lines for a FastAPI + Arrow-IPC
wrapper modeled on Mosaic DuckDB Server.

- ✅ App-tier HA, rolling deploys, zero downtime on image bumps.
- ❌ DB tier is still SPOF. Outage there still cascades.
- ❌ Latency tax: every query +1-5ms; chatty repos feel it.
- ❌ Need to re-implement Agnes's five DuckDB patterns (§4) on the wire
  — including the killer `register(arrow_table)` pattern which no
  off-the-shelf wrapper supports (requires in-process Arrow buffer).
- **Verdict:** sweet spot for deploy-downtime pain alone, but doesn't deliver
  DB-tier HA and forces us to own the wire protocol. **Rejected in favor of C**
  because C delivers both app-tier AND db-tier HA with comparable
  engineering cost, leaving DuckDB in-process where its value sits.

## 3. Off-the-shelf DuckDB-server landscape (why we don't adopt any)

Surveyed for Option I evaluation. None production-grade for HA.

| Project | Wire | Auth | License | Production state | Verdict |
|---|---|---|---|---|---|
| MotherDuck | Native + cloud sync | Token | Proprietary | Production (SaaS) | Corp-blocking, eliminate |
| `pg_duckdb` | Postgres wire | PG roles | MIT | Stable v0.x, joint DuckDB Labs + Hydra | Strong fit if we self-host PG with the extension. Not on Cloud SQL allowlist → must self-host on GCE/GKE → at that point past 80% of Option C anyway. Adds operational complexity. |
| `duckdb-httpserver` extension | HTTP + JSON | Static token | MIT | Community, small users | Drop-in `INSTALL httpserver`. OK prototype; weak production. No Arrow IPC. |
| Mosaic DuckDB Server | Custom WS + Arrow IPC | None built-in | Apache-2.0 | Production at UW for viz | Closest reference if we ever do Option I. Fork + customize ~500 LOC. |
| GlareDB | Postgres wire + multi-source federation | PG-like | Apache-2.0 | OSS works; company pivoted late 2024, future unclear | Lowest deploy friction *today*; upstream momentum dying. Pin a version + own forks if adopted. |
| `chsql` / `quackpipe` | ClickHouse wire | Token | MIT | Hobby, single maintainer | Skip. |
| DuckLake | Catalog layer | N/A | MIT | Early | Solves catalog, not HA. Out of scope. |

**Bottom line.** `pg_duckdb` is the only tier-1 option, but adopting it
means self-hosting Postgres-with-extension — which gets us all the
operational cost of Postgres HA *plus* a custom extension to maintain.
Pure Cloud SQL Postgres (Option C) costs the same ops + zero custom
extension surface. So C wins.

## 4. Agnes-specific DuckDB patterns that must keep working

Audit from `src/db.py`, `src/orchestrator.py`, `connectors/*/access.py`,
`src/remote_query.py` at the time of the session. Five patterns; the
solution chosen MUST preserve them.

### A. Multi-DB ATTACH

```python
# src/db.py:937-947, src/orchestrator.py:340
for ext_dir in extracts_dir.iterdir():
    conn.execute(f"ATTACH '{ext_dir/'extract.duckdb'}' AS {ext_dir.name} (READ_ONLY)")
```

Master `analytics.duckdb` is empty; its views reference attached
`extract.duckdb` files. Every read-only connection re-ATTACHes them. N
connectors = N ATTACHed databases per connection.

### B. Community extension + per-session secret + ATTACH chain (BigQuery)

```python
# src/db.py:864-892, connectors/bigquery/access.py:312
conn.execute("LOAD bigquery")
bq_token = get_metadata_token()  # GCE metadata, refreshes per session (1h TTL)
conn.execute(f"CREATE OR REPLACE SECRET bq_secret_{alias} (TYPE bigquery, ACCESS_TOKEN '{tok}')")
apply_bq_session_settings(conn)
conn.execute(f"ATTACH '{url}' AS {alias} (TYPE bigquery, READ_ONLY)")
```

Secret is session-scoped. **Must run on every connection open** — can't
share across clients without sharing creds.

### C. Token-from-env extensions (Keboola)

```python
# connectors/keboola/access.py:38, src/db.py:893-905
conn.execute("INSTALL keboola FROM community; LOAD keboola")
conn.execute(f"ATTACH '{stack_url}' AS kbc (TYPE keboola, TOKEN '{kbc_token}')")
```

Different community extension, different secret model — token inline in
ATTACH, not via `CREATE SECRET`.

### D. Client-side Arrow → server-side view registration (the killer)

```python
# src/remote_query.py:330
arrow_table = bq_job.to_arrow()        # client process fetched from BQ
self._conn.register(alias, arrow_table) # registers as a DuckDB view in-memory
# then user SQL can SELECT FROM <alias>
```

`register(arrow_table)` requires the Arrow buffer in the **same process
address space** as DuckDB. A remote DB server would have to accept Arrow
IPC uploads and register them server-side. **None of the off-the-shelf
wrappers do this.** This pattern alone kills Option I in the absence of a
custom protocol.

### E. WAL auto-recovery + pre-migrate snapshot dance

```python
# src/db.py:680-724
try: duckdb.connect(db_path)
except WAL_REPLAY_ERROR:
    shutil.copy(snapshot, db_path); duckdb.connect(db_path)
```

Process-local filesystem operations. Off-the-shelf servers don't expose
this. **Note**: this exact code path was implicated in the
2026-05-21 prod data-packages wipe — pre-migrate snapshot dance is one of
the things Option C replaces with Postgres point-in-time recovery.

### Pattern × off-the-shelf compatibility matrix

| Project | A (multi-ATTACH) | B (BQ ext + secret/session) | C (community ext token-ATTACH) | D (Arrow register) | E (WAL recovery) |
|---|---|---|---|---|---|
| MotherDuck | ? proprietary | ❌ | ❌ | ❌ | ❌ |
| `pg_duckdb` | ⚠️ FDW, not ATTACH | ❌ allowlist | ❌ | ❌ no Arrow register | ⚠️ pg WAL ≠ DuckDB WAL |
| httpserver ext | ✅ server-side | ✅ single-tenant | ✅ | ❌ no client upload | ❌ |
| Mosaic server | ✅ | ✅ | ✅ | ❌ no client upload | ❌ |
| GlareDB | ❌ own catalog | ❌ own datasource | ❌ no Keboola | ❌ no Arrow register | ❌ |

**Verdict:** zero clean fits. The combination of B + C + D is unique to
Agnes. Every wrapper would need a fork.

**Implication for Option C:** patterns A, B, C, D, E all keep working
**unchanged** because they stay in-process per VM. Only `system.duckdb`
operations rewire to Postgres.

## 5. Recommended two-phase rollout

Don't conflate "deploy downtime" with "true HA" — different solutions.

### Phase 1 — kill the deploy downtime (1-2 days)

Stopgap to get from "10-min unavailable on every `terraform apply -replace`"
to "≤30s, ideally zero". No architecture change.

- New systemd unit on the VM that runs `docker pull` of the new tag in the
  background BEFORE swapping.
- Pre-bake the boot image with docker + Agnes wheel via Packer → boot
  disk replace no longer means re-pulling everything.
- Add a `recreate_targets` flow that does: stop agnes, remount config-disk
  RW on a new VM, start agnes there, DNS swap (Option A from §2 as a
  controlled pattern, with hard-fence).

Estimated effort: 1-2 days. Cuts apply-downtime from ~10min to ~1-2min.

### Phase 2 — true HA via state→Postgres (3-6 weeks)

Option C. Concrete migration order, lowest blast radius first.

1. **Audit `src/repositories/*.py`** — 18 files, ~340 callsites. Rough
   categorization at session time:
   - Easy: `users.py`, `user_groups.py`, `user_group_members.py`,
     `resource_grants.py`, `audit.py` — clean CRUD.
   - Easy: `table_registry.py`, `sync_state.py`, `sync_history.py`,
     `metrics.py`, `claude_md_template.py` — config/state.
   - Moderate: `data_packages.py`, `marketplace_*.py`, `store_*.py`.
   - Careful port: `knowledge.py` (52 KB; complex queries).
2. **Build `src/db_pg.py`** with same shape as `src/db.py::get_system_db()`
   — returns a connection-pool client. SQLAlchemy or asyncpg.
3. **Feature flag** in `src/repositories/__init__.py` — switch backend per
   repo. Dual-write + dual-read shadow for at least one week of risk
   control before cutover per repo.
4. **Migrate schema** — `src/db.py::_v51_to_v52` etc. become Alembic
   migrations. Pre-migrate snapshot dance (Pattern E) is replaced by
   Postgres point-in-time recovery.
5. **GCS-fuse the extracts dir**, mount read-only on each app VM.
   Orchestrator writes from one VM (single-writer to GCS bucket),
   others see new files within seconds.
6. **`analytics.duckdb`** stays per-VM, regenerated on boot or via
   filewatcher. No coordination needed.
7. **MIG, N=2 minimum**, rolling deploys, zero downtime.

The hard work is repository audit + migration testing, not the DuckDB
extension dance (which doesn't change).

### Phase 3 — Cloud Run / autoscale (optional, defer)

Once Phase 2 is stable for ≥1 month, re-evaluate Option E. Adds 0→N
autoscale + native blue-green. Trade-off: cold start + parquet caching
work. Likely never worth it for an internal corp deploy with bounded
traffic; document as "considered, deferred".

## 6. What changes in *this* (infra) repo

Bulk of the work is in the app repo (keboola/agnes-the-ai-analyst).
Infra-side delta:

| File / resource | Change |
|---|---|
| `modules/agnes-vm/` | Add an MIG variant (separate module or `count`-gated branch). Existing single-VM module stays for non-HA environments. |
| New `modules/agnes-cloudsql/` | Cloud SQL Postgres (HA, regional). Private IP only. SA + IAM. |
| New `modules/agnes-gcs-extracts/` | GCS bucket for shared `/data/extracts`. Versioning on. SA read-only access from app VMs, write from orchestrator only. |
| `locals.tf` | New per-environment toggle: `mode = "single-vm" \| "mig"`. `mig` mode opts into Cloud SQL + GCS-fuse extracts + ILB + N≥2 VMs. |
| `iam.tf` | App VM SA → `roles/cloudsql.client` + GCS bucket reader. Orchestrator SA → GCS bucket writer. |
| Networking | Private Service Connect / VPC peering for Cloud SQL private IP from app VMs. |
| `backup.tf` | Snapshot policies still apply to boot + ephemeral config disks. Cloud SQL has its own automated backups + PITR — separate concern. |
| `startup.sh` | Render `DATABASE_URL` from a SM-fetched Postgres connection string + creds (rotated via Cloud SQL IAM auth or SM-backed user/password). |
| `docs/architecture.md` | Document the split. Update CLAUDE.md per-VM matrix headings (state on Postgres vs on VM disk). |

## 7. Open questions / risks (logged at session end)

- **DuckDB-specific SQL in `system.duckdb` callsites.** Postgres doesn't
  natively support `STRUCT`, `LIST`, `QUALIFY`. Need a syntax audit before
  committing to repo-by-repo cutover order. Open audit item.
- **`knowledge.py` complexity** (52 KB) — port last; treat as a separate
  spike.
- **`session_processor_state`** + **`session_pipeline.runner`** — runtime
  state. Audit whether these are app-state (→ Postgres) or analytics
  cache (→ stays DuckDB, regen on boot).
- **Sessions / auth cookies** — currently JWT-only with HS256, no DB
  side. Stays as-is. No Memorystore Redis needed unless we add
  server-side session storage later (Phase 3 territory).
- **Latency budget audit.** Postgres calls are network hops; today's
  request handlers issue many `.execute()` calls serially. Need to flag
  worst offenders that need batching before this works at production
  load. Surface via PostHog timings before committing.
- **Cloud SQL vs AlloyDB.** AlloyDB has better OLAP performance and is
  PG-compatible; Cloud SQL has cheaper baseline + more org familiarity.
  Decision deferred — pick at provisioning time.

## 8. Triggering incidents (why this matters)

Two recent prod incidents that this plan would have prevented or
shortened:

- **2026-05-21 10-min `apply -replace` outage on `foundryai-production`**
  — the immediate trigger for this discussion. Phase 1 alone fixes this
  to ~1-2min; Phase 2 to zero.
- **2026-05-21 prod `data_packages` wipe** — caused by interaction
  between `scripts/fix-shadow-mount.sh` and the Pattern-E pre-migrate
  snapshot recovery in `src/db.py`. Phase 2 replaces the snapshot dance
  with Postgres PITR, removing the class of bug. See
  `docs/incident-2026-05-21-data-packages-wipe.md`.

## 9. Next decisions needed from operator

The session ended with two questions left open:

1. **Repo-by-repo migration sequence** with risk + line count per file —
   want this generated next?
2. **DuckDB-specific SQL syntax audit** in repos (`STRUCT`, `LIST`,
   `QUALIFY`, anything else PG doesn't support) — want this generated
   next?

Pick either or both; both are inputs to a concrete Phase-2 spec.

## 10. Provenance / further reading

- Session JSONL: `/Users/vrysanek/.claude/projects/-Users-vrysanek-foundry-ai-agnes-the-ai-analyst--worktrees-vr-marker-io-integration/b1b2b33a-0dc5-487b-9d99-1a013d6560a4.jsonl`
- Key message timestamps within session:
  - User prompt — 2026-05-21T11:25:53Z
  - Options A-F assistant reply — 2026-05-21T11:27:27Z
  - DuckDB-server landscape survey — 2026-05-21T12:05:29Z
  - Agnes pattern audit + refined Phase 2 — 2026-05-21T12:09:07Z → 12:11:08Z
- Session left in an "awaiting decision" state; never produced a committed
  artefact until this document. Created 2026-05-22.
