# AgentOps Hermes Runtime MVP Roadmap

## North star

Make Hermes a distributed, multi-tenant business runtime without losing what makes Hermes useful: the same agent loop, same native `memory` tool, same skills semantics, same cron/autonomous jobs, same tool-calling ergonomics, and same ability to run fully local.

The MVP proves one thing:

> Multiple scoped Hermes runs can execute concurrently across a local process, Docker Compose worker fleet, or cloud-managed worker fleet while native Hermes memory, skills, sessions, cron jobs, credentials, artifacts, audit, and delivery routes are selected by `RuntimeContext` and backed by cloud/database/secret-store-agnostic adapters.

This is not a wrapper that injects memory around Hermes. The native Hermes surfaces remain the interface. The backing stores become pluggable and context-scoped.

## Architecture principles

1. **Backend contracts before cloud code.** Define Python interfaces/protocols first; AWS/GCP/local implementations come behind those contracts.
2. **Local remains first-class.** Existing local behavior is not legacy; it is the reference implementation and dev mode.
3. **Local is not single-process.** Local mode must still support multiple concurrent Hermes runs with scoped memory/sessions/skills/cron and safe locking.
4. **Native tools, remote backing.** Do not build sidecar memory/skills/cron that bypass Hermes. Native Hermes surfaces call pluggable backends.
5. **Runtime context is load-bearing.** Every backend decision is scoped by `RuntimeContext`: org, workspace, user, conversation/thread, project, agent profile, run, and execution mode.
6. **Cloud agnostic.** AWS is likely the first production adapter, but no core interface should name ECS, SQS, DynamoDB, Secrets Manager, etc.
7. **DB agnostic at the contract level, optimized at the adapter level.** Do not force lowest-common-denominator data modeling; Postgres, SQLite, DynamoDB, etc. can implement the same contracts differently.
8. **Secret-store agnostic.** Local env files, encrypted local stores, AWS Secrets Manager, GCP Secret Manager, Vault, and future stores are implementations of a credential/secret resolver contract.
9. **Remote cron is mandatory.** Cron/autonomous jobs are part of Hermes’s identity; they need remote storage, leases, delivery targeting, worker execution, and local fallback.
10. **Tenant isolation beats convenience.** Cross-user/org/project/thread memory, skill, credential, cron, or session leakage is an MVP blocker.
11. **Warm workers are an optimization, remote state is authoritative.** Conversation runs may stay warm until idle timeout, but all durable state must survive worker death.
12. **Infrastructure provisioning is separate from application activation.** Terraform/OpenTofu should create inert cloud resources and output URLs/refs; a bootstrap UI/CLI should store integration secrets, configure policies, run migrations, and prove the system works.
13. **Secrets stay out of Terraform state.** Terraform may create secret containers/placeholders and IAM access, but Slack/GitHub/Linear/Jira/model-provider secret values should be written by bootstrap directly into the configured secret backend.
14. **Small fork deltas.** Prefer modular seams that could be upstreamed or maintained as a small patch stack.

## Required deployment profiles

AgentOps Hermes Runtime must support these as first-class profiles over the same backend contracts.

### Profile A: local-multi

A developer or solo operator runs multiple scoped Hermes runs on one machine.

Typical shape:

```text
Hermes worker/supervisor process(es)
→ local RuntimeContext registry
→ local SQLite WAL or file-backed state
→ local filesystem skills/memory/artifacts where selected
→ local cron scheduler/backend
→ local env/profile credentials
```

Acceptance invariants:

- More than one Hermes run can execute concurrently.
- Local state is still context-scoped by user/org/profile/project/thread.
- Local backends implement the same contracts as remote backends.
- Existing single-user Hermes behavior remains available as the default compatibility path.

### Profile B: compose-self-hosted

A small team or development environment runs the distributed stack locally or on one VM via Docker Compose.

Typical shape:

```text
agentops-api/control-plane
agentops-worker --max-concurrent-runs=N
agentops-scheduler
postgres
redis/nats or equivalent queue
minio or filesystem object store
local encrypted secret store
slack-adapter / webhook ingress
```

Acceptance invariants:

