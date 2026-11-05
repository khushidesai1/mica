# Independent of Mica's request handlers and its Slack incident sender.
variable "enable_outage_alerts" {
  type    = bool
  default = false
}
variable "notification_email" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Operator email, supplied only in private tfvars."
}
variable "scheduler_region" {
  type    = string
  default = "us-central1"
}

resource "google_monitoring_notification_channel" "operator" {
  count        = var.enable_outage_alerts ? 1 : 0
  display_name = "${var.service_name} outage operator"
  type         = "email"
  labels       = { email_address = var.notification_email }
  enabled      = true
  lifecycle {
    precondition {
      condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.notification_email))
      error_message = "Supply the approved operator email in private tfvars."
    }
  }
}

resource "google_monitoring_uptime_check_config" "api" {
  count        = var.enable_outage_alerts ? 1 : 0
  display_name = "${var.service_name} public health"
  timeout      = "10s"
  period       = "60s"
  http_check {
    path         = "/api/v1/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
    accepted_response_status_codes { status_value = 200 }
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(var.service_url, "https://")
    }
  }
  content_matchers {
    content = "\"status\":\"ok\""
    matcher = "CONTAINS_STRING"
  }
  selected_regions = ["USA", "EUROPE", "ASIA_PACIFIC"]
}

resource "google_monitoring_alert_policy" "api_unavailable" {
  count                 = var.enable_outage_alerts ? 1 : 0
  display_name          = "${var.service_name}: Mica not responding"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator[0].name]
  conditions {
    display_name = "Public health fails from multiple locations for five minutes"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.api[0].uptime_check_id}\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host", "resource.label.project_id"]
      }
      trigger { count = 1 }
    }
  }
  documentation {
    mime_type = "text/markdown"
    content   = "Mica's public health endpoint has failed from multiple check locations for five minutes. Inspect Cloud Run availability and the serving revision. The Slack failures channel can be silent during an outage; silence does not mean healthy. This alert does not authorize replaying user requests."
  }
}

# Count completed failed attempts, not start logs or HTTP errors from unrelated jobs.
resource "google_logging_metric" "reporter_failures" {
  count  = var.enable_outage_alerts ? 1 : 0
  name   = "${var.service_name}-failure-reporter-errors"
  filter = <<-FILTER
    resource.type="cloud_scheduler_job"
    resource.labels.project_id="${var.project_id}"
    resource.labels.location="${var.scheduler_region}"
    resource.labels.job_id="${var.service_name}-reconcile-batch"
    jsonPayload."@type"="type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished"
    severity>=ERROR
  FILTER
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "reporter_unavailable" {
  count                 = var.enable_outage_alerts ? 1 : 0
  display_name          = "${var.service_name}: failure reporting interrupted"
  combiner              = "OR"
  enabled               = true
  notification_channels = [google_monitoring_notification_channel.operator[0].name]
  conditions {
    display_name = "At least two failed reconciliation attempts in ten minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.reporter_failures[0].name}\" AND resource.type=\"cloud_scheduler_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "0s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger { count = 1 }
    }
  }
  documentation {
    mime_type = "text/markdown"
    content   = "The scheduled Mica reconciliation call failed repeatedly. Slack failure cards may be delayed or absent even if ordinary requests still work. Inspect the Scheduler job's completion logs, authentication and the reconcile-batch endpoint. Do not replay user requests or clear ambiguous delivery receipts."
  }
}
