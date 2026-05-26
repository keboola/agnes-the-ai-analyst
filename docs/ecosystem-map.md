# Agnes ecosystem map

What an Agnes deployment actually depends on — beyond this OSS repo.

An Agnes deployment is rarely just "this repo deployed somewhere". Around the
core app there are 4 satellite repo roles that an operator typically owns,
each playing a different part. This doc maps all 5 tiers so a new operator can
see what they need to set up (and a contributor can see why certain features
exist).

> This doc is the bird's-eye view. Each tier links into the existing
> reference doc for the deep dive.

## The 5 tiers

```
┌───────────────────────────────────────────────────────────────────────┐
│ TIER 1 — Agnes OSS (this repo)                                        │
│ App + connectors + CLI + reusable TF module                           │
│ → keboola/agnes-the-ai-analyst (public)                               │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
        consumed by ↓ (Docker image + TF module + raw bootstrap script)
                               │
┌──────────────────────────────┴────────────────────────────────────────┐
│ TIER 2 — per-customer infra repo (private, you own)                   │
│ Provisions GCP project + VMs + secrets + TLS + CI/CD                  │
│ → see docs/ONBOARDING.md                                              │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
            deploys ↓        registers ↓               links ↓
                               │
       ┌───────────────────────┼─────────────────────────┐
       ▼                       ▼                         ▼
┌─────────────┐         ┌────────────────┐         ┌─────────────────┐
│ TIER 3      │         │ TIER 4         │         │ TIER 5          │
│ Curated     │         │ Initial        │         │ Legacy / data   │
│ marketplace │         │ workspace      │         │ sources / glue  │
│ repo(s)     │         │ template repo  │         │ repos           │
│ (optional)  │         │ (optional)     │         │ (optional)      │
└─────────────┘         └────────────────┘         └─────────────────┘
```

## Tier 1 — Agnes OSS

**What it provides**

| Surface | Where it lives in the repo | Consumer |
|---------|----------------------------|----------|
| App container image | published as `ghcr.io/<org>/agnes-the-ai-analyst:<tag>` | Tier 2 VM startup |
| Reusable TF module | `infra/modules/customer-instance/` (tagged `infra-vX.Y.Z`) | Tier 2 TF root |
| Bootstrap script | `scripts/bootstrap-gcp.sh` | Tier 2 one-time setup |
| CLI binary | `cli/` → `agnes` on analyst laptops | analysts |
| Marketplace contract | `docs/marketplace.md` + `docs/curated-marketplace-format.md` | Tier 3 |
| Initial workspace contract | `docs/initial-workspace-override.md` | Tier 4 |
| Data source connectors | `connectors/<name>/extractor.py` (`extract.duckdb` contract) | the app itself |

**Release cadence:** two parallel trains.

- **App release:** `stable-YYYY.MM.N` Docker tag → `:stable` floating tag. Auto-pulled by VMs in `upgrade_mode = "auto"`.
- **Infra release:** `infra-vX.Y.Z` git tag on `infra/modules/customer-instance/`. Tier 2 repos pin this tag; Renovate bumps minor/patch.

The two are independent — `infra-v1.7.0` does not imply `stable-2026.05.964`.

## Tier 2 — per-customer infra repo

**What it owns:** one GCP project, the VMs, persistent disks, Secret Manager
entries, TLS material, Cloudflare or DNS records, and the CI/CD that applies
all of the above.

**Two patterns observed in real deployments.** Pick whichever fits.

### Pattern A — Template fork + pinned module

For greenfield customers and the path the OSS docs assume.

```
keboola/agnes-infra-template (public, GitHub template)
                │
                │ "Use this template" button
                ▼
   <your-org>/agnes-infra-<customer>  (private)
   ├── terraform/main.tf
   │     module "agnes" {
   │       source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.9.0"
   │     }
   ├── terraform/terraform.tfvars   (customer-specific values)
   ├── .github/workflows/{plan,apply,validate}.yml
   └── renovate.json   (auto-bumps minor/patch on infra-v* tags)
```

- **CI/CD:** PR → `terraform plan` (commented on PR); push to `main` → `apply-dev` (auto) → `apply-prod` (GitHub Environment with reviewer gate, 5-min wait).
- **Module upgrades:** Renovate watches `infra-v*` tags, opens auto-merging PRs for minor/patch, labels major as `breaking` for manual review.
- **VM recreation after startup-script changes:** module sets `lifecycle { ignore_changes = [metadata_startup_script] }` so apply doesn't bounce VMs every run. After bumping the module ref, use `apply.yml`'s `workflow_dispatch` input `recreate_targets` to pass `-replace=` for the affected VMs (data disks + static IPs survive).

**Setup walkthrough:** [`docs/ONBOARDING.md`](ONBOARDING.md).

### Pattern B — Self-contained TF

For customers with strong internal infra opinions (custom VPC, internal IAM
roles, in-house secret stores, corporate enterprise GitHub instead of
github.com, dev VMs per engineer instead of a single shared dev).

