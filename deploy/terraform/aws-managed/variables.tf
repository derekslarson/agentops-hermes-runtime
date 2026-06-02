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

# --- Optional custom-domain HTTPS / TLS --------------------------------------
#
# Both are OPTIONAL and non-secret. Leaving them empty (the default) keeps the
# ALB-DNS HTTP path: the public ALB serves on port 80 and URLs derive from the
# ALB DNS name. Supplying them opts into a custom-domain HTTPS path without
# changing the default behavior.

variable "acm_certificate_arn" {
  description = "Optional ACM certificate ARN for the custom domain. When set, an HTTPS listener on 443 is added to the existing ALB (forwarding to the same API target group). Empty keeps the HTTP-only default. This is a certificate REFERENCE, not a secret value."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional Route53 hosted zone id for var.domain. When set, an alias record pointing var.domain at the ALB is created. Empty leaves DNS for you to wire manually. Not a secret value."
  type        = string
  default     = ""
}

# --- Container images --------------------------------------------------------
#
# Customer-supplied runtime container images for each service. Defaults are
# obvious placeholders meant to be overridden with the images the customer
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

variable "api_container_port" {
  description = "TCP port the API / control-plane container listens on (target group + listener forward here). Not a secret."
  type        = number
  default     = 8080
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

variable "existing_alb_subnet_ids" {
  description = "Public/edge subnet IDs the internet-facing ALB attaches to. These are the public-routing subnets, distinct from the private service subnets in existing_subnet_ids. Leave empty ONLY when this module creates the VPC — it then creates public ALB subnets with IGW + default route for you. A bring-your-own VPC (existing_vpc_id set) MUST supply at least two public/edge subnets here, because the module does not add public routing to a VPC it did not create. An Application Load Balancer requires at least two subnets in different AZs, so when provided this must list at least two."
  type        = list(string)
  default     = []

  validation {
    # Valid modes are either fully module-created networking (empty VPC + empty
    # ALB subnets) or BYO VPC plus at least two BYO public/edge ALB subnets.
    condition     = (var.existing_vpc_id == "" && length(var.existing_alb_subnet_ids) == 0) || (var.existing_vpc_id != "" && length(var.existing_alb_subnet_ids) >= 2)
    error_message = "existing_alb_subnet_ids must be empty when this module creates the VPC; when existing_vpc_id is set, it must list at least two public/edge subnets in different AZs."
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
