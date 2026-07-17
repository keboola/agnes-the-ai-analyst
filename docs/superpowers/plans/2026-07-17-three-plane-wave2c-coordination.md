# Three-Plane Wave 2-C — Coordination Backend (WS C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A swappable `CoordinationBackend` (memory | redis) carrying every piece of ephemeral cross-process state that today lives in module dicts: WS auth tickets, leader leases (Slack socket-mode, Telegram poll, singleton sweeps), rate limits + chat quotas, cache invalidation, and operational TTL codes — plus `.env_overlay` secrets moving to the control plane. Default `memory` mode keeps today's behavior byte-for-byte; `redis` mode makes api/gateway replicas coordination-safe.

**Architecture:** New `app/coordination/` package: `base.py` (interface), `memory.py`, `redis_backend.py`, `factory.py` (config-driven singleton, `coordination.backend` + `redis.url` from instance.yaml / `AGNES_REDIS_URL`). Consumers migrate one at a time behind the same interface. Redis invariant (spec §3.2): **disposable** — every consumer recovers from FLUSHALL (leases re-acquired ≤ TTL, tickets re-minted on next request, counters reset). Chat frame streams + session routing are **WS D (gateway)** — NOT this wave.

**Tech Stack:** Python/FastAPI, redis-py (sync client), fakeredis (dev dep, contract tests), pytest.

## Global Constraints

- Default (`coordination.backend: memory`, unset) ⇒ behavior identical to today; NO new deployment requirements for S tier. Full suite must stay green with zero config.
- Contract tests parametrize memory + fakeredis through ONE shared assertion set (`tests/test_coordination_contract.py`); things fakeredis can't emulate faithfully (pub/sub timing) get an env-gated real-Redis variant (`AGNES_TEST_REDIS_URL`) exercised by the m-tier smoke.
- Every consumer keeps a documented FLUSHALL recovery story (comment at the call site).
- Static ratchet discipline: new cross-process state goes through the backend — no new module-level dicts/locks outside `app/coordination/` (the wave-1 review rule).
- Playbooks for any endpoint/CLI touches: `.claude/skills/agnes-conventions/references/endpoint-rbac.md`, `command-ux.md`.
- Full suite before push; CHANGELOG consolidated in the final task; vendor-agnostic.

---

### Task 1: CoordinationBackend interface + memory + redis + factory + contract tests

