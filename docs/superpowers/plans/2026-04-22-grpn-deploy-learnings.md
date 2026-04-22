# GRPN deploy learnings — hackathon 2026-04-22

Running log of constraints encountered while deploying Agnes to GRPN's `prj-grp-foundryai-dev-7c37` on an existing VM (`foundryai-development`). Recorded during deploy; each entry captures the constraint, workaround, and what it implies for our Terraform flow.

## Constraints hit

### 1. No `projectIamAdmin` on human identity

- **Signal:** `bootstrap-gcp.sh` failed on `gcloud projects add-iam-policy-binding` with `[e_zsrotyr@groupon.com] does not have permission ... setIamPolicy`.
- **Root cause:** `roles/editor` intentionally excludes `resourcemanager.projects.setIamPolicy`.
- **Workaround (hackathon):** Skip `bootstrap-gcp.sh`. Deploy on existing VM with docker-compose; use VM's existing SA without adding any new IAM bindings.
- **Implication for TF flow:** For a proper per-customer deploy, the GRPN admin must grant `roles/resourcemanager.projectIamAdmin` to either the onboarding engineer or directly to `agnes-deploy` SA. Or onboarding becomes two-phase: engineer creates SA + bucket; admin grants roles.

### 2. Organization policy `iam.disableServiceAccountKeyCreation`

- **Signal:** `gcloud iam service-accounts keys create` returned `Key creation is not allowed on this service account`.
- **Root cause:** Org-level `constraints/iam.disableServiceAccountKeyCreation` applies to all projects in the organization. Intentional security posture — static SA JSON keys are the highest-risk credential type.
- **Workaround:** Can't produce a `GCP_SA_KEY` GitHub secret for CI/CD. Options:
  - **WIF (Workload Identity Federation)**: GitHub Actions OIDC → GCP, no static keys. Requires bootstrap updates (create WIF pool + provider + binding on deploy SA).
  - **Skip CI/CD for GRPN**: Run `terraform apply` only from developer laptops with user ADC (`gcloud auth application-default login`). Works for hackathon, does not scale.
- **Implication for TF flow:** Our current bootstrap + `apply.yml` assume SA JSON key. GRPN (and any org with this org policy) requires WIF path. Track as follow-up; for hackathon we skip CI entirely.

### 3. Resource-level `setIamPolicy` also blocked

- **Signal:** `gcloud secrets add-iam-policy-binding` returned `Permission 'secretmanager.secrets.setIamPolicy' denied`.
- **Root cause:** `editor` does not grant `setIamPolicy` on any resource, even secret-level. Stricter than standard GCP default; likely additional org policies.
- **Workaround:** Don't use Secret Manager for hackathon secrets. Store JWT + any tokens directly in `.env` on the VM with `chmod 600`.
- **Implication:** Our module's secret-based `.env` assembly from Secret Manager needs a fallback path when `setIamPolicy` is blocked. For now: document that customers who can't grant IAM must bake secrets into `.env` manually (still via `scp`, not git).

### 4. VM has no external IP (IAP tunnel only)

- **Signal:** `gcloud compute ssh` auto-falls-back to IAP tunnel; direct IP access from browser impossible.
- **Root cause:** GRPN VMs are created in a private VPC. Standard security posture. Our module default (`access_config { nat_ip = ... }`) is the opposite — external IP by default.
- **Workaround:** Browser access via IAP tunnel: `gcloud compute start-iap-tunnel foundryai-development 8000 --local-host-port=localhost:8000`. Then `http://localhost:8000`.
- **Implication:** Our module needs an `external_ip` variable (default `true`) that customers can disable. Plus docs for IAP tunnel access pattern.

### 5. VM's SA scopes include `cloud-platform` (default overkill)

- **Signal:** `grpn-sa-foundryai-execution@...` has `cloud-platform` scope — full GCP access.
- **Root cause:** GRPN's default compute SA configuration.
- **Workaround:** Use VM's existing SA; it already has enough (BigQuery datasets, Compute, etc.). No need to create a dedicated `agnes-vm` SA (and we couldn't anyway — would need `projectIamAdmin`).
- **Implication:** For hackathon OK. For production the SA is overprovisioned — different customer than us, our opinion doesn't apply.

### 6. Docker not pre-installed

- **Signal:** `docker: command not found` on fresh VM.
- **Root cause:** VM is generic Ubuntu, no opinions about Docker.
- **Workaround:** `curl -fsSL https://get.docker.com | sudo sh` + `sudo apt install docker-compose-plugin`. Took ~30 s.
- **Implication:** Any non-TF-managed VM will need this. Our module's startup script already does this; manual deploys need it inline or a small bootstrap script.

### 7. `/data` did not exist

- **Signal:** `df /data` → No such file or directory.
- **Root cause:** Fresh VM, no persistent disk attached for data.
- **Workaround:** `mkdir -p /data/{state,analytics,extracts}` on boot disk. Ephemeral — data lives with VM. Acceptable for hackathon.
- **Implication:** For production this would mean no data survives VM recreate. Module's persistent-disk + `host-mount` overlay is the right long-term answer. For hackathon, boot disk is fine.

## Derived follow-ups (post-hackathon)

- [ ] **Add WIF path to `bootstrap-gcp.sh`** — alternative to SA JSON key. Detect `iam.disableServiceAccountKeyCreation` constraint and switch automatically.
- [ ] **Make `external_ip` + `iap_only` optional in customer-instance module** — GRPN-style customers need VMs without NAT.
- [ ] **Document two-phase bootstrap flow** — engineer creates SA, admin grants roles. Or admin runs the script on behalf.
- [ ] **Fallback `.env` assembly** — when Secret Manager is blocked, allow operator to `scp` secrets.
- [ ] **Customer onboarding checklist addition** — verify required project IAM before onboarding starts:
  - `resourcemanager.projects.setIamPolicy` (for adding binding to SA)
  - `iam.serviceAccountKeys.create` — check org policy `iam.disableServiceAccountKeyCreation` → if true, mandate WIF
  - `compute.firewalls.create` (for firewall rules)
  - `compute.disks.create`, `compute.instances.create` (for VM)
  - `secretmanager.*` (for secrets)
  - `storage.buckets.create` (for tfstate bucket, if hosted in customer project)

## Hackathon deploy summary (live)

- VM: `foundryai-development` in `prj-grp-foundryai-dev-7c37`, zone `us-central1-a`, e2-medium, 30GB boot, IAP-only access
- Data source: `csv` (no external data ingest needed for hackathon)
- App directory: `/opt/agnes/`, docker-compose fetched from upstream `main`
- Data directory: `/data` on boot disk (ephemeral)
- Secrets: plain `.env` with chmod 600 (org policy blocks Secret Manager IAM bindings)
- Access: IAP tunnel on port 8000
