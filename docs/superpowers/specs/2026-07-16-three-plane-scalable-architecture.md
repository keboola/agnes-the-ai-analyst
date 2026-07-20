# Three-Plane Scalable Architecture

**Status:** Draft v2 — post-review (4 independent review passes: architecture-vs-code, ops/rollout, data plane, open-questions advisory; 41 findings incorporated)
**Date:** 2026-07-16
**Scope:** One integration branch delivering horizontal scalability (multi-process, load-balanced) for the entire platform, verified by E2E + load tests on a dedicated test instance before merge.

---

## 1. Problem

Agnes is architected as a single stateful process on a single disk:

- One uvicorn worker; cloud chat disables itself when `UVICORN_WORKERS > 1`.
- App-state (`system.duckdb`), analytics (`server.duckdb`), and operational DuckDB files are opened read-write with exclusive per-process file locks — a second process cannot even start.
- Coordination is in-process memory: WS auth tickets, live chat session map, sync lock, rate limiter, TTL caches, scheduler job state.
- Heavy background work (extract, BigQuery materialize, profiling) runs inside the API process and competes with live queries for one memory budget — the direct cause of production OOM kills.
- All artifacts (parquets, marketplace clones, file corpora, session transcripts) live on one local disk; distribution bandwidth is bounded by one NIC.
- The reverse proxy has exactly one upstream. There is nothing to load-balance.

Adding a load balancer changes nothing until state leaves the process. This spec removes the state, splits the process into roles, and makes scaling a deployment-configuration choice rather than an architecture fork.

## 2. Goals

