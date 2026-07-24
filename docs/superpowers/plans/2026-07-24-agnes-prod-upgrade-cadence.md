# Auto-Upgrade Cadence Override + Maintenance-Page Delivery Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the never-worked maintenance page (Caddy serves it during an
auto-upgrade recreate, but the file never lands on any VM) and make the
auto-upgrade cron cadence overridable per instance, so agnes-prod can move
off the current `*/5 * * * *` to a quiet nightly window without touching
dev/monika's fast iteration cadence.

**Architecture:** Two independent, additive fixes in the `customer-instance`
Terraform module and its baked ops scripts — no new services, no schema
changes, no runtime app code touched. (1) `static/maintenance.html` gets
added to both delivery paths (`Dockerfile` bake list for fresh boots,
`agnes-auto-upgrade.sh`'s `CONFIG_FILES` for already-running VMs). (2) A new
`upgrade_schedule` optional attribute on `prod_instance` / `dev_instances`
(same shape as the existing `machine_type` / `app_mem_limit` overrides)
flows through `main.tf`'s `templatefile()` call into
`startup-script.sh.tpl`'s cron-install line, replacing the hardcoded
`*/5 * * * *`.

**Tech Stack:** Terraform (`hashicorp/google` ~> 5.0), bash (customer-instance
startup script + ops scripts), pytest for static contract tests (no live VM
or cloud credentials needed for any test in this plan).

## Global Constraints

- Default behavior MUST be unchanged for every instance that doesn't set
  `upgrade_schedule` — default value is the current `"*/5 * * * *"`.
- Vendor-agnostic wording in all docs/comments (no customer names, no cloud
  project ids) — per root `CLAUDE.md`.
- CHANGELOG bullet under `## [Unreleased]` ships in this branch (this repo's
  non-negotiable release process rule).
- Full suite (`.venv/bin/pytest tests/ --tb=short -n auto -q`) green before
  the final commit.
- No customer-specific values (e.g. the `"30 3 * * *"` schedule for the
  actual Keboola prod VM) land in this repo — that's a private-infra-repo
  follow-up, out of scope here (see spec's Non-goals).

---

### Task 1: Maintenance-page delivery fix

**Files:**
- Modify: `Dockerfile:54-81`
- Modify: `scripts/ops/agnes-auto-upgrade.sh:167-192`
- Test: `tests/test_maintenance_page_delivery.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `/opt/agnes/static/maintenance.html` present on every VM (fresh
  boot via Dockerfile bake, existing VM via `CONFIG_FILES` sync) — consumed
  by Caddy's already-existing `handle_errors 502 503` block (`Caddyfile`,
  unmodified by this plan).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_maintenance_page_delivery.py
"""Static contract for maintenance.html delivery to running VMs.

Caddy's `handle_errors 502 503` block (Caddyfile) serves
`/caddy-static/maintenance.html` during an app-container recreate — but
`static/maintenance.html` was never shipped to `/opt/agnes/static/` on any
real VM: not baked into the Dockerfile's `/opt/agnes-host/` artifact set
(so a fresh boot never gets it), and not in `agnes-auto-upgrade.sh`'s
`CONFIG_FILES` (so an already-running VM never picks it up either). Users
saw a raw connection error instead of the friendly page during every
redeploy. Pins both delivery paths so this can't regress silently again.
"""

import re
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
AUTO_UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")


def test_dockerfile_bakes_maintenance_html_into_agnes_host():
    body = DOCKERFILE.read_text()
    assert "mkdir -p /opt/agnes-host/static" in body, (
        "Dockerfile must create /opt/agnes-host/static/ so the recursive "
        "docker cp on VM boot preserves the static/ subdirectory Caddy expects"
    )
    assert "cp /app/static/maintenance.html /opt/agnes-host/static/" in body, (
        "Dockerfile must COPY static/maintenance.html into "
        "/opt/agnes-host/static/ — otherwise a fresh VM boot never gets "
        "the maintenance page"
    )


def test_auto_upgrade_config_files_includes_maintenance_html():
    body = AUTO_UPGRADE.read_text()
    m = re.search(r"CONFIG_FILES=\((.*?)\)", body, re.DOTALL)
    assert m, "agnes-auto-upgrade.sh must declare CONFIG_FILES"
    assert "static/maintenance.html" in m.group(1), (
        "CONFIG_FILES must include static/maintenance.html so a page-content "
        "edit propagates to already-running VMs (same rationale as Caddyfile)"
    )


def test_auto_upgrade_creates_parent_dir_before_fetch():
    body = AUTO_UPGRADE.read_text()
    # The fetch loop must mkdir -p the destination's parent before curl -o,
    # since curl does not create intermediate directories and
    # static/maintenance.html introduces the first nested CONFIG_FILES path.
    m = re.search(r'for f in "\$\{CONFIG_FILES\[@\]\}"; do\n(.*?)\ndone', body, re.DOTALL)
    assert m, "could not find the CONFIG_FILES fetch loop"
    loop_body = m.group(1)
    assert 'mkdir -p "/opt/agnes/$(dirname "$f")"' in loop_body, (
        "fetch loop must mkdir -p the parent dir before curl -o, or a nested "
        "CONFIG_FILES path (e.g. static/maintenance.html) fails on a VM "
        "where that subdirectory doesn't already exist"
    )
    assert loop_body.index("mkdir -p") < loop_body.index("curl -fsSL"), (
        "mkdir -p must run BEFORE the curl call in the loop"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_maintenance_page_delivery.py -v`
Expected: FAIL — all three tests fail (`assert ... in body` false for the
Dockerfile/CONFIG_FILES assertions; the mkdir-related test fails on the
`assert m` / `mkdir -p` lookups).

- [ ] **Step 3: Fix the Dockerfile bake list**

In `Dockerfile`, the existing bake block (around line 54) is:

```dockerfile
RUN mkdir -p /opt/agnes-host && \
    cp /app/scripts/ops/agnes-auto-upgrade.sh \
       /app/scripts/ops/agnes-tls-rotate.sh \
       /app/scripts/ops/agnes-state-applier.sh \
       /app/scripts/ops/agnes-state-applier.service \
       /app/scripts/ops/agnes-state-applier.timer \
       /app/scripts/ops/agnes-state-applier-bootstrap.service \
       /app/scripts/tls-fetch.sh \
       /opt/agnes-host/ && \
    cp /app/docker-compose.yml /app/docker-compose.prod.yml \
       /app/docker-compose.host-mount.yml /app/docker-compose.tls.yml \
       /app/docker-compose.postgres.yml \
       /app/docker-compose.postgres-host-mount.yml \
       /app/Caddyfile /opt/agnes-host/ && \
    chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \
              /opt/agnes-host/agnes-tls-rotate.sh \
              /opt/agnes-host/agnes-state-applier.sh \
              /opt/agnes-host/tls-fetch.sh && \
    chmod 0644 /opt/agnes-host/agnes-state-applier.service \
              /opt/agnes-host/agnes-state-applier.timer \
              /opt/agnes-host/agnes-state-applier-bootstrap.service \
              /opt/agnes-host/docker-compose.yml \
              /opt/agnes-host/docker-compose.prod.yml \
              /opt/agnes-host/docker-compose.host-mount.yml \
              /opt/agnes-host/docker-compose.tls.yml \
              /opt/agnes-host/docker-compose.postgres.yml \
              /opt/agnes-host/docker-compose.postgres-host-mount.yml \
              /opt/agnes-host/Caddyfile
```

Replace it with (adds the `static/` subdirectory + the maintenance page,
keeping every existing line unchanged):

```dockerfile
RUN mkdir -p /opt/agnes-host /opt/agnes-host/static && \
    cp /app/scripts/ops/agnes-auto-upgrade.sh \
       /app/scripts/ops/agnes-tls-rotate.sh \
       /app/scripts/ops/agnes-state-applier.sh \
       /app/scripts/ops/agnes-state-applier.service \
       /app/scripts/ops/agnes-state-applier.timer \
       /app/scripts/ops/agnes-state-applier-bootstrap.service \
       /app/scripts/tls-fetch.sh \
       /opt/agnes-host/ && \
    cp /app/docker-compose.yml /app/docker-compose.prod.yml \
       /app/docker-compose.host-mount.yml /app/docker-compose.tls.yml \
       /app/docker-compose.postgres.yml \
       /app/docker-compose.postgres-host-mount.yml \
       /app/Caddyfile /opt/agnes-host/ && \
    cp /app/static/maintenance.html /opt/agnes-host/static/ && \
    chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \
              /opt/agnes-host/agnes-tls-rotate.sh \
              /opt/agnes-host/agnes-state-applier.sh \
              /opt/agnes-host/tls-fetch.sh && \
    chmod 0644 /opt/agnes-host/agnes-state-applier.service \
              /opt/agnes-host/agnes-state-applier.timer \
              /opt/agnes-host/agnes-state-applier-bootstrap.service \
              /opt/agnes-host/docker-compose.yml \
              /opt/agnes-host/docker-compose.prod.yml \
              /opt/agnes-host/docker-compose.host-mount.yml \
              /opt/agnes-host/docker-compose.tls.yml \
              /opt/agnes-host/docker-compose.postgres.yml \
              /opt/agnes-host/docker-compose.postgres-host-mount.yml \
              /opt/agnes-host/Caddyfile \
              /opt/agnes-host/static/maintenance.html
```

Also update the doc comment above it (currently lines 40-47, the "Includes:"
list) to add a line: `#   - static/maintenance.html — Caddy's handle_errors
502/503 fallback page`.

- [ ] **Step 4: Fix `agnes-auto-upgrade.sh`'s CONFIG_FILES + fetch loop**

In `scripts/ops/agnes-auto-upgrade.sh`, the existing block (around line 167)
is:

```bash
CONFIG_FILES=(
  docker-compose.yml docker-compose.prod.yml docker-compose.host-mount.yml
  docker-compose.postgres.yml docker-compose.postgres-host-mount.yml
  docker-compose.tls.yml Caddyfile
)
hash_config_files() {
  # Sort to keep hash stable across operator add/remove, missing files
  # contribute the empty string (sha256 of "" is well-defined). Run
  # from /opt/agnes to keep relative paths terse in the hash input.
  # docker-compose.gcp-logging.yml is hashed here too (even though it is NOT
  # in CONFIG_FILES, which are fetched unconditionally) so an overlay-only
  # refresh triggers a recreate and actually lands on running containers.
  # Absent on non-GCE hosts it contributes a stable "missing" line, so it
  # never causes spurious drift there.
  ( cd /opt/agnes && for f in "${CONFIG_FILES[@]}" docker-compose.gcp-logging.yml; do
      sha256sum "$f" 2>/dev/null || printf 'missing %s\n' "$f"
    done ) | sort | sha256sum | awk '{print $1}'
}
for f in "${CONFIG_FILES[@]}"; do
  if curl -fsSL "$RAW_BASE/$f" -o "/opt/agnes/$f.new" 2>/dev/null; then
    mv -f "/opt/agnes/$f.new" "/opt/agnes/$f"
  else
    rm -f "/opt/agnes/$f.new"
    logger -t agnes-auto-upgrade "WARN: failed to fetch $f from $RAW_BASE — keeping existing /opt/agnes/$f"
  fi
done
```

Replace with (adds `static/maintenance.html` to the array, and a `mkdir -p`
before the `curl -o` so a nested path always has its parent directory —
`dirname` of a flat filename like `Caddyfile` resolves to `.`, so this is a
no-op for every existing entry):

```bash
CONFIG_FILES=(
  docker-compose.yml docker-compose.prod.yml docker-compose.host-mount.yml
  docker-compose.postgres.yml docker-compose.postgres-host-mount.yml
  docker-compose.tls.yml Caddyfile static/maintenance.html
)
hash_config_files() {
  # Sort to keep hash stable across operator add/remove, missing files
  # contribute the empty string (sha256 of "" is well-defined). Run
  # from /opt/agnes to keep relative paths terse in the hash input.
  # docker-compose.gcp-logging.yml is hashed here too (even though it is NOT
  # in CONFIG_FILES, which are fetched unconditionally) so an overlay-only
  # refresh triggers a recreate and actually lands on running containers.
  # Absent on non-GCE hosts it contributes a stable "missing" line, so it
  # never causes spurious drift there.
  ( cd /opt/agnes && for f in "${CONFIG_FILES[@]}" docker-compose.gcp-logging.yml; do
      sha256sum "$f" 2>/dev/null || printf 'missing %s\n' "$f"
    done ) | sort | sha256sum | awk '{print $1}'
}
for f in "${CONFIG_FILES[@]}"; do
  mkdir -p "/opt/agnes/$(dirname "$f")"
  if curl -fsSL "$RAW_BASE/$f" -o "/opt/agnes/$f.new" 2>/dev/null; then
    mv -f "/opt/agnes/$f.new" "/opt/agnes/$f"
  else
    rm -f "/opt/agnes/$f.new"
    logger -t agnes-auto-upgrade "WARN: failed to fetch $f from $RAW_BASE — keeping existing /opt/agnes/$f"
  fi
done
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_maintenance_page_delivery.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add Dockerfile scripts/ops/agnes-auto-upgrade.sh tests/test_maintenance_page_delivery.py
git commit -m "fix(infra): deliver maintenance.html to VMs on boot and via auto-upgrade sync"
```

---

### Task 2: Per-instance `upgrade_schedule` override

**Files:**
- Modify: `infra/modules/customer-instance/variables.tf`
- Modify: `infra/modules/customer-instance/main.tf:367-376`
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl:8-20,690-700`
- Test: `tests/test_upgrade_schedule_toggle.py` (new)

**Interfaces:**
- Consumes: nothing from Task 1 (fully independent change).
- Produces: `var.prod_instance.upgrade_schedule` / `var.dev_instances[*].upgrade_schedule`
  (Terraform string, default `"*/5 * * * *"`) — a private infra repo can set
  this per instance without any further code change here.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_upgrade_schedule_toggle.py
"""Static contract for the per-instance auto-upgrade cadence override.

Pins the three-part infra contract (same pattern as
test_startup_studio_toggle.py) so a rename or dropped template argument
can't silently break the override:

* variables.tf declares upgrade_schedule on BOTH prod_instance and
  dev_instances, defaulting to the historical "*/5 * * * *" so no caller
  is affected unless they explicitly override;
* main.tf forwards each.value.upgrade_schedule into templatefile(...);
* startup-script.sh.tpl's cron install line is built from the templated
  value, not a hardcoded "*/5 * * * *" string.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_variables_tf_declares_upgrade_schedule_on_both_instance_types():
    body = (MODULE / "variables.tf").read_text()
    pattern = re.compile(r'upgrade_schedule\s*=\s*optional\(string,\s*"\*/5 \* \* \* \*"\)')
    occurrences = pattern.findall(body)
    assert len(occurrences) == 2, (
        f"expected upgrade_schedule = optional(string, \"*/5 * * * *\") on "
        f"BOTH prod_instance and dev_instances object types, found "
        f"{len(occurrences)} occurrence(s)"
    )


def test_main_tf_forwards_upgrade_schedule():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"upgrade_schedule\s*=\s*each\.value\.upgrade_schedule", body), (
        "main.tf must forward each.value.upgrade_schedule into the "
        "startup-script templatefile() call"
    )


def test_tpl_cron_line_uses_templated_schedule_not_hardcoded():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert 'UPGRADE_SCHEDULE="${upgrade_schedule}"' in body, (
        "startup-script.sh.tpl must capture the templated upgrade_schedule "
        "value, matching the existing UPGRADE_MODE pattern"
    )
    assert 'CRON_LINE="$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh' in body, (
        "the installed crontab line must be built from $UPGRADE_SCHEDULE, "
        "not a literal cadence string"
    )
    assert '"*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh' not in body, (
        "no hardcoded */5 cron line may remain once the variable is wired in"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_upgrade_schedule_toggle.py -v`
Expected: FAIL (all 3 fail — variable not declared, not forwarded, cron line
still hardcoded)

- [ ] **Step 3: Add the variable to `variables.tf`**

In the `prod_instance` variable's `type = object({ ... })` block, the
existing lines are:

```hcl
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    tls_mode     = optional(string, "caddy")
```

Add `upgrade_schedule` right after `upgrade_mode`:

```hcl
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    # Standard 5-field cron expression consumed by startup-script.sh.tpl's
    # crontab install line. Default matches the historical fixed cadence —
    # override to reduce upgrade-triggered blips on a customer-facing
    # instance (e.g. a nightly window) while dev/monika stay on fast
    # iteration.
    upgrade_schedule = optional(string, "*/5 * * * *")
    tls_mode          = optional(string, "caddy")
```

And in the `dev_instances` variable's `type = list(object({ ... }))` block,
the existing lines are:

```hcl
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    app_cpus            = optional(string, "2.0")
    scheduler_cpus      = optional(string, "1.0")
    dispatcher_enabled  = optional(bool, false)
  }))
```

Add `upgrade_schedule` before the closing `}))`:

```hcl
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    app_cpus            = optional(string, "2.0")
    scheduler_cpus      = optional(string, "1.0")
    dispatcher_enabled  = optional(bool, false)
    # See prod_instance for the rationale; same default.
    upgrade_schedule    = optional(string, "*/5 * * * *")
  }))
```

- [ ] **Step 4: Forward the value in `main.tf`**

The `templatefile()` call (around line 367) currently has:

```hcl
  metadata_startup_script = templatefile("${path.module}/startup-script.sh.tpl", {
    customer_name                   = var.customer_name
    image_repo                      = var.image_repo
    image_tag                       = each.value.image_tag
    app_mem_limit                   = each.value.app_mem_limit
    scheduler_mem_limit             = each.value.scheduler_mem_limit
    app_cpus                        = each.value.app_cpus
    scheduler_cpus                  = each.value.scheduler_cpus
    upgrade_mode                    = each.value.upgrade_mode
    tls_mode                        = each.value.tls_mode
```

Add `upgrade_schedule` right after `upgrade_mode`:

```hcl
  metadata_startup_script = templatefile("${path.module}/startup-script.sh.tpl", {
    customer_name                   = var.customer_name
    image_repo                      = var.image_repo
    image_tag                       = each.value.image_tag
    app_mem_limit                   = each.value.app_mem_limit
    scheduler_mem_limit             = each.value.scheduler_mem_limit
    app_cpus                        = each.value.app_cpus
    scheduler_cpus                  = each.value.scheduler_cpus
    upgrade_mode                    = each.value.upgrade_mode
    upgrade_schedule                = each.value.upgrade_schedule
    tls_mode                        = each.value.tls_mode
```

- [ ] **Step 5: Consume the value in `startup-script.sh.tpl`**

Near the top of the file, the existing variable captures (around line 8) are:

```bash
CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
TLS_MODE="${tls_mode}"
```

Add `UPGRADE_SCHEDULE` right after `UPGRADE_MODE`:

```bash
CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
UPGRADE_SCHEDULE="${upgrade_schedule}"
TLS_MODE="${tls_mode}"
```

Then in the auto-upgrade cron install section (around line 690), the
existing code is:

```bash
# --- 6. Auto-upgrade via cron (pulls new image digest every 5 min) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    # agnes-auto-upgrade.sh was already extracted to /usr/local/bin/ in
    # section 3 alongside the compose files — the host artifacts ship
    # together from the pinned image. Nothing more to fetch here.
    :

    # Install cron entry idempotently: remove any prior agnes-auto-upgrade line, then append ours.
    CRON_LINE="*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1"
    (crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "$CRON_LINE") | crontab -
fi
```

Replace with:

```bash
# --- 6. Auto-upgrade via cron (pulls new image digest on $UPGRADE_SCHEDULE) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    # agnes-auto-upgrade.sh was already extracted to /usr/local/bin/ in
    # section 3 alongside the compose files — the host artifacts ship
    # together from the pinned image. Nothing more to fetch here.
    :

    # Install cron entry idempotently: remove any prior agnes-auto-upgrade line, then append ours.
    CRON_LINE="$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1"
    (crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "$CRON_LINE") | crontab -
fi
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_upgrade_schedule_toggle.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: `terraform fmt` + `terraform validate` the module**

```bash
terraform fmt infra/modules/customer-instance/
cd infra/examples/minimal && terraform init -backend=false -input=false && terraform validate && cd -
```

Expected: `fmt` reports the files it reformatted (column-aligns the new
`=` signs — the pytest regexes use `\s*` so this can't break Step 6);
`validate` prints `Success! The configuration is valid.`

- [ ] **Step 8: Re-run Task 2's tests after `terraform fmt`**

Run: `.venv/bin/pytest tests/test_upgrade_schedule_toggle.py -v`
Expected: PASS (3 passed) — confirms the regex-based assertions tolerate
`fmt`'s column alignment.

- [ ] **Step 9: Commit**

```bash
git add infra/modules/customer-instance/variables.tf \
        infra/modules/customer-instance/main.tf \
        infra/modules/customer-instance/startup-script.sh.tpl \
        tests/test_upgrade_schedule_toggle.py
git commit -m "feat(infra): make auto-upgrade cron cadence overridable per instance"
```

---

### Task 3: CHANGELOG + full suite + wrap-up

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1 and 2 complete.
- Produces: nothing further downstream — this is the final task.

- [ ] **Step 1: Add CHANGELOG entries**

In `CHANGELOG.md`, under `## [Unreleased]`, the current empty subsections are:

```markdown
## [Unreleased]

### Added

### Changed

### Fixed

### Removed

### Internal

### Security
```

Fill in `### Added` and `### Fixed`:

```markdown
## [Unreleased]

### Added

- **Per-instance `upgrade_schedule` override** on `prod_instance` /
  `dev_instances` in the `customer-instance` Terraform module — the
  auto-upgrade cron cadence (default `*/5 * * * *`) can now be set per VM,
  e.g. to move a customer-facing instance to a quiet nightly window without
  affecting dev iteration speed.

### Changed

### Fixed

- **Maintenance page now actually reaches running VMs.** `static/maintenance.html`
  was never baked into the Dockerfile's `/opt/agnes-host/` artifact set nor
  synced by `agnes-auto-upgrade.sh`'s `CONFIG_FILES`, so Caddy's
  `handle_errors 502 503` fallback had nothing to serve during an
  auto-upgrade recreate — users saw a raw connection error instead of the
  friendly auto-refreshing page. Both delivery paths now ship the file.

### Removed

### Internal

### Security
```

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: all tests pass (or only pre-existing unrelated failures — if any
appear, confirm with `git stash` on a clean branch per this repo's release
process before treating them as pre-existing).

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): auto-upgrade cadence override + maintenance-page fix"
```

---

## Follow-up (explicitly out of scope, not a task in this plan)

In the private `agnes-infra-keboola` repo, set
`prod_instance.upgrade_schedule = "30 3 * * *"` and apply. Because
`startup-script.sh.tpl` changes don't reach a running VM
(`lifecycle { ignore_changes = [metadata_startup_script] }`), landing the
new cadence on the live agnes-prod VM needs either a `-replace` recreate of
that one instance via the `apply.yml` workflow, or an interim manual
`crontab` edit — an operational decision for whoever runs that apply, not
part of this repo's change.
