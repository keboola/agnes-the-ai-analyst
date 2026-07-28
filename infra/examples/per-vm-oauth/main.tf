# Per-VM OAuth client example: prod + dev VMs, each with its own Google
# Sign-In OAuth client (separate redirect URIs, separate blast radius).
#
# Preconditions:
#
#   1. Four Secret Manager secrets pre-created (Console can't manage OAuth
#      clients via public API, so the secret values themselves are uploaded
#      out-of-band):
#
#        gcloud secrets create agnes-google-oauth-client-id-prod     ...
#        gcloud secrets create agnes-google-oauth-client-secret-prod ...
#        gcloud secrets create agnes-google-oauth-client-id-dev      ...
#        gcloud secrets create agnes-google-oauth-client-secret-dev  ...
#
#   2. Two OAuth 2.0 Web Application client IDs created in Cloud Console,
#      each with its own Authorized redirect URI:
#
#        prod client -> https://<prod-domain>/auth/google/callback
#        dev client  -> https://<dev-domain>/auth/google/callback
#
#      The {id, secret} pairs are loaded into the four secrets above.
#
# The module grants the VM SA secretAccessor on each resolved name
# automatically — no need to wire them via `runtime_secrets`.
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = "europe-west1"
}

variable "gcp_project_id" {
  type = string
}

variable "admin_email" {
  type = string
}

variable "prod_domain" {
  description = "Public hostname for the prod VM (must resolve to its static IP before apply for Caddy + Let's Encrypt to succeed)."
  type        = string
}

variable "dev_domain" {
  description = "Public hostname for the dev VM."
  type        = string
}

module "agnes" {
  source = "../../modules/customer-instance"

  gcp_project_id   = var.gcp_project_id
  customer_name    = "example"
  seed_admin_email = var.admin_email

  prod_instance = {
    name      = "agnes-prod"
    image_tag = "stable"
    tls_mode  = "caddy"
    domain    = var.prod_domain
  }

  dev_instances = [
    {
      name      = "agnes-dev"
      image_tag = "stable"
      tls_mode  = "caddy"
      domain    = var.dev_domain
      # role defaults to "dev" — left implicit here. Override below for stage.
    },
    # Stage VM — same module, just a different role. Requires four extra
    # secrets in Secret Manager:
    #   agnes-google-oauth-client-{id,secret}-stage
    # and an OAuth Web Application client with redirect URI
    #   https://<stage-domain>/auth/google/callback
    # already loaded into them. No module-side code change needed.
    # {
    #   name      = "agnes-stage"
    #   image_tag = "stable"
    #   tls_mode  = "caddy"
    #   domain    = "agnes-stage.example.com"
    #   role      = "stage"
    # },
  ]

  # The module expands this per VM at plan time:
  #   agnes-prod  (role=prod)  -> agnes-google-oauth-client-{id,secret}-prod
  #   agnes-dev   (role=dev)   -> agnes-google-oauth-client-{id,secret}-dev
  #   agnes-stage (role=stage) -> agnes-google-oauth-client-{id,secret}-stage
  oauth_secret_name_template = "agnes-google-oauth-client-{kind}-{role}"

  # `runtime_secrets` is kept strictly for OTHER secrets; the OAuth ones are
  # bound automatically by the template-derived IAM resource.
  runtime_secrets = []
}

output "prod_ip" {
  value = module.agnes.prod_ip
}
