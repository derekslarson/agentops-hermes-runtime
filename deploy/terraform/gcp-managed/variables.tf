###############################################################################
# AgentOps Hermes Runtime — GCP managed profile inputs (scaffold)
#
# Mirrors the aws-managed contract for the GCP equivalent resources. Raw
# app/integration secret VALUES are NOT inputs here; Terraform only creates
# Secret Manager secret containers, and bootstrap fills them.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to all created resource names. Must also fit GCP service-account account_id rules when '-runtime' is appended."
  type        = string
  default     = "agentops"

  validation {
    condition     = length(var.name_prefix) <= 22 && can(regex("^[a-z][a-z0-9-]*$", var.name_prefix))
    error_message = "name_prefix must be <=22 chars and contain only lowercase letters, numbers, and hyphens, starting with a letter, so '<prefix>-runtime' is a valid GCP service account id."
  }
}

variable "project" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "GCP region to deploy the managed profile into."
  type        = string
}

variable "domain" {
  description = "OPTIONAL custom domain for the AgentOps API/control-plane. Empty (default) keeps the Cloud Run control-plane service URI as the public API endpoint; set it together with enable_custom_domain to create a Cloud Run domain mapping. Non-secret."
  type        = string
  default     = ""
}

# --- Public API endpoint contract -------------------------------------------
#
# The control-plane has no provisioned load balancer / DNS, so the default
# public API endpoint is the Cloud Run service URI. These non-secret toggles
# opt into public unauthenticated access and an optional custom-domain mapping.

variable "enable_public_invoker" {
  description = "When true, grant allUsers roles/run.invoker on the CONTROL-PLANE Cloud Run service so its API is reachable without IAM auth. Default false keeps the service IAM-gated (private). Non-secret toggle; the worker/scheduler never get this binding."
  type        = bool
  default     = false
}

variable "enable_custom_domain" {
  description = "When true (and domain is set), create a Cloud Run domain mapping for the control-plane at var.domain. Default false uses the Cloud Run service URI as the endpoint. Non-secret toggle."
  type        = bool
  default     = false
}

# --- Container images --------------------------------------------------------
#
# Customer-supplied runtime container images for each Cloud Run service. Defaults
# are obvious placeholders meant to be overridden with the images the customer
# builds/pushes; they are NOT secret values.

variable "control_plane_image" {
  description = "Container image for the API / control-plane service (e.g. <registry>/hermes-control-plane:<tag>)."
  type        = string
  default     = "agentops/hermes-control-plane:replace-me"
}

variable "worker_image" {
  description = "Container image for the worker fleet service (e.g. <registry>/hermes-worker:<tag>)."
  type        = string
  default     = "agentops/hermes-worker:replace-me"
}

variable "scheduler_image" {
  description = "Container image for the scheduler service (e.g. <registry>/hermes-scheduler:<tag>)."
  type        = string
  default     = "agentops/hermes-scheduler:replace-me"
}

# --- Capacity ----------------------------------------------------------------

variable "desired_task_count" {
  description = "Number of worker Cloud Run instances the service runs (fleet size)."
  type        = number
  default     = 2
}

variable "max_concurrent_runs" {
  description = "Per-instance run-slot bound each worker advertises locally."
  type        = number
  default     = 2
}

variable "min_task_count" {
  description = "Autoscaling floor for the worker service."
  type        = number
  default     = 1
}

variable "max_task_count" {
  description = "Autoscaling ceiling for the worker service."
  type        = number
  default     = 10
}

# --- Bring-your-own-network --------------------------------------------------

variable "existing_vpc_id" {
  description = "Reuse an existing VPC network for Direct VPC egress (set together with existing_subnet_ids). When empty, services use Cloud Run default egress; creating a new private network is a documented parity gap (see README), not done here."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = "Reuse existing subnet self-links for Direct VPC egress (set together with existing_vpc_id). When empty, services use Cloud Run default egress; creating new subnets is a documented parity gap (see README), not done here."
  type        = list(string)
  default     = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_subnet_ids) > 0
    error_message = "existing_subnet_ids must be provided when existing_vpc_id is set (Direct VPC egress requires at least one subnet self-link)."
  }
}

# --- Bring-your-own-managed-resource ----------------------------------------

variable "existing_database_arn" {
  description = "Reuse an existing Cloud SQL/Postgres instance (connection name)."
  type        = string
  default     = ""
}

variable "existing_artifact_bucket" {
  description = "Reuse an existing GCS artifact bucket name. Empty creates a new bucket."
  type        = string
  default     = ""
}

variable "existing_secret_prefix" {
  description = "Reuse an existing Secret Manager name prefix. Empty creates new containers."
  type        = string
  default     = ""
}
