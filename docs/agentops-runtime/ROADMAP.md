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
9. **Remote cron is mandatory.** Cron/autonomous jobs are part of Hermes’s identity; they need remote storage, leases, delivery targeting, worker execution, and local fallback. Cron locking must be per job/run/lease, not one global scheduler-execution lock: a long-running autonomous coding job may prevent another run of that same job, but must not starve unrelated lightweight watchdog/script jobs that are due on their own schedules.
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

Cron concurrency invariant:

- Scheduler coordination may briefly claim due work atomically, but it must not hold a global execution lock while jobs run.
- Each cron firing is protected by a scoped lease/idempotency key so duplicate execution is prevented per job/firing.
- Long-running cron jobs, including repo/workdir agent jobs, only serialize against conflicting jobs for the same scoped resource; unrelated `no_agent` script/watchdog jobs continue to fire on time.
- Tests must cover the historical failure mode where a long workdir/autonomous builder job is active while a separate script-only watchdog becomes due; the watchdog must still be claimed and executed without waiting for the builder to finish.

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

**Status:** Done

**Goal:** Establish this repo as the clean AgentOps Hermes runtime fork.

**Completion note (2026-05-31):** M0 is complete: `origin` points to Derek's fork, `upstream` is fetch-only for `NousResearch/hermes-agent`, AgentOps runtime architecture/roadmap docs live under `docs/agentops-runtime/`, and `README.md` documents the local dev baseline, focused test command, pre-commit hygiene, and upstream sync strategy.

**Acceptance criteria:**

- Fork exists at `derekslarson/agentops-hermes-runtime` with `origin` pointing to Derek’s fork and `upstream` fetch-only pointing to `NousResearch/hermes-agent`.
- A dedicated roadmap/architecture lives under `docs/agentops-runtime/`.
- Baseline test command(s) and local dev setup are documented before runtime changes.
- Upstream sync strategy is documented.

### M1. RuntimeContext foundation

**Status:** Done

**Goal:** Introduce a first-class context object that can be passed through Hermes without changing local behavior.

**Autonomous run note (2026-05-31):** Landed the first RuntimeContext primitive and propagation seam: local/env/config/work-item resolution, per-agent context assignment, ContextVar binding for conversation and tool execution paths, immutable metadata snapshots, fail-closed malformed context handling, and focused regression coverage.

**Completion note (2026-05-31):** M1 is complete: native memory, skills, session search/state, cron management/execution, credential resolution, logging/audit, delivery, and tool execution paths can observe the bound RuntimeContext without changing local behavior. Backend abstractions remain M2+; do not start M2 unless M1 remains literally `Done`.

**Acceptance criteria:**

- Local mode creates a RuntimeContext from existing profile/session/platform data.
- AgentOps mode can load RuntimeContext from env var, JSON payload, config, or queued work item.
- Context is available to memory, skills, session state, cron, credential resolution, logging/audit, delivery, and tool execution paths.
- Tests prove absent AgentOps context preserves current behavior.
- Tests prove two different RuntimeContexts can coexist in the same process test without sharing mutable context state.

### M2. Backend registry/contracts

**Status:** Done

**Goal:** Add generic backend contracts and a runtime backend registry without implementing cloud-specific behavior yet.

**Completion note (2026-05-31):** Landed `agent/runtime_backends.py`: structural `Protocol` contracts for all thirteen capabilities (`MemoryBackend`, `SkillBackend`, `SessionBackend`, `CronBackend`, `CredentialResolver`, `SecretStore`, `QueueBackend`, `RunLeaseBackend`, `ConversationRouter`, `WorkerRegistry`, `ArtifactBackend`, `AuditBackend`, `DeliveryBackend`), lightweight `Local*` implementations that preserve the current local-mode path while partitioning their in-process state by `RuntimeContext`, and a `RuntimeBackendRegistry` that selects a backend per capability by a deployment profile derived from config + `RuntimeContext` (precedence: per-capability config override > `RuntimeContext.backend_profile` > config default profile > `"local"`). Selection fails closed via `BackendSelectionError` when a requested profile is unregistered, fakes can be injected per profile through `register(...)`, factory replacement invalidates cached instances, and each registry owns its own factory table and instance cache so isolated registries never share mutable state. Factory construction receives static options only; tenant/run scope stays on backend method calls. Cloud/database/secret-store behavior is intentionally deferred to later milestones. Test evidence: `python -m pytest tests/agent/test_runtime_backends.py -q` → 16 passed; `python -m pytest tests/agent/test_runtime_context.py tests/agent/test_runtime_context_surfaces.py -q` → 16 passed; the suite includes source and sentinel tests asserting no AWS/GCP/database/secret-store provider names leak into the core module and local audit redacts sensitive event keys.

**Acceptance criteria:**

- Local implementations wrap existing behavior.
- Backend selection is driven by config + RuntimeContext.
- `QueueBackend`, `RunLeaseBackend`, `ConversationRouter`, and `WorkerRegistry` are included alongside memory/session/skill/cron/credential/artifact/audit contracts.
- No AWS/GCP/Postgres/DynamoDB-specific names leak into core interfaces.
- Tests instantiate local/fake backends through the registry.

### M3. Local multi-run concurrency baseline

**Status:** Done

**Goal:** Prove that local mode can run multiple scoped Hermes runs concurrently before remote adapters are added.

**Completion note (2026-05-31):** Landed `agent/runtime_supervisor.py` with a local `LocalRunSupervisor` that binds each run inside its own `RuntimeContext`, can run multiple callable Hermes run units through a configurable `ThreadPoolExecutor`, records local worker/audit/session events through the runtime backend registry, isolates ordinary run failures so a crashed run returns a failed `RunResult` without corrupting sibling state, and keeps `run_sync()` inline by default to preserve existing single-user local behavior unless concurrent local execution is explicitly selected. Hardened local runtime backends with re-entrant locks around shared in-process state and deep-copy boundaries for mutable session, cron, and queue payloads so concurrent callers cannot mutate backend internals outside the lock. Added context-scoped run lease and queue idempotency coverage; queue payloads are deep-copied on enqueue/claim/requeue. Audit metadata now redacts common secret-looking exception fragments while returning raw local `RunResult.error` to the caller. Cloud/database/provider-specific adapters remain deferred to later milestones; local SQLite/file WAL integration is covered at this milestone by the local locking/idempotency baseline where those local backends are represented by in-process contract adapters. Test evidence: `python -m pytest tests/agent/test_runtime_supervisor.py tests/agent/test_runtime_backends.py -q` → 21 passed; `python -m pytest tests/agent/test_runtime_context.py tests/agent/test_runtime_context_surfaces.py -q` → 16 passed, 1 known dependency deprecation warning; `python -m ruff check agent/runtime_backends.py agent/runtime_supervisor.py tests/agent/test_runtime_supervisor.py` → passed; `git diff --check` → passed. Independent review gates: spec compliance PASS; final quality/security PASS after mutable-state deep-copy fixes.

**Acceptance criteria:**

- A local worker/supervisor can run at least two concurrent Hermes runs with different RuntimeContexts.
- Local sessions/memory/artifacts/locks are isolated by context.
- Local SQLite/file backends use safe locking/WAL/idempotency where applicable.
- One run crashing does not corrupt another run’s local state.
- Existing single-user local Hermes behavior remains unchanged when distributed mode is not enabled.

### M4. Native memory tool backend abstraction

**Status:** Done

**Completion note (2026-05-31):** Landed native memory backend abstraction while preserving the native `memory` tool path. `MemoryStore` now accepts an optional `MemoryBackend` plus bound `RuntimeContext`; `backend=None` keeps the existing profile-scoped `MEMORY.md` / `USER.md` local behavior, including threat scanning, char limits, file locking, drift protection, and prompt-snapshot semantics. Added `LocalFileMemoryBackend` to represent the historical local file store at the backend-contract layer, with an AgentOps local-multi mode that stores files under collision-resistant RuntimeContext-scoped paths. AgentOps memory initialization now routes non-local profiles through `RuntimeBackendRegistry` and maps the local AgentOps profile to scoped `LocalFileMemoryBackend`; remote/fake backends receive the active RuntimeContext through the native memory store. Added `memory(action="read")` for live non-mutating reads. Test coverage proves local add/replace/remove/read behavior, `LocalFileMemoryBackend` round trips and preserves drift guard semantics, fake remote backends receive context and isolate two users/conversations across `memory` and `user` targets, backend audit actions distinguish add/replace/remove, scoped local-file paths avoid sanitizer/default/external-thread collisions, and no sidecar context injection replaces the native tool. Test evidence: `python -m pytest tests/tools/test_memory_tool.py tests/agent/test_runtime_backends.py -q` → 105 passed; `python -m pytest tests/agent/test_runtime_context.py tests/agent/test_runtime_context_surfaces.py tests/agent/test_runtime_supervisor.py -q` → 22 passed, 1 known dependency deprecation warning; `python -m ruff check tools/memory_tool.py tests/tools/test_memory_tool.py agent/agent_init.py agent/runtime_backends.py tests/agent/test_runtime_backends.py tests/agent/test_runtime_context_surfaces.py` → passed; `git diff --check` → passed. Independent review gates: spec/security PASS after scoped-path collision fix; quality/security APPROVED with remote concurrency left documented as a future adapter responsibility.

**Goal:** Preserve the native `memory` tool while moving storage behind `MemoryBackend`.

**Acceptance criteria:**

