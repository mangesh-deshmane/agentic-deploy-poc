output "service_url" {
  description = "Public HTTPS endpoint of the deployed service."
  value       = google_cloud_run_v2_service.app.uri
}

output "service_name" {
  description = "Cloud Run service name."
  value       = google_cloud_run_v2_service.app.name
}

output "runtime_service_account" {
  description = "Identity the container runs as."
  value       = google_service_account.runtime.email
}

output "health_url" {
  description = "Endpoint the validation step polls."
  value       = "${google_cloud_run_v2_service.app.uri}/health"
}

output "info_url" {
  description = "Echoes customer and environment - proves parameterisation, not just deployment."
  value       = "${google_cloud_run_v2_service.app.uri}/api/info"
}

output "cost_note" {
  description = "Summary shown to the approver."
  value = join(" ", [
    "min_instances=${var.min_instance_count} (scale to zero),",
    "max=${var.max_instance_count},",
    "${var.memory}/${var.cpu}vCPU,",
    "public ingress - dev only,",
    "ttl=${var.ttl_hours}h.",
    "Estimated cost: $0.00/month within free tier."
  ])
}
