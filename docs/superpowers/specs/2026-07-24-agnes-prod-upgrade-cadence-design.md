# Auto-upgrade cadence override + maintenance-page delivery fix

## Context

A `:stable` image redeploy on 2026-07-22 (13:11-13:17 UTC) caused a
user-visible ~5-10 minute rough patch on agnes-prod: the `agnes-app-1`
container recreate produced brief 502s from Caddy, and — coincidentally —
a manual Jira historical backfill (`--parallel 6`) pushed the 2-vCPU box's
load average to ~8.5 at the same time. Investigating the redeploy surfaced
two independent, pre-existing defects rather than one:

1. **The maintenance page has never actually worked.** Caddy's
   `handle_errors 502 503` block (`Caddyfile:110-114`) is supposed to serve
   `/caddy-static/maintenance.html` during exactly this kind of blip, but
   `static/maintenance.html` was never included in the file list that
   `scripts/ops/agnes-auto-upgrade.sh` syncs down to `/opt/agnes/` — so
   `/opt/agnes/static/` is empty on every live VM, and users see a raw
   connection error instead of the friendly auto-refreshing page.
2. **The auto-upgrade cadence (`*/5 * * * *`, hardcoded) is a plain cron
   entry**, not a Terraform variable, so every recreate-driven blip happens
   up to 12x/hour, at any time of day, on every instance including prod.

## Non-goals

- **No zero-downtime deploy.** True zero-downtime requires adopting the
  in-flight three-plane / mtier role-split topology (API replicas + Redis
  coordination for cross-replica WebSocket notifications + DuckLake catalog
  for safe multi-process analytics access — see
  `docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md`
  and [[three-plane-scalability-project]], PR #905). That's a substantial,
  separately-planned migration of a live production instance and is
  explicitly out of scope here. This spec only makes the existing,
  unavoidable upgrade blip both rarer and visibly graceful.
- **No VM scale-up.** Bumping agnes-prod's `machine_type` is a one-line
  Terraform value change in a private customer infra repo (outside this repo's scope), tracked
  separately, not part of this spec.
- **No change to the Jira `incremental_transform` path-mismatch bug** found
  during the same investigation — that's a separate, unrelated defect
  (`service.py`'s `trigger_incremental_transform` not passing
  `raw_dir`/`output_dir` overrides to `transform_single_issue`), tracked as
  its own follow-up.

## Design

### 1. Fix maintenance-page delivery

Add `static/maintenance.html` to the `CONFIG_FILES` array in
`scripts/ops/agnes-auto-upgrade.sh` (currently `docker-compose*.yml` +
`Caddyfile` only). The script already fetches every entry in that array
from `raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main` on every
tick (including the first tick during initial VM provisioning, which calls
this same script) and atomically installs it to `/opt/agnes/<path>` — so
this is a one-line addition, no new fetch/install machinery needed. Content
drift on this file also naturally feeds the existing `CONFIG_DRIFT` hash
used to decide whether a recreate is warranted, which is fine: a
maintenance-page wording change is not a reason to recreate containers, but
piggy-backing on the existing hash is harmless (worst case: one extra
no-op-safe `docker compose up -d` the day someone edits the page).

No changes to `Caddyfile` — `handle_errors 502 503` already does the right
thing once the file exists on disk.

### 2. Per-instance configurable upgrade schedule

Add a new optional Terraform attribute, following the exact pattern
established by `machine_type` (`variables.tf:41,82`) and the `mem_limit`/
`app_cpus` PR chain (OSS #485/#486/#897):

- `variables.tf`: `prod_instance.upgrade_schedule = optional(string, "*/5 * * * *")`
  and the identical attribute on `dev_instances[*]`. Default preserves
  today's behavior for every instance that doesn't set an override.
- `main.tf`: thread `each.value.upgrade_schedule` into the `templatefile(...)`
  call that renders `startup-script.sh.tpl`, alongside the existing
  `machine_type`/mem-limit variables.
- `startup-script.sh.tpl:529-538`: the `CRON_LINE` that installs the
  crontab entry uses `${upgrade_schedule}` instead of the hardcoded
  `*/5 * * * *`.

This is deploy-time only — changing the Terraform value and re-applying
(which already resizes VMs in place via `allow_stopping_for_update = true`
for `machine_type` changes; a cron-line change needs no VM
stop/recreate at all, just a re-run of the startup-script's cron-install
step, or a manual `crontab` edit until the next VM recreate picks it up
from the rendered template — see Rollout below) is how an operator opts an
instance into a different cadence.

### Rollout (out of this repo's scope, noted for the private infra repo)

In the private infra repo that consumes this module, set `prod_instance.upgrade_schedule = "30 3 * * *"`
(03:30 UTC daily — right after the existing `agnes-db-backup.timer`,
03:17 UTC, so a fresh backup exists before any upgrade-triggered recreate).
Other (dev/staging) instances keep the default 5-minute cadence for fast
iteration. Since `startup-script.sh.tpl` has
`lifecycle { ignore_changes = [metadata_startup_script] }`, a plain `terraform apply` after this Terraform change
will NOT reach a running VM — it needs either a `-replace` recreate of that
one instance, or a manual `crontab` edit on agnes-prod as an interim step.
That operational choice is the infra repo's call, not this spec's.

## Testing

- Unit/contract coverage for `CONFIG_FILES` — if an existing test asserts
  the array's contents (e.g. a "every compose file we depend on is synced"
  regression test), extend it to cover the new entry; otherwise add one.
- Terraform: `terraform validate` / `terraform plan` on the customer-instance
  module confirming the new variable renders into the startup-script output
  for both a default instance and one with `upgrade_schedule` overridden.
- Manual live verification after rollout (both are already-established
  operator checks, not new tooling):
  - `crontab -l` on the target VM shows the overridden line.
  - `ls /opt/agnes/static/` (or `docker exec agnes-caddy-1 ls /caddy-static`)
    shows `maintenance.html` present after one auto-upgrade tick.