- Current local file memory behavior is represented by `LocalFileMemoryBackend`.
- `memory(action=add|replace|remove|read, target=user|memory, ...)` continues to work in local mode.
- AgentOps/remote backend can be stubbed/faked in tests and receives RuntimeContext.
- Tests prove writes for two users/conversations route to separate backend scopes.
- Threat scanning, char limits, drift protection semantics are preserved or explicitly mapped.
- No sidecar context injection replaces the native memory tool path.

### M5. Remote memory adapter MVP

**Status:** Done

**Completion note (2026-05-31):** Landed the first real remote memory adapter without changing the native Hermes `memory` tool interface. `agent/runtime_memory_http.py` adds a stdlib-only `HttpMemoryBackend` plus `register_http_memory_backend(...)` for non-local runtime memory profiles such as `compose-self-hosted`; AgentOps memory init now maps `local`/`local-multi` to scoped `LocalFileMemoryBackend` and registers the HTTP adapter for remote profiles. The adapter reads/writes complete native memory snapshots through a provider-neutral `/memory` HTTP contract, sends a minimal RuntimeContext-derived memory scope (org/workspace/project/channel/thread/conversation/user) instead of raw metadata/secret refs/run IDs, keeps bearer tokens in the Authorization header only, validates base URL/timeout configuration fail-closed, and sanitizes error messages. Tests use a fake in-memory HTTP control-plane server and prove native `MemoryStore` round trips through the remote adapter, cross-user/thread isolation, registry-based compose profile selection, local-multi fallback availability, secret-safe payload/error behavior, and minimal scope serialization. Linearizable multi-writer remote memory updates remain the control plane's responsibility for later durable backend/worker milestones; this client preserves the existing MemoryBackend full-snapshot contract. Test evidence: `./scripts/run_tests.sh tests/agent/test_remote_memory_backend.py tests/agent/test_runtime_context_surfaces.py tests/tools/test_memory_tool.py tests/agent/test_runtime_backends.py` → 131 passed; `python -m ruff check agent/runtime_memory_http.py agent/agent_init.py tests/agent/test_remote_memory_backend.py tests/agent/test_runtime_context_surfaces.py` → passed.

**Goal:** Implement the first real remote memory adapter against a cloud/db-agnostic contract.

**Initial implementation preference:** HTTP adapter to an AgentOps control-plane API with fake/in-memory test server; Postgres-backed implementation can be the first durable distributed backend.

**Acceptance criteria:**

- Remote memory read/write uses RuntimeContext scope.
- Same native memory tool is used by the model.
- No raw memory from another user/org/thread appears in prompt or tool output.
- Local fallback remains available.
- Same memory contract works in local-multi and compose-self-hosted profiles.

### M6. Session/conversation backend abstraction

**Status:** Done

**Goal:** Make Hermes session persistence worker-safe and optionally remote.

**Completion note (2026-05-31):** Completed the native session/conversation backend abstraction. `agent/runtime_sessions.py` provides `LocalSQLiteSessionBackend`, wrapping the existing `SessionDB` lifecycle/transcript/search/resume-lineage path while preserving native message encoding/counters/FTS/WAL behavior, scoped append/read/search helpers, collision-resistant AgentOps session IDs derived from `RuntimeContext` conversation scope, and expiry-aware per-conversation turn locks. The generic `SessionBackend` protocol covers transcript append/read/search, resume lineage, and turn-lock claim/renew/release; fake remote backends can be registered through `RuntimeBackendRegistry` and exercised through the same protocol. `LocalRunSupervisor.process_turn(...)` now routes worker turn writes through the registry-selected `SESSION` backend, strips payload-supplied `session_id` before persistence so request bodies cannot steer scope, requires conversation identity for AgentOps conversation turns, claims the turn lock before mutating session state, renews/checks the lock before outbound writes to fail closed on mid-turn expiry, releases locks on success/failure, and preserves secret-safe audit errors. The default in-memory `LocalSessionBackend` now keys transcript/turn-lock state by tenant/user/profile/project/conversation identity rather than per-run IDs so a restarted or rescheduled worker with a new run ID can resume the same conversation, while unrelated users/conversations remain isolated. Test evidence: `./scripts/run_tests.sh tests/agent/test_runtime_supervisor.py tests/agent/test_runtime_session_backend.py tests/agent/test_runtime_backends.py` → 42 passed; `python -m ruff check agent/runtime_supervisor.py agent/runtime_backends.py tests/agent/test_runtime_supervisor.py tests/agent/test_runtime_session_backend.py` → passed; `git diff --check` → passed.

**Acceptance criteria:**

- Existing SQLite session behavior is wrapped as `LocalSQLiteSessionBackend`.
- Remote session backend contract supports append/read/search/resume lineage.
- Worker can process a turn and write transcript/tool events to remote session backend.
- Concurrent turn lock/lease semantics are specified and tested.
- Conversation/session state survives worker restart and resumes in a different worker.

### M7. Skills backend abstraction

**Status:** Done

**Goal:** Keep native Hermes skills while allowing remote scoped skill sources.

**Planning note (2026-05-31):** M6 is complete and pushed at `7854bd839`, so M7 is the next roadmap target when implementation resumes. The implementation must preserve native `skills_list`, `skill_view`, and `skill_manage` semantics rather than adding a sidecar prompt-injection mechanism. Existing auto-loaded skill bindings from platform/channel/topic configuration are load-time selections only; they should resolve through the same scoped skill backend path and must not silently grant mutation rights or bypass policy.

**Completion note (2026-05-31):**

- `SkillBackend` (agent/runtime_backends.py) extended beyond raw `list_skills`/`load_skill` strings to a native-shaped contract: metadata listing, full progressive-disclosure load (main content or linked file), and `manage_skill` mutation with scope/policy. Return shapes match the existing tool output.
- `LocalSkillBackend` (agent/runtime_backends.py) reimplemented as a thin wrapper over the native filesystem discovery/loading: it delegates to `tools.skills_tool._skills_list_impl`/`_skill_view_impl` and `tools.skill_manager_tool._skill_manage_impl`, so linked files, readiness/setup metadata, platform compatibility, prompt-injection scanning, the pinned-delete guard, and `absorbed_into` semantics are preserved unchanged. It remains the registry default for the `skill` capability and the fallback when no remote skill backend is registered.
- `ScopedSkillBackend` (agent/runtime_skills.py) is the in-memory, multi-tenant reference remote backend: visibility + deterministic precedence over `SCOPE_PRECEDENCE = (runtime, user, project, org, bundled)`, linked-file reads, readiness metadata, and mutation policy (user-private/runtime allowed; shared org/project require `RuntimeContext.metadata['skill_write_approved']` or `allow_shared_write=True`; bundled read-only; pinned-delete guard; `absorbed_into` target validation). Error/load payloads never echo another tenant's content or private paths. Registrable via `register_scoped_skill_backend(...)`.
- Native surfaces route through the selected backend only when a `RuntimeContext` selects AgentOps mode and a backend is bound (`set_active_skill_backend`); otherwise the default single-user local filesystem read path is used unchanged. AgentOps mutations fail closed with `agentops_skill_backend_required` when no backend is bound, so a missing or misconfigured remote backend cannot silently write local filesystem skills. Routing lives inside the public `skills_list`/`skill_view`/`skill_manage` functions, so auto-loaded channel/topic/preload skills (which call `skill_view`) resolve through the scoped backend read-only without gaining mutation rights. agent/agent_init.py binds the active backend in AgentOps mode.

**Test evidence (RED → GREEN):**

- `tests/agent/test_runtime_skill_backend.py`, `tests/tools/test_skills_runtime_backend.py`, and `tests/tools/test_skill_manager_runtime_backend.py` — 34 tests covering contract/registry, AgentOps backend resolution fail-closed behavior, RuntimeContext-keyed backend isolation (sequential + concurrent), tenant isolation (user A cannot list/load user B private; org shared within org only; runtime records require full tenant/runtime identity), precedence, linked files, mutation policy (approval/fail-closed/bundled read-only/pinned/absorbed_into), no-leak payloads, native routing, autoload read-only, local default/read fallback, and `LocalSkillBackend` filesystem wrapping.
- Regression: `tests/tools/test_skills_tool.py`, `tests/tools/test_skill_manager_tool.py`, and `tests/agent/test_runtime_backends.py` pass (188 passed).
- Hygiene: `python -m ruff check agent/runtime_backends.py agent/runtime_skills.py tools/skills_tool.py tools/skill_manager_tool.py agent/agent_init.py tests/agent/test_runtime_skill_backend.py tests/tools/test_skills_runtime_backend.py tests/tools/test_skill_manager_runtime_backend.py` passes; `git diff --check` passes.

**Scope model:**

- built-in bundled skills
- org skills
- team/project skills
- user-private skills
- runtime/ephemeral skills

**Implementation notes:**

- Wrap existing filesystem/profile skill discovery as `LocalSkillBackend`, including bundled/user/external skill directories, platform compatibility filtering, linked file access, readiness metadata, and existing validation/guard behavior.
- Extend `SkillBackend` beyond raw `list_skills`/`load_skill` strings if needed so it can represent progressive disclosure metadata, linked files, categories, readiness/setup status, and mutation operations without losing current tool output shape.
- Route native skill tools through the runtime backend registry when a `RuntimeContext` selects an AgentOps/local-multi profile; preserve the default single-user local path when no distributed mode is selected.
- Model mutation authorization explicitly: user-private skill writes may be allowed by policy, while shared org/project skills require an approval or policy flag. Pinned/delete protections and `absorbed_into` semantics must be preserved.
- Preserve deterministic skill precedence across bundled, org, project/team, user-private, external-dir, and runtime/ephemeral sources. Precedence must be testable and documented because auto-loaded channel/topic skills depend on stable resolution.
- Include audit events for skill list/load/mutation attempts, but never leak private local paths, secret-looking setup values, or another tenant's skill content.

