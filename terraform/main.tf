locals {
  # Every name derives from here, so the orphan sweep in destroy.sh can find
  # anything a partial apply left behind.
  service_name = "${var.app}-${var.customer}-${var.env_name}"

  labels = {
    managed-by = "agentic-poc"
    env        = var.env_name
    customer   = var.customer
    app        = var.app
    ttl-hours  = tostring(var.ttl_hours)
    protected  = tostring(var.protected)
  }
}

# The runtime identity is NOT created here. install.sh creates it and grants
# it datastore.user, because managing project IAM from Terraform would require
# the CI identity to hold resourcemanager.projectIamAdmin - a role that can
# grant any role to any principal, including owner.
#
# Terraform therefore creates no IAM at all beyond the service's own invoker
# binding. The CI identity cannot mint identities or widen permissions.

resource "google_cloud_run_v2_service" "app" {
  name     = local.service_name
  location = var.region
  labels   = local.labels

  # Traffic from the internet only, no load balancer (which would bill).
  ingress = "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = var.runtime_service_account
    labels          = local.labels

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.image

      resources {
        limits = {
          memory = var.memory
          cpu    = var.cpu
        }
        # Bill only while a request is in flight. Without this, CPU is
        # allocated for the whole instance lifetime.
        cpu_idle          = true
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "CUSTOMER"
        value = var.customer
      }
      env {
        name  = "ENVIRONMENT"
        value = var.env_name
      }
      env {
        name  = "APP_NAME"
        value = var.app
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 10
      }
    }

    max_instance_request_concurrency = 80
    timeout                          = "60s"
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# Public ingress. Correct for a sales demo, wrong for production - the plan
# output says so explicitly so the approver sees it.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = google_cloud_run_v2_service.app.project
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
