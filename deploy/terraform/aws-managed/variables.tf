###############################################################################
# AgentOps Hermes Runtime — AWS managed profile inputs
#
# Customers edit account/region/domain/capacity here. Bring-your-own-network
# and bring-your-own-managed-resource refs let you reuse an existing VPC,
# database, bucket, or secret namespace instead of creating new ones.
#
# Raw app/integration secret VALUES are intentionally NOT inputs here. The
# Terraform layer only creates empty secret containers/refs; bootstrap (M16)
# writes the real Slack/GitHub/Linear/Jira/model-provider values into them.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to all created resource names."
  type        = string
  default     = "agentops"
}

variable "region" {
  description = "AWS region to deploy the managed profile into."
  type        = string
}

variable "domain" {
  description = "Public domain for the AgentOps API/control-plane (e.g. agentops.example.com)."
  type        = string
}

variable "tags" {
  description = "Extra tags applied to all created resources."
  type        = map(string)
  default     = {}
}

# --- Capacity ----------------------------------------------------------------

variable "desired_task_count" {
  description = "Number of worker ECS tasks the service runs (fleet size)."
  type        = number
  default     = 2
}

variable "max_concurrent_runs" {
  description = "Per-task run-slot bound each worker advertises locally."
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
  description = "Reuse an existing VPC. Empty string creates a new one."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = "Private subnet IDs the services run in. Required when existing_vpc_id is set (bring-your-own VPC requires bring-your-own subnets). Leave empty only when existing_vpc_id is also empty, in which case the module creates a new VPC and subnets."
  type        = list(string)
  default     = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_subnet_ids) > 0
    error_message = "existing_subnet_ids must be provided when existing_vpc_id is set (bring-your-own VPC requires bring-your-own subnets)."
  }
}

# --- Bring-your-own-managed-resource ----------------------------------------

variable "existing_database_arn" {
  description = "Reuse an existing RDS/Postgres database (ARN). Empty creates a new instance."
  type        = string
  default     = ""
}

variable "existing_artifact_bucket" {
  description = "Reuse an existing S3 artifact bucket name. Empty creates a new bucket."
  type        = string
  default     = ""
}

variable "existing_secret_prefix" {
  description = "Reuse an existing Secrets Manager name prefix. Empty creates new secret containers."
  type        = string
  default     = ""
}

# --- Database sizing ---------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class for the managed Postgres database."
  type        = string
  default     = "db.t4g.medium"
}