**Acceptance criteria:**

- Existing filesystem skill loading is `LocalSkillBackend`.
- Remote skill backend can list/load skill content by RuntimeContext.
- Skill mutation permissions are represented: user-private allowed, shared org/project skills require policy/approval flag.
- Tests prove user A cannot load user B private skill and org skills can be shared.
- Skill precedence is deterministic across local and remote sources.
- Auto-loaded channel/topic skills resolve through the scoped backend path without granting implicit mutation privileges.
- Existing linked-file, readiness/setup metadata, platform compatibility, pinned-delete guard, and skill security scan behavior are preserved or explicitly mapped.

### M8. Cron/autonomous jobs backend abstraction

**Status:** Done

**Completion note (2026-05-31):** Completed the cron/autonomous jobs backend abstraction across four small slices. The native `CronBackend` contract now covers CRUD/list/history plus worker lease/run-recording semantics; `LocalCronBackend` preserves existing local behavior while recording stable non-secret RuntimeContext/delivery bindings and durable cron scoping; `HttpCronBackend` provides a provider-neutral remote-control-plane surface for Compose/cloud adapters with fail-closed validation and header-only bearer authentication; `SQLiteCronBackend` persists scoped jobs, run history, and worker leases with WAL/busy-timeout settings and explicit transactional claim/renew/finish mutations; and `agent/runtime_cron_worker.py` adds the shared backend-agnostic execution path for local, Compose, and future cloud workers. Focused tests cover recurring, one-shot, paused, lease timeout/recovery, scheduler restart via SQLite reopen, worker drain, delivery routing, silent output, sanitized error-alert recording, lost-lease behavior before delivery/completion, and local/remote logical job-model parity without adding cloud-specific core names.

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

**Status:** Done

**Autonomous run note (2026-06-01):** Completed the credential/secret resolver slice with `agent/runtime_credentials.py`, a `RuntimeCredentialBroker` that resolves capability/ref requests through the pluggable `CredentialResolver` + `SecretStore` contracts, returns redacted `CredentialHandle` objects, exposes secrets to legacy provider/tool paths only inside the provider execution path, and records audit metadata containing refs/status rather than raw values. `LocalCredentialResolver` now supports explicit logical-ref bindings plus local compatibility fallback for existing env/profile-file provider secrets in local mode, while AgentOps mode fails closed, rejects ambient `env:`/`profile:` secret refs, blocks runtime-provider fallback and auto-detection from local env/profile credentials, and does not read or mutate local auth sources during provider-pool loading even when RuntimeContext comes from config instead of a bound ContextVar. Agent initialization binds the scoped broker immediately after RuntimeContext resolution, before client/provider construction. The native provider credential pool can seed runtime-broker credentials into a non-persistent runtime pool without copying raw values into the local profile auth store, and both active broker lookup and local credential storage are stable across run-id replacement while still partitioned by tenant/user/profile/project, backend profile, and permissions policy. Focused tests cover ref-based resolution, cross-user isolation, run-id-stable lookup, backend-profile/permissions partitioning, audit redaction, scoped env restoration, fail-closed missing refs, local env/profile compatibility through the contract layer, native provider-pool broker seeding, AgentOps runtime-provider fail-closed/no-local-autodetect behavior, config-context runtime-provider fail-closed behavior, non-persistent runtime-pool status updates, and no local-secret fallback in AgentOps mode. Compose/cloud profiles continue to use the same registry contracts by registering profile-specific credential/secret adapters; no cloud-specific logic is hard-coded into the core broker.

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

**Status:** Done

**Completion note (2026-06-01):** Completed the artifact/audit backend slice. `agent/runtime_artifacts_audit.py` provides local durable file-backed artifact and audit backends scoped by `RuntimeContext`, provider-neutral HTTP artifact/audit adapters for remote control-plane profiles, registry registration helpers, path-escape rejection for artifact refs, RuntimeContext scope serialization, and recursive audit sanitization for secret-like keys, secret-like/path-bearing text, and local path fields. `RuntimeBackendRegistry` now instruments selected native backend methods in-place while preserving backend object identity for mutable local/native adapters, falling back to a proxy only when protocol-compatible slotted adapters cannot be mutated, so the selected audit backend receives events for memory writes, skill list/load/mutation, session events, cron scheduling/run/lease operations, queue operations, run leases, conversation routing, worker registry lifecycle, artifact access, and delivery. `model_tools.handle_function_call` records sanitized tool-call audit events for direct scoped tool dispatches, and `agent/tool_executor.py` records scoped audit events for normal sequential/concurrent agent tool paths, including agent-level tools and denied attempts. Credential resolution already records through `RuntimeCredentialBroker`, and worker lifecycle/run failures continue to record through `LocalRunSupervisor`; existing supervisor tests were updated to tolerate the additional backend-level audit events. Tests prove local artifact durability/isolation, unsafe ref and symlink rejection, sanitized local audit JSONL persistence, HTTP artifact/audit registry selection without leaking bearer tokens into payloads, path/secret redaction (including failure exception/result-preview text and audit-backend boundary sanitization), tool-call audit emission, shared/slotted backend compatibility, and required runtime-surface audit event emission. Test evidence: `python -m pytest tests/agent/test_runtime_artifacts_audit.py tests/agent/test_runtime_backends.py tests/agent/test_runtime_supervisor.py tests/agent/test_runtime_credentials.py tests/tools/test_memory_tool.py tests/tools/test_skills_runtime_backend.py tests/tools/test_skill_manager_runtime_backend.py tests/test_model_tools.py tests/test_transform_tool_result_hook.py -q` → 206 passed; `python -m ruff check agent/runtime_backends.py agent/runtime_artifacts_audit.py model_tools.py agent/tool_executor.py tests/agent/test_runtime_artifacts_audit.py tests/agent/test_runtime_supervisor.py` → passed; `git diff --check` → passed.

**Goal:** Centralize durable artifacts and audit trails for distributed runs.

**Acceptance criteria:**

- Local artifacts remain available.
- Remote artifact backend stores tool outputs/files by scoped refs.
- Audit backend receives memory writes, skill loads/mutations, credential resolutions, cron runs, session events, worker lifecycle events, queue/lease events, delivery events, and tool calls.
- Tests prove sensitive local paths/secrets are not surfaced in audit payloads.

### M11. Worker fleet and run lifecycle

**Status:** Done

**Autonomous run note (2026-06-01):** Continued M11. This run added explicit lifecycle surface for run-to-completion cancellation requests and max-runtime classification: `LocalRunSupervisor.cancel_run(...)` records cooperative cancellation by durable job/run key, active runs report `RunStatus.CANCELLED` at the next lifecycle boundary, and `run_to_completion(..., max_runtime_seconds=...)` reports a failed terminal result when a completed callable exceeded its allowed runtime. Focused tests also cover scoped backend profile selection for `local-multi`, `compose-self-hosted`, and `aws-managed` profile names via registered backend contracts. Test evidence: `python -m pytest tests/agent/test_runtime_supervisor.py tests/agent/test_runtime_context.py -q` → 66 passed, 1 known dependency deprecation warning; `python -m ruff check agent/runtime_supervisor.py tests/agent/test_runtime_supervisor.py` → passed; `git diff --check` → passed. M11 remains Started after independent review: cancellation is same-supervisor/in-memory and cooperative, max-runtime enforcement is post-call classification rather than forcibly exiting stuck user code, and the profile evidence proves registry selection under profile names rather than real Compose/AWS adapter operation. Remaining acceptance work is durable/fleet-visible cancellation or an explicit roadmap deferral, actual max-runtime termination semantics (likely process-backed for run-to-completion), and stronger lifecycle evidence for compose/AWS adapter profiles before marking M11 Done.

**Autonomous run note (2026-06-01, later):** Continued M11 max-runtime work. `LocalRunSupervisor(run_isolation="process").run_to_completion(..., max_runtime_seconds=...)` now executes the user callable in a spawned child process, keeps durable lease/audit ownership in the parent, terminates/kills the child when the runtime budget expires, releases the per-job lease, and records a failed terminal result. The child returns successful or failed outcomes through a temporary pickle result file instead of a multiprocessing queue, avoiding false timeouts for large results and explicit-failing unpickleable results rather than silently reporting success with `None`. Test evidence: focused RED/GREEN on `test_process_isolated_run_to_completion_terminates_when_max_runtime_expires`; focused RED/GREEN on `test_process_isolated_run_to_completion_returns_failed_result_when_child_cannot_start`; `python -m pytest tests/agent/test_runtime_supervisor.py -q` → 59 passed; `python -m ruff check agent/runtime_supervisor.py tests/agent/test_runtime_supervisor.py` → passed; `git diff --check` → passed. M11 remains Started: remaining acceptance work is durable/fleet-visible cancellation (or an explicit deferral), process-tree cleanup for user-spawned grandchildren on max-runtime/cancel, and stronger lifecycle evidence for compose/AWS adapter profiles before marking M11 Done.