```
<your-org>/<customer>-agnes-infra   (private, often on enterprise GH)
├── main.tf, locals.tf, iam.tf, secrets.tf, tls.tf, …  (flat root)
├── modules/<your-vm-module>/       (LOCAL module, not the OSS one)
├── startup.sh                       (rendered via templatefile())
├── .github/workflows/{plan,apply,validate}.yml
└── .claude/skills/<your-vm-manager>/  (optional: bundled skill that edits TF)
```

Key differences vs Pattern A:

- **No dependency on the OSS `customer-instance` TF module.** The local
  `modules/agnes-vm/` is the canonical per-VM pattern. Renovate-based
  module-version bumps don't apply.
- **OSS scripts pulled by raw URL.** `startup.sh` typically curls
  `https://raw.githubusercontent.com/<oss-repo>/main/scripts/...` for
  things like Caddy reload or TLS rotation. **No pin** — when OSS renames
  a script in `main`, VM startup breaks. Trade-off: simpler than vendoring,
  riskier than pinning. Worth tracking as a known limitation.
- **`apply.yml` is `workflow_dispatch`-only.** No push-trigger → apply.
  Every apply is a deliberate human action with the plan visible upfront.
- **Per-dev VM matrix.** Instead of `prod` + one `dev`, the `local.vms` map
  holds prod + shared dev + N per-developer VMs (`<vm>-dev-<handle>`).

**When to pick Pattern B over Pattern A:**

- Mandatory shared VPC / specific subnet you can't shoehorn into the module's
  variables.
- IAM roles that come from an org-custom role registry, not stock GCP roles.
- Enterprise GitHub (GHES) host that can't pull tagged modules from
  github.com without proxying.
- You want each engineer to get a personal VM tied to a branch — the OSS
  module supports `dev_instances[]` but the surrounding workflows (per-dev
  image tags, per-dev hostnames, per-dev seed admins) often need infra-side
  glue.

**Cost of Pattern B:** you opt out of OSS module upgrades and Renovate
discipline. When the OSS app needs a new TF resource (a new secret, an
extra firewall rule), you have to mirror the change manually.

## Tier 3 — Curated marketplace repo(s)

**Optional.** Without one, analysts see only the bundled Agnes skills/agents.

**Contract:** any git repo with a `.claude-plugin/marketplace.json` at the
root. Register it via the admin UI at `/admin/marketplaces`. Agnes clones it
nightly into `${DATA_DIR}/marketplaces/<slug>/`, parses the manifest, and
re-serves a single aggregated marketplace through `/marketplace.zip` (ZIP
download) and `/marketplace.git/*` (git smart-HTTP), both PAT-gated and
RBAC-filtered per caller.

**Three repo flavors in the wild:**

| Flavor | Purpose | Layout |
|--------|---------|--------|
| **Template** | "Use this template" scaffolding for department curators | `.claude-plugin/marketplace.json` + `.claude-plugin/marketplace-metadata.json` + `plugins/<placeholder>/` + `scripts/init-marketplace.mjs` (rename helper) |
| **Instance** | Real per-department / per-product marketplace | Same shape, with real `plugins/<name>/` directories |
| **Flea-market sink** | Destination repo for community-contributed skills submitted via Agnes's flea market | Minimal scaffold: marketplace.json + one `plugins/flea-market/` with empty `skills/` |

**Optional UI enrichment:** put a `.claude-plugin/marketplace-metadata.json`
alongside `marketplace.json` to add cover photos, taglines, descriptions,
sample interactions, doc links, and per-skill / per-agent metadata. The
authoritative schema is in [`docs/curated-marketplace-format.md`](curated-marketplace-format.md).

**Common authoring mistakes:**

- Putting metadata in `.agnes/agnes-metadata.json` instead of
  `.claude-plugin/marketplace-metadata.json` — **the parser will not read
  it.** `.agnes/` is reserved for cover photo assets and per-request
  diagnostics (`version.json` in the served ZIP), not metadata.
- Forgetting to register the repo in `/admin/marketplaces` — pushing to
  the repo alone does nothing; Agnes only knows about repos in its registry.
- Putting the PAT in the repo URL — store PATs out-of-band via the
  admin UI; they persist to `${DATA_DIR}/state/.env_overlay`, not git.

**Internals reference:** [`docs/marketplace.md`](marketplace.md).

## Tier 4 — Initial workspace template repo

**Optional.** Without one, `agnes init` builds the analyst workspace from
Agnes's bundled defaults (server-rendered `CLAUDE.md` from
`/api/welcome`, client-hardcoded `.claude/settings.json` from
`cli/lib/hooks.py`, two default slash commands).

**Why an operator opts in:** corporate `CLAUDE.md` with team-specific
golden paths, custom hooks beyond what Agnes ships, restricted permission
sets, pre-populated corporate documentation under `docs/`.

**Contract:** any git repo with a `workspace/` subdirectory at the root.
**Only `workspace/`** is shipped to analysts — the repo's own README,
LICENSE, CI configs, and admin maintenance scripts stay in the repo and
never reach an analyst.

