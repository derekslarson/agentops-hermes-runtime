# AgentOps Hermes Runtime — GCP managed profile (Terraform/OpenTofu scaffold)

This module scaffolds the GCP equivalent of the `aws-managed` profile so a
customer can edit project/region/domain/capacity settings, apply Terraform or
OpenTofu, and receive the same bootstrap and webhook outputs.

## What it scaffolds

- API / control-plane — Cloud Run service
- Worker fleet — Cloud Run service (autoscaled)
- Scheduler — Cloud Run service
- Postgres — Cloud SQL
- Queue — Pub/Sub topic + subscription
- Artifact store — GCS bucket
- Secret Manager secret **containers** (no raw values)

## Apply

> **Completeness caveat:** Running `apply` provisions the resource skeleton but
> **does not yield a working deployment** while `main.tf` still carries `TODO`
> container image references. Fill those in (and close the parity gaps below)
> before expecting running services.

All commands run from inside this module directory. With Terraform:

```bash
cd deploy/terraform/gcp-managed
cp terraform.tfvars.example terraform.tfvars
# Edit project/region/domain/capacity values.
terraform init
terraform plan
terraform apply
```

OpenTofu is a drop-in replacement — substitute `tofu` for `terraform`:

```bash
cd deploy/terraform/gcp-managed
tofu init
tofu plan
tofu apply
```

## Live-smoke helper (`./smoke.sh`)

`./smoke.sh` is a module-local helper you run from inside this directory after
configuring `terraform.tfvars` and `gcloud` (auth + project/region). It fails
closed *before* any Terraform/OpenTofu side effect when:

- neither `terraform` nor `tofu` (OpenTofu) is on `PATH`, or
- the `gcloud` CLI is missing, or
- no gcloud account is active (a read-only `gcloud auth list` check).

It defaults to a safe **`PLAN_ONLY=1`** mode (`init` + `validate` + `plan`, no
apply). Set `PLAN_ONLY=0` to run `apply`, then **probe the live API** and surface
the `agentops_api_url` and `smoke_test_hints` outputs. On the apply path the
helper fetches the bare `agentops_api_url` output and curls
`${agentops_api_url}/healthz`, **failing closed (non-zero)** if the endpoint is
unhealthy or unreachable. `curl` is required on the apply path (the helper fails
clearly before applying if it is missing); the plan-only default never probes and
so never needs `curl`.

```bash
cd deploy/terraform/gcp-managed
./smoke.sh              # plan-only (default)
PLAN_ONLY=0 ./smoke.sh  # opt-in apply, probe ${agentops_api_url}/healthz, surface outputs
```

Honest caveat: this module is still scaffold-level (see the parity gaps below),
so **no live GCP apply/smoke is captured** — a successful plan is not a captured
live deployment. The helper never accepts or echoes raw integration secret
values; bootstrap (M16) owns those.

## Bring your own network / managed resources

Set the `existing_*` variables (`existing_vpc_id`, `existing_subnet_ids`,
`existing_database_arn`, `existing_artifact_bucket`, `existing_secret_prefix`)
to reuse infrastructure. For the database, artifact bucket, and secret prefix,
leaving the value empty creates a new resource. For the network,
`existing_vpc_id` + `existing_subnet_ids` attach the services via Direct VPC
egress when set; when empty the services use Cloud Run default egress — creating
a *new* private network/subnets is a parity gap (see below), not done here.
Whether the database is created or reused, it is surfaced through the
`database_refs` output.

## Secret handling — raw values stay out of state

Like `aws-managed`, this module creates **empty Secret Manager containers
only** and accepts no raw `slack_bot_token`, `github_token`,
`model_provider_api_key`, or similar input. No raw integration secret value
enters Terraform/OpenTofu state. Bootstrap (M16) writes the real values into the
secret backend after apply.

## Parity gaps vs aws-managed

This GCP module is a scaffold and is intentionally behind the `aws-managed`
module in the following areas:

- **IAM/service accounts:** dedicated worker/task service accounts and bindings
  are not yet defined (aws-managed defines task/execution IAM roles).
- **Networking:** bring-your-own VPC/subnet refs are consumed (the services
  attach via Direct VPC egress when `existing_vpc_id` + `existing_subnet_ids`
  are set), but creating a *new* private network / VPC connector when none is
  provided is not yet wired.
- **Autoscaling policy:** Cloud Run min/max instances are set, but no explicit
  CPU target-tracking policy equivalent to the AWS Application Auto Scaling
  policy is configured.
- **Load balancer / DNS:** custom domain mapping for the control-plane is not
  yet provisioned.
- **Container images:** service images are `TODO-*` placeholders.

Track these to reach full parity with `aws-managed` before treating the GCP
profile as production-ready.

## Outputs

After apply, `terraform output` (or `tofu output`) surfaces the same contract as
`aws-managed`: `agentops_api_url`, `bootstrap_url`, `bootstrap_token_secret_ref`,
the Slack/GitHub/Linear/Jira webhook URLs, `secret_refs`, `queue_refs`,
`artifact_refs`, `database_refs`, `network_refs`, `worker_service_name`, and
`smoke_test_hints`.
