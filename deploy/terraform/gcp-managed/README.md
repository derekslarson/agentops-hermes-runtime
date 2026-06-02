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

## Container images

The Cloud Run services run customer-supplied, **non-secret** image references set
via `control_plane_image`, `worker_image`, and `scheduler_image` (placeholder
defaults like `agentops/hermes-control-plane:replace-me`). Override them with the
images you build and push. These are image references, not secret values.

Each service also carries a shared non-secret runtime env
(`local.runtime_common_env`): `AGENTOPS_RUNTIME_PROFILE=gcp-managed`, the Pub/Sub
topic/subscription refs, the artifact bucket, the secret prefix, and the database
**secret container ref** plus the non-secret Cloud SQL connection name. The
worker additionally advertises its per-instance run-slot bound
(`AGENTOPS_WORKER_MAX_CONCURRENT_RUNS`). No raw secret value is passed as env.

## Apply

> **Completeness caveat:** Running `apply` provisions the resource skeleton and
> wires the customer images + shared runtime env. The default public API endpoint
> is the Cloud Run control-plane service **URI** (see "Public API endpoint"
> below); it is reachable without IAM auth only when `enable_public_invoker` is
> set. This module is still **scaffold-level** (placeholder images, parity gaps
> below), and **no live `PLAN_ONLY=0` GCP apply/smoke has been captured** — a
> successful `plan` is not a captured live deployment.

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

Before a `PLAN_ONLY=0` apply/smoke you must replace the placeholder
`:replace-me` container images (`control_plane_image`, `worker_image`,
`scheduler_image`) with real image references — the apply path inspects the
effective image values and fails closed if any placeholder remains.

Honest caveat: this module is still scaffold-level (see the parity gaps below),
so **no live GCP apply/smoke is captured** — a successful plan is not a captured
live deployment. The helper never accepts or echoes raw integration secret
values; bootstrap (M16) owns those.

## Public API endpoint

The control-plane has **no provisioned load balancer / DNS**, so the default
public API endpoint is the Cloud Run control-plane service **URI** (surfaced as
the `cloud_run_api_url` output; `agentops_api_url` and the webhook URLs derive
from it). Two non-secret toggles control public exposure:

- **`enable_public_invoker`** (default `false`): when `true`, grants
  `allUsers` `roles/run.invoker` on the **control-plane only** so the API is
  reachable without IAM auth. Default `false` keeps the service IAM-gated
  (private). The worker/scheduler never receive this binding.
- **`enable_custom_domain`** (default `false`) + **`domain`**: when both are
  set, a Cloud Run domain mapping is created for the control-plane at `domain`,
  and `agentops_api_url` becomes `https://<domain>`. With `enable_custom_domain`
  unset, the endpoint stays the Cloud Run service URI.

A live `PLAN_ONLY=0` GCP apply/smoke against a real project is still pending.

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

## IAM — dedicated runtime service account

Like the `aws-managed` task role, all three Cloud Run services run as a
dedicated runtime **service account** (`<prefix>-runtime`) rather than the
default Compute service account. That account is granted least-reasonable,
resource-**scoped** bindings to exactly the backend refs in
`local.runtime_common_env`:

- **Pub/Sub:** `roles/pubsub.publisher` on the runs topic and
  `roles/pubsub.subscriber` on the runs subscription.
- **GCS artifacts:** `roles/storage.objectAdmin` on the effective bucket
  (module-created or bring-your-own by name).
- **Secret Manager:** `roles/secretmanager.secretAccessor` (read-only) on each
  created secret container — bootstrap still owns the raw values, and no secret
  **version** resource is created here.
- **Cloud SQL:** `roles/cloudsql.client` at project-level scope so the runtime can
  open connections to the managed or BYO instance.

## Parity gaps vs aws-managed

This GCP module is a scaffold and is intentionally behind the `aws-managed`
module in the following areas:

- **Networking:** bring-your-own VPC/subnet refs are consumed (the services
  attach via Direct VPC egress when `existing_vpc_id` + `existing_subnet_ids`
  are set), but creating a *new* private network / VPC connector when none is
  provided is not yet wired.
- **Autoscaling policy:** Cloud Run min/max instances are set, but no explicit
  CPU target-tracking policy equivalent to the AWS Application Auto Scaling
  policy is configured.
- **Load balancer / DNS:** the default endpoint is the Cloud Run service URI;
  no external HTTP(S) load balancer or managed DNS fronts the control-plane.
  Optional public access (`enable_public_invoker`) and an optional Cloud Run
  domain mapping (`enable_custom_domain` + `domain`) are wired (see "Public API
  endpoint"), but a load-balancer/CDN front door is not.
- **Live apply/smoke:** no live `PLAN_ONLY=0` GCP apply/smoke has been captured;
  a successful `plan` is not a captured live deployment.

Track these to reach full parity with `aws-managed` before treating the GCP
profile as production-ready.

## Outputs

After apply, `terraform output` (or `tofu output`) surfaces the same contract as
`aws-managed`: `agentops_api_url`, `bootstrap_url`, `bootstrap_token_secret_ref`,
the Slack/GitHub/Linear/Jira webhook URLs, `secret_refs`, `queue_refs`,
`artifact_refs`, `database_refs`, `network_refs`, `worker_service_name`, and
`smoke_test_hints`.
