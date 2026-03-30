output "instance_ip" {
  description = "Public IP address of the server"
  value       = google_compute_address.data_analyst.address
}

output "ssh_command" {
  description = "SSH command to connect"
  value       = "ssh ${var.ssh_user}@${google_compute_address.data_analyst.address}"
}

output "api_url" {
  description = "API URL"
  value       = "http://${google_compute_address.data_analyst.address}:8000"
}

output "web_url" {
  description = "Web UI URL"
  value       = var.domain != "" ? "https://${var.domain}" : "http://${google_compute_address.data_analyst.address}:8000"
}

output "swagger_url" {
  description = "Swagger API docs URL"
  value       = "http://${google_compute_address.data_analyst.address}:8000/docs"
}

output "bootstrap_command" {
  description = "Command to bootstrap first admin user"
  value       = "curl -X POST http://${google_compute_address.data_analyst.address}:8000/auth/bootstrap -H 'Content-Type: application/json' -d '{\"email\":\"admin@keboola.com\",\"name\":\"Admin\"}'"
}

output "cli_setup_commands" {
  description = "Commands to set up local CLI"
  value       = <<-EOT
    da setup init --server http://${google_compute_address.data_analyst.address}:8000
    da setup bootstrap admin@keboola.com
    da setup test-connection
    da sync
  EOT
}
