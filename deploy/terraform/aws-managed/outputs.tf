###############################################################################
# AgentOps Hermes Runtime — AWS managed profile outputs
#
# Outputs surface the URLs/refs the customer needs to bootstrap and wire
# integrations. Secret values are never output — only the secret REFS (ARNs)
# of the containers that bootstrap fills in.
###############################################################################

locals {
  api_base_url = "https://${var.domain}"
}

output "agentops_api_url" {
  description = "Base URL of the AgentOps API / control-plane."
  value       = local.api_base_url
}

output "bootstrap_url" {
  description = "URL where the bootstrap UI/CLI activation flow is served."
  value       = "${local.api_base_url}/bootstrap"
}

output "bootstrap_token_secret_ref" {
  description = "Secrets Manager ref holding the bootstrap token (value written by bootstrap, not Terraform)."
  value       = aws_secretsmanager_secret.containers["bootstrap-token"].arn
}

# --- Integration webhook URLs ------------------------------------------------

output "slack_webhook_url" {
  description = "Slack events webhook URL to register in the Slack app config."
  value       = "${local.api_base_url}/webhooks/slack"
}

output "github_webhook_url" {
  description = "GitHub webhook URL to register in the GitHub app/repo config."
  value       = "${local.api_base_url}/webhooks/github"
}

output "linear_webhook_url" {
  description = "Linear webhook URL to register in the Linear integration config."
  value       = "${local.api_base_url}/webhooks/linear"
}

output "jira_webhook_url" {
  description = "Jira webhook URL to register in the Jira automation config."
  value       = "${local.api_base_url}/webhooks/jira"
}

# --- Backend refs ------------------------------------------------------------

output "secret_refs" {
  description = "Map of created Secrets Manager container ARNs (raw values written by bootstrap)."
  value       = { for name, secret in aws_secretsmanager_secret.containers : name => secret.arn }
}

output "queue_refs" {
  description = "SQS queue refs used by the runtime."
  value = {
    runs_queue_url = aws_sqs_queue.runs.url
    runs_queue_arn = aws_sqs_queue.runs.arn
  }
}

output "artifact_refs" {
  description = "Artifact store (S3) refs. Reuses existing bucket when provided."
  value = {
    bucket = local.create_artifact_store ? aws_s3_bucket.artifacts[0].bucket : var.existing_artifact_bucket
  }
}

output "database_refs" {
  description = "Postgres database refs the runtime/bootstrap connect to (reuses BYO database when provided)."
  value = {
    ref               = local.create_database ? aws_db_instance.this[0].arn : var.existing_database_arn
    address           = local.create_database ? aws_db_instance.this[0].address : null
    master_secret_arn = local.create_database ? aws_db_instance.this[0].master_user_secret[0].secret_arn : null
  }
}

output "network_refs" {
  description = "Effective VPC/subnet refs the services attach to (reuses BYO values when provided)."
  value = {
    vpc_id     = local.effective_vpc_id
    subnet_ids = local.effective_subnet_ids
  }
}

output "worker_service_name" {
  description = "ECS worker fleet service name."
  value       = aws_ecs_service.worker.name
}

output "smoke_test_hints" {
  description = "Commands/URLs to verify the deployment after bootstrap."
  value = {
    api_health = "curl ${local.api_base_url}/healthz"
    api_ready  = "curl ${local.api_base_url}/readyz"
    note       = "After bootstrap, send a test Slack/GitHub/Linear/Jira event and confirm a worker run is claimed from the runs queue."
  }
}