- Workers can be scaled horizontally with `docker compose --scale worker=N`.
- Each worker can run zero to N concurrent Hermes runs.
- Shared database/queue/object-store backends coordinate leases, sessions, cron, artifacts, and delivery.
- This profile is the main distributed testbed before cloud adapters.

### Profile C: aws-managed

A customer deploys into AWS with managed services.

Initial preferred AWS shape:

```text
ECS/Fargate worker service with autoscaling
RDS Postgres first; DynamoDB later where useful
SQS or EventBridge/SQS for queues and cron triggers
S3 artifact backend
AWS Secrets Manager credential backend
CloudWatch logs/metrics
Slack/GitHub/Linear ingress via API service/Lambda/ECS
```

Acceptance invariants:

- ECS task count can scale up/down during the day based on expected load or queue metrics.
- Each ECS task can host zero to N concurrent Hermes runs, e.g. 0–8 active slots.
- Draining tasks stop accepting new runs and gracefully finish/checkpoint active runs before shutdown.
- Terraform/OpenTofu can provision the managed AWS stack from account/region/domain/capacity settings and output bootstrap/webhook URLs.
- App/integration secret values are added after infrastructure provisioning through bootstrap, not stored directly in Terraform state.
- AWS-specific code lives in adapter packages, not core runtime contracts.

### Profile D: gcp-managed

A later cloud-managed profile with GCP services.

Candidate shape:

```text
Cloud Run worker service
Cloud SQL Postgres or Firestore adapter
Pub/Sub or Cloud Tasks
GCS artifact backend
GCP Secret Manager credential backend
Cloud Logging/Monitoring
```

Acceptance invariants:

- Same runtime contracts and worker lifecycle as AWS.
- Terraform/OpenTofu can provision the managed GCP stack from project/region/domain/capacity settings and output bootstrap/webhook URLs.
- App/integration secret values are added after infrastructure provisioning through bootstrap, not stored directly in Terraform state.
- GCP-specific code lives in adapter packages.

### Profile E: hybrid

Mixed deployments are allowed: for example, local/compose workers with remote RDS, or ECS workers with external Postgres/Vault.

Acceptance invariant:

- Backend choices are configured independently per capability: memory, sessions, skills, cron, queue, credentials, artifacts, audit, delivery.

## Provisioning and bootstrap flow

The intended customer deployment experience is two-phase:

```text
terraform/tofu apply
→ cloud infrastructure exists, workers/API/scheduler can start, URLs/refs are output

agentops bootstrap or /bootstrap UI
→ admin/org/project/integrations/secrets/policies are configured
→ migrations and smoke tests run
→ first real Hermes-backed conversation/job works
```

### Terraform/OpenTofu phase

Terraform/OpenTofu owns infrastructure shape, not app secrets.

Customer-provided inputs should be infrastructure/account settings such as:

- project/application name
- environment name
- cloud account/project and region
- domain name or generated cloud URL preference
- VPC/network choices, including bring-your-own VPC/subnet options
- worker min/max task counts
- worker CPU/memory
- worker `max_concurrent_runs`
- DB size/class/storage settings
- artifact retention settings
- enabled integration surfaces, e.g. Slack/GitHub/Linear/Jira webhooks

Terraform/OpenTofu should output copy-paste-friendly activation values:

- AgentOps API URL
- one-time or time-bounded bootstrap URL/token reference
- Slack events URL
- GitHub webhook URL
- Linear webhook URL
- Jira webhook URL
- artifact bucket/store ref
- queue refs
- secret backend refs/placeholders
- worker/scheduler service names
- smoke-test command hints

### Bootstrap UI/CLI phase

Bootstrap owns application activation:

- create initial org/workspace/admin user
- run DB migrations
- select default backend profile
- configure RuntimeContext policy defaults
- configure model provider credential refs
- store model/Slack/GitHub/Linear/Jira/etc. secret values directly into the selected secret backend
- configure integration webhook secrets/signing validation
- configure default worker/run policies: idle timeout, max run duration, approval policy, allowed tools, default memory scope, cron enablement
- create first project/workspace bindings
- run smoke tests