**Files:** Create `app/coordination/{__init__,base,memory,redis_backend,factory}.py`, `tests/test_coordination_contract.py`. Modify `pyproject.toml` (add `redis` runtime dep + `fakeredis` dev dep — check they aren't already present).

**Interface (Produces):**

```python
class CoordinationBackend(ABC):
    # KV with TTL (tickets, operational codes)
    def kv_set(self, key: str, value: str, *, ttl_s: int) -> None: ...
    def kv_get(self, key: str) -> str | None: ...
    def kv_delete(self, key: str) -> str | None:
        """Atomically get-and-delete (single-use ticket semantics)."""
    # Counters (rate limits, quotas); window handled by caller-chosen key naming
    def incr(self, key: str, *, ttl_s: int) -> int:
        """Increment and return new value; sets TTL on first increment."""
    # Leases (leader election, singleton sweeps)
    def lease_acquire(self, name: str, holder_id: str, *, ttl_s: int) -> bool: ...
    def lease_renew(self, name: str, holder_id: str, *, ttl_s: int) -> bool: ...
    def lease_release(self, name: str, holder_id: str) -> None: ...
    # Pub/sub (cache invalidation); subscribe returns an unsubscribe callable
    def publish(self, channel: str, message: str) -> None: ...
    def subscribe(self, channel: str, handler: Callable[[str], None]) -> Callable[[], None]: ...
    def ping(self) -> bool: ...

def coordination() -> CoordinationBackend:  # factory.py singleton
    """memory (default) | redis per instance.yaml coordination.backend /
    AGNES_COORDINATION_BACKEND env override; redis.url / AGNES_REDIS_URL."""
def reset_coordination_for_tests() -> None: ...
```

Memory impl: dict + threading.Lock + monotonic-clock TTLs; subscribe = in-process handler list (fires synchronously on publish). Redis impl: redis-py; `kv_delete` via GETDEL; `lease_acquire` = `SET name holder NX PX`; renew = compare-holder-then-PEXPIRE via Lua or WATCH (must be atomic — copy the standard redlock-single-instance pattern); pub/sub via a daemon listener thread started lazily on first subscribe.

- [ ] Contract tests (both impls): TTL expiry; single-use get-and-delete atomicity (two threads, one winner); incr window semantics; lease exclusivity + renew-by-holder-only + release; steal after expiry; publish→handler delivery; FLUSHALL-equivalent (memory: reset; fakeredis: flushall) leaves the backend functional. Redis `ping` False (connection error) must not raise from consumers' happy paths — backend raises `CoordinationUnavailable`, callers decide.
- [ ] Wire the wave-1 guard: `app/startup_guards.py` — `coordination.backend=redis` becomes a third multi-process trigger (`is_multi_process()` returns True), closing the deferred wave-1 finding; update its tests.
- [ ] Commit `feat(coordination): backend interface with memory and redis implementations`

### Task 2: WS auth tickets ride the backend

**Files:** Modify `app/api/chat.py` (grep `_TICKETS`); test updates + new contract-behavior tests.

`_issue_ticket`/`_consume_ticket` (and the co-drive `/join` sibling — grep both call sites) switch to `coordination().kv_set/kv_delete` with the existing 60 s TTL, key prefix `ws-ticket:`. Delete the module dict + its expiry sweep. The code comment that says "HA needs ticket store in DuckDB or Redis (future spec)" gets replaced by the resolved story. Behavior contract: single-use (second consume of the same ticket fails), expiry honored — tests via the memory backend; existing chat tests must pass unchanged.

- [ ] Commit `feat(chat): WS tickets via coordination backend`

### Task 3: Leader leases — Slack socket-mode, Telegram poll, singleton sweeps

**Files:** Modify `app/main.py` (slack socket setup — grep `socket_mode_preflight`), `services/telegram_bot/bot.py` (poll loop), `app/chat/manager.py` (paused-sandbox TTL sweep — grep the reaper the wave-1 review located); tests.

Pattern (shared helper in `app/coordination/leases.py`): `run_with_lease(name, holder_id, ttl_s=15, work=...)` — acquire-or-wait loop with renew at ttl/3, release on exit; on lost renew, STOP the consumer and re-enter the acquire loop (Slack: disconnect socket; Telegram: stop polling). In `memory` mode the lease is process-local ⇒ always acquired ⇒ today's behavior. Gateway-role gating from wave 1 stays; the lease adds N-replica safety on top. FLUSHALL story: lease lost → consumer stops → re-acquires ≤ TTL; document at each site.

- [ ] Tests: two fake consumers, one lease — exactly one active; holder death (no renew) → takeover ≤ TTL; memory-mode always-acquire.
- [ ] Commit `feat(coordination): leader leases for slack socket, telegram poll and sweeps`

### Task 4: Rate limits + chat quotas

**Files:** `app/auth/rate_limit.py` (slowapi `storage_uri`), `app/chat/manager.py` (hourly msg window — grep `_user_msg_window`; daily token cache — grep `_daily_tokens_cache`; per-user concurrency — grep `_active_count_for_user`); tests.

- slowapi: when backend is redis, pass `storage_uri=<redis url>` to `Limiter` (slowapi supports redis natively); memory mode unchanged. One functional test with fakeredis storage… slowapi needs a real URI — if fakeredis can't plug in, test only the wiring (Limiter constructed with the URI when configured) and leave enforcement to the m-tier smoke; document.
- Chat hourly window + daily tokens: replace in-memory structures with `incr` counters keyed `chat-msgs:{user}:{hour_bucket}` / `chat-tokens:{user}:{date}` (TTL 2 h / 25 h). Per-user concurrency: count via `incr`/decr-style… simpler and honest: keep concurrency counting process-local in this wave (it becomes lease-derived in WS D when routing leases exist) — document that explicitly instead of building a throwaway.
- [ ] Commit `feat(coordination): shared rate limits and chat quotas`

### Task 5: Cache invalidation + operational TTL codes

**Files:** `app/api/v2_cache.py` + invalidation call sites (grep `invalidate_for_table|invalidate_all`), `app/api/cli_auth.py` + `services/slack_bot/binding.py` (operational.duckdb users — grep `get_operational_db`); tests.

- Cache invalidation: publisher side calls `coordination().publish("cache-invalidate", json{scope,table})` in addition to local invalidation; each api process subscribes at startup (lifespan) and drops matching local TTL entries. Memory mode: synchronous local delivery = today's behavior.
- CLI auth codes + Slack binding codes: reads/writes move to `kv_set/kv_get/kv_delete` (prefixes `cli-auth:`, `slack-bind:`) with their existing TTLs; the DuckDB `operational.duckdb` path REMAINS as the memory-mode storage (no data migration — codes are ephemeral; redis mode simply stops touching the file, removing one of the two remaining always-RW DuckDB files from multi-process topologies). Audit/issue logs that live next to binding codes stay where they are (they're durable — control plane).
- [ ] Tests: invalidation event drops the local cache entry; codes round-trip + single-use + TTL on both backends; memory mode still uses operational.duckdb (regression).
- [ ] Commit `feat(coordination): cache invalidation pub/sub + operational codes`

### Task 6: `.env_overlay` → control-plane vault + reload hook

**Files:** `app/secrets.py` (grep `persist_overlay_token`, `_overlay_lock`), vault storage (grep `app/secrets_vault.py` + its repo), startup overlay-load site in `app/main.py`; tests.

`persist_overlay_token` writes the token to the vault-backed control-plane store (both backends via factory — grep for the existing vault/secret repo pair; if the vault requires `AGNES_VAULT_KEY` and it is unset, fall back to the current file behavior with a warning — do NOT break keyless S-tier installs), then publishes `env-overlay-changed` via coordination; every process (api/worker/gateway) subscribes and re-applies tokens to `os.environ` on the event. File overlay remains as legacy-read fallback at boot. FLUSHALL story: event lost ⇒ stale env until next restart — document; acceptable (PATs change rarely) — plus a periodic re-read (piggyback on an existing periodic loop, grep the checkpoint loop) as belt-and-braces.

- [ ] Tests: set token → vault row exists + env updated in-process; event handler re-applies; keyless fallback warns + keeps file path; worker process sees the token after event (simulated by invoking the handler).
- [ ] Commit `feat(secrets): overlay tokens via vault + cross-process reload`

### Task 7: m-tier smoke extension + FLUSHALL chaos

**Files:** `scripts/dev/mtier-smoke.sh`, `config/instance.mtier.yaml` (already has `coordination: {backend: redis}` — verify redis.url plumbing reaches the containers).

Extend the smoke: after boot, (a) assert a WS ticket lands in redis (`docker compose exec redis redis-cli --scan --pattern 'ws-ticket:*'` after hitting the ticket-minting endpoint — or simpler observable: hit an endpoint that mints, then SCAN count > 0); (b) `redis-cli FLUSHALL` under traffic → healthz/readyz stay green, api containers log no tracebacks, a fresh login/ticket flow works (mint + consume OK); (c) kill the gateway container → Slack/Telegram lease reacquisition is N/A in smoke (no tokens) — instead assert the lease keys reappear for the sweep lease within TTL.

- [ ] Run `./scripts/dev/mtier-smoke.sh` to `MTIER SMOKE OK`.
- [ ] Commit `feat(harness): coordination assertions + FLUSHALL chaos in m-tier smoke`

### Task 8: Docs + CHANGELOG + full suite

- DEPLOYMENT.md Multi-process section: coordination backend paragraph (redis config, disposability invariant, FLUSHALL semantics). architecture.md: coordination section. CHANGELOG consolidated wave bullet. Full suite + smoke green.
- [ ] Commit `docs: coordination backend (wave 2C)`

## Self-review notes

Deliberately out of this wave (say so in docs): chat session routing + frame streams + inbound command routing + cross-gateway takeover (WS D); per-user chat concurrency across replicas (WS D, lease-derived); request-id correlation (WS G); LISTEN/NOTIFY job wakeup (unrelated); redis HA (single non-HA redis is the supported stance, spec §3.2).