```
your-template-repo/
├── README.md, LICENSE, .github/   ← admin-only, never shipped
└── workspace/                     ← this is the analyst payload
    ├── CLAUDE.md                  ← your golden path content
    ├── AGNES_WORKSPACE.md
    └── .claude/
        ├── settings.json          ← hooks, permissions, model, statusLine
        ├── CLAUDE.local.md
        └── commands/
            ├── agnes-private.md
            └── update-agnes-plugins.md
```

**Register via** the admin UI at `/admin/server-config` → "Initial Workspace
Template" → URL + branch + (optional) PAT. Sync is manual ("Sync now"
button); the repo is cloned into `${DATA_DIR}/initial-workspace/`.

**What `agnes init` does instead of defaults when override is active**

| Default | Override |
|---------|----------|
| `CLAUDE.md` fetched from `/api/welcome` (Jinja2-rendered) | `workspace/CLAUDE.md` shipped verbatim |
| `.claude/settings.json` seeded with `{model: sonnet, permissions, hooks}` | Whatever your repo ships |
| Default `/agnes-private` + `/update-agnes-plugins` commands installed | Whatever your repo's `.claude/commands/` has |

**What still happens regardless** (data-plane concerns, not skeleton):
PAT verification, `agnes pull` of parquets + corporate-memory rules,
`.claude/init-complete` sentinel.

**Common authoring mistakes:**

- Putting the analyst payload at the repo root instead of under `workspace/`
  — sync fails with a typed error. The convention is mandatory so admin-only
  files (README, CI) don't accidentally reach analysts.
- Shipping `.claude/init-complete` inside `workspace/` — reserved path,
  sync fails. Agnes writes this sentinel itself.
- Hand-rolling `settings.json` with only `agnes pull` on SessionStart —
  loses `agnes self-upgrade`, `agnes capture-session`,
  `agnes refresh-marketplace --check`, and the detached `nohup agnes push`
  on SessionEnd that Agnes's own default ships. Mirror the full default from
  `cli/lib/hooks.py` and deviate intentionally.

**Full reference:** [`docs/initial-workspace-override.md`](initial-workspace-override.md).

## Tier 5 — Legacy and glue repos

The rest is customer-specific connective tissue. Two recurring patterns:

### Predecessor data platform running in parallel

Customers migrating from an older internal "data broker" (CSV → parquet over
SSH/rsync, per-analyst SSH keys, Linux group-based RBAC) typically keep it
running alongside Agnes while migrating consumers. The blocker is rarely
infrastructure — it's the **business-semantic layer**: hand-maintained
metric definitions (CARR, PAYG, custom segments), curated finance datasets,
and team-specific golden paths that haven't yet been translated into Agnes
constructs (`metric_definitions` DuckDB table + `docs/metrics/*.yaml`).

Until that layer migrates, the legacy platform keeps shipping. Plan a
deliberate sunset; don't expect organic migration.

### Data-source content repos

Marketplace-style "source of truth" repos that feed an Initial Workspace
template via committed snapshots:

```
data-canonical-repo                 →  initial-workspace-repo/data/
(team-curated semantics,                (pinned snapshot with .canonical-pin
 metric definitions, glossary,           = commit SHA; sync script
 cross-package join rules)               regenerates on demand)
```

This two-repo split keeps the canonical source under one team's ownership
while the workspace template stays under operator ownership; the snapshot is
a controlled review surface between them.

## Cross-tier checklist for a new customer deployment

For a greenfield customer following Pattern A, in order:

1. **Tier 1** — pick an OSS `infra-vX.Y.Z` tag and a `stable-*` image tag.
2. **Tier 2** — fork `agnes-infra-template`, fill `terraform.tfvars`, run
   `bootstrap-gcp.sh`, push to `main`, let CI apply.
3. **Bootstrap admin** — `POST /auth/bootstrap` with seed email + password
   (single-shot, self-disables after first password is set).
4. **Tier 3** — optionally register one or more curated marketplace repos via
   `/admin/marketplaces`. Skip if the bundled Agnes skills are enough.
5. **Tier 4** — optionally register an initial-workspace template repo via
   `/admin/server-config`. Skip if the bundled `CLAUDE.md` + default hooks
   are fine.
6. **Tier 5** — if migrating from a predecessor, plan the cutover for the
   business-semantic layer; expect this to be the slow part.

For Pattern B, replace step 2 with "stand up your own TF root using the
existing repo as the canonical reference" — there is no template, only
prior art.

## Related docs

- [`ONBOARDING.md`](ONBOARDING.md) — Tier 2, Pattern A walkthrough
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Terraform vs Docker Compose
- [`PLATFORM_SETUP.md`](PLATFORM_SETUP.md) — consolidated operator playbook
- [`marketplace.md`](marketplace.md) — Tier 3 internals
- [`curated-marketplace-format.md`](curated-marketplace-format.md) — Tier 3 authoring
- [`initial-workspace-override.md`](initial-workspace-override.md) — Tier 4 contract
- [`architecture.md`](architecture.md) — internal architecture (orchestrator, extractors)
