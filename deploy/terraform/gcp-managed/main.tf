###############################################################################
# AgentOps Hermes Runtime — GCP managed profile (M15 scaffold)
#
# Equivalent topology to aws-managed using GCP managed services:
#   * API / control-plane     -> Cloud Run service
#   * worker fleet            -> Cloud Run service (autoscaled)
#   * scheduler               -> Cloud Run service
#   * Postgres                -> Cloud SQL
#   * queue                   -> Pub/Sub
#   * artifact store          -> GCS bucket
#   * secret containers       -> Secret Manager (no raw values)
#   * logs/IAM                -> Cloud Logging / service accounts
#
# This is a SCAFFOLD. See README.md "Parity gaps vs aws-managed" for what is
# intentionally not yet wired compared to the AWS module.
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  prefix = var.name_prefix

  create_artifact_store = var.existing_artifact_bucket == ""
  create_database       = var.existing_database_arn == ""

  # Bring-your-own-network: when an existing VPC + subnet are provided, the
  # services attach to them via Direct VPC egress. Otherwise Cloud Run uses
  # default egress (see README "Parity gaps" — creating a new private network /
  # connector is not yet wired).
  effective_vpc_id     = var.existing_vpc_id
  effective_subnet_ids = var.existing_subnet_ids
  byo_network          = var.existing_vpc_id != "" && length(var.existing_subnet_ids) > 0

  # Secret CONTAINERS created here; raw values are written by bootstrap.
  secret_names = [
    "bootstrap-token",
    "slack",
    "github",
    "linear",
    "jira",
    "model-provider",
    "database",
  ]
}

# API / control-plane service.
resource "google_cloud_run_v2_service" "control_plane" {
  name     = "${local.prefix}-control-plane"
  location = var.region
  template {
    containers {
      image = "TODO-control-plane-image" # set after building the API/control-plane image
    }
    dynamic "vpc_access" {
      for_each = local.byo_network ? [1] : []
      content {
        network_interfaces {
          network    = local.effective_vpc_id
          subnetwork = local.effective_subnet_ids[0]
        }
      }
    }
  }
}

# Worker fleet service.
resource "google_cloud_run_v2_service" "worker" {
  name     = "${local.prefix}-worker"
  location = var.region
  template {
    scaling {
      min_instance_count = var.min_task_count
      max_instance_count = var.max_task_count
    }
    containers {
      image = "TODO-worker-image"
      env {
        name  = "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS"
        value = tostring(var.max_concurrent_runs)
      }
    }
    dynamic "vpc_access" {
      for_each = local.byo_network ? [1] : []
      content {
        network_interfaces {
          network    = local.effective_vpc_id
          subnetwork = local.effective_subnet_ids[0]
        }
      }
    }
  }
}

# Scheduler service.
resource "google_cloud_run_v2_service" "scheduler" {
  name     = "${local.prefix}-scheduler"
  location = var.region
  template {
    containers {
      image = "TODO-scheduler-image"
    }
    dynamic "vpc_access" {
      for_each = local.byo_network ? [1] : []
      content {
        network_interfaces {
          network    = local.effective_vpc_id
          subnetwork = local.effective_subnet_ids[0]
        }
      }
    }
  }
}

# Managed Postgres (Cloud SQL).
resource "google_sql_database_instance" "this" {
  count            = local.create_database ? 1 : 0
  name             = "${local.prefix}-postgres"
  database_version = "POSTGRES_16"
  region           = var.region
  settings {
    tier = "db-custom-2-7680"
  }
  deletion_protection = false
}

# Queue (Pub/Sub).
resource "google_pubsub_topic" "runs" {
  name = "${local.prefix}-runs"
}

resource "google_pubsub_subscription" "runs" {
  name  = "${local.prefix}-runs-sub"
  topic = google_pubsub_topic.runs.name
}

# Artifact store (GCS) — bring-your-own supported.
resource "google_storage_bucket" "artifacts" {
  count                       = local.create_artifact_store ? 1 : 0
  name                        = "${local.prefix}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
}

# Secret CONTAINERS only — no raw values, so secrets stay out of state.
resource "google_secret_manager_secret" "containers" {
  for_each  = toset(local.secret_names)
  secret_id = var.existing_secret_prefix != "" ? "${var.existing_secret_prefix}-${each.key}" : "${local.prefix}-${each.key}"
  replication {
    auto {}
  }
}
