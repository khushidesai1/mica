# Optional output publishing. Workers cannot impersonate this account. The trusted
# controller downscopes each upload to a single object and checks the Slack grant.
variable "slack_agent_write_admin_ids" {
  description = "Write-grant admins as workspace:member IDs; separate from onboarding admins."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.slack_agent_write_admin_ids : can(regex("^[A-Z0-9]+:[A-Z0-9]+$", id))])
    error_message = "Each write admin must be a workspace:member ID."
  }
}

variable "slack_agent_write_prefixes" {
  description = "Maximum writable GCS folders. Include the final slash; a bucket root allows any folder in that bucket. Buckets must use uniform bucket-level access."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([
      for uri in var.slack_agent_write_prefixes :
      can(regex("^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]/", uri)) && endswith(uri, "/") &&
      !can(regex("[[:cntrl:][:space:]]", uri)) && !can(regex("/(\\.|\\.\\.)/", uri)) &&
      !strcontains(trimprefix(uri, "gs://"), "//") &&
      alltrue([for invalid in ["%", "*", "?", "<", ">", "|", "`", "#", "\\", "[", "]", ","] : !strcontains(uri, invalid)])
    ])
    error_message = "Use literal gs://bucket/folder/ prefixes with no traversal, escapes, or wildcards."
  }
}

locals {
  slack_write_buckets = {
    for uri in var.slack_agent_write_prefixes : split("/", uri)[2] =>
    "projects/_/buckets/${split("/", uri)[2]}/objects/${join("/", slice(split("/", uri), 3, length(split("/", uri))))}"...
  }
}

resource "google_project_service" "slack_write_apis" {
  for_each           = length(var.slack_agent_write_prefixes) == 0 ? toset([]) : toset(["iamcredentials.googleapis.com", "sts.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "slack_writer" {
  count        = length(var.slack_agent_write_prefixes) == 0 ? 0 : 1
  account_id   = "${local.name}-writer"
  display_name = "Pistachio scoped output publisher"
}

resource "google_project_iam_custom_role" "slack_writer" {
  count       = length(var.slack_agent_write_prefixes) == 0 ? 0 : 1
  role_id     = "pistachio_${var.environment}_output_writer"
  title       = "Pistachio output creator"
  description = "Create outputs and read them to reconcile retries; never overwrite or delete."
  permissions = ["storage.objects.create", "storage.objects.get"]
}

resource "google_storage_bucket_iam_member" "slack_writer" {
  for_each = local.slack_write_buckets
  bucket   = each.key
  role     = google_project_iam_custom_role.slack_writer[0].name
  member   = "serviceAccount:${google_service_account.slack_writer[0].email}"
  condition {
    title       = "pistachio_approved_output_folders"
    description = "Ceiling for Slack write grants; individual uploads are further downscoped."
    expression = format(
      "resource.type == 'storage.googleapis.com/Object' && (%s)",
      join(" || ", [for prefix in each.value : format("resource.name.startsWith(%s)", jsonencode(prefix))])
    )
  }
}

resource "google_service_account_iam_member" "slack_writer_controller" {
  count              = length(var.slack_agent_write_prefixes) == 0 ? 0 : 1
  service_account_id = google_service_account.slack_writer[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.control.email}"
}

resource "google_storage_bucket_iam_member" "slack_write_grants" {
  count  = local.slack_agent_configured ? 1 : 0
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.control.email}"
  condition {
    title      = "slack_write_grants_and_audit"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/write-access/')"
  }
}

# Preserve registry/brief/conversation writes when broad live objectAdmin grants are removed.
resource "google_storage_bucket_iam_member" "slack_control_projects" {
  count  = local.slack_agent_configured ? 1 : 0
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.control.email}"
  condition {
    title      = "slack_control_project_objects"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/projects/')"
  }
}
