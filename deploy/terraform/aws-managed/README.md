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
  creates an **alias record** pointing `domain` at the ALB DNS/zone. Set **without**
  `acm_certificate_arn` (route53 **only**), the custom domain is served over **HTTP**
  at `http://<domain>` (the alias points at the existing HTTP listener — there is no
  TLS until you also supply a certificate).

The effective `agentops_api_url` (and the `bootstrap_url`/webhook URLs derived from
it) follows the three cases honestly:

- `acm_certificate_arn` set → `https://<domain>` (HTTPS listener on 443).
- `route53_zone_id` set, no certificate → `http://<domain>` (Route53 alias over the
  HTTP listener — HTTP, not HTTPS).
- neither set (the default) → the ALB-DNS HTTP URL.

The raw ALB HTTP URL is always available via the `alb_http_url` output regardless
of these inputs.

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

## Image-publishing helper (`publish-images.sh`)

`publish-images.sh` is a module-local helper that builds, tags, and pushes the
three runtime container images (control-plane / worker / scheduler) to Amazon
ECR and prints the non-secret `terraform.tfvars` image lines you paste before a
`PLAN_ONLY=0 ./smoke.sh` apply. It is the supported path to produce **real**
image references that replace the `:replace-me` placeholders without ever
handing app/integration secrets to Terraform.

It is **side-effect-safe by default**: with `DRY_RUN=1` (the default) it only
**prints** the `docker`/`aws` commands it would run — it never builds, logs in,
tags, pushes, creates ECR repositories, or modifies Terraform vars. Set
`DRY_RUN=0` for a live publish; the live path **fails closed before any side
effect** if the `aws` or `docker` CLI is missing or AWS credentials are
unavailable (verified with `aws sts get-caller-identity`). Inputs are env-only
and non-secret: `AWS_REGION` (falls back to `AWS_DEFAULT_REGION`),
`AWS_ACCOUNT_ID` (autodetected via `aws sts` when unset), `IMAGE_TAG` (default:
UTC timestamp), and the `*_REPO` repository names (defaulting to the
`agentops-hermes-runtime/*` ECR naming). The `*_CONTEXT` docker build contexts
(`CONTROL_PLANE_CONTEXT` / `WORKER_CONTEXT` / `SCHEDULER_CONTEXT`) default to the
**repo root** (`../../..` from this module dir, where the `Dockerfile` lives —
the helper `cd`s into the module dir, so they must not default to `.`) and are
overridable via env.

On a live publish (`DRY_RUN=0`) the helper **preflights each build context
before any ECR login, repository creation, or `docker build`/`push`**: every
`*_CONTEXT` must be an existing directory containing a `Dockerfile`, or the run
fails closed with a non-secret error naming the offending variable. Any
`*_CONTEXT` override must therefore point at a build-context directory that
contains a `Dockerfile`. The default dry-run skips this check and stays
permissive.

```bash
cd deploy/terraform/aws-managed
./publish-images.sh                       # DRY_RUN=1 (default): print commands only
DRY_RUN=0 AWS_REGION=us-east-1 ./publish-images.sh   # live build/tag/push to ECR
```

After a live publish it prints three lines like
`control_plane_image = "…"` / `worker_image = "…"` / `scheduler_image = "…"`.
Copy them into `terraform.tfvars`, then run `PLAN_ONLY=0 ./smoke.sh` to apply
with real images (the smoke helper refuses to apply while any `:replace-me`
placeholder remains).

To skip the hand-copy step, set `WRITE_TFVARS=1` on the live path: in addition
to printing the lines, the helper writes **only** those three non-secret image
assignments to a module-local tfvars override (default `image.auto.tfvars`,
overridable via `IMAGE_TFVARS_PATH`) **atomically** — building into a
`mktemp "${dest}.XXXXXX"` temp file and renaming it into place only once fully
written, so a failure never leaves a partial override. Terraform auto-loads
any `*.auto.tfvars` file, so the next `PLAN_ONLY=0 ./smoke.sh` picks the images up
with no manual edit. The writer is opt-in and **never runs in the default
`DRY_RUN=1` mode** (dry-run stays side-effect-free), writes no secret values, and
both the generated file and any interrupted `*.auto.tfvars.XXXXXX` temp artifact
are git-ignored. If the write is interrupted (a `HUP`/`INT`/`TERM` signal or an
unexpected exit) between the `mktemp` temp file and the rename, the helper
removes the partial `*.auto.tfvars.XXXXXX` temp file and fails closed (exiting
non-zero rather than continuing), so no half-written override is ever left
behind. A custom `IMAGE_TFVARS_PATH` must stay a
module-local plain filename (no leading `/`, no `/` path segments, no `..`); the
helper fails closed before publishing otherwise, so the writer can never clobber
files outside this module.

```bash
DRY_RUN=0 WRITE_TFVARS=1 AWS_REGION=us-east-1 ./publish-images.sh   # also writes image.auto.tfvars
```

## Live-smoke helper (`smoke.sh`)

