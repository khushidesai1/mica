locals {
  name                   = "pistachio-${var.environment}"
  slack_agent_configured = var.slack_agent_image != ""
  apis = toset([
    "artifactregistry.googleapis.com",
    "batch.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "gmail.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
  ])
}

data "google_project" "current" {}

resource "google_project_service" "enabled" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = false
}

resource "random_id" "suffix" { byte_length = 4 }

resource "google_firestore_database" "state" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.enabled]
}

resource "google_firestore_index" "tasks_by_project_created_at" {
  project     = var.project_id
  database    = google_firestore_database.state.name
  collection  = "tasks"
  query_scope = "COLLECTION"

  fields {
    field_path = "envelope.project_id"
    order      = "ASCENDING"
  }

  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${local.name}-artifacts-${random_id.suffix.hex}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    condition {
      age = 30
      # Project briefs, provenance and small frozen thread records persist. Only ephemeral
      # transport/checkpoints and existing web prefixes keep the 30-day deletion policy.
      matches_prefix = local.slack_agent_configured ? ["tasks/", "inputs/", "incoming/", "paused/"] : null
    }
    action { type = "Delete" }
  }
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = local.name
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled]
}

resource "google_service_account" "control" {
  account_id   = "${local.name}-control"
  display_name = "Mica control plane"
}

resource "google_service_account" "worker" {
  account_id   = "${local.name}-worker"
  display_name = "Mica Batch collector"
}

# Runtime identities receive object access only on the deployment-managed buckets. They cannot
# create buckets or write objects into arbitrary project storage locations.
resource "google_storage_bucket_iam_member" "control_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.control.email}"
  condition {
    title       = "managed_pistachio_objects_only"
    description = "Control plane writes are limited to managed task, input, and pause prefixes."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/tasks/') || resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/inputs/') || resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/incoming/') || resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/paused/')"
  }
}

resource "google_storage_bucket_iam_member" "worker_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
  condition {
    title       = "task_objects_only"
    description = "Batch workers may mutate only task-scoped transport and output objects."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/tasks/')"
  }
}

resource "google_storage_bucket_iam_member" "worker_inputs_reader" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker.email}"
  condition {
    title       = "managed_inputs_only"
    description = "Batch workers may read, but never modify, managed task inputs."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/inputs/')"
  }
}