**Completion note (2026-06-01):** M11 is complete for the MVP worker-fleet/run-lifecycle contract layer. `LocalRunSupervisor` registers worker capacity/capabilities, supports bounded 0-N concurrent runs, warm conversations with idle timeout, per-conversation sequential turn queues, run-to-completion jobs with scoped leases, drain/shutdown refusal semantics, expired-lease recovery through `RunLeaseBackend.expire_stale`, per-job cron leases so unrelated scheduled jobs run concurrently, and profile-selected lifecycle paths for `local-multi`, `compose-self-hosted`, and `aws-managed` backend profiles. This final slice closed the prior blockers by adding durable/fleet-visible cancellation requests to the required `RunLeaseBackend` contract/`LocalRunLeaseBackend` so one worker can cancel another worker's active run at the next lifecycle boundary, by interrupting active process-isolated cancellation before max-runtime expiry, and by cleaning up process-isolated user-spawned subprocess trees on POSIX max-runtime/cancel paths using a child process group. Real Compose deployment operation remains M12 and real cloud adapter operation remains M14; M11's adapter-profile criterion is satisfied at the core lifecycle/registry contract layer with fake/local registered backends, not by shipping Compose/AWS infrastructure. Test evidence: focused RED/GREEN for `test_process_isolated_run_to_completion_cleans_up_user_spawned_process_tree`, `test_run_to_completion_observes_fleet_visible_cancel_requested_by_another_worker`, and `test_process_isolated_run_to_completion_cancel_terminates_user_spawned_process_tree`; `python3 -m pytest tests/agent/test_runtime_supervisor.py -q -o 'addopts='` → 62 passed; `python3 -m pytest tests/agent/test_runtime_backends.py tests/agent/test_runtime_context.py -q -o 'addopts='` → 31 passed, 1 known dependency deprecation warning; `python3 -m ruff check agent/runtime_supervisor.py agent/runtime_backends.py tests/agent/test_runtime_supervisor.py` → passed; `git diff --check` → passed.

**Goal:** Support Derek’s production-style model: scale worker tasks up/down, each task hosting zero to N concurrent Hermes runs, with warm conversations and run-to-completion jobs.

**Acceptance criteria:**

- Worker registers with capacity, capabilities, and `max_concurrent_runs`.
- Worker can run 0–N concurrent Hermes runs, initially preferably subprocess-per-run for isolation.
- Conversation run starts on incoming message and stays warm until configurable idle timeout.
- Event/GitHub/Linear/cron/manual run works until done/fail/cancel/max runtime, then exits.
- Per-conversation turns are processed sequentially or explicitly queued while a run is busy.
- Worker drain prevents new claims and gracefully finishes, checkpoints, or releases active runs before shutdown.
- Expired leases allow recovery after worker death.
- Cron/job execution uses per-job/per-firing leases rather than a global run lock, so unrelated scheduled jobs can run concurrently; a long repo/workdir autonomous builder cannot block a lightweight watchdog job that becomes due later.
- Same lifecycle works in local-multi, compose-self-hosted, and AWS adapter profiles.

### M12. Compose self-hosted distributed MVP

**Status:** Done

**Autonomous run note (2026-06-01):** Started M12 with the first Compose packaging slice. Added `deploy/compose/docker-compose.yml`, `.env.example`, and README covering an API/control-plane service, horizontally scalable workers with per-worker slot configuration, scheduler, Postgres database, Redis queue, MinIO artifact store, and a local development secret-store service. Added packaged `agentops_runtime.compose_services`, a minimal stdlib health service used by the API/worker/scheduler/local-secrets containers so `docker compose config` validates a runnable topology while durable adapter wiring is completed in later M12 slices. Static tests assert the required services, health dependencies, scale-safe worker shape, AgentOps runtime profile/backend refs, DB credential wiring, package inclusion, and no raw app/integration secrets in the sample env. Test evidence: focused RED/GREEN for missing Compose profile; focused RED/GREEN for reviewer-found DB URL/package discovery gaps; `python3 -m pytest tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 5 passed; `python3 -m ruff check agentops_runtime/compose_services.py tests/deploy/test_agentops_compose_profile.py` → passed; `docker compose -f deploy/compose/docker-compose.yml config` → passed; `git diff --check` → passed. M12 remains Started: remaining acceptance work is wiring real Compose durable backends through the registry, proving horizontal workers/multiple slots with actual runs, proving two-user/thread memory/session isolation plus shared org/project skills, remote cron duplicate prevention with multiple schedulers/workers, and worker restart/resume from durable state.

**Autonomous run note (2026-06-01, later):** Continued M12 by adding `agentops_runtime.compose_backends.configure_compose_runtime_backends(...)`, a small Compose wiring helper that registers the existing provider-neutral HTTP memory, cron, artifact, and audit adapters for the `compose-self-hosted` profile from credential-free env/config URLs plus an optional control-plane bearer token. `RuntimeBackendRegistry.set_capability_options(...)` now provides a public way to set static factory options and invalidate cached backend instances instead of mutating private registry config. Focused tests prove HTTP adapter selection for the compose profile, per-capability URL overrides, fail-closed missing/unsafe URLs, token header-only handling, stale-token clearing on reconfiguration, app/integration secret exclusion from options, config-over-env precedence, and registry option merge/cache invalidation. Test evidence: `python -m pytest tests/agentops_runtime/test_compose_backends.py tests/agent/test_runtime_backends.py::test_set_capability_options_merges_options_and_invalidates_cached_instance -q -o 'addopts='` → 10 passed; adjacent suite `python -m pytest tests/agent/test_remote_memory_backend.py tests/agent/test_remote_cron_backend.py tests/agent/test_runtime_artifacts_audit.py tests/deploy/test_agentops_compose_profile.py tests/agentops_runtime/test_compose_backends.py tests/agent/test_runtime_backends.py -q -o 'addopts='` → 82 passed, 1 known dependency deprecation warning after the stale-token regression test was added; `python -m ruff check agentops_runtime/compose_backends.py tests/agentops_runtime/test_compose_backends.py agent/runtime_backends.py tests/agent/test_runtime_backends.py` → passed; `git diff --check` → passed. Independent review found and this run fixed a stale-token retention blocker by replacing compose capability options on reconfiguration. M12 remains Started: remaining acceptance work is invoking this wiring from the actual Compose service/runtime startup path, durable session/queue/skill backend proof, horizontal worker/multi-slot actual-run proof, remote cron duplicate-prevention proof with multiple schedulers/workers, and worker restart/resume from durable state.

**Autonomous run note (2026-06-01, service wiring):** Continued M12 by invoking Compose runtime backend wiring from the actual `agentops_runtime.compose_services` readiness path for API, worker, and scheduler containers. Their `/healthz`/`/readyz` payloads now fail closed when `configure_compose_runtime_backends(...)` cannot register the HTTP memory/cron/artifact/audit adapters, and report `compose_backends_configured` plus a backend error on failure; `local-secrets` remains a simple health-only service. The Compose profile now provides `AGENTOPS_API_URL=http://api:8710` to the runtime services so health checks exercise the wiring instead of silently running with only local defaults. Test evidence: focused RED/GREEN for `tests/agentops_runtime/test_compose_services.py`; focused RED/GREEN for the missing `AGENTOPS_API_URL` Compose contract; `python3.11 -m pytest tests/agentops_runtime/test_compose_services.py tests/agentops_runtime/test_compose_backends.py tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 16 passed; `python3.11 -m ruff check agentops_runtime/compose_services.py tests/agentops_runtime/test_compose_services.py tests/deploy/test_agentops_compose_profile.py` → passed; `docker compose -f deploy/compose/docker-compose.yml config` → passed; `git diff --check` → passed. M12 remains Started: remaining acceptance work is proving horizontal workers/multiple slots with actual runs, two-user/thread memory and session isolation plus shared org/project skills, remote cron duplicate prevention with multiple schedulers/workers, and worker restart/resume from durable state.

**Autonomous run note (2026-06-01, distributed smoke):** Continued M12 by adding `agentops_runtime.compose_smoke.create_compose_smoke_registry(...)`, a hermetic Compose-profile registry that binds every runtime capability to shared contract backends under `compose-self-hosted` for distributed-semantics smoke tests without Docker/network flakiness. New tests prove horizontally scaled worker supervisors can use multiple slots concurrently, two users/threads keep memory and session state isolated while loading the same project-scoped skill, two schedulers do not duplicate a leased long-running cron job while an unrelated watchdog firing still runs, and a restarted worker resumes a conversation from durable shared session state. Test evidence: focused RED for missing compose smoke registry, GREEN `python3 -m pytest tests/agentops_runtime/test_compose_distributed_smoke.py -q -o 'addopts='` → 4 passed; `python3 -m ruff check agentops_runtime/compose_smoke.py tests/agentops_runtime/test_compose_distributed_smoke.py` → passed. M12 remains Started: remaining acceptance work is connecting this smoke proof to real running Compose services/control-plane APIs and `docker compose up` health, plus any deeper end-to-end evidence needed before marking the slice Done.

