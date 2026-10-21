terraform {
  required_version = "= 1.13.5"
  backend "gcs" {}
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.50.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
