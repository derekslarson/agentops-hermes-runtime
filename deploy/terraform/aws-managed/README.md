# AgentOps Hermes Runtime — AWS managed profile (Terraform/OpenTofu)

This module packages the `aws-managed` backend profile so a customer can edit
account/region/domain/capacity settings and apply Terraform or OpenTofu. It
packages the resource topology and the variables/outputs contract; see the
remaining-gaps caveat under **Apply** before treating an apply as a fully
production-finished install.

## What it provisions

- API / control-plane ECS/Fargate service + task definition
- Public Application Load Balancer (target group + HTTP listener) fronting the
  control-plane, with ALB and service security groups. When the module creates
  the VPC it also creates **public ALB subnets** with an Internet Gateway and a
  public default route, so a default apply can stand up an internet-facing ALB
- Worker fleet ECS/Fargate service + task definition (Application Auto Scaling)
- Scheduler ECS/Fargate service + task definition
- RDS Postgres database
- SQS runs queue
- S3 artifact store
- Secrets Manager secret **containers** (no raw values)
- CloudWatch log group
- IAM task/execution roles

## Apply

> **Remaining-gaps caveat:** ECS task definitions, container images, and a public
> ALB (target group + HTTP listener) fronting the control-plane are wired, so an
> apply **provisions** an ALB DNS endpoint (see the `agentops_api_url` output).
> When this module creates the VPC it also creates **public ALB subnets** (IGW +
> public route), so a default apply stands up an internet-facing ALB. What is
> still **deferred** and needs site-specific work before this is
> production-complete:
>
> - **Public ALB routing for a BYO VPC** — when the module creates the VPC it
>   creates public ALB subnets with an Internet Gateway and a default route, so
>   the default apply is reachable. With a **bring-your-own VPC** you must supply
>   at least two **public** subnets via `existing_alb_subnet_ids`; the module does
>   not add public routing to a VPC it did not create.
> - **Custom DNS** — pointing `domain` at the ALB (e.g. a Route53 alias record)
>   is not created here; URLs derive from the ALB DNS name, not `domain`.
> - **TLS/ACM** — the listener is HTTP-only (port 80). HTTPS needs an ACM
>   certificate and a 443 listener.
> - **Applied smoke test** — no end-to-end `apply` against a live AWS account has
>   been run/verified in this slice; treat the first real apply as the smoke test.

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
  VPC (both left empty). These are the **private service subnets** the ECS tasks
  run in.
- `existing_alb_subnet_ids` — the **public/edge subnets** the internet-facing ALB
  attaches to, distinct from the private service subnets above. When this module
  creates the VPC (both `existing_vpc_id` and this list left empty) it creates
  public ALB subnets for you, with an Internet Gateway and a public default route.
  With a **bring-your-own VPC** (`existing_vpc_id` set) you **must** supply at
  least two **public** subnets here — the module does not add public routing to a
  VPC it did not create, so a BYO VPC needs its own public ALB subnets (enforced
  by a variable validation that also requires at least two entries, since an
  Application Load Balancer needs two subnets in different AZs).
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

This slice packages the profile topology with the variables/outputs contract,
wires ECS task definitions to the customer-supplied container images
(`control_plane_image`, `worker_image`, `scheduler_image`) with the non-secret
backend env (`AGENTOPS_RUNTIME_PROFILE`, queue/artifact/secret-prefix/database
refs, and the worker's `AGENTOPS_WORKER_MAX_CONCURRENT_RUNS`), and fronts the
control-plane with a public ALB (target group + HTTP listener on
`api_container_port`). When the module creates the VPC it also creates public ALB
subnets with an Internet Gateway and a public default route, so a default apply
stands up an internet-facing ALB; a bring-your-own VPC instead supplies its own
public/edge subnets via `existing_alb_subnet_ids`. Custom DNS for `domain`,
TLS/ACM (the listener is HTTP-only), and an applied end-to-end smoke test remain
deferred and require site-specific values; fill those in for a
production-complete deployment.
