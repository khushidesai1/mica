variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "environment" {
  type    = string
  default = "staging"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production"
  }
}
variable "api_image" { type = string }
variable "science_image" { type = string }
variable "batch_network" {
  type    = string
  default = "default"
}
variable "batch_subnetwork" {
  type    = string
  default = "default"
}
variable "control_plane_url" { type = string }
variable "planner_version" {
  type    = number
  default = 2
  validation {
    condition     = contains([1, 2], var.planner_version)
    error_message = "planner_version must be 1 or 2."
  }
}
variable "tool_build_trigger" {
  type    = string
  default = ""
}
variable "allowed_emails" {
  type      = string
  sensitive = true
}
variable "openai_api_key_version" {
  type    = string
  default = "latest"
}
variable "slack_user_map" {
  type      = string
  sensitive = true
  default   = "{}"
}
variable "gmail_account" {
  type    = string
  default = ""
}
variable "billing_account" {
  type    = string
  default = ""
}
variable "monthly_budget_usd" {
  type    = number
  default = 200
}

variable "development_latency_enabled" {
  description = "Enable preparation version 2 only after paired worker conformance."
  type        = bool
  default     = false
}
variable "worker_sessions_enabled" {
  description = "Reuse a task-scoped worker for new version-2 sessions."
  type        = bool
  default     = false
}

variable "slack_agent_enabled" {
  description = "Switch only Slack events to the Claude Spot worker after live acceptance."
  type        = bool
  default     = false
}
variable "slack_agent_access_mode" {
  description = "Slack admission: workspace permits private DMs for verified members; channel requires enabled-channel membership; mapped uses legacy lists."
  type        = string
  default     = "channel"
  validation {
    condition     = contains(["channel", "mapped", "workspace"], var.slack_agent_access_mode)
    error_message = "Use channel, mapped, or workspace."
  }
}
variable "slack_agent_access_admin_ids" {
  description = "One-time onboarding admins as workspace:member IDs, for example T123:U123."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.slack_agent_access_admin_ids : can(regex("^[A-Z0-9]+:[A-Z0-9]+$", id))])
    error_message = "Each admin must be a workspace:member ID."
  }
}
variable "slack_agent_image" {
  description = "Pinned Claude worker image; also prepares its storage permissions and retention."
  type        = string
  default     = ""
  validation {
    condition     = var.slack_agent_image == "" || can(regex("@sha256:[a-f0-9]{64}$", var.slack_agent_image))
    error_message = "slack_agent_image must be an immutable image digest."
  }
}
variable "slack_agent_data_buckets" {
  description = "Explicit read-only data buckets for the Slack agent."
  type        = set(string)
  default     = []
}
variable "slack_agent_data_project" {
  description = "Automatically accept data buckets owned by this Google Cloud project."
  type        = string
  default     = ""
}
variable "slack_agent_proc_mount_opt_in" {
  description = "Explicit staging-only approval to unmask outer Docker system paths for the inner private proc sandbox; requires the existing nested sandbox opt-in."
  type        = bool
  default     = false
}
variable "slack_agent_anthropic_secret" {
  description = "Existing Secret Manager secret with a populated version; never put a key in Terraform."
  type        = string
  default     = ""
}
variable "slack_agent_secret_version" {
  type    = string
  default = "latest"
}
variable "slack_agent_nat_enabled" {
  description = "Provision outbound NAT on the existing worker subnet, only if it lacks egress."
  type        = bool
  default     = false
}