Bootstrap may be a CLI, a temporary local UI, or a served `/bootstrap` page. The desired customer experience is: paste integration credentials, click OK/run bootstrap, see verified green checks, then send the first Slack/GitHub/Linear/Jira event.

### Integration readiness checklist

Each integration should have visible status checks, for example:

- Slack: bot token stored, signing secret stored, events URL verified, bot installed, test message delivered.
- GitHub: app/PAT credential stored, webhook secret stored, installation/repo selected, test webhook received.
- Linear: API/OAuth credential stored, workspace/team selected, test issue read or webhook received.
- Jira: base URL/account selected, credential stored, webhook URL/secret configured, test issue event received.
- Model provider: credential stored, provider health check passed, default model selected.

### Deployment packaging targets

The repo should eventually expose:

```text
deploy/
  compose/
    docker-compose.yml
    .env.example

  helm/
    Chart.yaml
    values.yaml
    templates/

  terraform/
    aws-managed/
      main.tf
      variables.tf
      outputs.tf
      terraform.tfvars.example
      README.md

    gcp-managed/
      main.tf
      variables.tf
      outputs.tf
      terraform.tfvars.example
      README.md
```

Kubernetes/Helm is the preferred cloud-agnostic path for customers who want container-managed Postgres/queue/object-store in cloud. Managed Terraform stacks are the preferred "easy AWS/GCP" path.

## Core runtime concepts

### RuntimeContext

Every run/turn/job has a context that selects memory, skills, credentials, sessions, cron ownership, artifacts, audit, and delivery routes.

Minimum fields:

- `mode`: `local | agentops`
- `org_id`
- `workspace_id`
- `workspace_type`: `slack | telegram | discord | cli | api | cron | github | linear | other`
- `user_id`
- `conversation_id`
- `external_channel_id`
- `external_thread_id`
- `agent_profile_id`
- `project_id`
- `run_id`
- `run_type`: `conversation | event | cron | delegation | manual`
- `job_id`
- `parent_session_id`
- `permissions_ref`
- `backend_profile`
- `delivery_ref`

### Worker task / worker container

An ECS task, Cloud Run instance, Docker container, or local process that can host zero to N Hermes runs.

### Worker slot

A bounded concurrent execution slot inside a worker task.

Example:

```text
worker-17 slot 0
worker-17 slot 1
...
worker-17 slot 7
```

### Agent run

A live Hermes runtime instance handling one conversation, event, cron job, delegated task, or manual request.

### Conversation session

A durable logical session, often mapped from Slack workspace/channel/thread/user, that may have an active warm run while users are interacting and then go dormant after idle timeout.

### Warm conversation run

Conversation runs may remain active for a configurable idle window so multi-turn chats are fast:

```text
message arrives
→ start/resume run
→ process turns sequentially
→ remain warm while messages keep arriving
→ idle timeout expires
→ flush/release durable state
→ exit
```

Remote state remains authoritative; warm process memory is only a cache/optimization.

### Event/cron run

GitHub comments, Linear tickets, webhooks, cron jobs, and one-shot events are run-to-completion:

```text
event/job arrives
→ claim lease
→ start Hermes run
→ work until done/fail/cancel/max runtime
→ write session/audit/artifacts
→ exit
```

## Required backend contracts

### Backend registry

Selects concrete implementations by config + RuntimeContext.

Minimum contracts:

- `MemoryBackend`
- `SkillBackend`
- `SessionBackend`
- `CronBackend`
- `CredentialResolver`
- `SecretStore`
- `QueueBackend`
- `RunLeaseBackend`
- `ConversationRouter`
- `WorkerRegistry`
- `ArtifactBackend`
- `AuditBackend`
- `DeliveryBackend`

### QueueBackend

For pending turns, events, cron firings, and jobs.

Required semantics:

- enqueue with idempotency key
- claim by worker/capability
- ack/nack/retry
- visibility timeout or lease expiry
- priority or run-type partitioning eventually

### RunLeaseBackend

For one active owner per run/job/conversation turn.

Required semantics:

- claim run/job/conversation lease
- renew lease/heartbeat
- release/complete/fail/cancel
- expire stale leases
- support worker draining

