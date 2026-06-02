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
> - **Custom DNS / TLS (optional)** — by default URLs derive from the ALB DNS
>   name over HTTP. You can opt into a custom-domain HTTPS path with the optional
>   `acm_certificate_arn` and `route53_zone_id` inputs (see **Optional
>   custom-domain HTTPS** below); leaving them empty keeps the ALB-HTTP default.
> - **Applied smoke test** — no end-to-end `apply` against a live AWS account has
>   been run/verified in this slice; treat the first real apply as the smoke test.

## Optional custom-domain HTTPS

The default path serves the API over **HTTP** at the ALB DNS name. To serve a
custom domain over **HTTPS**, set these two **optional**, non-secret inputs:

- `acm_certificate_arn` — an ACM certificate ARN for `domain`. When set, the
  module adds an **HTTPS listener on 443** to the existing ALB (terminating TLS
  with your certificate and forwarding to the same API target group), opens 443
  on the ALB security group, and the `agentops_api_url` output flips to
  `https://<domain>`. This is a certificate **reference**, not a secret value.
- `route53_zone_id` — a Route53 hosted zone id for `domain`. When set, the module
  creates an **alias record** pointing `domain` at the ALB DNS/zone.

Leave both empty (the default) to stay on the ALB-DNS HTTP path. The raw ALB HTTP
URL is always available via the `alb_http_url` output regardless of these inputs.
The effective `agentops_api_url`, `bootstrap_url`, and webhook URLs derive from
the HTTPS custom domain when a certificate is configured, otherwise from the ALB
HTTP URL.

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

## Live-smoke helper (`smoke.sh`)

`smoke.sh` is a module-local helper that an operator runs from inside this
directory after configuring `terraform.tfvars` and AWS credentials. It **fails
closed before any side effect** when neither the `terraform` nor `tofu`
(OpenTofu) CLI is available, or when AWS credentials/config are unavailable
(verified with `aws sts get-caller-identity`). It uses the same module-local
commands as above (no root-relative `-chdir`), auto-detecting whichever CLI is
installed.

It defaults to a safe **plan-only** mode (`PLAN_ONLY=1`): `init` + `validate` +
`plan` with no apply. Set `PLAN_ONLY=0` to also `apply`, then **probe the live
API** and print the post-apply smoke hints/outputs (`agentops_api_url`,
`smoke_test_hints`). On the apply path the helper fetches the bare
`agentops_api_url` output and curls `${agentops_api_url}/healthz`, **failing
closed (non-zero)** if the endpoint is unhealthy or unreachable — so a live smoke
transcript proves the provisioned API responds. `curl` is required on the apply
path (the helper fails clearly before applying if it is missing); the plan-only
default never probes and so never needs `curl`.

```bash
cd deploy/terraform/aws-managed
cp terraform.tfvars.example terraform.tfvars   # edit account/region/capacity
./smoke.sh                 # PLAN_ONLY=1 (default): init + validate + plan
PLAN_ONLY=0 ./smoke.sh     # apply, probe ${agentops_api_url}/healthz, print outputs
```

Before a `PLAN_ONLY=0` apply/smoke you must replace the placeholder
`:replace-me` container images (`control_plane_image`, `worker_image`,
`scheduler_image`) with real image references — the apply path inspects the
effective image values and fails closed if any placeholder remains.

The helper never accepts or echoes raw app/integration secret values; bootstrap
(M16) owns those. **No live AWS apply/smoke has been captured for this module
yet** — treat the first real `PLAN_ONLY=0 ./smoke.sh` against an account as the
smoke test.

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

- `agentops_api_url` (effective — HTTPS custom domain when configured, else ALB HTTP), `alb_http_url` (raw ALB HTTP URL)
- `bootstrap_url`, `bootstrap_token_secret_ref`
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
public/edge subnets via `existing_alb_subnet_ids`. Custom-domain HTTPS is
**optional**: supply `acm_certificate_arn` (adds a 443 HTTPS/TLS listener) and/or
`route53_zone_id` (aliases `domain` at the ALB); with both empty the deployment
stays on the ALB-DNS HTTP path. An applied end-to-end smoke test against a live
AWS account remains deferred and requires site-specific values; fill that in for
a production-complete deployment.