**Autonomous run note (2026-06-01, running-service health smoke):** Continued M12 by adding `agentops_runtime.compose_health_smoke`, a packaged smoke surface that checks the *running* Compose services over HTTP and returns a structured, fail-closed JSON report. It probes `/healthz` and `/readyz` for `api`/`worker`/`scheduler` and `/healthz` for the health-only `local-secrets` service, targets the in-network Compose service DNS names by default (`http://api:8710`, `http://worker:8711`, `http://scheduler:8712`, `http://local-secrets:8713`) with per-service env overrides (`AGENTOPS_API_URL`/`AGENTOPS_WORKER_URL`/`AGENTOPS_SCHEDULER_URL`/`AGENTOPS_SECRET_STORE_URL`), and is invokable as a module entrypoint (`python -m agentops_runtime.compose_health_smoke`, exit 0 healthy / 1 unhealthy). It fails closed on unreachable services, non-200 status, or `"ok": false` payloads. Static/contract tests inject a fake fetcher so no Docker/network is required: they prove URL/endpoint targeting (readiness probed for api/worker/scheduler but not local-secrets), env overrides, fail-closed behavior for unreachable/unhealthy/status-ok-but-payload-not-ok cases, default-fetch preservation of HTTP error status/backend-error payloads, and the CLI exit codes. `deploy/compose/README.md` now documents the exact `docker compose up` + `docker compose exec api python -m agentops_runtime.compose_health_smoke` workflow from the compose directory. Test evidence: focused RED (missing module import) → GREEN `python3.11 -m pytest tests/agentops_runtime/test_compose_health_smoke.py -q -o 'addopts='` → 9 passed; suite `python3.11 -m pytest tests/agentops_runtime/ tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 29 passed; `python3.11 -m ruff check agentops_runtime/compose_health_smoke.py tests/agentops_runtime/test_compose_health_smoke.py tests/deploy/test_agentops_compose_profile.py` → passed; `docker compose -f deploy/compose/docker-compose.yml config` → passed; `git diff --check` → passed; live entrypoint run against a non-running stack fails closed with exit 1 and a structured report. M12 remains Started: this slice supplies the running-service/control-plane health-proof tooling and docs, but a captured live `docker compose up` green-health transcript (image build + postgres/redis/minio/local-secrets actually healthy, then a passing smoke run) still requires a Docker host, which was unavailable in this hermetic session. That live-stack evidence is the last remaining item before M12 can be honestly marked Done.

**Autonomous run note (2026-06-01, one-shot smoke service):** Continued M12 by adding a Compose-profile-gated `smoke` one-shot service that runs the packaged `python -m agentops_runtime.compose_health_smoke` check inside the Compose network after `api`, `worker`, `scheduler`, and `local-secrets` report healthy. The service is scale-safe (no fixed `container_name`), does not start during normal `docker compose up`, and carries only in-network service URLs rather than raw app/integration secrets. `deploy/compose/README.md` now documents `docker compose --profile smoke run --rm smoke` from the compose directory while preserving the manual `docker compose exec api python -m agentops_runtime.compose_health_smoke` path. Test evidence: focused RED for missing `smoke` service and README command; GREEN `python3 -m pytest tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 6 passed; adjacent suite `python3 -m pytest tests/agentops_runtime/ tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 30 passed; `python3 -m ruff check tests/deploy/test_agentops_compose_profile.py` → passed; `docker compose -f deploy/compose/docker-compose.yml config` → passed; `docker compose config --services` excludes `smoke` and `docker compose --profile smoke config --services` includes it; `git diff --check` → passed. Live `docker compose build/up` could not be verified in this cron environment because the Docker daemon was unavailable (`Cannot connect to the Docker daemon`). M12 remains Started: remaining acceptance work is a live green Docker Compose startup/smoke transcript on a host with Docker daemon access, or explicit roadmap closure if static/service-level evidence is deemed sufficient.

**Autonomous run note (2026-06-01, live smoke script):** Continued M12 by adding `deploy/compose/smoke.sh`, an executable compose-directory script that checks Docker daemon availability, starts the stack with `docker compose up --build -d`, runs the in-network `docker compose --profile smoke run --rm smoke` health probe, and tears the stack down with `docker compose down --remove-orphans`. `deploy/compose/README.md` now points operators at `./smoke.sh` for the live transcript while still documenting the exact underlying commands for keeping the stack up. Test evidence: focused RED for missing script → GREEN `python3 -m pytest tests/deploy/test_agentops_compose_profile.py::test_compose_profile_includes_live_smoke_script_from_compose_dir -q -o 'addopts='`; adjacent suite `python3 -m pytest tests/agentops_runtime/ tests/deploy/test_agentops_compose_profile.py -q -o 'addopts='` → 31 passed; `python3 -m ruff check tests/deploy/test_agentops_compose_profile.py` → passed; `docker compose -f deploy/compose/docker-compose.yml config` → passed; `(cd deploy/compose && ./smoke.sh)` fails closed with `error: Docker daemon is not available. Start Docker and re-run ./smoke.sh.` in this cron environment. M12 remains Started: remaining acceptance work is to run `deploy/compose/smoke.sh` on a host with an available Docker daemon and capture a green transcript, or explicitly close the live-Docker evidence requirement in the roadmap.

**Autonomous run note (2026-06-02, live smoke completion):** Completed the remaining M12 live Docker evidence by starting Docker Desktop on the cron host and running `(cd deploy/compose && ./smoke.sh)` successfully. The script built/reused the `agentops-hermes-runtime:local` image, started Postgres, Redis, MinIO, local-secrets, API, worker, and scheduler, waited for health checks, then ran the in-network smoke service. The smoke JSON reported `"ok": true` for API `/healthz` and `/readyz`, worker `/healthz` and `/readyz`, scheduler `/healthz` and `/readyz`, and local-secrets `/healthz`; the script then tore the stack down with `docker compose down --remove-orphans`. This closes the final live-stack evidence gap. Prior M12 tests already cover horizontally scaled workers with multiple slots, two-user/thread memory and session isolation with shared project skills, duplicate-preventing cron leases across multiple schedulers/workers, and worker restart/resume from durable shared session state. M12 is now Done.

**Goal:** Prove distributed semantics without cloud lock-in.

**Acceptance criteria:**

- `docker compose up` starts API/control-plane, worker(s), scheduler, database, queue, object store, and local secret backend.
- Workers can be scaled horizontally and each worker can host multiple concurrent runs.
- Two users/threads get isolated memory/sessions while shared org/project skills can be loaded in both.
- Remote cron/leases prevent duplicate job execution with multiple schedulers/workers.
- Worker restart between turns resumes from durable state.

### M13. Slack multi-user/thread MVP

**Status:** Done

**Goal:** Prove business messaging integration with multiple users and threads.

**Completion note (2026-06-02):** Added `agentops_runtime.slack_runtime.run_slack_turn(...)` / `SlackTurnResult`, a hermetic end-to-end Slack ingress orchestrator that wires the existing native surfaces together: it routes an incoming Slack turn through the selected `ConversationRouter`, binds a RuntimeContext to the routed warm `run_id`, processes the scoped turn on a `LocalRunSupervisor` worker via the warm-turn path (`submit_warm_turn(...)`, falling back to `process_turn(...)` for a supervisor without it) against the Compose-profile contract backends (`MemoryBackend`, `SessionBackend`, `SkillBackend`), and delivers the reply back through `SlackDeliveryBackend` to the originating Slack thread metadata. Because the turn executes under the routed warm-run context, the handler actually runs as the selected warm run rather than only observing router bookkeeping. It imports no Slack SDK and carries no credentials. New focused smoke tests in `tests/agentops_runtime/test_slack_runtime_smoke.py` prove all acceptance criteria against shared in-process backends from `create_compose_smoke_registry()`: two Slack users in different threads (same workspace/channel/org/project) get isolated native per-user/thread memory and session transcripts while both load the same shared `project`-scope skill; the assistant reply is delivered to the correct thread metadata and never leaks to the other thread; a follow-up message in the same thread routes to the same active warm run (`routed_to_active_run` flips False→True, same `run_id`) AND the handler's ambient `RuntimeContext.run_id` observed during execution equals that warm run id for both the first turn and the follow-up; and a worker restarted between turns (new `worker_id`) still resumes the durable session transcript because session scope excludes per-run identity. RED/GREEN evidence: the smoke module first failed with `ImportError: cannot import name 'run_slack_turn'`; the review-gate test `test_run_slack_turn_executes_turns_under_the_selected_warm_run_context` then failed RED with the handler observing `run_id is None` before the warm-run binding was added, then `python3 -m pytest tests/agentops_runtime/test_slack_runtime_context.py tests/agentops_runtime/test_slack_runtime_smoke.py -q -o 'addopts='` → 12 passed; `python3 -m pytest tests/agentops_runtime/ tests/gateway/test_slack.py -q -o 'addopts='` → 222 passed (pre-existing AsyncMock warnings only); `ruff check agentops_runtime/slack_runtime.py tests/agentops_runtime/test_slack_runtime_smoke.py` → passed; `git diff --check` → clean.

**Autonomous run note (2026-06-02):** Continued M13 by adding Slack runtime routing and delivery contract surfaces. `agentops_runtime.slack_runtime.route_slack_turn(...)` routes Slack events through the selected `ConversationRouter` so follow-up turns in the same Slack workspace/channel/thread can target an active warm run instead of creating a new one, while the turn payload carries non-secret Slack message/channel/thread/user and delivery refs. The helper accepts an injected registry for production wiring and has a process-local default router for direct compose-profile smoke use. `SlackDeliveryBackend` maps scoped Slack `RuntimeContext` delivery back to the native Slack adapter's channel + `metadata={thread_id, thread_ts}` shape without importing Slack SDK credentials, and handles native async send callables when invoked from synchronous backend code. Focused RED/GREEN tests prove follow-up routing reuses the same active run and marks it as active, the default compose-profile helper path does not fail on an unregistered fresh registry, native-shaped sync and async send callables receive metadata by keyword rather than as `reply_to`, and two Slack thread contexts produce separate delivery metadata so replies target the correct thread. Existing context-mapping tests continue to prove workspace/channel/thread/user mapping and same-thread user separation. Test evidence: `python3 -m pytest tests/agentops_runtime/test_slack_runtime_context.py -q -o 'addopts='` → 8 passed; `python3 -m pytest tests/agentops_runtime/test_slack_runtime_context.py tests/gateway/test_slack.py -q -o 'addopts='` → 194 passed with existing Slack test AsyncMock warnings; `python3 -m ruff check agentops_runtime/slack_runtime.py tests/agentops_runtime/test_slack_runtime_context.py` → passed; `git diff --check` → passed. This prior remaining-work note was closed by the M13 completion note above, which added the hermetic end-to-end Slack/worker smoke for native memory/session isolation, shared project skills, warm-run routing, threaded delivery, and worker restart/resume.

**Acceptance criteria:**

- Slack workspace/channel/thread/user maps to RuntimeContext. (Done: `agentops_runtime/slack_runtime.py` maps Slack event/command payloads when AgentOps mode is enabled, Slack `MessageEvent` instances carry the context, and gateway-created `AIAgent` instances receive it.)
- Two Slack users in different threads get isolated user/thread memory. (Done: `test_two_slack_users_in_different_threads_get_isolated_memory_and_sessions_with_shared_skill`.)
- Shared org/project skill can be loaded in both threads. (Done: same test loads the shared `project`-scope `acme-style` skill in both threads.)
- Hermes replies in the correct Slack thread. (Done: same test asserts per-thread delivery metadata with no cross-thread leak.)
- Active conversation routing sends follow-up messages to the warm run when one exists. (Done: `test_followup_slack_message_in_same_thread_routes_to_same_warm_run`.)
- Worker can be restarted between turns and still resume state remotely. (Done: `test_worker_restart_between_slack_turns_resumes_remote_session_state`.)

### M14. Cloud adapter spike: AWS first, GCP later

**Status:** Done

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

**Autonomous run note (2026-06-02):** First reviewable spike slice landed. This is a contract-level spike, **not** a real AWS deployment — no boto3, AWS credentials, network, or Terraform are involved, and no ECS/SQS/RDS/S3/Secrets Manager/CloudWatch infrastructure is provisioned. What shipped: a new adapter module `agentops_runtime/aws_managed.py` isolating all AWS-specific naming (the `aws-managed` profile string and the `EcsWorkerFleetPlan` planner) outside the core contract modules; `configure_aws_managed_runtime_backends`/`build_aws_managed_test_registry` register the existing local/fake backends under the `aws-managed` profile through the unchanged `RuntimeBackendRegistry` contract; `build_aws_managed_run_supervisor` runs the same `LocalRunSupervisor` lifecycle under `RuntimeContext.backend_profile='aws-managed'`; and `EcsWorkerFleetPlan` proves desired ECS task count and per-task `max_concurrent_runs` scale independently (`capacity = desired_task_count * max_concurrent_runs`; rescaling one returns a new immutable plan and never mutates the other; both validate positive integers and fail closed). Acceptance-criteria status against this slice: criteria 1 (cloud code isolated to adapter module), 2 (local/fake remains default test path), and 4 (independent ECS task-count vs per-task concurrency) are satisfied at the contract layer with a leakage guard test asserting core modules carry no AWS provider/service strings; criterion 3 is satisfied only for the local/Compose paths plus an `aws-managed`-profiled local lifecycle — **real AWS-mode execution is still pending**, so Status stays Started. Test evidence: focused RED (`ModuleNotFoundError: agentops_runtime.aws_managed`) → GREEN `python -m pytest tests/agentops_runtime/test_aws_managed_runtime.py` → 16 passed; adjacent `tests/agent/test_runtime_backends.py tests/agent/test_runtime_context.py tests/agentops_runtime/test_compose_backends.py` → 41 passed (1 pre-existing unrelated `audioop` deprecation warning); `ruff check agentops_runtime/aws_managed.py tests/agentops_runtime/test_aws_managed_runtime.py` → passed; `git diff --check` → clean.

**Autonomous run note (2026-06-02) — closes the spike:** The prior slice's named gap was AWS-mode execution evidence for criterion 3 ("same worker lifecycle runs locally, in Compose, and in AWS adapter mode"). This slice closes it at the spike/contract layer by adding an AWS-shaped queued-item ingress that flows through the *unchanged* shared lifecycle: `AwsManagedWorkItem` models an SQS/EventBridge envelope (`message_id`, optional `receipt_handle`, `run_type`/`run_id`/`job_id`, tenant/user/project/conversation/agent-profile scope, `payload`, optional `delivery_ref`, `backend_profile` defaulting to `aws-managed`); `to_context()` maps it to an `agentops` `RuntimeContext` with `backend_profile='aws-managed'` and surfaces only the non-secret `work_item_id` (the SQS `receipt_handle` delete/visibility credential is deliberately kept out of scope metadata and audit); and `run_aws_managed_work_item(work_item, handler, …)` runs it via the same `RuntimeBackendRegistry` + `LocalRunSupervisor.run_to_completion` path used by local/Compose — claiming the same per-run lease, recording the same `started`/`succeeded` lifecycle audit, binding the AWS-managed context on the native surface, and passing the payload to the handler. The per-task `max_concurrent_runs` bound stays independent of fleet `desired_task_count` (a plan with `desired_task_count=8, max_concurrent_runs=2` yields `capacity=16` but a per-task supervisor bound of `2`, unchanged after running an item). **Scope/honesty:** this remains AWS-mode *contract/spike* execution — **no boto3, AWS credentials, network, real SQS/ECS/Fargate/RDS/S3/Secrets Manager/CloudWatch, or Terraform** are involved or imported; "AWS adapter mode" here means an AWS-shaped work item exercising the provider-neutral lifecycle under the `aws-managed` profile, not execution on provisioned AWS infrastructure. Real managed-AWS provisioning is **M15** (Terraform/OpenTofu), and boto3-backed durable adapters (SQS/RDS/S3/Secrets Manager) land with/after that; GCP adapters remain later. All four acceptance criteria are now met at the spike altitude (1: AWS naming isolated to `agentops_runtime/aws_managed.py`, guarded by the core-leakage test; 2: local/fake/Compose remain the default test path; 3: identical lifecycle now also runs an AWS-shaped queued item under `aws-managed`; 4: independent task-count vs per-task concurrency), so M14 is marked Done. Test evidence: focused RED (`ImportError: cannot import name 'AwsManagedWorkItem'`) → GREEN, plus reviewer-driven RED tests for receipt-handle repr redaction and backend-profile downgrade rejection → GREEN; `python -m pytest tests/agentops_runtime/test_aws_managed_runtime.py -q -o 'addopts='` → 21 passed; `tests/agentops_runtime/test_aws_managed_runtime.py tests/agentops_runtime/test_compose_distributed_smoke.py tests/agent/test_runtime_supervisor.py` → 87 passed; `ruff check agentops_runtime/aws_managed.py tests/agentops_runtime/test_aws_managed_runtime.py` → passed; `git diff --check` → clean. The core-contract leakage guard (`agent/runtime_backends.py`, `agent/runtime_supervisor.py`, `agent/runtime_context.py` carry no ECS/Fargate/SQS/RDS/S3/Secrets Manager/CloudWatch strings) still passes.

### M15. Managed cloud Terraform/OpenTofu packaging

**Status:** Started

**Goal:** Provide the easiest AWS/GCP path: customers edit account/region/domain/capacity settings, run Terraform/OpenTofu, and receive working infrastructure plus bootstrap/webhook outputs.

**Acceptance criteria:**

- `deploy/terraform/aws-managed/` provisions the AWS managed profile: API/control-plane, worker service, scheduler, RDS/Postgres or selected DB adapter, queue, artifact store, secret placeholders/refs, logs, IAM, and autoscaling.
- `deploy/terraform/gcp-managed/` is scaffolded or implemented with equivalent GCP resources and clear parity gaps.
- `terraform.tfvars.example` avoids raw app/integration secret values where possible; secret containers/refs are created instead.
- Outputs include AgentOps API URL, bootstrap URL/token ref, Slack/GitHub/Linear/Jira webhook URLs, secret refs, queue refs, artifact refs, worker service names, and smoke-test hints.
- Bring-your-own-network and bring-your-own-managed-resource paths are represented through variables such as existing VPC/subnet/database/bucket/secret refs.
- Terraform/OpenTofu state does not contain Slack/GitHub/Linear/Jira/model-provider raw secret values in the recommended path.

**Autonomous run note (2026-06-02) — first packaging slice:** Added the Terraform/OpenTofu packaging skeleton under `deploy/terraform/`. `aws-managed/` provides `main.tf`/`variables.tf`/`outputs.tf`/`terraform.tfvars.example`/`README.md` for the `aws-managed` profile: ECS/Fargate API-control-plane, worker, and scheduler services; RDS Postgres; SQS runs queue; S3 artifact store; Secrets Manager secret *containers* (no `aws_secretsmanager_secret_version` with raw values in the recommended path); CloudWatch log group; IAM task/execution roles; and Application Auto Scaling for the worker fleet. Bring-your-own-network (`existing_vpc_id`, `existing_subnet_ids`) and bring-your-own-managed-resource (`existing_database_arn`, `existing_artifact_bucket`, `existing_secret_prefix`) variables are represented. Outputs cover `agentops_api_url`, `bootstrap_url`, `bootstrap_token_secret_ref`, Slack/GitHub/Linear/Jira webhook URLs, `secret_refs`, `queue_refs`, `artifact_refs`, `worker_service_name`, and `smoke_test_hints`. `gcp-managed/` is scaffolded with the equivalent Cloud Run/Cloud SQL/Pub-Sub/GCS/Secret Manager resources and a documented "Parity gaps vs aws-managed" section (IAM/service accounts, VPC connector networking, explicit autoscaling policy, LB/DNS domain mapping, container images). Raw app/integration secret values are kept out of both `terraform.tfvars.example` files and are not accepted as input variables; bootstrap (M16) owns them. **Scope/honesty:** this slice packages the profile *topology* with the variables/outputs/secret-hygiene contract and both modules pass `terraform validate`, but service container images, ALB/DNS routing, and full private networking are intentionally left as `TODO-*` markers, so it does not yet provision a production-complete stack. Status stays **Started** until the packaging actually provisions a working managed deployment (real task definitions/images, networking, and an applied smoke run). Test evidence: focused RED (9 `FileNotFoundError` failures, files missing) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 9 passed; `tests/deploy` → 16 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -recursive deploy/terraform` applied; `terraform validate` for both `aws-managed` and `gcp-managed` → "Success! The configuration is valid."; `git diff --check` (with new files intent-added) → clean.