resource "google_project_iam_member" "control_roles" {
  for_each = toset([
    "roles/batch.jobsEditor",
    "roles/cloudtasks.enqueuer",
    "roles/cloudbuild.builds.editor",
    "roles/datastore.user",
    "roles/iam.serviceAccountUser",
    "roles/pubsub.publisher",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectViewer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.control.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/batch.agentReporter",
    "roles/pubsub.publisher",
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/storage.objectViewer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "cloud_build_run_deployer" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_service_account_iam_member" "cloud_build_control_user" {
  service_account_id = google_service_account.control.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_cloud_tasks_queue" "tasks" {
  name     = local.name
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 3
    max_dispatches_per_second = 3
  }
  retry_config {
    max_attempts       = 5
    max_retry_duration = "3600s"
    min_backoff        = "10s"
    max_backoff        = "300s"
  }
  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "gmail" { name = "${local.name}-gmail" }
resource "google_pubsub_topic" "completion" { name = "${local.name}-completion" }
resource "google_pubsub_topic" "cloud_builds" { name = "cloud-builds" }

resource "google_pubsub_topic_iam_member" "gmail_publish" {
  topic  = google_pubsub_topic.gmail.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "cloud_build_publish" {
  for_each = toset([
    "${data.google_project.current.number}@cloudbuild.gserviceaccount.com",
    "${data.google_project.current.number}-compute@developer.gserviceaccount.com",
  ])
  topic  = google_pubsub_topic.cloud_builds.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${each.value}"
}

resource "google_service_account_iam_member" "service_agent_token_creators" {
  for_each = toset([
    "service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com",
    "service-${data.google_project.current.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com",
    "service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com",
    "service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com",
  ])
  service_account_id = google_service_account.control.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${each.value}"
}

resource "google_secret_manager_secret" "secrets" {
  # Retain the legacy OAuth secret in state; the headless service no longer mounts it.
  # Removing the UI must not schedule deletion of existing credentials.
  for_each  = toset(["openai-api-key", "oauth-client-secret", "slack-bot-token", "slack-signing-secret", "github-app-token", "signed-link-secret"])
  secret_id = "${local.name}-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_service" "api" {
  name                = local.name
  location            = var.region
  deletion_protection = var.environment == "production"
  ingress             = "INGRESS_TRAFFIC_ALL"
  template {
    timeout         = "900s"
    service_account = google_service_account.control.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = var.api_image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      env {
        name  = "MICA_ENV"
        value = var.environment
      }
      env {
        name  = "MICA_STORE_BACKEND"
        value = "firestore"
      }
      env {
        name  = "MICA_EXECUTION_BACKEND"
        value = "gcp_batch"
      }
      env {
        name  = "MICA_AUTH_MODE"
        value = "oidc"
      }
      env {
        name  = "MICA_PLANNER_VERSION"
        value = tostring(var.planner_version)
      }
      env {
        name  = "MICA_MASTER_PLANNER_MODEL"
        value = "gpt-5.6-sol"
      }
      env {
        name  = "MICA_WORKER_MODEL"
        value = "gpt-6-luna"
      }
      env {
        name  = "MICA_WORKER_FALLBACK_MODEL"
        value = "gpt-5.6-sol"
      }
      env {
        name  = "MICA_WORKER_REASONING_EFFORT"
        value = "low"
      }
      env {
        name  = "MICA_MASTER_PLANNER_TIMEOUT_SECONDS"
        value = "180"
      }
      env {
        name  = "MICA_WORKER_TIMEOUT_SECONDS"
        value = "150"
      }
      env {
        name  = "MICA_ALLOWED_EMAILS"
        value = var.allowed_emails
      }
      env {
        name  = "MICA_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "MICA_GCP_REGION"
        value = var.region
      }
      env {
        name  = "MICA_GCS_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "MICA_GCS_QUARANTINE_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "MICA_ARTIFACT_ROOT"
        value = "/tmp/mica"
      }
      env {
        name  = "MICA_BATCH_SERVICE_ACCOUNT"
        value = google_service_account.worker.email
      }
      env {
        name  = "MICA_DEVELOPMENT_LATENCY_ENABLED"
        value = tostring(var.development_latency_enabled)
      }
      env {
        name  = "MICA_WORKER_SESSIONS_ENABLED"
        value = tostring(var.worker_sessions_enabled)
      }
      env {
        name  = "MICA_BATCH_IMAGE"
        value = var.science_image
      }
      env {
        name  = "MICA_BATCH_NETWORK"
        value = var.batch_network
      }
      env {
        name  = "MICA_BATCH_SUBNETWORK"
        value = var.batch_subnetwork
      }
      env {
        name  = "MICA_CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.tasks.name
      }
      env {
        name  = "MICA_CLOUD_TASKS_SERVICE_ACCOUNT"
        value = google_service_account.control.email
      }
      env {
        name  = "MICA_CONTROL_PLANE_URL"
        value = var.control_plane_url
      }
      env {
        name  = "MICA_OIDC_AUDIENCE"
        value = var.control_plane_url
      }
      env {
        name  = "MICA_SLACK_USER_MAP"
        value = var.slack_user_map
      }
      env {
        name  = "MICA_SLACK_AGENT_BACKEND"
        value = var.slack_agent_enabled ? "claude_batch" : "existing"
      }
      env {
        name  = "MICA_SLACK_AGENT_IMAGE"
        value = var.slack_agent_image
      }
      env {
        name  = "AGENT_GPU_TIER_ENABLED"
        value = tostring(var.slack_agent_gpu_enabled)
      }
      env {
        name  = "AGENT_GPU_IMAGE"
        value = var.slack_agent_gpu_image
      }
      env {
        name  = "AGENT_GPU_TYPES"
        value = join(",", sort(tolist(var.slack_agent_gpu_types)))
      }
      env {
        name  = "AGENT_GPU_DEFAULT_TYPE"
        value = var.slack_agent_gpu_default_type
      }
      env {
        name  = "AGENT_ACCESS_MODE"
        value = var.slack_agent_access_mode
      }
      env {
        name  = "AGENT_ACCESS_ADMIN_IDS"
        value = join(",", sort(tolist(var.slack_agent_access_admin_ids)))
      }
      env {
        name  = "AGENT_DATA_BUCKETS"
        value = join(",", sort(tolist(var.slack_agent_data_buckets)))
      }
      env {
        name  = "AGENT_DATA_PROJECT"
        value = var.slack_agent_data_project
      }
      env {
        name  = "AGENT_WRITE_ADMIN_IDS"
        value = join(",", sort(tolist(var.slack_agent_write_admin_ids)))
      }
      env {
        name  = "AGENT_WRITE_ALLOWED_PREFIXES"
        value = join(",", sort(tolist(var.slack_agent_write_prefixes)))
      }
      env {
        name  = "AGENT_WRITE_SERVICE_ACCOUNT"
        value = length(var.slack_agent_write_prefixes) == 0 ? "" : google_service_account.slack_writer[0].email
      }
      env {
        name  = "AGENT_BATCH_PROC_MOUNT_OPT_IN"
        value = tostring(var.slack_agent_proc_mount_opt_in)
      }
      dynamic "env" {
        for_each = var.slack_agent_anthropic_secret == "" ? [] : [var.slack_agent_anthropic_secret]
        content {
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = env.value
              version = var.slack_agent_secret_version
            }
          }
        }
      }
      env {
        name  = "MICA_COMPLETION_TOPIC"
        value = google_pubsub_topic.completion.name
      }
      env {
        name  = "MICA_GMAIL_ACCOUNT"
        value = var.gmail_account
      }
      env {
        name  = "MICA_GMAIL_LABEL"
        value = var.gmail_label
      }
      env {
        name  = "MICA_GMAIL_ALIASES"
        value = jsonencode(var.gmail_aliases)
      }
      env {
        name  = "MICA_GMAIL_OAUTH_CLIENT_ID"
        value = var.gmail_oauth_client_id
      }
      dynamic "env" {
        for_each = { for name, secret in {
          MICA_GMAIL_OAUTH_CLIENT_SECRET = var.gmail_oauth_client_secret_name
          MICA_GMAIL_REFRESH_TOKEN       = var.gmail_refresh_token_secret_name
        } : name => secret if secret != "" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = var.gmail_oauth_secret_version
            }
          }
        }
      }
      env {
        name  = "MICA_GMAIL_PUBSUB_TOPIC"
        value = google_pubsub_topic.gmail.id
      }
      env {
        name  = "MICA_TOOL_BUILD_TRIGGER"
        value = var.tool_build_trigger
      }
      env {
        name  = "MICA_TOOL_IMAGE_REPOSITORY"
        value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/tools"
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["openai-api-key"].secret_id
            version = var.openai_api_key_version
          }
        }
      }
      env {
        name = "MICA_SLACK_BOT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["slack-bot-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "MICA_SLACK_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["slack-signing-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "MICA_GITHUB_APP_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["github-app-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "MICA_SIGNED_LINK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["signed-link-secret"].secret_id
            version = "latest"
          }
        }
      }
    }
  }
  depends_on = [google_project_service.enabled, google_firestore_database.state]
}

