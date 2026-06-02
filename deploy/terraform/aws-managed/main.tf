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
# Scope/honesty: task definitions and customer-supplied container images are now
# wired to the services with the non-secret backend env contract. Public ingress
# (ALB + listener) and DNS for the API/control-plane are still left as a TODO
# below where a real deployment needs site-specific values, so an apply does not
# yet yield a reachable, production-complete stack unattended.
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

  # Effective backend refs the runtime containers read. Bring-your-own values
  # win when provided; otherwise the module-created resources are used.
  effective_artifact_bucket = local.create_artifact_store ? aws_s3_bucket.artifacts[0].bucket : var.existing_artifact_bucket
  effective_secret_prefix   = var.existing_secret_prefix != "" ? var.existing_secret_prefix : local.prefix

  # Non-secret runtime refs shared by all three services. These are container
  # ENV entries (names + refs), never raw secret values: the database entry
  # carries the ARN of the Secrets Manager container that bootstrap fills, not a
  # connection string.
  runtime_common_env = [
    { name = "AGENTOPS_RUNTIME_PROFILE", value = "aws-managed" },
    { name = "AGENTOPS_QUEUE_URL", value = aws_sqs_queue.runs.url },
    { name = "AGENTOPS_ARTIFACT_BUCKET", value = local.effective_artifact_bucket },
    { name = "AGENTOPS_SECRET_PREFIX", value = local.effective_secret_prefix },
    { name = "AGENTOPS_DATABASE_SECRET_ARN", value = aws_secretsmanager_secret.containers["database"].arn },
  ]

  log_config = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.this.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "runtime"
    }
  }
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

# Execution role needs CloudWatch Logs (awslogs driver) and ECR auth/pull for the
# customer images. The AWS-managed policy grants exactly this minimal set.
resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ARN of the effective artifact bucket: the module-created one, or the BYO bucket
# reconstructed from its name (BYO is supplied by name, not ARN).
locals {
  effective_artifact_bucket_arn = local.create_artifact_store ? aws_s3_bucket.artifacts[0].arn : "arn:aws:s3:::${local.effective_artifact_bucket}"
}

# Task role grants runtime tasks scoped access to the backend refs advertised in
# runtime_common_env: the runs queue, the artifact bucket + objects, and the
# Secrets Manager containers (read-only; bootstrap owns the raw values).
resource "aws_iam_role_policy" "task_runtime_backends" {
  name = "${local.prefix}-task-runtime-backends"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunsQueue"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = aws_sqs_queue.runs.arn
      },
      {
        Sid      = "ArtifactBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = local.effective_artifact_bucket_arn
      },
      {
        Sid    = "ArtifactObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${local.effective_artifact_bucket_arn}/*"
      },
      {
        Sid    = "SecretContainers"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [for s in aws_secretsmanager_secret.containers : s.arn]
      },
    ]
  })
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

# Task definitions. Each runs the customer-supplied runtime image with the
# shared non-secret backend env; the worker also advertises its run-slot bound.
resource "aws_ecs_task_definition" "control_plane" {
  family                   = "${local.prefix}-control-plane"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([{
    name             = "control-plane"
    image            = var.control_plane_image
    essential        = true
    environment      = local.runtime_common_env
    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([{
    name      = "worker"
    image     = var.worker_image
    essential = true
    environment = concat(local.runtime_common_env, [
      { name = "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS", value = tostring(var.max_concurrent_runs) },
    ])
    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_task_definition" "scheduler" {
  family                   = "${local.prefix}-scheduler"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn
  tags                     = local.common_tags

  container_definitions = jsonencode([{
    name             = "scheduler"
    image            = var.scheduler_image
    essential        = true
    environment      = local.runtime_common_env
    logConfiguration = local.log_config
  }])
}

# TODO(ingress): public ALB + listener and DNS for the control-plane are not yet
# wired. Until they are, the API/control-plane is not reachable at var.domain and
# an apply does not yield a working, internet-facing deployment.

# API / control-plane service.
resource "aws_ecs_service" "control_plane" {
  name            = "${local.prefix}-control-plane"
  cluster         = aws_ecs_cluster.this.id
  desired_count   = 1
  launch_type     = "FARGATE"
  task_definition = aws_ecs_task_definition.control_plane.arn
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
  task_definition = aws_ecs_task_definition.worker.arn
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
  task_definition = aws_ecs_task_definition.scheduler.arn
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
