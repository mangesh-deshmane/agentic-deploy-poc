terraform {
  # Pinned deliberately. The same version runs here and in CI; drift between
  # them produces plans that differ between your terminal and the pipeline.
  required_version = "~> 1.16.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Bucket and prefix come from -backend-config so one module serves every
  # environment, each with its own state file.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
