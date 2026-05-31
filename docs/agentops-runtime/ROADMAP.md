# AgentOps Hermes Runtime MVP Roadmap

## North star

Make Hermes a distributed, multi-tenant business runtime without losing what makes Hermes useful: the same agent loop, same memory tool, same skills semantics, same cron/autonomous jobs, same tool-calling ergonomics, and same ability to run fully local.

The MVP proves one thing:

> A Slack/user/thread-scoped Hermes turn can run on a disposable worker while native Hermes memory, skills, cron/session state, credentials, artifacts, and audit are backed by scoped remote services through cloud/database/secret-store-agnostic interfaces — with local backends remaining the default fallback.

## Architecture principles

1. **Backend contracts before cloud code.** Define Python interfaces/protocols first; AWS/GCP/local implementations come behind those contracts.
2. **Local remains first-class.** Existing local behavior is not legacy; it is the reference implementation and dev mode.
3. **Native tools, remote backing.** Do not build sidecar memory/skills/cron that bypass Hermes. The native Hermes surfaces call pluggable backends.
4. **Runtime context is load-bearing.** Every backend decision is scoped by `RuntimeContext`: org, workspace, user, conversation/thread, project, agent profile, and execution mode.
5. **Cloud agnostic.** AWS is likely the first production adapter, but no core interface should name ECS, SQS, DynamoDB, Secrets Manager, etc.
6. **DB agnostic.** Initial SQL/Postgres adapters are okay, but contracts should not assume one database engine.
7. **Secret-store agnostic.** Local env files, AWS Secrets Manager, GCP Secret Manager, Vault, and future stores are implementations of a credential/secret resolver contract.
8. **Remote cron is mandatory.** Cron/autonomous jobs are part of Hermes’s identity; they need remote storage, leases, delivery targeting, and worker execution.
9. **Tenant isolation beats convenience.** Cross-user/org/project memory or credential leakage is an MVP blocker.
10. **Small fork deltas.** Prefer modular seams that could be upstreamed or maintained as a small patch stack.

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

**Status:** Pending

**Goal:** Introduce a first-class context object that can be passed through Hermes without changing local behavior.

**RuntimeContext fields, minimum:**

- `mode`: `local | agentops`
- `org_id`
- `workspace_id`
- `workspace_type`: `slack | telegram | discord | cli | api | cron | other`
- `user_id`
- `conversation_id`
- `external_channel_id`
- `external_thread_id`
- `agent_profile_id`
- `project_id`
- `run_id`
- `parent_session_id`
- `permissions_ref`
- `backend_profile`

**Acceptance criteria:**

- Local mode creates a RuntimeContext from existing profile/session/platform data.
- AgentOps mode can load RuntimeContext from env var or JSON payload.
- Context is available to memory, skills, session state, cron, credential resolution, logging/audit, and tool execution paths.
- Tests prove absent AgentOps context preserves current behavior.

### M2. Backend registry/contracts

**Status:** Pending

**Goal:** Add generic backend contracts and a runtime backend registry without implementing cloud-specific behavior yet.

**Contracts, minimum:**

- `MemoryBackend`
- `SkillBackend`
- `SessionBackend`
- `CronBackend`
- `CredentialResolver`
- `ArtifactBackend`
- `AuditBackend`
- `DeliveryBackend` / route resolver

**Acceptance criteria:**

- Local implementations wrap existing behavior.
- Backend selection is driven by config + RuntimeContext.
- No AWS/GCP/Postgres-specific names leak into core interfaces.
- Tests instantiate local backends through the registry.

### M3. Native memory tool backend abstraction

**Status:** Pending

**Goal:** Preserve the native `memory` tool while moving storage behind `MemoryBackend`.

**Acceptance criteria:**

- Current local file memory behavior is represented by `LocalFileMemoryBackend`.
- `memory(action=add|replace|remove|read, target=user|memory, ...)` continues to work in local mode.
- AgentOps/remote backend can be stubbed/faked in tests and receives RuntimeContext.
- Tests prove writes for two users/conversations route to separate backend scopes.
- Threat scanning, char limits, drift protection semantics are preserved or explicitly mapped.

### M4. Remote memory adapter MVP

**Status:** Pending

**Goal:** Implement the first real remote memory adapter against a cloud/db-agnostic HTTP or SQL contract.

**Initial implementation preference:** HTTP adapter to an AgentOps control-plane API, with an in-memory/local test server and optional Postgres-backed server later.

**Acceptance criteria:**

- Remote memory read/write uses RuntimeContext scope.
- Same native memory tool is used by the model.
- No raw memory from another user/org/thread appears in prompt or tool output.
- Local fallback remains available.

### M5. Session/conversation backend abstraction

**Status:** Pending

**Goal:** Make Hermes session persistence worker-safe and optionally remote.

**Acceptance criteria:**

