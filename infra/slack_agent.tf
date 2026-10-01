# Opt-in additions to the existing deployment. No dedicated long-lived worker VM.
resource "google_project_iam_member" "slack_bucket_catalog" {
  count   = var.slack_agent_data_project == "" ? 0 : 1
  project = var.slack_agent_data_project
  role    = "roles/storage.bucketViewer"
  member  = "serviceAccount:${google_service_account.control.email}"
}

resource "google_storage_bucket_iam_member" "slack_projects" {
  count  = local.slack_agent_configured ? 1 : 0
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
  condition {
    title       = "slack_project_objects_only"
    description = "Claude workers persist project briefs, outputs and frozen thread metadata."
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.artifacts.name}/objects/projects/')"
  }
}

resource "google_storage_bucket_iam_member" "slack_data_reader" {
  for_each = local.slack_agent_configured ? var.slack_agent_data_buckets : toset([])
  bucket   = each.value
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.worker.email}"
}

# The existing worker has project-wide object access, which supplies list permission.
# No Secret Manager role is added to the worker. Trusted bootstrap fetches only the two
# required credentials over the worker-authenticated endpoint on the existing control plane.

resource "google_compute_router" "slack_egress" {
  count   = var.slack_agent_nat_enabled ? 1 : 0
  name    = "${local.name}-slack-egress"
  region  = var.region
  network = var.batch_network
}

resource "google_compute_router_nat" "slack_egress" {
  count                              = var.slack_agent_nat_enabled ? 1 : 0
  name                               = "${local.name}-slack-egress"
  router                             = google_compute_router.slack_egress[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat  = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = startswith(var.batch_subnetwork, "projects/") ? var.batch_subnetwork : "projects/${var.project_id}/regions/${var.region}/subnetworks/${var.batch_subnetwork}"
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

check "slack_rollout_ready" {
  assert {
    condition     = !var.slack_agent_enabled || (local.slack_agent_configured && (length(var.slack_agent_data_buckets) > 0 || var.slack_agent_data_project != "") && var.slack_agent_anthropic_secret != "")
    error_message = "Configure the pinned worker image, data buckets and populated Anthropic secret before enabling Slack."
  }
}
