output "instance_ips" {
  description = "Map of { name => external IP }."
  value       = { for k, v in google_compute_address.ip : k => v.address }
}

output "prod_ip" {
  description = "External IP of the production instance."
  value       = google_compute_address.ip[var.prod_instance.name].address
}

output "vm_service_account" {
  description = "VM service-account email (for additional IAM bindings, e.g. BigQuery)."
  value       = google_service_account.vm.email
}

output "jwt_secret_name" {
  description = "Full name of the JWT secret in Secret Manager."
  value       = google_secret_manager_secret.jwt.name
}

output "backup_policy_id" {
  description = "ID daily backup resource policy attached to data disks"
  value       = google_compute_resource_policy.daily_backup.id
}

output "uptime_check_ids" {
  description = "Map of instance name → uptime check ID (empty when enable_monitoring = false)"
  value       = { for k, v in google_monitoring_uptime_check_config.health : k => v.uptime_check_id }
}
