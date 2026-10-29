output "service_url" {
  value = try(google_cloud_run_v2_service.api[0].uri, null)
}
output "artifact_bucket" { value = google_storage_bucket.artifacts.name }
output "artifact_repository" { value = google_artifact_registry_repository.images.name }
output "secret_names" { value = { for name, secret in google_secret_manager_secret.runtime : name => secret.secret_id } }
output "service_accounts" { value = { for name, identity in google_service_account.runtime : name => identity.email } }
output "stage" { value = var.stage }
