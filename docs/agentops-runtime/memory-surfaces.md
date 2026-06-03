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
  - **AgentOps/remote profiles:** **fail closed.** With no registered deep-memory
    adapter, backend selection raises `BackendSelectionError` rather than falling
    back to an unscoped local Chroma store. Compose/cloud deployments register a
    durable/remote adapter behind the same `MemoryRecordBackend` contract.
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