resource "google_cloud_run_v2_service_iam_member" "authenticated" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.control.email}"
}

resource "google_cloud_run_v2_service_iam_member" "public_edge" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_pubsub_subscription" "completion_push" {
  name  = "${local.name}-completion-push"
  topic = google_pubsub_topic.completion.name
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/batch/completion"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_pubsub_subscription" "gmail_push" {
  name  = "${local.name}-gmail-push"
  topic = google_pubsub_topic.gmail.name
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api.uri}/integrations/gmail/push"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_pubsub_subscription" "tool_build_push" {
  name  = "${local.name}-tool-build-push"
  topic = google_pubsub_topic.cloud_builds.name
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/tools/build-completion"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_cloud_scheduler_job" "compact_idle" {
  name      = "${local.name}-compact-idle"
  schedule  = "17 3 * * *"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/maintenance/compact-idle"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_cloud_scheduler_job" "reconcile_batch" {
  name      = "${local.name}-reconcile-batch"
  schedule  = "*/5 * * * *"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/maintenance/reconcile-batch"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

import {
  to = google_cloud_scheduler_job.reconcile_batch
  id = "projects/${var.project_id}/locations/${var.region}/jobs/${local.name}-reconcile-batch"
}

resource "google_cloud_scheduler_job" "gmail_watch" {
  name      = "${local.name}-gmail-watch"
  schedule  = "0 2 * * *"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/gmail/renew-watch"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_cloud_scheduler_job" "tool_catalog" {
  name      = "${local.name}-tool-catalog"
  schedule  = "0 4 * * 1"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/api/v1/internal/tools/catalog"
    oidc_token {
      service_account_email = google_service_account.control.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

resource "google_billing_budget" "monthly" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "${local.name} monthly budget"
  budget_filter { projects = ["projects/${var.project_id}"] }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = var.monthly_budget_usd
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }
}
