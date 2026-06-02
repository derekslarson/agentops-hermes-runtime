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

  # Effective backend refs the runtime containers read. Bring-your-own values
  # win when provided; otherwise the module-created resources are used.
  effective_artifact_bucket = local.create_artifact_store ? google_storage_bucket.artifacts[0].name : var.existing_artifact_bucket
  effective_secret_prefix   = var.existing_secret_prefix != "" ? var.existing_secret_prefix : local.prefix
  # Non-secret Cloud SQL connection name (the secret container below carries the
  # raw connection string filled by bootstrap, not a value passed here).
  effective_database_connection_name = local.create_database ? google_sql_database_instance.this[0].connection_name : var.existing_database_arn

  # Non-secret runtime refs shared by all three services. These are container
  # ENV entries (names + refs), never raw secret values: the database entry
  # carries the Secret Manager container ref that bootstrap fills (plus the
  # non-secret Cloud SQL connection name), not a connection string.
  runtime_common_env = [
    { name = "AGENTOPS_RUNTIME_PROFILE", value = "gcp-managed" },
    { name = "AGENTOPS_QUEUE_TOPIC", value = google_pubsub_topic.runs.id },
    { name = "AGENTOPS_QUEUE_SUBSCRIPTION", value = google_pubsub_subscription.runs.id },
    { name = "AGENTOPS_ARTIFACT_BUCKET", value = local.effective_artifact_bucket },
    { name = "AGENTOPS_SECRET_PREFIX", value = local.effective_secret_prefix },
    { name = "AGENTOPS_DATABASE_SECRET_REF", value = google_secret_manager_secret.containers["database"].id },
    { name = "AGENTOPS_DATABASE_CONNECTION_NAME", value = local.effective_database_connection_name },
  ]
}

# API / control-plane service.
resource "google_cloud_run_v2_service" "control_plane" {
  name     = "${local.prefix}-control-plane"
  location = var.region
  template {
    service_account = google_service_account.runtime.email
    containers {
      image = var.control_plane_image
      dynamic "env" {
        for_each = local.runtime_common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
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

# Worker fleet service.
resource "google_cloud_run_v2_service" "worker" {
  name     = "${local.prefix}-worker"
  location = var.region
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = var.min_task_count
      max_instance_count = var.max_task_count
    }
    containers {
      image = var.worker_image
      dynamic "env" {
        for_each = local.runtime_common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
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
    service_account = google_service_account.runtime.email
    containers {
      image = var.scheduler_image
      dynamic "env" {
        for_each = local.runtime_common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
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

###############################################################################
# IAM — dedicated runtime service account + scoped backend bindings
#
# GCP parity with the aws-managed task role: all three Cloud Run services run as
# a dedicated runtime service account (not the default Compute SA), and that SA
# is granted least-reasonable, resource-scoped access to exactly the backend
# refs advertised in local.runtime_common_env. Bindings stay read-only where
# possible (Secret Manager accessor, not admin); raw secret values remain a
# bootstrap concern, so no secret VERSION resource is created here.
###############################################################################

resource "google_service_account" "runtime" {
  account_id   = "${local.prefix}-runtime"
  display_name = "AgentOps Hermes runtime (Cloud Run services)"
}

# Queue: publish to the runs topic and consume the runs subscription.
resource "google_pubsub_topic_iam_member" "runtime_publisher" {
  topic  = google_pubsub_topic.runs.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_pubsub_subscription_iam_member" "runtime_subscriber" {
  subscription = google_pubsub_subscription.runs.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.runtime.email}"
}

# Artifacts: object read/write on the effective bucket (created or BYO by name).
resource "google_storage_bucket_iam_member" "runtime_artifacts" {
  bucket = local.effective_artifact_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Secrets: read-only accessor on each created container (bootstrap fills values).
resource "google_secret_manager_secret_iam_member" "runtime_secret_accessor" {
  for_each  = google_secret_manager_secret.containers
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# Cloud SQL: client role to open connections (managed or BYO connection name).
resource "google_project_iam_member" "runtime_cloudsql" {
  project = var.project
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

###############################################################################
# Public API endpoint — opt-in unauthenticated invoker + optional custom domain
#
# The control-plane has no provisioned load balancer / DNS, so the default
# public endpoint is the Cloud Run service URI (surfaced via outputs). Public
# unauthenticated access is OFF by default; set enable_public_invoker to grant
# allUsers roles/run.invoker on the CONTROL-PLANE only. A custom domain mapping
# is created only when enable_custom_domain is set together with a domain.
###############################################################################

resource "google_cloud_run_v2_service_iam_member" "control_plane_public" {
  count    = var.enable_public_invoker ? 1 : 0
  location = google_cloud_run_v2_service.control_plane.location
  name     = google_cloud_run_v2_service.control_plane.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_domain_mapping" "control_plane" {
  count    = var.enable_custom_domain && var.domain != "" ? 1 : 0
  location = var.region
  name     = var.domain
  metadata {
    namespace = var.project
  }
  spec {
    route_name = google_cloud_run_v2_service.control_plane.name
  }
}
