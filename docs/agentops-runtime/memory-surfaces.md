# Memory surfaces: curated memory vs deep memory vs session search

The AgentOps Hermes runtime has **three distinct memory surfaces**. They are
intentionally separate abstractions with separate storage, scoping, and tools.
Do not collapse them into one system — each answers a different question.

| Surface | Question it answers | Native tool(s) | Storage | Scoping |
| --- | --- | --- | --- | --- |
| **Curated memory** | "What durable facts/preferences should I always keep in context?" | `memory(target=user\|memory, ...)` | `MEMORY.md` / `USER.md` (or a scoped `MemoryBackend`) | `BackendCapability.MEMORY` |
| **Deep memory** | "What did we actually say/do in past completed turns?" | automatic hints + `memory_record_search` / `memory_record_get` / `memory_record_get_many` | flat verbatim record store (ChromaDB + ONNX), or a scoped/remote adapter | `BackendCapability.DEEP_MEMORY` |
| **Session search** | "Find a specific past session/message." | session backend search | session/transcript store | `BackendCapability.SESSION` |

## 1. Curated memory (the native `memory` tool)

Small, model-curated set of durable facts and user preferences that are always
rendered into the system prompt. Written explicitly by the model via
`memory(target=memory|user, action=add|replace|remove, ...)`. This is **not**
historical recall — it is a hand-maintained, size-bounded working set. Backed by
the `MemoryBackend` contract (M4/M5); local mode uses `MEMORY.md` / `USER.md`,
remote profiles use a scoped HTTP/durable backend.

## 2. Deep memory (historical completed-turn recall)

Separate tier added in M5A. Stores **successful, user-facing completed turns** as
flat verbatim records and makes them retrievable:

- **Automatic prefetch:** bounded historical *hints* are injected only into the
  API-call copy of the current message. The persisted session transcript is
  never mutated by prefetch.
- **Explicit fetch:** the model pulls full verbatim records by ID with
  `memory_record_get` / `memory_record_get_many`, or finds them with
  `memory_record_search`. Returned records are fenced as historical data, **not
  instructions**.

Key properties (see `agent/local_memory/` and `agent/runtime_backends.py`):

- **Ingestion policy:** only successful turns; interrupted turns (empty user or
  assistant side) are skipped; secrets are redacted **fail-closed** (a turn that
  cannot be redacted is dropped, never stored); `sync_platforms` keeps cron,
  subagent/review forks, and other internal traces out of auto-recall unless
  policy opts them in.
- **Search semantics:** stable SHA-256 record IDs, source/provenance metadata,
  hybrid vector + BM25 union search, deterministic extracted-signal rank boosts,
  metadata filters, bounded excerpts, and optional local summary metadata with a
  verbatim fallback.
- **Scoping & isolation (`DEEP_MEMORY` capability):**
  - **Local single-user (`local` profile):** one shared store at
    `$HERMES_HOME/deep-memory` (preserves prior behaviour; cross-session recall
    for the single user).
  - **Local-multi (`local-multi` profile):** record storage/search is
    partitioned by `RuntimeContext` (org / workspace / user / project /
    conversation/thread / agent profile). No cross-user/org/project/thread
    leakage: a record cannot be searched or fetched (even by ID) from another
    scope.
  - **Compose self-hosted (`compose-self-hosted` profile):** uses the
    `HttpMemoryRecordBackend` HTTP adapter against the compose control-plane
    API (`AGENTOPS_DEEP_MEMORY_URL`). The API is backed by a
    `RelationalMemoryRecordBackend` (Postgres/pgvector by default via
    `AGENTOPS_DEEP_MEMORY_DB_URL`). Scope isolation is enforced on the
    server side by all seven `RuntimeContext` scope columns. Missing or
    misconfigured URLs fail closed before any store write.
  - **AWS-managed (`aws-managed` profile):** the first managed-cloud
    deep-memory path uses `RelationalMemoryRecordBackend` directly via an
    explicit RDS/Postgres (or SQLite for local testing) DB URL configured in
    `agentops.deep_memory_db_url` (config) or `AGENTOPS_DEEP_MEMORY_DB_URL`
    (environment variable, which takes precedence over config). The factory is
    registered lazily so no connection
    is made at startup — connection errors are raised only on first access.
    Scope isolation (seven `RuntimeContext` columns) is enforced by the
    relational backend on every read and write. Missing/blank configuration
    leaves `DEEP_MEMORY` unregistered for the profile, so the backend fails
    closed rather than falling back to any local store.
  - **AgentOps/remote profiles without a registered adapter:** **fail closed.**
    Backend selection raises `BackendSelectionError` rather than falling back
    to an unscoped local Chroma store, a process-local dict, or another
    tenant's records. There is no `$HERMES_HOME/deep-memory`, Chroma, or
    in-process fallback for AgentOps remote/managed profiles.

**Open work (M5B not yet complete):** Vector embedding population for the
Postgres/pgvector path and extracted-signal boost parity with the local
deep-memory provider remain unfinished. The current Postgres adapter stores
`embedding` as `NULL` and searches use full-text/BM25 only. Do not assume full
vector search or extracted-signal rank parity in managed cloud profiles until
these items land.
- **Imports** (`scripts/local_memory_import.py`) are idempotent and
  source-preserving, and **require an explicit target scope/profile**
  (`--storage-dir` or `--profile`/`--hermes-home`) so bulk history cannot
  contaminate the wrong org/user/project store.

## 3. Session search

Conversation/transcript history retrieval via the `SESSION` backend. Used to
locate a specific past session or message. This is durable conversation state,
distinct from both curated facts and deep-memory records.

## Why keep them separate

Collapsing curated memory, deep memory, and session search into one abstraction
loses the distinct scoping, write policies, and trust boundaries each needs:
curated memory is always-in-context and model-authored; deep-memory records are
fenced historical data with strict tenant/thread isolation and fail-closed
remote behaviour; session search is durable transcript lookup. Future work should
extend the matching capability/backend rather than merge surfaces.
