locals {
  # The capture tool resolves legacy aliases first. Only these reviewed resource
  # references and the migration guard differ from the working deployment.
  environment = merge(var.runtime_environment, {
    MICA_GCS_BUCKET                  = var.artifact_bucket
    MICA_GCS_QUARANTINE_BUCKET       = var.artifact_bucket
    MICA_BATCH_SERVICE_ACCOUNT       = google_service_account.runtime["worker"].email
    MICA_CLOUD_TASKS_SERVICE_ACCOUNT = google_service_account.runtime["control"].email
    MICA_CLOUD_TASKS_QUEUE           = google_cloud_tasks_queue.tasks.name
    MICA_COMPLETION_TOPIC            = google_pubsub_topic.runtime["completion"].name
    MICA_GMAIL_PUBSUB_TOPIC          = "projects/${var.project_id}/topics/${var.name}-gmail"
    MICA_CONTROL_PLANE_URL           = var.control_plane_url
    MICA_OIDC_AUDIENCE               = var.control_plane_url
    MICA_TOOL_IMAGE_REPOSITORY       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.name}/tools"
    AGENT_DATA_BUCKETS = join(",", [for bucket in split(",", lookup(var.runtime_environment, "AGENT_DATA_BUCKETS", "")) :
    bucket == var.source_artifact_bucket ? var.artifact_bucket : bucket])
    AGENT_WRITE_SERVICE_ACCOUNT = google_service_account.runtime["writer"].email
    AGENT_MEMORY_TOKEN_SECRET   = "projects/${var.project_id}/secrets/${var.name}-memory-token/versions/1"
    MICA_MIGRATION_SERVICE      = var.name
    MICA_MIGRATION_DEFAULT_MODE = "frozen"
  })
  schedules = {
    compact-idle    = { schedule = "17 3 * * *", path = "/api/v1/internal/maintenance/compact-idle" }
    reconcile-batch = { schedule = "*/5 * * * *", path = "/api/v1/internal/maintenance/reconcile-batch" }
    gmail-watch     = { schedule = "0 2 * * *", path = "/api/v1/internal/gmail/renew-watch" }
    tool-catalog    = { schedule = "0 4 * * 1", path = "/api/v1/internal/tools/catalog" }
  }
  subscriptions = {
    gmail      = { topic = google_pubsub_topic.runtime["gmail"].id, path = "/integrations/gmail/push" }
    completion = { topic = google_pubsub_topic.runtime["completion"].id, path = "/api/v1/internal/batch/completion" }
    tool-build = { topic = "projects/${var.project_id}/topics/cloud-builds", path = "/api/v1/internal/tools/build-completion" }
  }
}

resource "google_cloud_run_v2_service" "api" {
  count               = local.runtime ? 1 : 0
  name                = var.name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true
  # Normalize API defaults; the revision below retains the configured runtime limits.
  scaling {
    min_instance_count = 0
  }
  template {
    service_account                  = google_service_account.runtime["control"].email
    timeout                          = "${var.runtime_limits.timeout}s"
    max_instance_request_concurrency = var.runtime_limits.concurrency
    scaling {
      min_instance_count = var.runtime_limits.min_instances
      max_instance_count = var.runtime_limits.max_instances
    }
    containers {
      image = var.api_image
      ports { container_port = 8080 }
      resources {
        limits   = { cpu = var.runtime_limits.cpu, memory = var.runtime_limits.memory }
        cpu_idle = var.runtime_limits.cpu_idle
      }
      startup_probe {
        failure_threshold = 1
        period_seconds    = 240
        timeout_seconds   = 240
        tcp_socket { port = 8080 }
      }
      dynamic "env" {
        for_each = nonsensitive(toset(keys(local.environment)))
        content {
          name  = env.value
          value = local.environment[env.value]
        }
      }
      dynamic "env" {
        for_each = var.runtime_secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
    }
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = !local.active || var.cutover_verified
      error_message = "Cutover requires verified storage, admission and operator handoff."
    }
    precondition {
      condition = alltrue([for key in ["MICA_BATCH_IMAGE", "MICA_SLACK_AGENT_IMAGE"] :
      can(regex("@sha256:[a-f0-9]{64}$", lookup(var.runtime_environment, key, "")))])
      error_message = "Preserve both immutable worker image pins."
    }
    precondition {
      condition = lower(lookup(var.runtime_environment, "AGENT_GPU_TIER_ENABLED", "false")) != "true" || can(
        regex("@sha256:[a-f0-9]{64}$", lookup(var.runtime_environment, "AGENT_GPU_IMAGE", ""))
      )
      error_message = "An enabled GPU tier requires its separately verified immutable worker image."
    }
  }
  depends_on = [google_project_iam_member.runtime, google_secret_manager_secret.runtime]
}

# Match the existing signed public ingress, while application admission is frozen.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = local.runtime ? {
    public  = "allUsers"
    control = "serviceAccount:${google_service_account.runtime["control"].email}"
  } : {}
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_cloud_scheduler_job" "runtime" {
  for_each  = local.runtime ? local.schedules : {}
  name      = "${var.name}-${each.key}"
  region    = var.region
  schedule  = each.value.schedule
  time_zone = "Etc/UTC"
  paused    = !local.active
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api[0].uri}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.runtime["control"].email
      audience              = var.control_plane_url
    }
  }
}

# No new subscriber (especially on shared cloud-builds) exists before cutover.
resource "google_pubsub_subscription" "runtime" {
  for_each = local.active ? local.subscriptions : {}
  name     = "${var.name}-${each.key}-push"
  topic    = each.value.topic
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api[0].uri}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.runtime["control"].email
      audience              = var.control_plane_url
    }
  }
  depends_on = [google_service_account_iam_member.service_agents]
}
