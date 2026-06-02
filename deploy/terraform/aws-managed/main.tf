###############################################################################
# AgentOps Hermes Runtime — AWS managed profile (M15)
#
# Provisions the aws-managed backend profile topology:
#   * API / control-plane ECS/Fargate service
#   * worker fleet ECS/Fargate service (autoscaled)
#   * scheduler ECS/Fargate service
#   * RDS Postgres database
#   * SQS queue
#   * S3 artifact store
#   * Secrets Manager secret CONTAINERS (no raw values — bootstrap fills these)
#   * CloudWatch log group
#   * IAM task/execution roles
#   * Application Auto Scaling for the worker service
#
# Scope/honesty: this is the packaging scaffold. Resource shapes are wired with
# the variables/outputs contract; some attributes (image, container defs, ALB
# routing, networking) are left as TODO markers where a real deployment needs
# site-specific values. It is meant to be edited and applied, not to provision a
# production-complete stack unattended.
###############################################################################

terraform {
  required_version = ">= 1.9" # cross-variable validation (existing_subnet_ids requires existing_vpc_id)
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  prefix = var.name_prefix

  common_tags = merge({
    "app"     = "agentops-hermes-runtime"
    "profile" = "aws-managed"
  }, var.tags)

  create_vpc            = var.existing_vpc_id == ""
  create_subnets        = local.create_vpc && length(var.existing_subnet_ids) == 0
  create_database       = var.existing_database_arn == ""
  create_artifact_store = var.existing_artifact_bucket == ""

  # Effective network the runtime services attach to. Bring-your-own values win
  # when provided; otherwise the module's created VPC/subnets are used.
  effective_vpc_id     = local.create_vpc ? aws_vpc.this[0].id : var.existing_vpc_id
  effective_subnet_ids = local.create_subnets ? aws_subnet.this[*].id : var.existing_subnet_ids

  # Secret CONTAINERS created by Terraform. Raw values are written by bootstrap
  # (M16) into these refs — never passed as Terraform inputs or stored in state.
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

# --- Networking (bring-your-own-network supported) --------------------------

resource "aws_vpc" "this" {
  count      = local.create_vpc ? 1 : 0
  cidr_block = "10.20.0.0/16"
  tags       = merge(local.common_tags, { "Name" = "${local.prefix}-vpc" })
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Subnets are created only when this module also creates the VPC. A
# bring-your-own VPC is expected to come with bring-your-own subnets via
# var.existing_subnet_ids.
resource "aws_subnet" "this" {
  count             = local.create_subnets ? 2 : 0
  vpc_id            = aws_vpc.this[0].id
  cidr_block        = cidrsubnet(aws_vpc.this[0].cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = merge(local.common_tags, { "Name" = "${local.prefix}-subnet-${count.index}" })
}

# --- Logs --------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/${local.prefix}/runtime"
  retention_in_days = 30
  tags              = local.common_tags
}

# --- IAM ---------------------------------------------------------------------

resource "aws_iam_role" "task_execution" {
  name = "${local.prefix}-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.common_tags
}

resource "aws_iam_role" "task" {
  name = "${local.prefix}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.common_tags
}

# --- Managed Postgres (RDS) — bring-your-own supported ----------------------

resource "aws_db_instance" "this" {
  count             = local.create_database ? 1 : 0
  identifier        = "${local.prefix}-postgres"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.db_instance_class
  allocated_storage = 20
  db_name           = "agentops"
  username          = "agentops"
  # Password is generated/managed by RDS-managed master credentials and surfaced
  # through Secrets Manager; it is NOT a Terraform input.
  manage_master_user_password = true
  skip_final_snapshot         = true
  tags                        = local.common_tags
}

# --- Queue (SQS) -------------------------------------------------------------

resource "aws_sqs_queue" "runs" {
  name = "${local.prefix}-runs"
  tags = local.common_tags
}

# --- Artifact store (S3) — bring-your-own supported -------------------------

resource "aws_s3_bucket" "artifacts" {
  count  = local.create_artifact_store ? 1 : 0
  bucket = "${local.prefix}-artifacts"
  tags   = local.common_tags
}

# --- Secret CONTAINERS (no raw values) --------------------------------------
#
# Only the container/ref is created here. The recommended path intentionally
# creates no secret-version resource holding a raw value, so
# Slack/GitHub/Linear/Jira/model-provider secrets never enter Terraform state.

resource "aws_secretsmanager_secret" "containers" {
  for_each = toset(local.secret_names)
  name     = var.existing_secret_prefix != "" ? "${var.existing_secret_prefix}/${each.key}" : "${local.prefix}/${each.key}"
  tags     = local.common_tags
}

# --- ECS cluster + services --------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = "${local.prefix}-cluster"
  tags = local.common_tags
}

# API / control-plane service.
resource "aws_ecs_service" "control_plane" {
  name            = "${local.prefix}-control-plane"
  cluster         = aws_ecs_cluster.this.id
  desired_count   = 1
  launch_type     = "FARGATE"
  task_definition = "TODO-control-plane-task-definition" # set after building the API/control-plane image
  tags            = local.common_tags

  network_configuration {
    subnets          = local.effective_subnet_ids
    assign_public_ip = false
  }
}

# Worker fleet service.
resource "aws_ecs_service" "worker" {
  name            = "${local.prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  desired_count   = var.desired_task_count
  launch_type     = "FARGATE"
  task_definition = "TODO-worker-task-definition" # worker reads AGENTOPS_WORKER_MAX_CONCURRENT_RUNS=${var.max_concurrent_runs}
  tags            = local.common_tags

  network_configuration {
    subnets          = local.effective_subnet_ids
    assign_public_ip = false
  }
}

# Scheduler service.
resource "aws_ecs_service" "scheduler" {
  name            = "${local.prefix}-scheduler"
  cluster         = aws_ecs_cluster.this.id
  desired_count   = 1
  launch_type     = "FARGATE"
  task_definition = "TODO-scheduler-task-definition"
  tags            = local.common_tags

  network_configuration {
    subnets          = local.effective_subnet_ids
    assign_public_ip = false
  }
}

# --- Autoscaling for the worker service -------------------------------------

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.max_task_count
  min_capacity       = var.min_task_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  name               = "${local.prefix}-worker-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 60
  }
}
