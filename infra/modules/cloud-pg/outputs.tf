/* Outputs.
 *
 * The connection URL is intentionally NOT exported as a single
 * `database_url` value because that string would contain the
 * password — exposing it as a TF output makes it land in plan
 * artifacts, CI logs, and any downstream module that consumes it.
 * Operators construct the final URL by combining the public IP
 * here with the password they retrieve from Secret Manager.
 */

output "instance_name" {
  description = "Cloud SQL instance name (matches var.name)."
  value       = google_sql_database_instance.this.name
}

output "instance_connection_name" {
  description = "Fully-qualified instance connection name (`<project>:<region>:<instance>`). For Cloud SQL Auth Proxy and IAM auth setups."
  value       = google_sql_database_instance.this.connection_name
}

output "public_ip" {
  description = "Public IPv4 address of the instance. Pair with the password from Secret Manager + database_name to form the URL the app's /admin/database endpoint expects."
  value       = google_sql_database_instance.this.public_ip_address
}

output "database_name" {
  description = "Application database name (matches var.database_name)."
  value       = google_sql_database.agnes.name
}

output "app_user" {
  description = "Application Postgres user (matches var.app_user)."
  value       = google_sql_user.app.name
}

output "url_template" {
  description = "SQLAlchemy URL template with a literal `<PASSWORD>` placeholder. Operator substitutes the value from Secret Manager when triggering the cloud cutover via /admin/database."
  value       = "postgresql+psycopg://${google_sql_user.app.name}:<PASSWORD>@${google_sql_database_instance.this.public_ip_address}:5432/${google_sql_database.agnes.name}"
  sensitive   = false
}