**Review follow-up (2026-06-02):** Closed a dead-variable gap and tightened README honesty. The declared bring-your-own-network refs are now actually consumed: `aws-managed` computes `effective_vpc_id`/`effective_subnet_ids` (creating subnets only when it also creates the VPC, else reusing `existing_subnet_ids`) and feeds them into every ECS service `network_configuration` plus a new `network_refs` output; `gcp-managed` attaches its Cloud Run services to a provided VPC via Direct VPC egress (`vpc_access.network_interfaces`) when `existing_vpc_id` + `existing_subnet_ids` are set, and surfaces a `network_refs` output (its parity note now scopes the gap to *creating a new* private network/connector, not consuming a provided one). Both module READMEs now carry a "Completeness caveat" stating that `apply` **does not yield a working deployment** while `main.tf` still has `TODO` task definitions/container images, and the overstated "receive working infrastructure" phrasing was removed. Raw app/integration secrets remain absent from tfvars/variables and out of the state path. New focused RED (3 failures: dead `existing_subnet_ids`/`existing_vpc_id`, no network wiring in `main.tf`, missing apply caveat) → GREEN. Test evidence: `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 12 passed; `tests/deploy` → 19 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform validate` for both modules → "Success! The configuration is valid."; `git diff --check` → clean. Status stays **Started** (real task definitions/images, ALB/DNS, new-network creation, and an applied smoke run still pending).

