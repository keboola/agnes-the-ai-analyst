output "instance_ips" {
  description = "Mapa { name => external IP }"
  value       = { for k, v in google_compute_address.ip : k => v.address }
}

output "prod_ip" {
  description = "External IP prod instance"
  value       = google_compute_address.ip[var.prod_instance.name].address
}

output "vm_service_account" {
  description = "Email VM SA (pro další IAM bindings, např. BigQuery)"
  value       = google_service_account.vm.email
}

output "jwt_secret_name" {
  description = "Plný název JWT secretu v Secret Manageru"
  value       = google_secret_manager_secret.jwt.name
}
