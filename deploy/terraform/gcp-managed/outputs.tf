###############################################################################
# AgentOps Hermes Runtime — GCP managed profile outputs (scaffold)
#
# Mirrors the aws-managed outputs contract. Secret values are never output —
# only secret refs of the containers bootstrap fills in.
###############################################################################

locals {
  # The default public API endpoint is the Cloud Run control-plane service URI.
  # When var.enable_load_balancer_custom_domain is off, operators may keep that
  # Cloud Run URI or use the lightweight Cloud Run domain mapping. Reachability
  # still requires public unauthenticated access (var.enable_public_invoker);
  # otherwise the service is IAM-gated.
  cloud_run_api_url = google_cloud_run_v2_service.control_plane.uri

  # Effective API URL the customer hands out. When a custom domain is explicitly
  # configured — either the opt-in External HTTPS Load Balancer
  # (var.enable_load_balancer_custom_domain) or the lightweight Cloud Run domain
  # mapping (var.enable_custom_domain), both terminated at var.domain — the
  # effective URL is the custom HTTPS domain; otherwise it stays the Cloud Run
  # service URI. Bootstrap and webhook URLs derive from this base.
  api_base_url = (
    (var.enable_load_balancer_custom_domain || var.enable_custom_domain) && var.domain != ""
    ? "https://${var.domain}"
    : local.cloud_run_api_url
  )
}

output "agentops_api_url" {
  description = "Effective base URL of the AgentOps API / control-plane (custom HTTPS domain when configured, otherwise the Cloud Run service URI)."
  value       = local.api_base_url
}

output "cloud_run_api_url" {
  description = "Raw Cloud Run control-plane service URI (always available, regardless of the optional custom domain)."
  value       = local.cloud_run_api_url
}

output "load_balancer_ip" {
  description = "Reserved global IP of the opt-in External HTTPS Load Balancer fronting the control-plane (empty when enable_load_balancer_custom_domain is off). Point var.domain at this address if create_dns_record is not used."
  value       = local.create_lb ? google_compute_global_address.control_plane[0].address : ""
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
  description = "Effective VPC/subnet refs the Cloud Run services attach to via Direct VPC egress. module_created is true when the module created the private network/subnet (no BYO VPC), false when bring-your-own refs are used."
  value = {
    vpc_id         = local.effective_vpc_id
    subnet_ids     = local.effective_subnet_ids
    module_created = local.create_vpc
  }
}

output "worker_service_name" {
  description = "Cloud Run worker fleet service name."
  value       = google_cloud_run_v2_service.worker.name
}

output "smoke_test_hints" {
  description = "Commands/URLs to verify the deployment after bootstrap."
  value = {
    api_health    = "curl ${local.api_base_url}/healthz"
    api_ready     = "curl ${local.api_base_url}/readyz"
    endpoint_note = "The default API endpoint is the Cloud Run control-plane service URI (cloud_run_api_url). It is reachable without IAM auth only when enable_public_invoker = true (allUsers roles/run.invoker on the control-plane); otherwise the service is IAM-gated."
    domain_note   = "A custom domain is OPTIONAL via two paths: a lightweight Cloud Run domain mapping (enable_custom_domain) or an opt-in External HTTPS Load Balancer (enable_load_balancer_custom_domain + domain) terminated by a Google-managed SSL certificate, with an OPTIONAL Cloud DNS A record (create_dns_record + managed_zone) pointing at load_balancer_ip. With both unset the endpoint stays the Cloud Run service URI."
    lb_note       = "When the optional load balancer is enabled, point your DNS at the load_balancer_ip output (or set create_dns_record + managed_zone). The Google-managed SSL certificate provisions only after DNS resolves to that IP, so the HTTPS endpoint becomes healthy a few minutes after DNS propagates."
    smoke_note    = "A live PLAN_ONLY=0 GCP apply/smoke against a real project is still pending — treat the first real apply as the smoke test."
    note          = "After bootstrap, send a test Slack/GitHub/Linear/Jira event and confirm a worker run is claimed from the Pub/Sub subscription."
  }
}
