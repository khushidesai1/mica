variable "project_id" { type = string }
variable "project_number" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+$", var.project_number))
    error_message = "Use the verified project number from the source service metadata."
  }
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "name" {
  type    = string
  default = "mica-staging"
  validation {
    condition     = var.name == "mica-staging"
    error_message = "This migration root only manages mica-staging."
  }
}
variable "stage" {
  type    = string
  default = "foundation"
  validation {
    condition     = contains(["foundation", "preview", "cutover"], var.stage)
    error_message = "Use foundation, preview or cutover."
  }
}
variable "artifact_bucket" {
  type = string
  validation {
    condition     = startswith(var.artifact_bucket, "mica-staging-artifacts-")
    error_message = "Use an explicit new mica-staging artifacts bucket name."
  }
}
variable "source_artifact_bucket" { type = string }
variable "api_image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "The API must use an approved immutable release digest."
  }
}
variable "control_plane_url" { type = string }
variable "runtime_environment" {
  description = "Effective live plain settings, captured without secret values."
  type        = map(string)
  sensitive   = true
}
variable "runtime_secrets" {
  description = "New secret references; operator copies the selected source versions first."
  type        = map(object({ secret = string, version = string }))
}
variable "runtime_limits" {
  type = object({
    cpu           = string, memory = string, concurrency = number, timeout = number,
    min_instances = number, max_instances = number, cpu_idle = bool
  })
}
variable "project_roles" {
  description = "Audited existing effective project roles, per identity. No owner/editor roles."
  type        = map(set(string))
  validation {
    condition = alltrue(flatten([
      for roles in values(var.project_roles) : [for role in roles :
      !contains(["roles/owner", "roles/editor", "roles/viewer"], role)]
    ]))
    error_message = "Do not grant primitive project roles."
  }
}
variable "write_prefixes" {
  description = "Existing write ceiling only; changing this requires separate access approval."
  type        = set(string)
  validation {
    condition = alltrue([for uri in var.write_prefixes :
      can(regex("^gs://[a-z0-9][a-z0-9._-]+/", uri)) && endswith(uri, "/") &&
      !can(regex("[[:space:]%*?#\\[\\]\\\\]", uri)) && !strcontains(uri, "/../")
    ])
    error_message = "Use literal, normalized gs://bucket/folder/ prefixes."
  }
}
variable "cutover_verified" {
  description = "Set only after admission hold, storage checks and operator handoff review."
  type        = bool
  default     = false
}