**Review follow-up 2 (2026-06-02):** Closed the remaining BYO-database blocker and aligned BYO-network docs/validation with actual behavior. Both modules now expose a `database_refs` output so `existing_database_arn` is *consumed and surfaced*, not just used to suppress creation: `aws-managed` returns `{ ref, address, master_secret_arn }` (created DB attrs or the provided ARN); `gcp-managed` returns `{ ref, connection_name }` (created Cloud SQL attrs or the provided connection name) — so a BYO database keeps a usable runtime/bootstrap reference. AWS `existing_subnet_ids` docs no longer claim "empty creates new subnets" unconditionally; the description states subnets are required with a BYO VPC and are only created when the module also creates the VPC, and a cross-variable `validation` block now *enforces* `existing_vpc_id` set ⇒ `existing_subnet_ids` non-empty (verified firing: `terraform plan -var existing_vpc_id=vpc-123` → "existing_subnet_ids must be provided when existing_vpc_id is set"). This required bumping the AWS module `required_version` to `>= 1.9`. GCP `existing_vpc_id`/`existing_subnet_ids` descriptions now match the README parity gap — they attach via Direct VPC egress when set and otherwise fall back to Cloud Run default egress; creating a *new* private network/subnets is explicitly called out as not done. READMEs list the new `database_refs`/`network_refs` outputs and the BYO-VPC-requires-subnets rule. Raw app/integration secrets remain out of tfvars/variables/state. New focused RED (3 failures: no `database_refs` output / `existing_database_arn` not in outputs, misleading AWS subnet docs, GCP docs claiming new-network creation) → GREEN. Test evidence: `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 15 passed; `tests/deploy` → 22 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init`/`validate` for both modules → "Success! The configuration is valid."; `git diff --check` → clean. Status stays **Started** (real task definitions/images, ALB/DNS, new-network creation, and an applied smoke run still pending).

**Autonomous run note (2026-06-02) — task definitions/images wired:** Replaced the three `TODO-*-task-definition` placeholders in `aws-managed/main.tf` with real `aws_ecs_task_definition` resources for the control-plane, worker, and scheduler, and pointed each `aws_ecs_service.task_definition` at `aws_ecs_task_definition.<svc>.arn`. Added customer-supplied image inputs `control_plane_image`/`worker_image`/`scheduler_image` (placeholder, non-secret defaults like `agentops/hermes-control-plane:replace-me`), consumed by the matching container definitions. All three task defs share a `local.runtime_common_env` carrying non-secret backend refs — `AGENTOPS_RUNTIME_PROFILE=aws-managed`, `AGENTOPS_QUEUE_URL` (→ `aws_sqs_queue.runs.url`), `AGENTOPS_ARTIFACT_BUCKET` (BYO-aware bucket), `AGENTOPS_SECRET_PREFIX`, and `AGENTOPS_DATABASE_SECRET_ARN` (→ the `database` Secrets Manager container ARN, not a raw connection string) — and the worker additionally advertises `AGENTOPS_WORKER_MAX_CONCURRENT_RUNS` from `var.max_concurrent_runs`. No raw secret values or `aws_secretsmanager_secret_version` entered the module. **Scope/honesty:** public ingress (ALB + listener) and DNS for the control-plane are still an explicit `TODO(ingress)` in `main.tf`, so `apply` still does not yield a reachable, working deployment; the README completeness caveat now scopes the gap to ingress/DNS rather than task defs/images. Status stays **Started** (ALB/DNS ingress, new-network creation, and an applied smoke run still pending). Test evidence: focused RED (7 failures: missing `aws_ecs_task_definition` resources, services still on `TODO` literals, missing image vars, missing `runtime_common_env`) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 22 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init -backend=false`/`validate` for `aws-managed` → "Success! The configuration is valid."; `git diff --check` → clean.

**Review follow-up 3 (2026-06-02) — IAM permissions for the wired task defs:** Closed two blocking IAM gaps a reviewer flagged on the task-definition slice — the roles existed with only assume-role policies, so the wired `execution_role_arn`/`task_role_arn` could not actually do anything. (1) The execution role now attaches the AWS-managed `AmazonECSTaskExecutionRolePolicy` (`aws_iam_role_policy_attachment.task_execution`), granting the CloudWatch Logs (`awslogs` driver) and ECR auth/pull permissions the task defs' log config and customer images require. (2) The task role gains a scoped inline policy `aws_iam_role_policy.task_runtime_backends` (`jsonencode`) covering exactly the `runtime_common_env` backend refs: SQS send/receive/delete on `aws_sqs_queue.runs.arn`; S3 list/get/put/delete on the effective artifact bucket and its objects (BYO bucket ARN reconstructed from its name via `local.effective_artifact_bucket_arn`); and Secrets Manager `GetSecretValue`/`DescribeSecret` on the created secret containers (`[for s in aws_secretsmanager_secret.containers : s.arn]`). No raw secret values or `aws_secretsmanager_secret_version` were added. **Scope/honesty:** unchanged — public ingress (ALB + listener), DNS, new-network creation, and an applied smoke run are still pending, so `apply` still does not yield a reachable deployment. Status stays **Started**. Test evidence: focused RED (2 failures: execution role lacks `AmazonECSTaskExecutionRolePolicy`, missing `task_runtime_backends` policy with SQS/S3/Secrets Manager actions+scoped ARNs) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 24 passed; `tests/deploy` → 31 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init -backend=false`/`validate` for `aws-managed` → "Success! The configuration is valid."; `git diff --check` → clean.

**Autonomous run note (2026-06-02) — public ALB ingress wired:** Closed the explicit `TODO(ingress)` gap in `aws-managed/main.tf`. Added a public Application Load Balancer (`aws_lb.this`, `internal=false`), an `api` target group (`aws_lb_target_group.api`, `target_type="ip"` for Fargate `awsvpc` tasks, in `local.effective_vpc_id`, health check on `/healthz`), and an HTTP listener (`aws_lb_listener.api`, port 80) that forwards to the target group. Two security groups gate traffic: `aws_security_group.alb` (HTTP 80 from `0.0.0.0/0`) and `aws_security_group.service` (API container port from the ALB SG only, all egress), the latter attached to every service's `network_configuration`. The control-plane `aws_ecs_service` now carries a `load_balancer` block (`target_group_arn` → `aws_lb_target_group.api.arn`, `container_name="control-plane"`, `container_port=var.api_container_port`) and `depends_on` the listener; its task definition exposes `portMappings` on `var.api_container_port` (new non-secret variable, default `8080`). Outputs now derive the API base URL from the ALB DNS name over HTTP (`api_base_url = "http://${aws_lb.this.dns_name}"`), so `agentops_api_url`/`bootstrap_url`/webhook URLs are real reachable endpoints rather than the not-yet-routed `domain`; `smoke_test_hints.dns_note` surfaces `var.domain` as the deferred custom-DNS target. No raw secret values or `aws_secretsmanager_secret_version` were added. **Scope/honesty:** an apply now yields an HTTP-reachable endpoint at the ALB DNS name, but custom DNS (Route53 for `var.domain`), TLS/ACM (the listener is HTTP-only), guaranteed-public edge networking (no IGW/public-route wiring on module-created subnets), and an applied end-to-end smoke test against a live account remain deferred; the README "Apply"/"Scope" sections enumerate these honestly and the blocking "does not yield a working deployment" caveat is removed. Status stays **Started** (DNS/ACM/HTTPS, public edge networking, and an applied smoke run still pending). Test evidence: focused RED (9 failures: missing `aws_lb`/`aws_lb_target_group`/`aws_lb_listener`, no `load_balancer` block on the control-plane service, no `portMappings`/`api_container_port`, outputs not derived from ALB DNS, lingering `TODO` in `main.tf`, README still asserting non-working apply) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 33 passed; `tests/deploy` → 40 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init -backend=false`/`validate` for `aws-managed` → "Success! The configuration is valid."