### ConversationRouter

Maps external messaging events to conversations and active runs.

Required semantics:

- resolve/create conversation from external event
- find active warm run if any
- route turn to active run inbox or enqueue resume/start request
- preserve per-conversation sequential turn processing
- handle stale active-run leases

### WorkerRegistry

Tracks fleet capacity and lifecycle.

Required semantics:

- register worker with capacity/capabilities
- heartbeat active slots and health
- mark draining
- stop new claims for draining workers
- recover expired workers/runs

### RunSupervisor

A local component inside each worker task.

Required semantics:

- manage bounded slots, e.g. `max_concurrent_runs=8`
- start one Hermes run per slot, initially preferably as a subprocess for isolation
- send follow-up turns to warm conversation runs
- cancel/interrupt runs
- drain gracefully on shutdown
- report lifecycle/audit events

## MVP slice list

### M0. Fork baseline and contribution hygiene

**Status:** Started

**Goal:** Establish this repo as the clean AgentOps Hermes runtime fork.

**Acceptance criteria:**

- Fork exists at `derekslarson/agentops-hermes-runtime` with `origin` pointing to Derek’s fork and `upstream` fetch-only pointing to `NousResearch/hermes-agent`.
- A dedicated roadmap/architecture lives under `docs/agentops-runtime/`.
- Baseline test command(s) and local dev setup are documented before runtime changes.
- Upstream sync strategy is documented.

### M1. RuntimeContext foundation

**Status:** Started

**Goal:** Introduce a first-class context object that can be passed through Hermes without changing local behavior.

**Autonomous run note (2026-05-31):** Landed the first RuntimeContext primitive and propagation seam: local/env/config/work-item resolution, per-agent context assignment, ContextVar binding for conversation and tool execution paths, immutable metadata snapshots, fail-closed malformed context handling, and focused regression coverage. Future M1/M2 follow-up should connect this seam to concrete backend registries and cron/delivery adapter contracts rather than sidecar context injection.

**Acceptance criteria:**

- Local mode creates a RuntimeContext from existing profile/session/platform data.
- AgentOps mode can load RuntimeContext from env var, JSON payload, config, or queued work item.
- Context is available to memory, skills, session state, cron, credential resolution, logging/audit, delivery, and tool execution paths.
- Tests prove absent AgentOps context preserves current behavior.
- Tests prove two different RuntimeContexts can coexist in the same process test without sharing mutable context state.

### M2. Backend registry/contracts

**Status:** Pending

**Goal:** Add generic backend contracts and a runtime backend registry without implementing cloud-specific behavior yet.

**Acceptance criteria:**

- Local implementations wrap existing behavior.
- Backend selection is driven by config + RuntimeContext.
- `QueueBackend`, `RunLeaseBackend`, `ConversationRouter`, and `WorkerRegistry` are included alongside memory/session/skill/cron/credential/artifact/audit contracts.
- No AWS/GCP/Postgres/DynamoDB-specific names leak into core interfaces.
- Tests instantiate local/fake backends through the registry.

### M3. Local multi-run concurrency baseline

**Status:** Pending

**Goal:** Prove that local mode can run multiple scoped Hermes runs concurrently before remote adapters are added.

**Acceptance criteria:**

- A local worker/supervisor can run at least two concurrent Hermes runs with different RuntimeContexts.
- Local sessions/memory/artifacts/locks are isolated by context.
- Local SQLite/file backends use safe locking/WAL/idempotency where applicable.
- One run crashing does not corrupt another run’s local state.
- Existing single-user local Hermes behavior remains unchanged when distributed mode is not enabled.

### M4. Native memory tool backend abstraction

**Status:** Pending

**Goal:** Preserve the native `memory` tool while moving storage behind `MemoryBackend`.

**Acceptance criteria:**

- Current local file memory behavior is represented by `LocalFileMemoryBackend`.
- `memory(action=add|replace|remove|read, target=user|memory, ...)` continues to work in local mode.
- AgentOps/remote backend can be stubbed/faked in tests and receives RuntimeContext.
- Tests prove writes for two users/conversations route to separate backend scopes.
- Threat scanning, char limits, drift protection semantics are preserved or explicitly mapped.
- No sidecar context injection replaces the native memory tool path.

