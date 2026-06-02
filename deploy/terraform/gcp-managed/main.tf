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
  required_version = ">= 1.9"
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

  # Networking: when no existing VPC is provided, the module creates its own
  # private VPC network + regional subnet and the Cloud Run services attach to
  # it via Direct VPC egress. When existing_vpc_id + existing_subnet_ids are
  # provided, those bring-your-own refs are used instead. Either way the
  # services always run with Direct VPC egress onto the effective network.
  create_vpc           = var.existing_vpc_id == ""
  effective_vpc_id     = local.create_vpc ? google_compute_network.this[0].id : var.existing_vpc_id
  effective_subnet_ids = local.create_vpc ? [google_compute_subnetwork.this[0].id] : var.existing_subnet_ids

  # Opt-in External HTTPS Load Balancer front door for the control-plane. Only
  # built when explicitly enabled together with a domain; the default endpoint
  # stays the Cloud Run service URI.
  create_lb = var.enable_load_balancer_custom_domain && var.domain != ""

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
    vpc_access {
      network_interfaces {
        network    = local.effective_vpc_id
        subnetwork = local.effective_subnet_ids[0]
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
    vpc_access {
      network_interfaces {
        network    = local.effective_vpc_id
        subnetwork = local.effective_subnet_ids[0]
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
    vpc_access {
      network_interfaces {
        network    = local.effective_vpc_id
        subnetwork = local.effective_subnet_ids[0]
      }
    }
  }
}

# Private network — created by default when no existing VPC is provided. The
# Cloud Run services attach to it via Direct VPC egress (see the vpc_access
# blocks above). Bring-your-own VPC/subnet refs replace these when supplied.
resource "google_compute_network" "this" {
  count                   = local.create_vpc ? 1 : 0
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  count         = local.create_vpc ? 1 : 0
  name          = "${local.prefix}-subnet"
  region        = var.region
  network       = google_compute_network.this[0].id
  ip_cidr_range = var.private_subnet_cidr
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
# By default, without enable_load_balancer_custom_domain, the public endpoint is
# the Cloud Run service URI (surfaced via outputs). Public unauthenticated access
# is OFF by default; set enable_public_invoker to grant allUsers roles/run.invoker
# on the CONTROL-PLANE only. A lightweight Cloud Run custom domain mapping is
# created only when enable_custom_domain is set together with a domain; the
# alternative External HTTPS Load Balancer custom-domain path is below.
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

###############################################################################
# Optional External HTTPS Load Balancer — control-plane front door
#
# An opt-in alternative to the Cloud Run domain mapping above: a global External
# HTTPS Load Balancer fronts the CONTROL-PLANE service only via a serverless
# NEG -> backend service -> URL map -> target HTTPS proxy -> global forwarding
# rule, terminated by a Google-managed SSL certificate for var.domain. An
# optional Cloud DNS A record points var.domain at the reserved global IP.
# Everything is gated on local.create_lb (enable_load_balancer_custom_domain +
# domain); the worker/scheduler are never fronted. No secret values are
# introduced here — raw integration secrets remain a bootstrap (M16) concern.
###############################################################################

# Serverless NEG targeting the control-plane Cloud Run service.
resource "google_compute_region_network_endpoint_group" "control_plane" {
  count                 = local.create_lb ? 1 : 0
  name                  = "${local.prefix}-cp-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = google_cloud_run_v2_service.control_plane.name
  }
}

# Backend service wrapping the serverless NEG.
resource "google_compute_backend_service" "control_plane" {
  count                 = local.create_lb ? 1 : 0
  name                  = "${local.prefix}-cp-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  backend {
    group = google_compute_region_network_endpoint_group.control_plane[0].id
  }
}

# URL map routing all traffic to the control-plane backend.
resource "google_compute_url_map" "control_plane" {
  count           = local.create_lb ? 1 : 0
  name            = "${local.prefix}-cp-urlmap"
  default_service = google_compute_backend_service.control_plane[0].id
}

# Google-managed SSL certificate for the custom domain.
resource "google_compute_managed_ssl_certificate" "control_plane" {
  count = local.create_lb ? 1 : 0
  name  = "${local.prefix}-cp-cert"
  managed {
    domains = [var.domain]
  }
}

# Target HTTPS proxy binding the URL map to the managed certificate.
resource "google_compute_target_https_proxy" "control_plane" {
  count            = local.create_lb ? 1 : 0
  name             = "${local.prefix}-cp-https-proxy"
  url_map          = google_compute_url_map.control_plane[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.control_plane[0].id]
}

# Reserved global IP for the load balancer (also used for the optional DNS A record).
resource "google_compute_global_address" "control_plane" {
  count = local.create_lb ? 1 : 0
  name  = "${local.prefix}-cp-lb-ip"
}

# Global forwarding rule serving HTTPS (443) at the reserved global IP.
resource "google_compute_global_forwarding_rule" "control_plane" {
  count                 = local.create_lb ? 1 : 0
  name                  = "${local.prefix}-cp-fwd"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.control_plane[0].id
  ip_address            = google_compute_global_address.control_plane[0].id
  port_range            = "443"
}

# Optional Cloud DNS A record pointing the custom domain at the load balancer IP.
resource "google_dns_record_set" "control_plane" {
  count        = local.create_lb && var.create_dns_record && var.managed_zone != "" ? 1 : 0
  name         = "${var.domain}."
  type         = "A"
  ttl          = 300
  managed_zone = var.managed_zone
  rrdatas      = [google_compute_global_address.control_plane[0].address]
}