**Review follow-up 4 (2026-06-02) — honest ALB reachability contract:** Closed three independent-review blockers on the public-ingress slice that conflated the ALB's edge subnets with the private service subnets and overstated reachability. (1) The ALB no longer attaches to `local.effective_subnet_ids` (the private service subnets); a new non-secret variable `existing_alb_subnet_ids` carries the **public/edge** ALB subnets, and a new `local.effective_alb_subnet_ids = length(var.existing_alb_subnet_ids) > 0 ? var.existing_alb_subnet_ids : local.effective_subnet_ids` drives `aws_lb.this.subnets`. The ECS services keep running in `local.effective_subnet_ids`. (2) `existing_alb_subnet_ids` is validated to list **at least two** subnets when provided (an Application Load Balancer requires two-or-more subnets in different AZs); empty stays allowed as the scaffold/default fallback. (3) Docs/outputs/main.tf comments no longer claim the endpoint is "reachable now" or that an apply unconditionally "yields a reachable" endpoint — they now state the provisioned ALB DNS endpoint is internet-reachable **only when its subnets have public routing** (IGW + public route), and that supplying `existing_alb_subnet_ids` (or making the service subnets public) is required for reachability; custom DNS (Route53 for `var.domain`), TLS/ACM (HTTP-only listener), and an applied end-to-end smoke test remain deferred. The brittle blanket "`TODO` absent anywhere in `main.tf`" test was replaced with a targeted assertion that the old `TODO(ingress)` placeholder is gone. No raw secret values or `aws_secretsmanager_secret_version` were added. Status stays **Started** (public ALB routing depends on customer-supplied public subnets; DNS/ACM/HTTPS and an applied smoke run still pending). Test evidence: focused RED (5 failures: missing `existing_alb_subnet_ids` variable+validation, ALB still on `effective_subnet_ids` / no `effective_alb_subnet_ids` local, outputs/README/main.tf claiming unconditional "reachable now"/"yields a reachable") → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` and `tests/deploy` results recorded in the run; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init -backend=false`/`validate` for `aws-managed` → valid; `git diff --check` → clean.

**Review follow-up 5 (2026-06-02) — default public ALB subnets, not a private-subnet fallback:** Closed the remaining independent-review blocker: follow-up 4's `effective_alb_subnet_ids` still *fell back to the private service subnets* (`local.effective_subnet_ids`) when no BYO ALB subnets were supplied, so a default apply (module-created VPC) would try to build an internet-facing ALB on subnets with no IGW/public route — docs promised an ALB DNS endpoint that the default apply could fail to create. Now the module creates **real public edge networking** when it creates the VPC: `local.create_alb_subnets = local.create_vpc && length(var.existing_alb_subnet_ids) == 0` gates a new `aws_internet_gateway.this`, two public `aws_subnet.alb` (in distinct AZs, `map_public_ip_on_launch = true`, a high CIDR offset so they don't collide with the private service subnets), an `aws_route_table.alb` with a `0.0.0.0/0` route to the IGW, and `aws_route_table_association.alb` binding them. `local.effective_alb_subnet_ids = local.create_alb_subnets ? aws_subnet.alb[*].id : var.existing_alb_subnet_ids` now resolves to the module-created public subnets by default and the BYO public subnets otherwise — **never** the private service subnets. The `existing_alb_subnet_ids` validation was tightened to require **exactly one valid network mode**: empty `existing_vpc_id` with empty `existing_alb_subnet_ids` for the module-created VPC/public-ALB-subnet path, or non-empty `existing_vpc_id` with **>= 2** BYO public/edge ALB subnets; the invalid mixed mode (module-created VPC plus caller-supplied ALB subnets from another VPC) is rejected. `terraform.tfvars.example` now surfaces `existing_alb_subnet_ids = []` with a comment explaining it must be public/edge subnets for a BYO VPC. README/main.tf comments/outputs are honest: the default module-created ALB subnets get public routing, a BYO VPC must supply public ALB subnets, and DNS/TLS/applied smoke remain deferred; `network_refs` now also surfaces `alb_subnet_ids`. No raw secret values or `aws_secretsmanager_secret_version` were added. Status stays **Started** (DNS/ACM/HTTPS and an applied end-to-end smoke run still pending). Test evidence: focused RED (6 failures: no IGW/public route table/`aws_subnet.alb`/association, missing `create_alb_subnets` local, `effective_alb_subnet_ids` still falling back to the private service subnets, validation not gating empty on creating the VPC, tfvars not surfacing `existing_alb_subnet_ids`, README still claiming a private-subnet fallback) plus independent-review RED for the invalid mixed network mode (module-created VPC + BYO ALB subnets) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 47 passed; `tests/deploy` → 54 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform init -backend=false`/`validate` for `aws-managed` → "Success! The configuration is valid."; `git diff --check` → clean.

**Autonomous run note (2026-06-02) — optional custom-domain HTTPS/TLS:** Added an *optional* custom-domain HTTPS/TLS path to `aws-managed` while preserving the ALB-DNS HTTP default and the secret-hygiene contract. Two new non-secret inputs: `acm_certificate_arn` (a certificate **reference**, default `""`) and `route53_zone_id` (default `""`). When `acm_certificate_arn` is set, the module adds an HTTPS listener on 443 to the **existing** `aws_lb.this` (`aws_lb_listener.api_https`, `count = var.acm_certificate_arn != "" ? 1 : 0`, `ssl_policy = ELBSecurityPolicy-TLS13-1-2-2021-06`, terminating TLS with the supplied cert and forwarding to the **same** `aws_lb_target_group.api`), and opens 443 on the ALB security group via a `dynamic "ingress"` gated the same way (so the HTTP-only default never exposes a 443 port with no listener). When `route53_zone_id` is set, `aws_route53_record.api` (count-gated) creates an A/alias record pointing `var.domain` at `aws_lb.this.dns_name`/`zone_id`. Outputs now expose both the raw `alb_http_url` (always available) and an **effective** `agentops_api_url` = `var.acm_certificate_arn != "" ? "https://${var.domain}" : local.alb_http_url`; `bootstrap_url`/Slack/GitHub/Linear/Jira webhook URLs derive from that effective base. `smoke_test_hints` gains a `tls_note` flagging that custom-domain HTTPS is optional and that a live applied smoke test is still pending; the default HTTP listener on port 80 stays unconditional. README/`terraform.tfvars.example` document the optional HTTPS/TLS path, the default ALB-HTTP path, and reiterate that raw app/integration secret values are still set by bootstrap, not Terraform. No `aws_secretsmanager_secret_version`/raw secret values were added; GCP scaffold untouched. **Scope/honesty:** the TLS/DNS path is wired but unexercised — no end-to-end `apply`/smoke against a live AWS account, so Status stays **Started**. Test evidence: focused RED (8 failures: missing `acm_certificate_arn`/`route53_zone_id` vars, no `aws_lb_listener.api_https`/443 ingress/`aws_route53_record.api`, outputs not exposing `alb_http_url`/effective HTTPS URL, smoke hints/README/tfvars not documenting the optional path) → GREEN, with 2 pre-existing-correct contracts already passing (HTTP listener unconditional, webhook URLs already derive from `local.api_base_url`); `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 57 passed; `tests/deploy` → 64 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `terraform fmt -check -recursive` → clean; `terraform -chdir=aws-managed init -backend=false`/`validate` → "Success! The configuration is valid."; `git diff --check` → clean.

**Autonomous run note (2026-06-02) — module-local live-smoke helper:** Added `deploy/terraform/aws-managed/smoke.sh`, an executable, module-local live-smoke helper an operator runs from inside the module directory after configuring `terraform.tfvars` + AWS credentials. It **fails closed before any side effect**: it auto-detects a `terraform` or `tofu` (OpenTofu) CLI via `command -v` (exit 1 if neither is present), requires the `aws` CLI, and verifies credentials/config with `aws sts get-caller-identity` (exit 1 if unavailable) — all before the first `-input=false` CLI invocation. It uses module-local commands (`cd "$(dirname "$0")"`, no root-relative `-chdir=deploy/terraform/...`), defaults to a safe plan-only mode (`PLAN_ONLY=1` → `init`+`validate`+`plan`, no apply), and only on `PLAN_ONLY=0` runs `apply` and then prints the post-apply smoke hints/outputs (`terraform output agentops_api_url` and `smoke_test_hints`). The script never accepts or echoes raw app/integration secret values; bootstrap (M16) still owns those. The aws-managed README documents the helper, the `PLAN_ONLY` default, and `./smoke.sh` usage. **Honesty:** no live AWS apply/smoke was captured in this run — the helper was exercised only for `bash -n` syntax and the static packaging contract; the first real `PLAN_ONLY=0 ./smoke.sh` against an account is still the pending smoke test, so Status stays **Started**. Test evidence: focused RED (8 failures: missing `smoke.sh`/README docs) → GREEN; `python -m pytest tests/deploy/test_agentops_terraform_packaging.py -q -o 'addopts='` → 65 passed; `tests/deploy` → 72 passed; `ruff check tests/deploy/test_agentops_terraform_packaging.py` → passed; `bash -n smoke.sh` → OK; `terraform fmt -check -recursive deploy/terraform` → clean; `terraform -chdir=deploy/terraform/aws-managed init -backend=false`/`validate` → "Success! The configuration is valid."; `git diff --check` → clean.

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

Schedule two unrelated cron jobs:
  repo autonomous builder every 5m, long-running, workdir-scoped
  lightweight script watchdog every 10m, no_agent/script-only

While the builder is still running:
  the next watchdog firing is still claimed and executed on time
  the builder does not start a duplicate overlapping builder run

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