`smoke.sh` is a module-local helper that an operator runs from inside this
directory after configuring `terraform.tfvars` and AWS credentials. It **fails
closed before any side effect** when neither the `terraform` nor `tofu`
(OpenTofu) CLI is available, or when AWS credentials/config are unavailable
(verified with `aws sts get-caller-identity`). It uses the same module-local
commands as above (no root-relative `-chdir`), auto-detecting whichever CLI is
installed.

It also **preflights the required non-secret inputs** (`region`, `domain`)
before any Terraform/OpenTofu side effect, so an unconfigured run fails closed
without creating `.terraform/` or a lock file (rather than erroring at plan time
after `init`). Satisfy the check either with a `terraform.tfvars` /
`*.auto.tfvars` file that defines each required input, or with `TF_VAR_region` /
`TF_VAR_domain` environment variables. These are non-secret account/location
inputs only; raw app/integration secrets remain a bootstrap (M16) concern.

It defaults to a safe **plan-only** mode (`PLAN_ONLY=1`): `init` + `validate` +
`plan` with no apply. Set `PLAN_ONLY=0` to also `apply`, then **probe the live
API** and print the post-apply smoke hints/outputs (`agentops_api_url`,
`smoke_test_hints`). On the apply path the helper fetches the bare
`agentops_api_url` output and curls `${agentops_api_url}/healthz`, **failing
closed (non-zero)** if the endpoint is unhealthy or unreachable — so a live smoke
transcript proves the provisioned API responds. `curl` is required on the apply
path (the helper fails clearly before applying if it is missing); the plan-only
default never probes and so never needs `curl`.

Optionally set `DESTROY_ON_FAILURE=1` to add a cleanup guard on the apply path:
if `apply` **succeeds** but the `/healthz` probe then **fails**, the helper runs
`terraform/tofu destroy -auto-approve -input=false` (through the detected CLI — no
cloud SDK) before exiting non-zero, so a failed live smoke does not leave a
half-provisioned stack behind. It defaults to `0` (**no destroy**, preserving the
existing behavior) and never destroys on a plan-only run, before a successful
apply, or on a preflight failure (missing inputs, placeholder `:replace-me`
images). Leave it unset unless you explicitly want failed-smoke cleanup.

```bash
PLAN_ONLY=0 DESTROY_ON_FAILURE=1 ./smoke.sh   # apply, then destroy if /healthz fails
```

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

On the `PLAN_ONLY=0` apply path, after a healthy `/healthz` probe, the helper
also writes a module-local **non-secret smoke transcript** (`smoke-transcript-<UTC
timestamp>.log`) recording the provider/profile, a UTC timestamp, the effective
`agentops_api_url`, the `/healthz` success, and the `smoke_test_hints` output. The
helper prints the transcript path so you can attach it to the still-pending M15
live-smoke evidence before marking the milestone Done. The transcript is produced
**only** after a `PLAN_ONLY=0` apply with a healthy `/healthz` probe — never in
the plan-only default. Although the script writes only non-secret outputs,
**review the transcript for secrets before sharing it**. The transcript is
written **atomically** — built in a `${TRANSCRIPT}.tmp` (`smoke-transcript-<UTC
timestamp>.log.tmp`) temporary file and renamed into place only once fully
written. The partial `.tmp` file is removed if the transcript build command fails
**or if the run is interrupted**: the `EXIT` handler removes it on normal exit,
and the `HUP`/`INT`/`TERM` handlers remove it and then **terminate the run
non-zero** (128+signal, i.e. 129/130/143) so an interrupted smoke fails closed
instead of continuing. The final transcript is written **only after** the atomic
`mv` succeeds (which also clears the cleanup traps), so a partial transcript is
never left behind or committed as evidence if `terraform output` errors mid-write
or the run is cancelled.

The helper never accepts or echoes raw app/integration secret values; bootstrap
(M16) owns those. **No live AWS apply/smoke has been captured for this module
yet** — treat the first real `PLAN_ONLY=0 ./smoke.sh` against an account as the
smoke test.

A smoke/plan run also leaves module-local Terraform/OpenTofu artifacts here — the
`.terraform/` working dir and any `*.tfstate`/`*.tfplan`/`crash.log` files. These
hold infrastructure details and may carry sensitive values, so they are
operator-local and must not be committed (the root `.gitignore` already ignores
them).

Optionally set `CLEAN_TERRAFORM_ARTIFACTS=1` to have the helper clean up its own
scratch after a **successful plan-only run**: it removes only those local
working artifacts (`.terraform/`, `.terraform.lock.hcl`, `*.tfstate[.*]`,
`*.tfplan`, `crash.log`/`crash.*.log`) and never touches source (`*.tf`), inputs
(`terraform.tfvars`, generated `*.auto.tfvars`), this README, or any
`smoke-transcript-*.log` evidence. It defaults to `0` (**off** — artifacts are
left in place) and never runs after a failed preflight (it only runs once the
plan succeeds) or on the apply path (`PLAN_ONLY=0` keeps its state and
transcript). It involves no raw app/integration secret values.

```bash
CLEAN_TERRAFORM_ARTIFACTS=1 ./smoke.sh   # plan-only, then remove local Terraform/OpenTofu scratch
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