### M5. Remote memory adapter MVP

**Status:** Pending

**Goal:** Implement the first real remote memory adapter against a cloud/db-agnostic contract.

**Initial implementation preference:** HTTP adapter to an AgentOps control-plane API with fake/in-memory test server; Postgres-backed implementation can be the first durable distributed backend.

**Acceptance criteria:**

- Remote memory read/write uses RuntimeContext scope.
- Same native memory tool is used by the model.
- No raw memory from another user/org/thread appears in prompt or tool output.
- Local fallback remains available.
- Same memory contract works in local-multi and compose-self-hosted profiles.

### M6. Session/conversation backend abstraction

**Status:** Pending

**Goal:** Make Hermes session persistence worker-safe and optionally remote.

**Acceptance criteria:**

- Existing SQLite session behavior is wrapped as `LocalSQLiteSessionBackend`.
- Remote session backend contract supports append/read/search/resume lineage.
- Worker can process a turn and write transcript/tool events to remote session backend.
- Concurrent turn lock/lease semantics are specified and tested.
- Conversation/session state survives worker restart and resumes in a different worker.

### M7. Skills backend abstraction

**Status:** Pending

**Goal:** Keep native Hermes skills while allowing remote scoped skill sources.

**Scope model:**

- built-in bundled skills
- org skills
- team/project skills
- user-private skills
- runtime/ephemeral skills

**Acceptance criteria:**

- Existing filesystem skill loading is `LocalSkillBackend`.
- Remote skill backend can list/load skill content by RuntimeContext.
- Skill mutation permissions are represented: user-private allowed, shared org/project skills require policy/approval flag.
- Tests prove user A cannot load user B private skill and org skills can be shared.
- Skill precedence is deterministic across local and remote sources.

### M8. Cron/autonomous jobs backend abstraction

**Status:** Pending

**Goal:** Move Hermes cron from local scheduler files/state to pluggable local/remote cron backends.

**Acceptance criteria:**

- Existing cron behavior remains as `LocalCronBackend`.
- Remote cron contract supports create/update/pause/resume/remove/list/run history.
- Jobs include RuntimeContext and delivery target bindings.
- Worker-safe leases prevent duplicate execution across multiple worker tasks/schedulers.
- Empty-output/silent semantics and error-alert semantics are preserved.
- Tests cover recurring, one-shot, paused, lease timeout, scheduler restart, worker drain, and delivery routing cases.
- Cron jobs can run locally, in Compose, or via cloud scheduler/queue adapters using the same logical job model.

### M9. Credential/secret resolver abstraction

**Status:** Pending

**Goal:** Decouple provider/tool credentials from local `.env` without exposing raw secrets to prompts/transcripts.

**Backends:**

- local env/profile files
- local encrypted store
- AgentOps remote broker
- AWS Secrets Manager adapter
- GCP Secret Manager adapter
- future Vault adapter

**Acceptance criteria:**

- Credential requests are made by ref/capability, not raw values.
- RuntimeContext determines which credentials are available.
- Resolved secrets are available only to the tool/provider process path that needs them.
- Audit stores refs and usage metadata, never secret values.
- Credential resolution works for local, compose, AWS, and future GCP profiles without changing tool code.

### M10. Artifact and audit backends

**Status:** Pending

**Goal:** Centralize durable artifacts and audit trails for distributed runs.

**Acceptance criteria:**

- Local artifacts remain available.
- Remote artifact backend stores tool outputs/files by scoped refs.
- Audit backend receives memory writes, skill loads/mutations, credential resolutions, cron runs, session events, worker lifecycle events, queue/lease events, delivery events, and tool calls.
- Tests prove sensitive local paths/secrets are not surfaced in audit payloads.

### M11. Worker fleet and run lifecycle

**Status:** Pending

**Goal:** Support Derek’s production-style model: scale worker tasks up/down, each task hosting zero to N concurrent Hermes runs, with warm conversations and run-to-completion jobs.

**Acceptance criteria:**

