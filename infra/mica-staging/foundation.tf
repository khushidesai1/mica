# No resources in the legacy root are imported here. Project services, Firestore,
# cloud-builds and the worker network remain owned by their existing root.
locals {
  runtime = var.stage != "foundation"
  active  = var.stage == "cutover"
  secret_names = toset([
    "openai-api-key", "oauth-client-secret", "slack-bot-token", "slack-signing-secret",
    "github-app-token", "signed-link-secret", "anthropic-api-key", "memory-token",
  ])
  project_grants = merge([for identity, roles in var.project_roles : {
    for role in roles : "${identity}:${role}" => { identity = identity, role = role }
  }]...)
  write_buckets = {
    for uri in var.write_prefixes : split("/", uri)[2] =>
    "projects/_/buckets/${split("/", uri)[2]}/objects/${join("/", slice(split("/", uri), 3, length(split("/", uri))))}"...
  }
}

resource "google_service_account" "runtime" {
  for_each     = toset(["control", "worker", "writer", "access"])
  account_id   = "${var.name}-${each.key}"
  display_name = "Mica ${each.key}"
  lifecycle { prevent_destroy = true }
}

resource "google_storage_bucket" "artifacts" {
  name                        = var.artifact_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  soft_delete_policy { retention_duration_seconds = 604800 }
  # Do not expire copied checkpoints while the migration/reference audit is pending.
  # Enable the matching source lifecycle only after storage compatibility verification.
  lifecycle { prevent_destroy = true }
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  lifecycle { prevent_destroy = true }
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = local.secret_names
  secret_id = "${var.name}-${each.key}"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}

resource "google_cloud_tasks_queue" "tasks" {
  name     = var.name
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
    max_doublings      = 16
  }
  lifecycle { prevent_destroy = true }
}

resource "google_pubsub_topic" "runtime" {
  for_each = toset(["gmail", "completion"])
  name     = "${var.name}-${each.key}"
  lifecycle { prevent_destroy = true }
}

resource "google_project_iam_member" "runtime" {
  for_each = local.project_grants
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.runtime[each.value.identity].email}"
}

resource "google_service_account_iam_member" "service_agents" {
  for_each           = toset(["pubsub", "cloudscheduler", "cloudtasks", "cloudbuild"])
  service_account_id = google_service_account.runtime["control"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${var.project_number}@gcp-sa-${each.value}.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "build_deployer" {
  service_account_id = google_service_account.runtime["control"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.project_number}-compute@developer.gserviceaccount.com"
}

resource "google_service_account_iam_member" "writer_controller" {
  service_account_id = google_service_account.runtime["writer"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.runtime["control"].email}"
}

resource "google_service_account_iam_member" "access_controller" {
  service_account_id = google_service_account.runtime["access"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.runtime["control"].email}"
}

resource "google_project_iam_member" "access_granter" {
  count   = length(var.access_grant_roles) > 0 ? 1 : 0
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.runtime["access"].email}"
  condition {
    title       = "Mica configured access roles only"
    description = "Limits modified roles, not recipients; controller grants users only."
    expression  = "api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(${jsonencode(sort(tolist(var.access_grant_roles)))})"
  }
}

resource "google_pubsub_topic_iam_member" "gmail_publisher" {
  topic  = google_pubsub_topic.runtime["gmail"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

locals {
  artifact_grants = {
    control = { identity = "control", prefixes = ["tasks/", "inputs/", "incoming/", "paused/", "projects/", "write-access/"] }
    worker  = { identity = "worker", prefixes = ["tasks/", "projects/"] }
  }
}
resource "google_storage_bucket_iam_member" "artifacts" {
  for_each = local.artifact_grants
  bucket   = google_storage_bucket.artifacts.name
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${google_service_account.runtime[each.value.identity].email}"
  condition {
    title = "mica_${each.key}_prefixes"
    expression = join(" || ", [for prefix in each.value.prefixes :
      "resource.name.startsWith('projects/_/buckets/${var.artifact_bucket}/objects/${prefix}')"
    ])
  }
}

resource "google_project_iam_custom_role" "writer" {
  role_id     = "mica_staging_output_writer"
  title       = "Mica output writer"
  description = "Existing publish permissions; controller downscopes every object operation."
  permissions = ["storage.objects.create", "storage.objects.get", "storage.objects.delete"]
}
resource "google_project_iam_custom_role" "export_reader" {
  role_id     = "mica_staging_export_reader"
  title       = "Mica staged export reader"
  permissions = ["storage.objects.get"]
}
resource "google_storage_bucket_iam_member" "writer" {
  for_each = local.write_buckets
  bucket   = each.key
  role     = google_project_iam_custom_role.writer.name
  member   = "serviceAccount:${google_service_account.runtime["writer"].email}"
  dynamic "condition" {
    for_each = contains(each.value, "projects/_/buckets/${each.key}/objects/") ? [] : [true]
    content {
      title = "mica_approved_output_folders"
      expression = format("resource.type == 'storage.googleapis.com/Object' && (%s)",
      join(" || ", [for prefix in each.value : format("resource.name.startsWith(%s)", jsonencode(prefix))]))
    }
  }
}
resource "google_storage_bucket_iam_member" "export_reader" {
  bucket = google_storage_bucket.artifacts.name
  role   = google_project_iam_custom_role.export_reader.name
  member = "serviceAccount:${google_service_account.runtime["writer"].email}"
  condition {
    title      = "mica_staged_exports_only"
    expression = "resource.name.startsWith('projects/_/buckets/${var.artifact_bucket}/objects/tasks/slack-analysis/events/') && resource.name.extract('projects/_/buckets/${var.artifact_bucket}/objects/tasks/slack-analysis/events/{event}/write-exports/') != '' && resource.name.extract('projects/_/buckets/${var.artifact_bucket}/objects/tasks/slack-analysis/events/{event}/') == resource.name.extract('projects/_/buckets/${var.artifact_bucket}/objects/tasks/slack-analysis/events/{event}/write-exports/')"
  }
}
