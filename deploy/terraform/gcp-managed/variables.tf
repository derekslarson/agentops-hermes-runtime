###############################################################################
# AgentOps Hermes Runtime — GCP managed profile inputs (scaffold)
#
# Mirrors the aws-managed contract for the GCP equivalent resources. Raw
# app/integration secret VALUES are NOT inputs here; Terraform only creates
# Secret Manager secret containers, and bootstrap fills them.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to all created resource names."
  type        = string
  default     = "agentops"
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
  description = "Public domain for the AgentOps API/control-plane."
  type        = string
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
