# Three-Plane Wave 2-E — Ops Tooling (WS I)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make the host-side operator tooling in `infra/modules/customer-instance/` role-split-aware and multi-process-safe: emit `SESSION_SECRET` (closes the wave-1 secrets-hard-fail gap), add a `pg_dump`-based backup + restore-canary alongside the existing DuckDB backup, make the auto-upgrade script do a sequential `/readyz`-gated recreate when a role-split topology is present, and make the watchdog aware of multiple role containers.

**Architecture:** These are GCE host scripts + Terraform templates in `infra/modules/customer-instance/`. They run on the VM, so testing is shell-level (`bash -n`, `shellcheck`) + a small pure-bash unit harness for the extractable logic; the true validation is the bod-3 VM run. Default single-container (S tier) behavior MUST be unchanged. Spec §3.8/§3.9.

**Tech Stack:** Bash, Terraform (HCL), pytest only where a Python helper is touched.

## Global Constraints

- S-tier single-VM single-container behavior byte-for-byte unchanged (the module's default path). Every new behavior is gated behind detecting a role-split/postgres topology.
- `shellcheck` clean (or documented disables) + `bash -n` on every touched script.
- Vendor-agnostic: no customer names, project IDs, hostnames in the module (it's the public reusable module — placeholders only).
- CHANGELOG bullet in the final task.
- Do NOT run live GCE/Terraform apply — static validation only (this wave ships the scripts; bod 3 exercises them).

---

### Task 1: Startup script emits SESSION_SECRET (closes wave-1 gap)

**Files:** `infra/modules/customer-instance/files/startup-script.sh.tpl` (grep where `JWT_SECRET_KEY` + `AGNES_VAULT_KEY` are minted/written to `.env`), possibly `variables.tf`/`main.tf` if the secret is sourced from Secret Manager like the others.

Today the startup script writes `JWT_SECRET_KEY` + `AGNES_VAULT_KEY` but NOT `SESSION_SECRET` (which the app auto-generates to a local file — fine single-node, but the wave-1 multi-process guard now hard-fails without it). Mint + write `SESSION_SECRET` the exact same way `JWT_SECRET_KEY` is handled (same source: Secret Manager fetch or generated-and-persisted, whichever the template uses), so a role-split deployment via this module satisfies the guard. Single-container deployments get it too (harmless — shared via the same .env).

- [ ] Extract the secret-writing block's logic into a testable shell function if practical; unit-test with a bash harness that the function writes all three keys. Otherwise `bash -n` + `shellcheck` + a grep-assert test in `tests/` that the template contains `SESSION_SECRET`.
- [ ] Commit `feat(infra): startup script emits SESSION_SECRET for multi-process guard`

### Task 2: pg_dump backup + restore-canary

**Files:** `infra/modules/customer-instance/files/agnes-db-backup.sh` (grep — it copies `system.duckdb` + WAL with canary verify), the systemd timer/unit if separate.

When the deployment uses the Postgres backend (detect via the persisted `instance.yaml::database.backend` or presence of a postgres container — mirror how other scripts detect it), ALSO `pg_dump` the control-plane DB to the backup dir with the same retention + a restore-canary (restore into a temp DB, run a trivial `SELECT count(*) FROM users`, drop). Keep the DuckDB backup path for the DuckDB backend unchanged. The DuckLake catalog (once WS E lands) lives in the same PG, so pg_dump covers it — note that forward-reference in a comment.

- [ ] `bash -n` + `shellcheck`; unit-test the backend-detection + dump-command-construction logic via a bash harness (mock `docker`/`pg_dump` on PATH, assert the command line). Document that the live restore-canary is bod-3-verified.
- [ ] Commit `feat(infra): pg_dump backup with restore canary for postgres backend`

### Task 3: Auto-upgrade sequential /readyz-gated recreate

**Files:** `infra/modules/customer-instance/files/agnes-auto-upgrade.sh` (grep — it detects image/config drift and does `docker compose up -d`; grep `docker compose ps` / `/api/sync/status`).

Today it recreates all changed containers at once (brief blip). When a role-split topology is detected (multiple api services / the mtier profile / a persisted marker), instead: pull the new image, then recreate role containers in an order that keeps serving — worker + gateway first, then api replicas ONE AT A TIME, waiting for each replica's `/readyz` to return 200 before the next (bounded timeout, abort + alert on failure). Single-container topology keeps the current one-shot behavior. Also: the script's sync-defer probe currently curls `/api/sync/status` — update it to also treat a running `data-refresh` job (query `/api/jobs?kind=data-refresh&status=running`) as "busy, defer" so it doesn't recreate mid-sync now that sync runs in the worker (grep the defer logic).

- [ ] Extract the recreate-order + readyz-poll logic into testable functions; bash-harness unit test: given a fake `docker compose` + a fake curl returning 503-then-200, the function waits then proceeds; on persistent 503 it aborts with a non-zero exit + alert call. `bash -n` + `shellcheck`.
- [ ] Commit `feat(infra): sequential readyz-gated rolling recreate for role-split topologies`

### Task 4: Watchdog role-container awareness

**Files:** `infra/modules/customer-instance/files/agnes-watchdog.sh` (grep — monitors a single app container's logs/restart-count/zombie-DB signature).

Iterate over all agnes role containers (api1/api2/gateway/worker or whatever compose reports) rather than a hardcoded single `app`, applying the existing incident signatures (crash loop, zombie-DB, OOM, restart burst, disk pressure) per container, and include the role/container name in the alert. Single-container topology still works (the loop finds one container). Add a coordination-backend signature: if redis is configured and unreachable (a role container logging repeated `CoordinationUnavailable`), alert. Keep the alert-webhook mechanism unchanged.

- [ ] Bash-harness unit test the container-enumeration + per-container signature scan with mocked `docker` output. `bash -n` + `shellcheck`.
- [ ] Commit `feat(infra): watchdog monitors all role containers`

### Task 5: Docs + CHANGELOG + static validation sweep

**Files:** `docs/DEPLOYMENT.md` (ops section: what the role-split module deploys, the rolling-upgrade behavior, backup coverage), `CHANGELOG.md`, `docs/RELEASING.md` if the rollback/upgrade runbook references these scripts.

- [ ] Run `shellcheck infra/modules/customer-instance/files/*.sh` (or the touched subset) + `bash -n` on each; if `terraform` CLI available, `terraform fmt -check` + `validate` the module (static; no apply). Document results.
- [ ] Full pytest suite (only touched Python helpers, if any) green.
- [ ] Commit `docs: role-split ops tooling (wave 2E)`

## Self-review notes

Deferred (say so): wiring the customer-instance Terraform module to actually deploy the m-tier role-split as a first-class option (this wave makes the HOST SCRIPTS ready; the module's compose-profile selection + Redis provisioning is a larger infra change tracked separately — the bod-3 load test uses the compose profile directly, not the module). The private consuming infra repos (agnes-infra-keboola etc.) need their own lockstep bumps — out of this public-repo scope, noted for the operator.