1. **N × api replicas** behind any L7 load balancer, zero sticky sessions required.
2. **Failure isolation:** a runaway materialize or sync can never OOM the API serving users.
3. **Low-disruption rolling upgrades:** zero-error scripted sequential upgrade at M tier; formal zero-downtime is an L-tier property (external orchestrator). Requires the N-1 schema rule (§3.9).
4. **Analytics storage/compute separation:** DuckLake catalog + immutable data files replace the rebuilt-and-swapped DuckDB file; query capacity scales by adding stateless replicas.
5. **Resumable realtime:** chat/WS survives replica death; client reconnects to any gateway, which replays missed frames and **respawns the runner** (v1 semantics: cross-gateway takeover = destroy + respawn + context replay — same as today's cross-restart recovery, not live-process handoff).
6. **Single-node mode stays first-class:** the current zero-ops single-VM deployment keeps working with the same image and default config. Scaling up is config, not a different product.
7. Verified by an **E2E suite and a load test with measurable SLOs** on a dedicated instance deployed from the branch.

### Non-goals

- Multi-region / multi-tenant control plane; distributed tracing (structured log correlation only, §3.7).
- DuckDB Quack backend (future third app-state backend via the existing factory).
- Removing the DuckDB-file app-state backend (stays for single-node tier).
- Live in-process chat handoff between gateways (v1 = respawn + replay).
- Autoscaling policies / Kubernetes manifests (branch proves fixed replica counts).

## 3. Architecture

### 3.1 Process roles

One image, one entrypoint, `AGNES_ROLE` env (comma-separable):

- **`api`** — REST, web UI, MCP (streamable), CLI endpoints, manifest/signing. Stateless and **write-free for analytics**: every current in-api writer path (Jira webhook rebuild, file-corpus ingest/delete, admin register-table rebuild) is converted to enqueue-and-ack (§3.3).
- **`gateway`** — WebSocket/SSE termination: web chat, MCP SSE, desktop notifications; Slack/Telegram consumers under leader lease. Absorbs the standalone `ws_gateway` service.
- **`worker`** — job consumers; the only analytics writer.
- **`all`** (default) — every role in one process; behavior-compatible with today.

Role-gated lifecycle: DB migrations and seeds run once under a coordination lease (worker preferred, any role can win it; others wait for completion marker); startup warmup and `AGNES_REBUILD_ON_BOOT` are worker-only. A concurrent-cold-boot test is part of §5.1.

### 3.2 Shared state backends

**Postgres — durable truth.** Required whenever more than one process shares state: `AGNES_ROLE ≠ all`, **or** `UVICORN_WORKERS > 1` (multi-worker *is* multi-process), or `coordination.backend=redis`. Holds control plane, job queue, DuckLake catalog. Single-process `all` mode keeps the DuckDB-file backend.

**Redis — ephemeral coordination** (optional; required for multi-process). Explicit invariant: **Redis is disposable** — every consumer must recover from `FLUSHALL` (leases re-acquired within TTL; stream loss ⇒ client full-refresh; counters reset). Verified by a chaos case in §5.1.

| Concern | Mechanism |
|---|---|
| WS auth tickets | `SETEX` (60 s TTL) |
| chat/session routing + per-user concurrency cap | lease keys `chat:{id} → gateway_id`; cap = count of live leases per user |
| realtime frame replay (outbound) | Redis Streams per session, `MAXLEN ~1000`, resume by last-seen frame id |
| inbound command routing (user → runner) | per-session command stream consumed by the lease-holding gateway (ordered, at-least-once with frame-id dedup) |
| rate limits & chat quotas | counters (slowapi `storage_uri`, chat token windows, daily budgets) |
| leader leases | `SET NX PX` + renew: Slack socket-mode, Telegram poll, singleton sweeps (paused-sandbox TTL reaper) |
| cache invalidation | pub/sub; replicas drop local TTL caches on event |
| operational TTL data | CLI auth codes, Slack binding codes (replaces `operational.duckdb`; DuckDB fallback in `all` mode) |

Durable secrets are **not** Redis material: the `.env_overlay` mechanism (marketplace/workspace PATs patched into process env) moves into the control-plane vault with a pub/sub reload hook, so a PAT set on one replica is visible to workers immediately.

### 3.3 Job queue (Postgres)

`jobs` table + repository (both backends, contract-tested): `id, kind, payload_json, status, priority, run_after, attempts, max_attempts, lease_expires_at, leased_by, idempotency_key, created_at, finished_at, error`. Claim via `SELECT … FOR UPDATE SKIP LOCKED` (PG) / single-writer semantics (DuckDB `all` mode); heartbeat leases; `LISTEN/NOTIFY` wakeup with polling fallback; `idempotency_key` replaces the in-process `_sync_lock`/409 logic. `GET /api/jobs/{id}` + CLI + MCP per the command-UX standard.

**Two claim lanes per worker** (static `kind → class` map, no schema change):
- **heavy, concurrency 1**: extract/sync, per-table materialize, profiling, corpus ingest. Rationale: DuckDB sessions are capped (2 GB/2 threads) but the envelope isn't — pyarrow full-table reads, BQ download buffers, ~2× result-size temp disk. One heavy job per 4 GiB worker cannot OOM; two can. Throughput scales by adding workers.
- **light, concurrency 2**: marketplace sync, session pipeline, corporate memory, knowledge digests, DuckLake maintenance, store lint/purge, usage prune. Prevents a multi-hour materialize from starving everything else.

Workers set DuckDB `temp_directory` to a scratch volume and **sweep stale scratch** (`kbc-export-*`, `*.tmp`) at job start — closing the known disk-full failure mode.

**Full inventory:** every scheduler-driven endpoint (~20) is classified job-kind vs stays-HTTP in an appendix table maintained in the workstream; the rule: anything LLM-, git-, BQ- or DuckDB-write-heavy becomes a job; sub-second idempotent pokes (health, cache refresh triggers) may stay HTTP. **In-api writer paths converted to enqueue:** Jira webhook → `jira-refresh` job (ack-fast); corpus upload → staged blob + `corpus-ingest` job; admin register-table → `analytics-register` job; `agnes push` uploads → staged + `session-pipeline` job. Staging target: shared data volume (S/M) or object store (L) — never an api replica's private disk.

The scheduler remains a cron clock but emits enqueues; per-job catch-up semantics replace its in-memory `last_run`.

### 3.4 Data plane — DuckLake

DuckLake v1.0 (spec production-ready 2026-04; extension in DuckDB ≥ 1.5.2) becomes the **server-side query surface**. Key correction from review: DuckLake owns its data files (immutable; compaction/expiry may rewrite or delete them), while our extractors rewrite parquets in place — the two must never share physical files.

**Dual-artifact design:**
- The **extracts tree keeps its current contract** — connectors untouched, same per-table parquet paths, same in-place atomic-rename rewrites. It remains (a) the *distribution artifact* for `agnes pull` (manifest MD5/size/ETag semantics unchanged, reverse-proxy file-server bypass unchanged) and (b) the *rollback source of truth*.
- The worker **copy-ingests** each successful sync/materialize into DuckLake (`CREATE OR REPLACE TABLE … AS SELECT * FROM read_parquet(…)`). No `ducklake_add_data_files` metadata-only registration of mutable paths — ever. Storage cost: extracts + DuckLake copies coexist (≈2× table footprint; sized in §3.8).

**Catalog:** PG for any multi-process topology; DuckDB file **only while all roles share one process** (POC-verified: a second process attaching a DuckDB-file catalog fails on the exclusive file lock; startup guard enforces this alongside §3.2's rules). SQLite was evaluated as a middle option and **rejected**: POC with two concurrent writer processes failed with `database is locked` — multi-process ⇒ PG, no exceptions.

**Readers:** api replicas hold a **long-lived catalog attach**. POC-verified: one ducklake ATTACH holds exactly **one persistent PG connection**, with no additional connections per query or write — so catalog load is N api + M workers connections total; `max_connections` sizing is trivial and pgbouncer is unnecessary at this scale. Queries see consistent snapshots (POC-verified: a reader transaction holds its snapshot across a concurrent writer commit). Per-request re-ATTACH of extract files disappears.

**Remote & internal tables:** `_remote_attach` rows move from extract.duckdb into the control plane (registry-driven); remote-mode tables are exposed as persisted DuckLake views with the reader session attaching the remote extension first. **Spike done (POC-verified):** a persisted DuckLake view referencing a foreign attached alias is late-bound — it resolves in any session that has the alias attached and errors cleanly otherwise, exactly the needed contract. Internal registry tables (`agnes_sessions`, …) stay on the control-plane query path, unaffected by `analytics.backend`. `/api/query/hybrid` rides the new reader session (temp registration + same view names), contract-tested.

**View namespace:** per-source schemas in the DuckLake catalog + a master views layer; the existing view-ownership claim/reconcile logic is ported as catalog-level claims so parallel per-table jobs from multiple workers cannot collide.

**Maintenance** (light-lane job, correct order — compact before expire, cleanup frees disk; call signatures POC-verified on DuckDB 1.5.4): `CALL <cat>.merge_adjacent_files()` → `CALL ducklake_expire_snapshots('<cat>', older_than => …)` → `CALL ducklake_cleanup_old_files('<cat>', cleanup_all => true)`, plus catalog VACUUM. Full-table-replace syncs generate ~1 table-size of dead files per sync; disk sizing assumes ≈2× peak between maintenance runs. Concurrent writers on different tables are safe (POC-verified: two writer processes on a PG catalog both committed via DuckLake's snapshot-conflict retry), so worker×M needs no extra analytics lock.

**Feature pins:** `ENCRYPTED` **off** (incompatible with raw-file distribution). Data inlining optional for high-frequency push connectors (Jira), with `ducklake_flush_inlined_data` added to maintenance; subject to PG-catalog type limits (UBIGINT/nested round-trips — guarded by a type-matrix test in §5.1).

**Legacy mode kept:** `analytics.backend: legacy` preserves today's ATTACH/rebuild flow (S tier default and rollback). Rollback semantics: flipping back rebuilds from extracts; materialized outputs re-materialize on next schedule (stated, accepted).

### 3.5 Realtime & agent plane

- Tickets, routing, replay, inbound command stream per §3.2. WS frames gain an **envelope with monotonic frame ids** (client + `SlackSinkBridge` change, scoped in WS D); reconnecting clients send last-seen id, gateway replays the gap from the stream.
- **Cross-gateway takeover (v1):** a gateway that doesn't own a live runner claims the routing lease, **destroys and respawns** the sandbox runner, and replays recent context — the same semantics today's restart-recovery path provides (the runner-protocol ticket guard makes foreign live-resume unsafe; a persisted protocol-version column enabling true foreign resume is a stated follow-up).
- **Reapers:** idle-reaper is owner-scoped (each gateway reaps its leased sessions); the paused-sandbox TTL destroyer is a leased singleton sweep.
- **Slack/Telegram:** socket-mode and long-poll consumers run in gateway under a leader lease (failover ≤ 5 s). In webhook mode, Slack HTTP handlers (api role) become thin producers: resolve session → publish to the session's inbound command stream; the owning gateway consumes. The standalone `telegram_bot`/`ws_gateway` services are absorbed; their compose entries removed.
- **`UVICORN_WORKERS` gate replaced, not removed:** >1 worker requires PG + Redis (same guard as multi-replica). S tier stays single-worker.

### 3.6 Distribution

Manifest and `agnes pull` keep today's per-table-parquet contract against the extracts tree (§3.4 dual-artifact). When an object store is configured, the worker mirrors distribution parquets to a bucket prefix after each sync and manifest v2 adds presigned GET URLs (TTL ≈ 15 min) next to md5/size; `agnes pull` prefers URLs, falls back to `/api/data/{id}/download`. Without an object store (S/M default), the app-served path + reverse-proxy file-server bypass remain the download path — **and are load-tested as such**. Marketplace zip / corpus reads: out of scope except interfaces (§3.8 covers their FS coupling).

### 3.7 Config, health, secrets, observability

- `instance.yaml`: `deployment.role`, `coordination.backend: memory|redis` + `redis.url`, `analytics.backend: legacy|ducklake` + `ducklake.{catalog_dsn,data_path}`, `distribution.signed_urls: auto|on|off`.
- **Health:** `/healthz` liveness. `/readyz` readiness = cheap cached connectivity check **plus the result of a low-frequency background write-canary** (catches the known "reads OK / writes 500" zombie state) with M-of-N hysteresis to prevent flapping; the proxy never removes the last healthy upstream (falls through to maintenance page instead of hard-503ing everything). `/api/health` remains an alias — existing compose healthchecks, watchdogs and uptime monitors keep working.
- **Secrets:** multi-process startup hard-fails without explicit `JWT_SECRET_KEY` + `SESSION_SECRET` (error names the variable + docs link). `AGNES_VAULT_KEY` is required only when vault-backed features are enabled. Infra templates/startup scripts are extended in lockstep to mint + emit `SESSION_SECRET` (today nothing emits it). Note: on a single VM the file-generated secrets are shared via the common volume — the hard-fail is policy (predictability), not a functional necessity; docs say so.
- **Observability:** Prometheus `/metrics` on every role (HTTP histograms, queue depth/lag, DuckLake snapshot age, stream lag). The m-tier compose profile ships a harness-local Prometheus + cAdvisor for per-container memory. Structured logging requirement: request-id propagated into job payloads and logged by workers; replica id on every line. Tracing = explicit non-goal.

### 3.8 Deployment tiers & compose changes

| Tier | Topology | State |
|---|---|---|
| S (default) | 1 VM, `AGNES_ROLE=all`, no Redis/PG | DuckDB file + local FS (legacy or DuckLake/DuckDB-catalog) |
| M | 1 VM, compose `m-tier` profile: api×2, gateway×1, worker×2, redis, postgres, proxy, prometheus | PG + Redis + local FS |
| L | multi-VM/k8s: LB → api×N, gateway×N, worker×M; managed PG + Redis; object store | fully external |

Enumerated compose/proxy changes (review finding — previously implied): api services drop host-port publishing (proxy-only exposure); proxy gains multi-upstream with active `/readyz` health checks (`health_uri`, `fail_duration`) for both main routing and `forward_auth`; per-role mem/cpu limit env knobs (`AGNES_<ROLE>_MEM_LIMIT/_CPUS`) with a **worked M-tier memory budget** (min VM 4 vCPU/16 GB; caps sum < RAM; DuckDB per-connection limits set per role); Redis runs ephemeral (`--save '' --appendonly no`), compose-network-only, no disk prep; the deployment state machine gains a persisted topology field next to `database.backend`.

**Local-FS trees shared across roles** (session transcripts, corpus blobs, knowledge artifacts, chat workdirs, marketplace clones): worker writes, api/gateway read via the shared data volume — free on one VM; **L tier explicitly requires a shared filesystem (NFS/GCS-FUSE) for these trees** until each is individually moved to object store (marketplace object-store snapshot is the flagged follow-up). Marketplace serving stays in api: clones mounted read-only, each replica builds its own git-cache lazily (deterministic commit metadata ⇒ identical SHAs across replicas; no api→worker proxy).

**Host tooling updated in the same branch:** auto-upgrade script (rewritten for role-split: drift detection per service, sequential api recreate gated on `/readyz`, sync-defer via job-queue query instead of `/api/sync/status`), watchdog (per-role containers), backup units (add `pg_dump` of control plane + DuckLake catalog alongside the existing DuckDB file backup, with restore canary).

### 3.9 Upgrade & compatibility rules

- **N-1 schema compatibility:** a migration shipped in release N must be readable by release N-1 (expand-migrate-contract), or the release is marked non-rolling and the upgrade script serializes it behind a brief maintenance window. This rule is what makes goal 3 achievable at all.
- **M-tier rolling upgrade:** scripted — upgrade worker/gateway, then api replicas one at a time, `/readyz`-gated; measured by the §5.3 rolling scenario. Zero-downtime as a formal guarantee = L tier.
- **Auto-upgrade cron compatibility:** new compose overlays are opt-in (profile/COMPOSE_FILE) and inert for pinned older images; the §4 migration runbook starts by disabling the cron and ends by re-enabling it.

## 4. Migration (existing instance, in-place)

1. **Freeze:** disable auto-upgrade cron; take PD snapshot + `pg_dump` (if side-car PG already present).
2. App-state: existing DuckDB→PG migrator (the one-shot migrate container is the downtime window — minutes; stated in the runbook with a measured estimate from the test instance).
3. Analytics: flip `analytics.backend: ducklake`; the first worker sync copy-ingests every table (extracts are already on disk — no re-extract). Rollback = flip back to `legacy` (extracts remained canonical; materialized tables re-materialize on schedule).
4. Coordination/operational: no data migration (ephemeral; new codes issue to Redis).
5. **Unfreeze:** re-enable cron; verify backup timer now covers PG.

## 5. Testing & acceptance

### 5.1 Suites (CI, both state backends)

- Contract tests: jobs repo (DuckDB/PG), coordination backend (memory/Redis), DuckLake **type-matrix** (PG catalog round-trips) and **catalog connection-pool** behavior, hybrid-query on the new reader path.
- Role-split integration harness (compose: api×2+gateway+worker+redis+pg): existing E2E chat/web/MCP suites against the LB endpoint; concurrent cold-boot (migration/seed lease); chaos: `kill -9` each role, **Redis `FLUSHALL`**, PG restart, mid-chat gateway kill → reconnect+replay+respawn.
- Static ratchet: new module-level mutable coordination state (`threading.Lock`, module dict registries) outside the coordination backend is review-blocking.

### 5.2 Instance E2E (test VM, deployed from branch)

Full user-path on M-tier: OAuth login, register→sync→query (local/remote/materialized), `agnes pull` (both download paths), web chat incl. gateway kill mid-session, Slack round-trip, MCP, admin surfaces, marketplace zip+git clone.

### 5.3 Load test — measurable SLOs

Measurement point: k6 at the LB. Error = HTTP ≥ 500 or unrecovered WS disconnect (client aborts excluded). Per-endpoint percentiles for the top endpoints, not blended. "Flat"/"no growth" = linear-fit RSS slope < 30 MB/h per container over the soak window.

| Scenario | Target |
|---|---|
| 50 concurrent analysts: catalog/schema/query mix (agg over ≥10 M rows) | per-endpoint p95: catalog < 300 ms, query < 2 s; errors 0 |
| 200 concurrent downloads (file-server bypass path on M; signed URLs on the bucket-configured instance) | manifest p95 < 300 ms; api CPU delta < 10 % |
| OAuth login storm (50 logins/min) + authed browsing | p95 < 1 s, errors 0 |
| marketplace.zip + git clone × 20 concurrent | p95 < 5 s, identical SHAs across replicas |
| 100 concurrent chat sessions (fake-agent runner; small real-E2B sample per sandbox quota) | frame delivery p95 < 500 ms |
| sync + 2 materialize during all of the above | query p95 degradation < 25 %; no OOM (cgroup counters) |
| BQ remote guard: 20 concurrent over-cap queries | 100 % clean 400s, no slot leak |
| disk pressure: scratch filled to 85 % | jobs fail cleanly with hint; sweeper recovers; api unaffected |
| kill one api replica at full load | error rate < 0.5 %, recovery < 10 s |
| Redis FLUSHALL at full load | reconnect storm absorbed; no 5xx beyond WS re-auth; leases re-acquired < 5 s |
| PG connection exhaustion probe (workers × catalog attaches) | graceful queuing, no reader failures |
| scripted rolling upgrade at full load | zero non-retryable errors |
| soak: 8 h at 50 % load with accelerated query/extract churn, THP=madvise (prod kernel parity) | RSS slope < 30 MB/h per container; queue lag flat |

### 5.4 Merge acceptance criteria

1. CI green on both backends; existing E2E suite passes unchanged against the default (S-tier) config.
2. Instance E2E checklist passes on the M-tier deployment.
3. All §5.3 SLOs met; results committed as a report artifact.
4. Docs: DEPLOYMENT (tiers, secrets, upgrade rules), architecture, RELEASING (new containers, N-1 rule), CHANGELOG.

## 6. Workstreams

| WS | Content | Depends on |
|---|---|---|
| A | `AGNES_ROLE` split, startup guards (secrets, catalog/backend rules, UVICORN_WORKERS), migration/seed lease, `/healthz`+`/readyz` (+alias), role-gated warmup | — |
| B | Jobs repo (dual-backend) + worker runtime (heavy/light lanes, scratch sweep) + full scheduler inventory classification + convert in-api writers (Jira webhook, corpus ingest, admin register, push staging); scheduler → enqueuer | A |
| C | CoordinationBackend (memory/Redis): tickets, leases, limits/quotas, cache invalidation, operational TTL data, `.env_overlay` → vault + reload | A |
| D | Gateway role: routing leases, frame envelope (client + SlackSinkBridge), outbound replay + inbound command streams, claim→respawn takeover, reaper scoping, bot leader election, Slack-webhook producer path, ws_gateway/telegram absorption | C |
| E | DuckLake backend: copy-ingest writer, reader sessions (long-lived attach), remote-view + `_remote_attach` relocation (view-over-alias contract already POC-verified), view-ownership port, maintenance jobs, migration flip, legacy fallback | B |
| F | Distribution: bucket mirror + manifest v2 signed URLs + `agnes pull` support + CI presign contract test | E |
| G | Observability: `/metrics`, m-tier Prometheus+cAdvisor, structured log correlation (request-id → job-id) | A |
| H | Test harness: role-split compose, chaos scripts (incl. FLUSHALL/PG), k6 suite + CLI swarm, SLO report generator | A |
| I | Ops tooling: auto-upgrade rewrite (sequential `/readyz`-gated), watchdog/backup updates (pg_dump + canary), infra template SESSION_SECRET emission, m-tier provisioning field | A |

Integration order: A → {B, C, G, H, I parallel} → D → E → F → full E2E + load.

## 7. Decisions (incl. resolved open questions)

- **Redis over NATS**; jobs in Postgres (durability + transactional coupling); swappable `CoordinationBackend`.
- **DuckLake over Quack / pg_ducklake**; dual-artifact (extracts = distribution + rollback; DuckLake = query surface) over single-artifact, because extractor in-place rewrites are incompatible with DuckLake file ownership and the `agnes pull` MD5 contract must survive.
- **Q1 resolved — no bundled object store.** M-tier default = local FS + file-server bypass; **MinIO rejected** (community edition unmaintained since 2026-02; AGPL/source-only trajectory unfit for a source-available product). Self-hosters needing on-prem S3 at L tier → SeaweedFS (Apache-2.0) or managed bucket, docs-only. Signed URLs verified via CI presign contract test + the branch test instance configured with a real bucket.
- **Q2 resolved — heavy lane 1 / light lane 2 per worker** (static kind→class map; scale-out = more workers; scratch sweep at job start).
- **Q3 resolved — marketplace: shared volume read-only into api + replica-local git-cache** (deterministic SHAs make caches consistent by construction); no api→worker proxy (would couple user latency to batch-worker load and still needs the volume with worker×M). L-tier object-store snapshot remains the follow-up.
- **Gateway as a role of the same image** (one build/release, shared auth/config).
- **Cross-gateway chat takeover = respawn + replay in v1** (foreign live-resume is unsafe under the runner-protocol guard; protocol-version persistence is the follow-up that would enable it).

## 8. Follow-ups (explicitly out of this branch)

Marketplace/corpus object-store snapshots (removes the L-tier shared-FS requirement); true foreign gateway resume; Quack app-state backend; autoscaling policies; DuckLake data inlining for push connectors (optional, revisit after type-matrix results).
