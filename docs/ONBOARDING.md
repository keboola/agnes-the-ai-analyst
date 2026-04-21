# Onboarding a new Agnes instance

End-to-end guide for deploying Agnes into a new GCP project. Target time: **under 1 hour**.

The target reader is a Keboola ops engineer or a customer with GCP Owner access.

## Overview

Every Agnes instance lives in **one GCP project per customer**, driven by a **private infra repo** cloned from [keboola/agnes-infra-template](https://github.com/keboola/agnes-infra-template). The upstream app + TF module is in [keboola/agnes-the-ai-analyst](https://github.com/keboola/agnes-the-ai-analyst); customers do not fork it.

## Prerequisites

- GCP project with billing linked (you / customer owns it)
- `gcloud` CLI authenticated as project Owner
- `terraform` ≥ 1.5
- `gh` CLI authenticated
- (optional) `docker` for local smoke tests

## 1. Bootstrap GCP

```bash
curl -fsSL https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/scripts/bootstrap-gcp.sh -o bootstrap-gcp.sh
chmod +x bootstrap-gcp.sh
./bootstrap-gcp.sh <GCP_PROJECT_ID>
```

Outputs:
- `agnes-deploy@<project>.iam.gserviceaccount.com` (Terraform SA with scoped roles)
- `gs://agnes-<project>-tfstate` (versioned, uniform bucket-level access)
- `./agnes-deploy-<project>-key.json` (SA JSON key — store in `~/.agnes-keys/` or password manager, **not git**)

Idempotent — safe to re-run.

## 2. Customer's data source secrets

If `data_source = "keboola"`:

```bash
echo -n "<KEBOOLA_STORAGE_TOKEN>" | gcloud secrets create keboola-storage-token \
    --data-file=- --replication-policy=automatic --project=<GCP_PROJECT_ID>
```

## 3. Create private infra repo from template

```bash
gh repo create <customer-org>/agnes-infra-<customer> \
    --template keboola/agnes-infra-template \
    --private
gh repo clone <customer-org>/agnes-infra-<customer>
cd agnes-infra-<customer>
```

Upload the SA key to GitHub secrets:

```bash
gh secret set GCP_SA_KEY < ~/.agnes-keys/agnes-deploy-<project>-key.json
```

Create GitHub environments `dev` (no protection) and `prod` (required reviewer, wait timer 5 min, branch `main` only):

```bash
gh api -X PUT repos/<customer-org>/agnes-infra-<customer>/environments/dev
echo '{"wait_timer":300,"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' \
  | gh api -X PUT repos/<customer-org>/agnes-infra-<customer>/environments/prod --input -
```

Add reviewers via GitHub UI (Settings → Environments → prod).

## 4. Configure tfvars and backend

Edit `terraform/main.tf`:

```hcl
backend "gcs" {
  bucket = "agnes-<GCP_PROJECT_ID>-tfstate"
  prefix = "<customer>"
}
```

Copy the example and fill it in:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit:
#   gcp_project_id    = "<GCP_PROJECT_ID>"
#   customer_name     = "<customer>"
#   seed_admin_email  = "...@customer.com"
#   (optionally) keboola_stack_url, prod_instance, dev_instances
```

## 5. First apply

```bash
cd terraform
export GOOGLE_APPLICATION_CREDENTIALS=~/.agnes-keys/agnes-deploy-<project>-key.json
terraform init
terraform plan
terraform apply
```

Or push `terraform.tfvars` committed path and let GitHub Actions do it:

```bash
git add . && git commit -m "initial: <customer> deployment" && git push origin main
# CI runs apply-dev, waits for prod reviewer, then apply-prod
```

Output: `prod_ip` = external IP.

## 6. Bootstrap admin user

On the first deploy the `users` table is empty. Create the first admin via `POST /auth/bootstrap` (this endpoint auto-disables once ≥1 user exists):

```bash
PROD_IP=$(terraform output -raw prod_ip)
curl -X POST "http://$PROD_IP:8000/auth/bootstrap" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@<customer>.com","name":"Admin","password":"<STRONG_PASSWORD>"}'
```

Log in: `http://<prod_ip>:8000/login`.

## 7. DNS + TLS (optional)

For HTTPS, set in `terraform.tfvars`:

```hcl
prod_instance = {
  ...
  tls_mode = "caddy"
  domain   = "agnes.<customer>.com"
}
```

Then create a DNS A-record pointing `agnes.<customer>.com` → `prod_ip`. Caddy will auto-issue Let's Encrypt cert.

## 8. Smoke test

```bash
PROD_IP=$(cd terraform && terraform output -raw prod_ip)

# Health
curl "http://$PROD_IP:8000/api/health" | jq '.status'  # "healthy" or "degraded"

# First sync (populates data from Keboola / other source)
curl -X POST "http://$PROD_IP:8000/api/sync/trigger" \
     -H "Authorization: Bearer $ADMIN_JWT"
```

## 9. Monitoring + backup (recommended)

- **Cloud Monitoring alert** on `/api/health` `status != "healthy"` for > 5 min
- **Daily snapshot of `/data` PD**: `gcloud compute resource-policies create snapshot-schedule ...`
- **Slack webhook** from Cloud Monitoring for alerts

(These are follow-ups — not required for first deploy.)

## Ongoing maintenance

- **App auto-upgrades** (cron every 5 min) to latest `:stable` if `upgrade_mode = "auto"`. Else Renovate will open PR on new `stable-YYYY.MM.N`.
- **Infra module upgrade:** change `ref=infra-vX.Y.Z` in `terraform/main.tf`, PR → plan → merge → apply.
- **Add dev VM for a branch:** add entry to `dev_instances` list with `image_tag = "dev-feature-xyz"`, PR, merge, apply.
- **Token rotation:** `gcloud secrets versions add keboola-storage-token --data-file=-` then `sudo docker compose restart app` on each VM.

## Decommission

```bash
cd terraform
terraform destroy
```

Then delete:
- GCS bucket `gs://agnes-<project>-tfstate` (or keep for audit)
- Service account `agnes-deploy@...`
- Secret Manager secrets (`keboola-storage-token`, `agnes-<customer>-jwt-secret`)
- GitHub private repo `<customer-org>/agnes-infra-<customer>`

## Troubleshooting

See [keboola/agnes-the-ai-analyst](https://github.com/keboola/agnes-the-ai-analyst) issues and docs.
