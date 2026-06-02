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

## Image-publishing helper (`publish-images.sh`)

`publish-images.sh` is a module-local helper that builds, tags, and pushes the
three runtime container images (control-plane / worker / scheduler) to Google
Artifact Registry and prints the non-secret `terraform.tfvars` image lines you
paste before a `PLAN_ONLY=0 ./smoke.sh` apply. It is the supported path to
produce **real** image references that replace the `:replace-me` placeholders
without ever handing app/integration secrets to Terraform.

It is **side-effect-safe by default**: with `DRY_RUN=1` (the default) it only
**prints** the `docker`/`gcloud` commands it would run — it never builds, logs
in, tags, pushes, creates/configures the Artifact Registry repository, or
modifies Terraform vars. Set `DRY_RUN=0` for a live publish; the live path
**fails closed before any side effect** if the `gcloud` or `docker` CLI is
missing or no gcloud account is active (verified with `gcloud auth list`).
Inputs are env-only and non-secret: `PROJECT` (autodetected via `gcloud config`
when unset), `AR_LOCATION` (default `us-central1`), `AR_REPO` (default
`agentops-hermes-runtime`), `IMAGE_TAG` (default: UTC timestamp), and
`CREATE_REPO=1` to also create/configure the Artifact Registry repo on the live
path (dry-run always prints that command). The `*_CONTEXT` docker build contexts
(`CONTROL_PLANE_CONTEXT` / `WORKER_CONTEXT` / `SCHEDULER_CONTEXT`) default to the
**repo root** (`../../..` from this module dir, where the `Dockerfile` lives —
the helper `cd`s into the module dir, so they must not default to `.`) and are
overridable via env.

On a live publish (`DRY_RUN=0`) the helper **preflights each build context
before any Artifact Registry repo create/`configure-docker` or `docker
build`/`push`**: every `*_CONTEXT` must be an existing directory containing a
`Dockerfile`, or the run fails closed with a non-secret error naming the
offending variable. Any `*_CONTEXT` override must therefore point at a
build-context directory that contains a `Dockerfile`. The default dry-run skips
this check and stays permissive.

```bash
cd deploy/terraform/gcp-managed
./publish-images.sh                              # DRY_RUN=1 (default): print commands only
DRY_RUN=0 CREATE_REPO=1 ./publish-images.sh      # live build/tag/push to Artifact Registry
```

After a live publish it prints three lines like
`control_plane_image = "…"` / `worker_image = "…"` / `scheduler_image = "…"`.
Copy them into `terraform.tfvars`, then run `PLAN_ONLY=0 ./smoke.sh` to apply
with real images (the smoke helper refuses to apply while any `:replace-me`
placeholder remains).

To skip the hand-copy step, set `WRITE_TFVARS=1` on the live path: in addition
to printing the lines, the helper writes **only** those three non-secret image
assignments to a module-local tfvars override (default `image.auto.tfvars`,
overridable via `IMAGE_TFVARS_PATH`) using a temp file + `mv`. Terraform auto-loads
any `*.auto.tfvars` file, so the next `PLAN_ONLY=0 ./smoke.sh` picks the images up
with no manual edit. The writer is opt-in and **never runs in the default
`DRY_RUN=1` mode** (dry-run stays side-effect-free), writes no secret values, and
the generated file is git-ignored. A custom `IMAGE_TFVARS_PATH` must stay a
module-local plain filename (no leading `/`, no `/` path segments, no `..`); the
helper fails closed before publishing otherwise, so the writer can never clobber
files outside this module.

```bash
DRY_RUN=0 WRITE_TFVARS=1 CREATE_REPO=1 ./publish-images.sh   # also writes image.auto.tfvars
```

## Live-smoke helper (`./smoke.sh`)

`./smoke.sh` is a module-local helper you run from inside this directory after
configuring `terraform.tfvars` and `gcloud` (auth + project/region). It fails
closed *before* any Terraform/OpenTofu side effect when:

- neither `terraform` nor `tofu` (OpenTofu) is on `PATH`, or
- the `gcloud` CLI is missing, or
- no gcloud account is active (a read-only `gcloud auth list` check).

It also **preflights the required non-secret inputs** (`project`, `region`)
before any Terraform/OpenTofu side effect, so an unconfigured run fails closed
without creating `.terraform/` or a lock file (rather than erroring at plan time
after `init`). Satisfy the check either with a `terraform.tfvars` /
`*.auto.tfvars` file that defines each required input, or with `TF_VAR_project` /
`TF_VAR_region` environment variables. These are non-secret account/location
inputs only; raw app/integration secrets remain a bootstrap (M16) concern.

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

On the `PLAN_ONLY=0` apply path, after a healthy `/healthz` probe, the helper
also writes a module-local **non-secret smoke transcript** (`smoke-transcript-<UTC
timestamp>.log`) recording the provider/profile, a UTC timestamp, the effective
`agentops_api_url`, the `/healthz` success, and the `smoke_test_hints` output. The
helper prints the transcript path so you can attach it to the still-pending M15
live-smoke evidence before marking the milestone Done. The transcript is produced
**only** after a `PLAN_ONLY=0` apply with a healthy `/healthz` probe — never in
the plan-only default. Although the script writes only non-secret outputs,
**review the transcript for secrets before sharing it**.

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
