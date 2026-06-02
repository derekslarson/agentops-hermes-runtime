# AgentOps Hermes Runtime — AWS managed profile (Terraform/OpenTofu)

This module packages the `aws-managed` backend profile so a customer can edit
account/region/domain/capacity settings and apply Terraform or OpenTofu. It
packages the resource topology and the variables/outputs contract; see the
completeness caveat under **Apply** before treating an apply as a finished
install.

## What it provisions

- API / control-plane ECS/Fargate service
- Worker fleet ECS/Fargate service (Application Auto Scaling)
- Scheduler ECS/Fargate service
- RDS Postgres database
- SQS runs queue
- S3 artifact store
- Secrets Manager secret **containers** (no raw values)
- CloudWatch log group
- IAM task/execution roles

## Apply

> **Completeness caveat:** Running `apply` provisions the resource skeleton but
> **does not yield a working deployment** while `main.tf` still carries `TODO`
> task definitions and container image references. Fill those in (and the ALB/DNS
> and private-networking wiring noted under **Scope**) before expecting running
> services.

All commands run from inside this module directory. With Terraform:

```bash
cd deploy/terraform/aws-managed
cp terraform.tfvars.example terraform.tfvars
# Edit account/region/domain/capacity values.
terraform init
terraform plan
terraform apply
```

OpenTofu is a drop-in replacement — substitute `tofu` for `terraform`:

```bash
cd deploy/terraform/aws-managed
tofu init
tofu plan
tofu apply
```

## Bring your own network / managed resources

Set the `existing_*` variables to reuse infrastructure instead of creating new
resources:

- `existing_vpc_id`, `existing_subnet_ids` — reuse an existing VPC/subnets. A
  bring-your-own VPC **requires** bring-your-own subnets: if `existing_vpc_id`
  is set you must also set `existing_subnet_ids` (enforced by a variable
  validation). Subnets are only created by this module when it also creates the
  VPC (both left empty).
- `existing_database_arn` — reuse an existing RDS/Postgres database. Whether
  created or reused, the database is surfaced through the `database_refs` output.
- `existing_artifact_bucket` — reuse an existing S3 bucket.
- `existing_secret_prefix` — reuse an existing Secrets Manager name prefix.

Leave them empty (the default) to have this module create new resources.

## Secret handling — raw values stay out of state

This is the critical safety property of the recommended path. Terraform creates
**empty Secrets Manager containers/refs only**. It never accepts a raw
`slack_bot_token`, `github_token`, `model_provider_api_key`, or similar value as
an input variable, and never writes a secret *version* with a raw value. As a
result, no Slack/GitHub/Linear/Jira/model-provider raw secret value is written
into Terraform/OpenTofu state.

Bootstrap (M16) owns those raw values and writes them directly into the secret
backend after `apply`, using the `secret_refs` / `bootstrap_token_secret_ref`
outputs. Do not put raw app/integration secret values in `terraform.tfvars`.

## Outputs

After apply, `terraform output` (or `tofu output`) surfaces:

- `agentops_api_url`, `bootstrap_url`, `bootstrap_token_secret_ref`
- `slack_webhook_url`, `github_webhook_url`, `linear_webhook_url`, `jira_webhook_url`
- `secret_refs`, `queue_refs`, `artifact_refs`
- `database_refs`, `network_refs`
- `worker_service_name`
- `smoke_test_hints`

## Scope

This slice packages the profile topology with the variables/outputs contract.
Container image references, ALB/DNS wiring, and full networking are marked TODO
in `main.tf` where they require site-specific values; fill those in for a
production-complete deployment.
