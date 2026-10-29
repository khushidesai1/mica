# Standalone, additive stack. It deliberately does not manage either Cloud Run service.
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}
provider "google" { project = var.project_id }

variable "project_id" { type = string }
variable "service_name" { type = string }
variable "service_url" {
  type = string
  validation {
    condition     = can(regex("^https://[a-z0-9-]+\\.a\\.run\\.app$", var.service_url))
    error_message = "Use the existing Cloud Run service URL without a trailing slash."
  }
}
variable "push_service_account" {
  type        = string
  description = "Existing control service account already allowed by internal OIDC and Pub/Sub token creation."
}

resource "google_pubsub_topic" "failures" {
  name = "${var.service_name}-failures"
}
resource "google_logging_project_sink" "failures" {
  name                   = "${var.service_name}-failures"
  destination            = "pubsub.googleapis.com/${google_pubsub_topic.failures.id}"
  unique_writer_identity = true
  filter                 = <<-FILTER
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    (
      (logName="projects/${var.project_id}/logs/run.googleapis.com%2Frequests"
       httpRequest.status>=500 httpRequest.status<600
       NOT httpRequest.requestUrl=~"/api/v1/internal/maintenance/(log-failure|reconcile-batch|compact-idle)/?([?].*)?$")
      OR
      (logName="projects/${var.project_id}/logs/run.googleapis.com%2Fvarlog%2Fsystem" severity>=ERROR)
    )
  FILTER
}
resource "google_pubsub_topic_iam_member" "log_writer" {
  topic  = google_pubsub_topic.failures.name
  role   = "roles/pubsub.publisher"
  member = google_logging_project_sink.failures.writer_identity
}
resource "google_pubsub_subscription" "failures" {
  name                       = "${var.service_name}-failures-push"
  topic                      = google_pubsub_topic.failures.name
  ack_deadline_seconds       = 30
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  push_config {
    push_endpoint = "${var.service_url}/api/v1/internal/maintenance/log-failure"
    oidc_token {
      service_account_email = var.push_service_account
      audience              = var.service_url
    }
  }
}
