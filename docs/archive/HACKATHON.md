# Hackathon TL;DR — Deploy & Develop

Two condensed playbooks, written to be copy-pasteable by both humans and AI agents. For depth see [`ONBOARDING.md`](ONBOARDING.md) and [`DEPLOYMENT.md`](DEPLOYMENT.md).

- [Part 1 — Deploy for a new customer](#part-1--deploy-for-a-new-customer)
- [Part 2 — Develop against Agnes](#part-2--develop-against-agnes)
- [Part 3 — AI agent checklist](#part-3--ai-agent-checklist)

---

## Part 1 — Deploy for a new customer

**Goal:** Agnes running in `https://<customer-name>'s own GCP project`, accessible on an IP. Target time: **45 minutes**.

### Prerequisites (verify first)

```bash
gcloud --version        # ≥ 500.0.0
terraform --version     # ≥ 1.5
gh --version            # any recent
gh auth status          # must be logged in with repo + workflow + admin:repo_hook scopes
```

Plus:
- GCP project with **billing linked**; you have `roles/owner` or equivalent
- Keboola Storage token (if `data_source = "keboola"`)

### Step 1 — Bootstrap GCP project

```bash
# Download + run bootstrap (creates deploy SA, tfstate bucket, enables APIs)
curl -fsSL https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/scripts/bootstrap-gcp.sh -o bootstrap-gcp.sh
chmod +x bootstrap-gcp.sh
./bootstrap-gcp.sh <GCP_PROJECT_ID>
```

Outputs you need from the run:
- SA email `agnes-deploy@<project>.iam.gserviceaccount.com`
- Bucket `gs://agnes-<project>-tfstate`
- Key file `./agnes-deploy-<project>-key.json` — move to `~/.agnes-keys/` and `chmod 600`. **Never git-commit.**

### Step 2 — Keboola token secret

Skip if `data_source` is not keboola.

```bash
echo -n "<KEBOOLA_STORAGE_TOKEN>" | gcloud secrets create keboola-storage-token \
    --data-file=- --replication-policy=automatic --project=<GCP_PROJECT_ID>
```

### Step 3 — Create customer's private infra repo from template

```bash
# <customer-org> can be your own org (if Keboola is deploying) or customer's org
gh repo create <customer-org>/agnes-infra-<customer-name> \
    --template keboola/agnes-infra-template \
    --private \
    --clone
cd agnes-infra-<customer-name>

# SA key to GitHub secret
gh secret set GCP_SA_KEY < ~/.agnes-keys/agnes-deploy-<project>-key.json
```

### Step 4 — GitHub environments + auto-merge

```bash
# dev environment — no protection
gh api -X PUT repos/<customer-org>/agnes-infra-<customer-name>/environments/dev

# prod environment — branch policy (main only), no reviewer here; add manually via UI
echo '{"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' \
  | gh api -X PUT repos/<customer-org>/agnes-infra-<customer-name>/environments/prod --input -

# Settings → Environments → prod → Add required reviewer

# Allow auto-merge on this repo (for Renovate)
gh api -X PATCH repos/<customer-org>/agnes-infra-<customer-name> \
    -f allow_auto_merge=true -f delete_branch_on_merge=true

# Install Renovate GitHub App on this repo:
#   https://github.com/apps/renovate → Configure → <customer-org>/agnes-infra-<customer-name>
```

### Step 5 — Edit `terraform/main.tf` and `terraform.tfvars`

```hcl
# terraform/main.tf — replace both placeholders
backend "gcs" {
  bucket = "agnes-<GCP_PROJECT_ID>-tfstate"
  prefix = "<customer-name>"
}
```

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit, minimum required:
#   gcp_project_id    = "<GCP_PROJECT_ID>"
#   customer_name     = "<customer-name>"      # lowercase, 2–21 chars
#   seed_admin_email  = "admin@<customer>.com"
#   keboola_stack_url = "https://connection.<region>.gcp.keboola.com/"
```

### Step 6 — First apply (from local, before CI is set up)

```bash
cd terraform
export GOOGLE_APPLICATION_CREDENTIALS=~/.agnes-keys/agnes-deploy-<project>-key.json
terraform init
terraform plan    # verify counts: ~20 resources to add
terraform apply   # type 'yes'

# Expected: ~5 min. Outputs include prod_ip.
terraform output -raw prod_ip
```

Alternative: commit tfvars + push to main — GitHub Actions `apply-dev` auto-runs, `apply-prod` waits for reviewer.

### Step 7 — Bootstrap the admin + log in

```bash
PROD_IP=$(terraform output -raw prod_ip)
curl -X POST "http://$PROD_IP:8000/auth/bootstrap" \
    -H "Content-Type: application/json" \
    -d '{"email":"<seed_admin_email from tfvars>","password":"<STRONG_PASSWORD>"}'
```

Expected: 200 OK + JSON with `role: "admin"`.

Open `http://<prod_ip>:8000/login` → sign in. Done.

### Verify (smoke tests)

```bash
curl -s "http://$PROD_IP:8000/api/health"  | jq '{status, version, channel}'
curl -s "http://$PROD_IP:8000/api/version" | jq
```

Both should return JSON. Badge in UI footer shows `channel-version · deployed Xs ago`.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PERMISSION_DENIED: monitoring.uptimeCheckConfigs.create` | monitoring.googleapis.com not enabled / role missing | Re-run `bootstrap-gcp.sh` (grants `roles/monitoring.editor`) |
| `At least one of the pre-conditions you specified did not hold` | Stale GCS state lock | Remove `gs://…/tfstate/<prefix>/default.tflock` + retry |
| `Error acquiring the state lock` | Active apply concurrent | Wait, or `terraform force-unlock <LOCK_ID>` |
| Docker pull fails `ghcr.io/…:<tag>: not found` | `image_tag` pins a branch/tag that was never built | Fix `image_tag` in tfvars (use `stable` or `dev` floating) |
| VM up but `/api/health` 000 / connection refused | Startup script still running (takes 60–90 s) | Tail `/var/log/agnes-startup.log` via `gcloud compute ssh` |
| `/auth/bootstrap` returns 403 `already have passwords` | Someone already bootstrapped. | Use normal login at `/login/password` |

---

## Part 2 — Develop against Agnes

**Loop:** branch in public repo → auto-built `:dev-<slug>` image → point a dev VM at that tag → iterate.

### Development architecture (mental model)

```
keboola/agnes-the-ai-analyst (public)
│
│  push branch zs/my-edit
├──▶ release.yml builds ghcr.io/…:dev-zs-my-edit (one image per push to branch)
│
│  push to main
└──▶ release.yml builds :stable + :stable-YYYY.MM.N

<customer-infra-repo>/terraform.tfvars
│
│  dev_instances = [..., { name = "agnes-zs", image_tag = "dev-zs-my-edit" }, ...]
└──▶ apply-dev recreates VM agnes-zs, pinned to that tag, cron pulls new digests every 5 min
```

### Step 1 — Branch and push (in `keboola/agnes-the-ai-analyst`)

```bash
git checkout -b zs/my-edit       # or feature/xyz, fix/bar — any slash prefix works
# …edit code…
git commit -am "wip: my experiment"
git push origin zs/my-edit
# → release.yml builds ghcr.io/keboola/agnes-the-ai-analyst:dev-zs-my-edit (~5 min)
```

**Slug rule:** `<branch>` is slugified — non-`[a-z0-9-]` → `-`, lowercased, max 50 chars. Leading `feature/` is stripped. So:
- `zs/my-edit` → `:dev-zs-my-edit`
- `feature/alice/dashboard` → `:dev-alice-dashboard`
- `fix/issue_42` → `:dev-fix-issue-42`

Verify the image exists before continuing:
```bash
docker manifest inspect ghcr.io/keboola/agnes-the-ai-analyst:dev-zs-my-edit
```

### Step 2 — Open PR in the customer's infra repo

```bash
cd <customer-infra-repo>
git checkout -b add-dev-vm-zs
# Edit terraform/variables.tf or terraform.tfvars:
#   dev_instances = [
#     { name = "agnes-dev",     image_tag = "dev" },
#     { name = "agnes-zs-edit", image_tag = "dev-zs-my-edit" },  # <-- added
#   ]
git commit -am "add: dev VM pinned to zs/my-edit"
git push origin add-dev-vm-zs
gh pr create
```

`plan.yml` comments on PR with diff. Review + merge → `apply-dev` creates VM (~2 min).

### Step 3 — Access your VM

```bash
cd terraform
terraform output -json instance_ips  # grep your VM's name

# Open http://<ip>:8000/login
# Use the customer's admin credentials (seed + password)
```

### Step 4 — Iterate

Every push to your branch:
1. release.yml rebuilds `:dev-zs-my-edit`
2. Cron on your VM (every 5 min) detects new digest, pulls, restarts containers
3. Within ~6 min of your push, your VM runs the new code

No manual apply needed.

### Step 5 — Merge to main

Open PR on public repo → review → merge. This:
1. Builds `:stable + :stable-YYYY.MM.N` (main)
2. Smoke test in CI
3. Cron on **all prod VMs** (every customer!) pulls new `:stable` within 5 min

Your branch's `:dev-zs-my-edit` tag persists in GHCR but is no longer updated. Your dev VM still runs the last build of your branch until you change its `image_tag`.

### Step 6 — Clean up

PR in customer infra repo removing the entry:

```diff
 dev_instances = [
   { name = "agnes-dev",     image_tag = "dev" },
-  { name = "agnes-zs-edit", image_tag = "dev-zs-my-edit" },
 ]
```

Merge → apply-dev destroys the VM + data disk + IP + monitoring resources. Daily snapshot (if enabled) retains data for 30 days.

### Common development tasks

| Task | Where | How |
|---|---|---|
| Write code | public repo | Normal git workflow |
| Run tests locally | public repo | `TESTING=1 pytest tests/ -v` |
| Bump infra module | public repo | Edit `infra/modules/customer-instance/`, PR, merge, create `infra-vX.Y.Z` tag |
| Point customer at new module | customer infra repo | Renovate opens PR; or edit `ref=` in main.tf manually |
| Force-propagate startup script change | customer infra repo | Actions → Terraform Apply → Run workflow → `recreate_targets=module.agnes.google_compute_instance.vm["agnes-prod"]` |
| Add dev VM for someone else | customer infra repo | Add entry to `dev_instances`, PR, merge |
| Rotate Keboola token | customer GCP + VM | `gcloud secrets versions add keboola-storage-token --data-file=-` then SSH + `sudo /usr/local/bin/agnes-auto-upgrade.sh` (no manual edits to `.env`) |
| Restart app manually | customer VM | `sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml restart app` |
| See current deployed version | any | Footer badge on UI, or `curl /api/version` |

### Rules

- **Never `gcloud compute ssh` + edit `/opt/agnes/.env`** — gets wiped on next VM recreate. Route all config through Terraform or Secret Manager.
- **Never edit inside a VM's `/opt/agnes/`** — same reason. If you need a change to survive, it belongs in the module / compose files / tfvars.
- **Never bump `infra-v*` by tagging upstream without first testing on a dev VM** — a broken module propagates to all customer repos via Renovate.
- **Never delete a customer's data disk without a manual `gcloud compute disks snapshot` first** if in doubt about what's on it.

---

## Part 3 — AI agent checklist

These are guardrails/verification steps an AI agent should follow autonomously.

### Before taking destructive action

Run and read, don't assume:
```bash
terraform plan                 # what will actually change?
gh run list --limit 3          # any CI failures?
curl -s <PROD_IP>:8000/api/health | jq .status  # is prod actually healthy?
```

### When propagating module bumps

1. Read `docs/superpowers/plans/2026-04-21-deployment-log.md` for context on iteration history.
2. Check current `ref=` in customer infra repo against latest `infra-v*` tag in upstream.
3. Prefer Renovate PR over manual edit — has automatic `terraform validate` gate.
4. For startup-script changes (not just module-resource changes), use `workflow_dispatch` → `recreate_targets` to force VM recreate. Normal apply won't propagate (`ignore_changes`).

### When a customer reports "it's broken"

```bash
# What version is deployed?
curl -s http://<ip>:8000/api/version | jq

# Recent deploys?
gh run list --repo <customer-org>/agnes-infra-<customer> --limit 5

# VM state?
gcloud compute instances list --project=<customer-project> --filter="name~agnes-"

# App logs (last 50)
gcloud compute ssh agnes-prod --zone=... --project=... \
    --command="sudo docker logs agnes-app-1 --tail 50"

# Startup script log (if VM just booted)
gcloud compute ssh agnes-prod --zone=... --project=... \
    --command="sudo tail -30 /var/log/agnes-startup.log"
```

### When you're unsure

Prefer non-destructive paths first:
1. `terraform plan` (read-only) before `apply`
2. Add a new resource before deleting an old one
3. Snapshot before destroying a disk
4. Dev VM before touching prod — always

### Common pitfalls to detect

| Pitfall | Check |
|---|---|
| Uncommitted local changes on operator's laptop | `git status -s` in infra repo |
| Multiple concurrent applies (state lock) | `gsutil ls gs://.../tflock` |
| `image_tag` points at non-existent GHCR image | `docker manifest inspect ghcr.io/…:<tag>` |
| Seed user without password on fresh deploy | `curl /api/health | jq .services.users.count` — if 1 and nobody has logged in, `/auth/bootstrap` is still open |
| Main branch protection prevents direct push | Use PR + auto-merge; never force-push to main |
| Renovate not installed → module bumps don't happen | Check `https://github.com/<org>/<repo>/pulls?q=author%3Aapp%2Frenovate` |
| `/opt/agnes/.env` edited manually → drift | `git diff` against module's expected `.env` shape |

### Safe-to-run anytime

- `curl /api/health`, `curl /api/version` — no auth, no side effects
- `terraform plan` — read-only
- `gh run list`, `gh pr list` — read-only
- `gcloud ... describe` / `list` — read-only
- `docker logs` / `docker inspect` — read-only (on the VM)

### Requires thought

- `terraform apply` — mutates infra
- `gh workflow run` with `recreate_targets` — destroys + recreates VMs
- `gcloud compute instances delete` — unrecoverable after 30 days
- `gcloud secrets versions destroy` — unrecoverable
- `gh repo delete` — unrecoverable

---

## Reference links

- Full onboarding: [`docs/ONBOARDING.md`](ONBOARDING.md)
- Deployment comparison: [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)
- Spec (architecture rationale): [`docs/superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`](superpowers/specs/2026-04-21-multi-customer-deployment-spec.md)
- Deployment log (what we actually built, with iterations and known limitations): [`docs/superpowers/plans/2026-04-21-deployment-log.md`](superpowers/plans/2026-04-21-deployment-log.md)
- Module source: [`infra/modules/customer-instance/`](../infra/modules/customer-instance/)
- Upstream issues: https://github.com/keboola/agnes-the-ai-analyst/issues