- Worker registers with capacity, capabilities, and `max_concurrent_runs`.
- Worker can run 0–N concurrent Hermes runs, initially preferably subprocess-per-run for isolation.
- Conversation run starts on incoming message and stays warm until configurable idle timeout.
- Event/GitHub/Linear/cron/manual run works until done/fail/cancel/max runtime, then exits.
- Per-conversation turns are processed sequentially or explicitly queued while a run is busy.
- Worker drain prevents new claims and gracefully finishes, checkpoints, or releases active runs before shutdown.
- Expired leases allow recovery after worker death.
- Same lifecycle works in local-multi, compose-self-hosted, and AWS adapter profiles.

### M12. Compose self-hosted distributed MVP

**Status:** Pending

**Goal:** Prove distributed semantics without cloud lock-in.

**Acceptance criteria:**

- `docker compose up` starts API/control-plane, worker(s), scheduler, database, queue, object store, and local secret backend.
- Workers can be scaled horizontally and each worker can host multiple concurrent runs.
- Two users/threads get isolated memory/sessions while shared org/project skills can be loaded in both.
- Remote cron/leases prevent duplicate job execution with multiple schedulers/workers.
- Worker restart between turns resumes from durable state.

### M13. Slack multi-user/thread MVP

**Status:** Pending

**Goal:** Prove business messaging integration with multiple users and threads.

**Acceptance criteria:**

- Slack workspace/channel/thread/user maps to RuntimeContext.
- Two Slack users in different threads get isolated user/thread memory.
- Shared org/project skill can be loaded in both threads.
- Hermes replies in the correct Slack thread.
- Active conversation routing sends follow-up messages to the warm run when one exists.
- Worker can be restarted between turns and still resume state remotely.

### M14. Cloud adapter spike: AWS first, GCP later

**Status:** Pending

**Goal:** Prove core contracts can be deployed to one real cloud without hard-coding that cloud into the core.

**AWS candidate adapters:**

- ECS/Fargate worker service with autoscaling
- SQS/EventBridge for queued turns/cron triggers
- RDS Postgres as first durable DB adapter; DynamoDB later where useful
- S3 artifact backend
- Secrets Manager credential backend
- CloudWatch logs/metrics adapter

**GCP candidate adapters:**

- Cloud Run worker
- Pub/Sub or Cloud Tasks
- Cloud SQL/Firestore adapter
- GCS artifact backend
- Secret Manager credential backend

**Acceptance criteria:**

- Cloud-specific code lives in adapter packages/modules.
- Local/fake/compose adapters remain the default test path.
- Same worker lifecycle runs locally, in Compose, and in AWS adapter mode.
- ECS desired task count can scale independently from per-task run concurrency.

### M15. Managed cloud Terraform/OpenTofu packaging

**Status:** Pending

**Goal:** Provide the easiest AWS/GCP path: customers edit account/region/domain/capacity settings, run Terraform/OpenTofu, and receive working infrastructure plus bootstrap/webhook outputs.

**Acceptance criteria:**

- `deploy/terraform/aws-managed/` provisions the AWS managed profile: API/control-plane, worker service, scheduler, RDS/Postgres or selected DB adapter, queue, artifact store, secret placeholders/refs, logs, IAM, and autoscaling.
- `deploy/terraform/gcp-managed/` is scaffolded or implemented with equivalent GCP resources and clear parity gaps.
- `terraform.tfvars.example` avoids raw app/integration secret values where possible; secret containers/refs are created instead.
- Outputs include AgentOps API URL, bootstrap URL/token ref, Slack/GitHub/Linear/Jira webhook URLs, secret refs, queue refs, artifact refs, worker service names, and smoke-test hints.
- Bring-your-own-network and bring-your-own-managed-resource paths are represented through variables such as existing VPC/subnet/database/bucket/secret refs.
- Terraform/OpenTofu state does not contain Slack/GitHub/Linear/Jira/model-provider raw secret values in the recommended path.

### M16. Bootstrap UI/CLI and activation smoke test

**Status:** Pending

**Goal:** Turn provisioned infrastructure into an activated AgentOps Hermes Runtime install through a customer-friendly UI/CLI.

