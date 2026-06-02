###############################################################################
# AgentOps Hermes Runtime — GCP managed profile outputs (scaffold)
#
# Mirrors the aws-managed outputs contract. Secret values are never output —
# only secret refs of the containers bootstrap fills in.
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
  description = "Secret Manager ref holding the bootstrap token (value written by bootstrap)."
  value       = google_secret_manager_secret.containers["bootstrap-token"].id
}

output "slack_webhook_url" {
  description = "Slack events webhook URL."
  value       = "${local.api_base_url}/webhooks/slack"
}

output "github_webhook_url" {
  description = "GitHub webhook URL."
  value       = "${local.api_base_url}/webhooks/github"
}

output "linear_webhook_url" {
  description = "Linear webhook URL."
  value       = "${local.api_base_url}/webhooks/linear"
}

output "jira_webhook_url" {
  description = "Jira webhook URL."
  value       = "${local.api_base_url}/webhooks/jira"
}

output "secret_refs" {
  description = "Map of created Secret Manager container refs (raw values written by bootstrap)."
  value       = { for name, secret in google_secret_manager_secret.containers : name => secret.id }
}

output "queue_refs" {
  description = "Pub/Sub queue refs used by the runtime."
  value = {
    runs_topic        = google_pubsub_topic.runs.id
    runs_subscription = google_pubsub_subscription.runs.id
  }
}

output "artifact_refs" {
  description = "Artifact store (GCS) refs. Reuses existing bucket when provided."
  value = {
    bucket = local.create_artifact_store ? google_storage_bucket.artifacts[0].name : var.existing_artifact_bucket
  }
}

output "database_refs" {
  description = "Cloud SQL database refs the runtime/bootstrap connect to (reuses BYO database when provided)."
  value = {
    ref             = local.create_database ? google_sql_database_instance.this[0].id : var.existing_database_arn
    connection_name = local.create_database ? google_sql_database_instance.this[0].connection_name : var.existing_database_arn
  }
}

output "network_refs" {
  description = "Effective VPC/subnet refs the services attach to (empty unless BYO network is provided)."
  value = {
    vpc_id     = local.effective_vpc_id
    subnet_ids = local.effective_subnet_ids
  }
}

output "worker_service_name" {
  description = "Cloud Run worker fleet service name."
  value       = google_cloud_run_v2_service.worker.name
}

output "smoke_test_hints" {
  description = "Commands/URLs to verify the deployment after bootstrap."
  value = {
    api_health = "curl ${local.api_base_url}/healthz"
    api_ready  = "curl ${local.api_base_url}/readyz"
    note       = "After bootstrap, send a test Slack/GitHub/Linear/Jira event and confirm a worker run is claimed from the Pub/Sub subscription."
  }
}
