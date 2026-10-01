output "api_url" { value = google_cloud_run_v2_service.api.uri }
output "artifact_bucket" { value = google_storage_bucket.artifacts.name }
output "quarantine_bucket" { value = google_storage_bucket.artifacts.name }
output "artifact_repository" { value = google_artifact_registry_repository.images.name }
output "gmail_topic" { value = google_pubsub_topic.gmail.id }