**Acceptance criteria:**

- Bootstrap can create the initial org/workspace/admin/project and default backend profile.
- Bootstrap runs migrations and validates DB/queue/worker/scheduler/artifact/secret backend health.
- Bootstrap stores model provider, Slack, GitHub, Linear, Jira, and future integration secrets directly into the configured secret backend.
- Bootstrap configures RuntimeContext defaults, worker/run policy, memory scope policy, approval policy, allowed tools, cron enablement, and delivery defaults.
- Bootstrap shows per-integration readiness checks and verifies webhook/signing secrets where possible.
- Smoke test verifies API health, worker registration/heartbeat, queue claim, scoped memory read/write, session append/read, cron create/claim, secret resolution, artifact write/read, and at least one integration event-to-response loop.
- Customer can complete the intended path: run Terraform/OpenTofu, open bootstrap UI or run bootstrap CLI, paste integration credentials, click/run OK, see green checks, then send a first Slack/GitHub/Linear/Jira event.

## Initial implementation order

1. M0 docs/repo hygiene.
2. M1 RuntimeContext.
3. M2 backend registry/contracts.
4. M3 local multi-run concurrency baseline.
5. M4 native memory backend abstraction.
6. M5 fake/HTTP remote memory adapter.
7. M8 cron backend abstraction early, because remote cron is not optional.
8. M6 sessions and M7 skills.
9. M9 credentials.
10. M11 worker fleet/run lifecycle.
11. M12 Compose self-hosted distributed MVP.
12. M13 Slack smoke.
13. M14 AWS adapter spike.
14. M15 managed cloud Terraform/OpenTofu packaging.
15. M16 bootstrap UI/CLI and activation smoke test.

## Reference MVP proof

A strong early proof should demonstrate:

```text
Start 3 workers locally or in Docker Compose.

Send message A:
  org=acme, user=derek, thread=1

Send message B:
  org=acme, user=alex, thread=2

Both run concurrently.

Hermes native memory tool in A writes:
  "Derek likes concise updates"

Hermes native memory tool in B writes:
  "Alex prefers detailed updates"

Next turn:
  A sees Derek memory only.
  B sees Alex memory only.

Create org skill:
  "Use Acme engineering style"

Both A and B can load org skill.

Create user-private skill:
  only Derek can load it.

Schedule cron:
  org=acme/project=x daily summary

Two schedulers/workers are running, but only one claims the job.

Restart a worker between turns:
  conversation resumes from remote/local-durable state.
```

This proof must work first in local-multi and compose-self-hosted profiles before AWS-specific adapters are considered complete.

## Explicit non-goals for MVP

- Polished dashboard beyond the minimal bootstrap/activation UI required to make deployments usable.
- Complex approval UI.
- Full enterprise RBAC.
- Deep billing/metering.
- Rewriting Hermes agent loop.
- Removing local mode.
- Locking architecture to AWS, GCP, Postgres, DynamoDB, RDS, SQS, Secrets Manager, Cloud Run, or any one provider/service.
- Treating remote memory as prompt injection around Hermes instead of native `memory` tool storage.

## Open questions

- Should AgentOps mode communicate with the control plane primarily over HTTP/gRPC, direct SQL, or both through adapters?
- Should remote cron scheduling live in AgentOps control plane, Hermes scheduler, cloud scheduler, or an adapter that can wrap all three?
- How much of RuntimeContext should be visible to the model versus internal only?
- Should shared skill mutations be impossible from an agent by default, or allowed with approval gates?
- What is the smallest safe credential grant model that still supports useful tools?
- Should the first durable distributed backend be Postgres everywhere, or should AWS-native DynamoDB be developed early as a separate adapter?
- Should warm conversation runs be subprocesses for isolation from the start, or can some local profiles safely run multiple agents in-process?
- Should the recommended AWS path use Terraform, OpenTofu, or a wrapper CLI that can drive both?
- Should bootstrap be primarily a CLI, a hosted web UI, or both from the beginning?
- Which integrations are mandatory for the first activation smoke test: Slack only, or Slack plus GitHub/Linear/Jira scaffolds?