- Existing SQLite session behavior is wrapped as `LocalSQLiteSessionBackend`.
- Remote session backend contract supports append/read/search/resume lineage.
- Worker can process a turn and write transcript/tool events to remote session backend.
- Concurrent turn lock/lease semantics are specified and tested.

### M6. Skills backend abstraction

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

### M7. Cron/autonomous jobs backend abstraction

**Status:** Pending

**Goal:** Move Hermes cron from local scheduler files/state to pluggable local/remote cron backends.

**Acceptance criteria:**

- Existing cron behavior remains as `LocalCronBackend`.
- Remote cron contract supports create/update/pause/resume/remove/list/run history.
- Jobs include RuntimeContext and delivery target bindings.
- Worker-safe leases prevent duplicate execution across multiple cloud tasks.
- Empty-output/silent semantics and error-alert semantics are preserved.
- Tests cover recurring, one-shot, paused, lease timeout, and delivery routing cases.

### M8. Credential/secret resolver abstraction

**Status:** Pending

**Goal:** Decouple provider/tool credentials from local `.env` without exposing raw secrets to prompts/transcripts.

**Backends:**

- local env/profile files
- AgentOps remote broker
- AWS Secrets Manager adapter
- GCP Secret Manager adapter
- future Vault adapter

**Acceptance criteria:**

- Credential requests are made by ref/capability, not raw values.
- RuntimeContext determines which credentials are available.
- Resolved secrets are available only to the tool/provider process path that needs them.
- Audit stores refs and usage metadata, never secret values.

### M9. Artifact and audit backends

**Status:** Pending

**Goal:** Centralize durable artifacts and audit trails for distributed runs.

**Acceptance criteria:**

- Local artifacts remain available.
- Remote artifact backend stores tool outputs/files by scoped refs.
- Audit backend receives memory writes, skill loads/mutations, credential resolutions, cron runs, session events, and tool calls.
- Tests prove sensitive local paths/secrets are not surfaced in audit payloads.

### M10. Stateless worker turn runner

**Status:** Pending

**Goal:** Allow a disposable worker process/container to run one scoped Hermes turn using remote backends.

**Acceptance criteria:**

- Worker accepts RuntimeContext + message payload.
- Worker claims a session/turn lease.
- Worker loads memory/skills/session through selected backends.
- Worker resolves credentials through selected resolver.
- Worker writes transcript/audit/artifacts back remotely.
- Worker exits cleanly without relying on persistent local disk except cache/temp.

### M11. Slack multi-user/thread MVP

**Status:** Pending

**Goal:** Prove business messaging integration with multiple users and threads.

**Acceptance criteria:**

- Slack workspace/channel/thread/user maps to RuntimeContext.
- Two Slack users in different threads get isolated user/thread memory.
- Shared org/project skill can be loaded in both threads.
- Hermes replies in the correct Slack thread.
- Worker can be restarted between turns and still resume state remotely.

### M12. Cloud adapter spike: AWS first, GCP later

**Status:** Pending

**Goal:** Prove core contracts can be deployed to one real cloud without hard-coding that cloud into the core.

**AWS candidate adapters:**

- ECS/Fargate or Lambda/container runner for workers
- SQS/EventBridge for queued turns/cron triggers
- Postgres/RDS or DynamoDB adapter depending contract fit
- S3 artifact backend
- Secrets Manager credential backend

**GCP candidate adapters:**

- Cloud Run worker
- Pub/Sub or Cloud Tasks
- Cloud SQL/Firestore adapter
- GCS artifact backend
- Secret Manager credential backend

**Acceptance criteria:**

- Cloud-specific code lives in adapter packages/modules.
- Local and fake adapters remain the default test path.
- Same worker contract runs locally and in AWS adapter mode.

## Initial implementation order

1. M0 docs/repo hygiene.
2. M1 RuntimeContext.
3. M2 backend registry/contracts.
4. M3 native memory backend abstraction.
5. M4 fake/HTTP remote memory adapter.
6. M7 cron backend abstraction early, because remote cron is not optional.
7. M5 sessions and M6 skills.
8. M8 credentials.
9. M10 worker runner.
10. M11 Slack smoke.
11. M12 AWS adapter spike.

## Explicit non-goals for MVP

- Polished dashboard.
- Complex approval UI.
- Full enterprise RBAC.
- Deep billing/metering.
- Rewriting Hermes agent loop.
- Removing local mode.
- Locking architecture to AWS, GCP, Postgres, DynamoDB, or any one secret store.

## Open questions

- Should AgentOps mode communicate with the control plane primarily over HTTP/gRPC, direct SQL, or both through adapters?
- Should remote cron scheduling live in AgentOps control plane, Hermes scheduler, cloud scheduler, or an adapter that can wrap all three?
- How much of RuntimeContext should be visible to the model versus internal only?
- Should shared skill mutations be impossible from an agent by default, or allowed with approval gates?
- What is the smallest safe credential grant model that still supports useful tools?
