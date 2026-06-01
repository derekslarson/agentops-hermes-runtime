"""Generic backend contracts and a runtime backend registry (M2).

This module defines the cloud/database/secret-store-agnostic contracts that
later milestones implement, plus a :class:`RuntimeBackendRegistry` that selects
a concrete backend per capability using a deployment ``profile`` derived from
config and the active :class:`~agent.runtime_context.RuntimeContext`.

At this milestone the only concrete implementations are the ``Local*`` backends.
They are intentionally lightweight: they represent the existing local-mode path
and keep current behavior unchanged. Remote/distributed backends are added in
later milestones behind the same contracts, so the registry is the single seam
that future code consults to obtain a scoped backend instance.

Design rules enforced here:

* Contracts are :class:`typing.Protocol` definitions — structural, so both the
  ``Local*`` backends and test fakes satisfy them without inheritance.
* No provider/service/database names appear in any contract, backend, profile,
  or docstring. Profiles are opaque strings chosen by config/context.
* Each registry instance owns its own factory table and instance cache, so two
  registries never share mutable backend state.
* Factories receive only static options. RuntimeContext selects a profile and is
  supplied to backend methods, preventing factories from accidentally binding
  tenant/run scope into a cached backend instance.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agent.runtime_context import RuntimeContext

_DEFAULT_PROFILE = "local"
_SENSITIVE_EVENT_KEYS = ("secret", "token", "password", "api_key", "apikey", "credential")
_CRON_SILENT_MARKERS = ("[SILENT]",)
# Context fields safe to bind into a durable cron job record. Secret-bearing
# refs (``permissions_ref``) and per-execution identifiers (``run_id``) are
# intentionally excluded so the stored binding is a stable, non-secret scope.
_CRON_BINDING_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "user_id",
    "conversation_id",
    "agent_profile_id",
    "project_id",
    "run_type",
    "delivery_ref",
)


def _cron_job_binding(context: RuntimeContext | None) -> dict[str, Any]:
    """Return the non-secret scope/delivery binding stored on a cron job.

    Captures the RuntimeContext scope a future run must reconstruct (tenant,
    user, project, conversation, profile) plus the delivery target reference,
    while never copying secret-bearing refs into the durable job record.
    """

    if context is None:
        return {}
    return {field: getattr(context, field, None) for field in _CRON_BINDING_FIELDS}


def _cron_output_is_silent(output: str | None) -> bool:
    if output is None:
        return True
    text = output.strip()
    if not text:
        return True
    return text in _CRON_SILENT_MARKERS


def _runtime_scope_key(context: RuntimeContext | None) -> tuple[Any, ...]:
    """Return the local-state scope for a runtime context.

    Local compatibility backends still use in-process state in M2, but the state
    is partitioned by the same RuntimeContext fields future durable backends
    will use. ``None`` represents the existing single-user local path.
    """

    if context is None:
        return (_DEFAULT_PROFILE,)
    return (
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.conversation_id,
        context.agent_profile_id,
        context.project_id,
        context.run_id,
        context.job_id,
    )


def _credential_scope_key(context: RuntimeContext | None) -> tuple[Any, ...]:
    """Return the stable tenant/user/project scope for credential state.

    Credentials are durable bindings, not per-run scratch data. A worker restart
    or warm-run replacement must be able to resolve the same logical credential
    reference with a new ``run_id`` while another user/org/project remains
    isolated.
    """

    if context is None:
        return (_DEFAULT_PROFILE,)
    return (
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.agent_profile_id,
        context.project_id,
    )


def _session_scope_key(context: RuntimeContext | None) -> tuple[Any, ...]:
    """Return the local session/conversation scope for transcript state.

    Session backends model durable conversation history, so the key is based on
    tenant/user/project/conversation identity and intentionally excludes
    per-execution identifiers such as ``run_id`` and ``job_id``. A restarted or
    rescheduled worker with a new run id must still see the same transcript.
    """

    if context is None:
        return (_DEFAULT_PROFILE,)
    return (
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.conversation_id,
        context.agent_profile_id,
        context.project_id,
    )


def _cron_scope_key(context: RuntimeContext | None) -> tuple[Any, ...]:
    """Return the durable cron job scope for a runtime context.

    Cron jobs are scheduled durable objects, so visibility follows stable
    tenant/user/project/conversation/profile identity and intentionally excludes
    per-execution identifiers such as ``run_id`` and ``job_id``. A scheduler or
    worker restarted with a new run id must still claim and complete the same
    job, while another user/conversation remains isolated.
    """

    return _session_scope_key(context)


def _redact_audit_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if any(marker in str(key).lower() for marker in _SENSITIVE_EVENT_KEYS) and item is not None
            else _redact_audit_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_audit_value(item) for item in value)
    return value


class BackendCapability(str, Enum):
    """The set of pluggable runtime capabilities selected by the registry."""

    MEMORY = "memory"
    SKILL = "skill"
    SESSION = "session"
    CRON = "cron"
    CREDENTIAL = "credential"
    SECRET = "secret"
    QUEUE = "queue"
    RUN_LEASE = "run_lease"
    CONVERSATION_ROUTER = "conversation_router"
    WORKER_REGISTRY = "worker_registry"
    ARTIFACT = "artifact"
    AUDIT = "audit"
    DELIVERY = "delivery"


REQUIRED_CAPABILITIES: frozenset[BackendCapability] = frozenset(BackendCapability)


class BackendSelectionError(LookupError):
    """Raised when no backend is registered for a capability/profile pair."""


class CronLeaseError(RuntimeError):
    """Raised when a worker acts on a cron job it does not currently lease.

    Worker-safe scheduling requires that only the live lease holder advances or
    completes a run. A stale or duplicate worker attempting to complete/fail a
    job whose lease has been reclaimed by another owner is rejected so a single
    due firing cannot be delivered twice.
    """


@runtime_checkable
class MemoryBackend(Protocol):
    """Scoped store for the native memory tool.

    Backends store the tool's complete §-delimited snapshot per target. Shared
    implementations must make ``write`` linearizable for a given
    RuntimeContext/target, or provide equivalent external coordination, because
    MemoryStore performs read-modify-write mutations while preserving the native
    tool's entry-level semantics.
    """

    def read(self, context: RuntimeContext | None, *, target: str = "memory") -> str | None: ...

    def write(
        self,
        context: RuntimeContext | None,
        content: str,
        *,
        target: str = "memory",
        action: str = "add",
    ) -> None: ...


@runtime_checkable
class SkillBackend(Protocol):
    """Scoped source for listing, loading, and mutating skill content.

    Extends the original raw ``list_skills``/``load_skill`` string contract so
    the native skill surfaces (``skills_list``, ``skill_view``, ``skill_manage``)
    can route through a backend without losing progressive-disclosure metadata,
    linked-file access, readiness/setup status, platform compatibility filtering,
    or mutation policy. Methods return native-shaped, JSON-ready dicts so the
    tool layer keeps its existing output shape regardless of which backend is
    selected.

    * ``list_skills`` returns one metadata mapping per visible skill
      (``name``/``description``/``category`` plus optional ``scope``).
    * ``load_skill`` returns the full ``skill_view`` payload mapping for the
      main content, or a linked file when ``file_path`` is given. It returns a
      ``{"success": False, ...}`` mapping (never another tenant's content) when
      the skill is not visible/found.
    * ``manage_skill`` performs a mutation and returns the result mapping.
      Implementations enforce scope/policy and must fail closed before any side
      effect for shared (org/project) scopes that lack approval.
    """

    def list_skills(
        self,
        context: RuntimeContext | None,
        *,
        category: str | None = None,
    ) -> list[Mapping[str, Any]]: ...

    def load_skill(
        self,
        context: RuntimeContext | None,
        name: str,
        *,
        file_path: str | None = None,
        preprocess: bool = True,
    ) -> Mapping[str, Any] | None: ...

    def manage_skill(
        self,
        context: RuntimeContext | None,
        *,
        action: str,
        name: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class SessionBackend(Protocol):
    """Durable conversation/session persistence.

    Backends support the legacy event-style append/read/search methods plus the
    richer M6 transcript surface needed by workers: create session rows, append
    native message dicts, read scoped transcripts, resolve resume lineage, and
    acquire a per-conversation turn lock.
    """

    def create_session(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
    ) -> str: ...

    def append_message(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> Any: ...

    def read_messages(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[Any]: ...

    def resolve_resume_session_id(self, session_id: str) -> str: ...

    def claim_turn_lock(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool: ...

    def renew_turn_lock(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool: ...

    def release_turn_lock(self, context: RuntimeContext | None, *, owner: str) -> None: ...

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None: ...

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[Any]: ...

    def search(self, context: RuntimeContext | None, query: str) -> list[Any]: ...


@runtime_checkable
class CronBackend(Protocol):
    """Scheduled/autonomous job storage, worker-safe leasing, and run history.

    The contract is cloud/provider-agnostic: a local file scheduler, a Compose
    worker pool, or a cloud scheduler/queue adapter all implement the same
    logical job model. Beyond CRUD it exposes a due-claim/lease surface so
    several worker tasks or schedulers can poll the same job set without firing
    a single due occurrence twice, and a run-recording surface that preserves
    Hermes' empty-output/silent and error-alert semantics as metadata rather
    than forcing a delivery.
    """

    def create(self, context: RuntimeContext | None, job: Mapping[str, Any]) -> str: ...

    def update(self, context: RuntimeContext | None, job_id: str, job: Mapping[str, Any]) -> None: ...

    def get_job(self, context: RuntimeContext | None, job_id: str) -> Mapping[str, Any] | None: ...

    def pause(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def resume(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def remove(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def list_jobs(self, context: RuntimeContext | None) -> list[Any]: ...

    def run_history(self, context: RuntimeContext | None, job_id: str) -> list[Any]: ...

    def claim_due(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
        draining: bool = False,
        limit: int | None = None,
    ) -> list[Mapping[str, Any]]: ...

    def renew_lease(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> bool: ...

    def release_lease(self, context: RuntimeContext | None, job_id: str, *, owner: str) -> None: ...

    def complete_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        output: str | None = None,
        delivery_error: str | None = None,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> Mapping[str, Any]: ...

    def fail_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        error: str,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class CredentialResolver(Protocol):
    """Resolves a credential by reference/capability, never by raw value."""

    def resolve(self, context: RuntimeContext | None, ref: str) -> str | None: ...


@runtime_checkable
class SecretStore(Protocol):
    """Backing store for secret values addressed by opaque reference."""

    def get_secret(self, context: RuntimeContext | None, ref: str) -> str | None: ...

    def put_secret(self, context: RuntimeContext | None, ref: str, value: str) -> None: ...


@runtime_checkable
class QueueBackend(Protocol):
    """Pending turns, events, cron firings, and jobs."""

    def enqueue(
        self,
        context: RuntimeContext | None,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str: ...

    def claim(self, context: RuntimeContext | None, *, capability: str | None = None) -> Any | None: ...

    def ack(self, context: RuntimeContext | None, receipt: Any) -> None: ...

    def nack(self, context: RuntimeContext | None, receipt: Any, *, requeue: bool = True) -> None: ...

    def extend_lease(self, context: RuntimeContext | None, receipt: Any, *, seconds: int) -> None: ...


@runtime_checkable
class RunLeaseBackend(Protocol):
    """One active owner per run/job/conversation turn."""

    def claim(self, context: RuntimeContext | None, key: str, *, owner: str) -> bool: ...

    def renew(self, context: RuntimeContext | None, key: str, *, owner: str) -> bool: ...

    def release(self, context: RuntimeContext | None, key: str, *, owner: str) -> None: ...

    def expire_stale(self, context: RuntimeContext | None) -> int: ...


@runtime_checkable
class ConversationRouter(Protocol):
    """Maps external messaging events to conversations and active runs."""

    def resolve_conversation(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> str: ...

    def find_active_run(self, context: RuntimeContext | None, conversation_id: str) -> str | None: ...

    def route_turn(self, context: RuntimeContext | None, conversation_id: str, turn: Mapping[str, Any]) -> str: ...


@runtime_checkable
class WorkerRegistry(Protocol):
    """Tracks fleet capacity and lifecycle."""

    def register(self, context: RuntimeContext | None, worker: Mapping[str, Any]) -> str: ...

    def heartbeat(self, context: RuntimeContext | None, worker_id: str, *, slots: Mapping[str, Any]) -> None: ...

    def mark_draining(self, context: RuntimeContext | None, worker_id: str) -> None: ...

    def recover_expired(self, context: RuntimeContext | None) -> list[str]: ...


@runtime_checkable
class ArtifactBackend(Protocol):
    """Durable storage for tool outputs/files by scoped reference."""

    def put(self, context: RuntimeContext | None, ref: str, data: bytes) -> str: ...

    def get(self, context: RuntimeContext | None, ref: str) -> bytes | None: ...

    def list_artifacts(self, context: RuntimeContext | None) -> list[str]: ...


@runtime_checkable
class AuditBackend(Protocol):
    """Receives sanitized audit events; never stores raw secret values."""

    def record(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None: ...


@runtime_checkable
class DeliveryBackend(Protocol):
    """Delivers a run's outbound messages to a configured route."""

    def deliver(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None: ...


class LocalMemoryBackend:
    def __init__(self) -> None:
        self._store: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def read(self, context: RuntimeContext | None, *, target: str = "memory") -> str | None:
        with self._lock:
            return self._store.get(_runtime_scope_key(context), {}).get(target)

    def write(
        self,
        context: RuntimeContext | None,
        content: str,
        *,
        target: str = "memory",
        action: str = "add",
    ) -> None:
        with self._lock:
            self._store.setdefault(_runtime_scope_key(context), {})[target] = content


class LocalSkillBackend:
    """Filesystem-backed skill source — the local compatibility backend.

    Wraps the existing ``~/.hermes/skills`` discovery/loading semantics in
    :mod:`tools.skills_tool` / :mod:`tools.skill_manager_tool` instead of
    reimplementing them, so linked files, readiness/setup metadata, platform
    compatibility filtering, prompt-injection scanning, the pinned-delete guard,
    and ``absorbed_into`` semantics are all preserved.

    Tenant isolation in local mode comes from Hermes' existing per-profile
    skills directories, so ``RuntimeContext`` is accepted for contract symmetry
    and audit scoping but does not repartition the on-disk tree. Imports are kept
    lazy so this core contract module stays lightweight at import time and free
    of tool-layer import cycles.
    """

    def list_skills(
        self,
        context: RuntimeContext | None,
        *,
        category: str | None = None,
    ) -> list[Mapping[str, Any]]:
        from tools.skills_tool import _skills_list_impl

        payload = json.loads(_skills_list_impl(category=category))
        if not payload.get("success"):
            return []
        return list(payload.get("skills", []))

    def load_skill(
        self,
        context: RuntimeContext | None,
        name: str,
        *,
        file_path: str | None = None,
        preprocess: bool = True,
    ) -> Mapping[str, Any]:
        from tools.skills_tool import _skill_view_impl

        return json.loads(
            _skill_view_impl(name, file_path=file_path, preprocess=preprocess)
        )

    def manage_skill(
        self,
        context: RuntimeContext | None,
        *,
        action: str,
        name: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        from tools.skill_manager_tool import _skill_manage_impl

        return json.loads(_skill_manage_impl(action=action, name=name, **fields))


class LocalSessionBackend:
    def __init__(self) -> None:
        self._events: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        self._children: dict[str, str] = {}
        self._locks: dict[tuple[Any, ...], dict[str, tuple[str, float]]] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
    ) -> str:
        resolved = session_id or (context.conversation_id if context and context.conversation_id else "local")
        if parent_session_id:
            with self._lock:
                self._children[parent_session_id] = resolved
        return resolved

    def append_message(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
        self.append(context, message)

    def read_messages(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        return self.read(context, limit=limit)

    def resolve_resume_session_id(self, session_id: str) -> str:
        with self._lock:
            return self._children.get(session_id, session_id)

    def claim_turn_lock(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        key = context.conversation_id if context and context.conversation_id else "local"
        with self._lock:
            locks = self._locks.setdefault(_session_scope_key(context), {})
            now = time.time()
            current = locks.get(key)
            if current is not None:
                current_owner, expires_at = current
                if expires_at >= now and current_owner != owner:
                    return False
            locks[key] = (owner, now + ttl_seconds)
            return True

    def renew_turn_lock(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        key = context.conversation_id if context and context.conversation_id else "local"
        with self._lock:
            current = self._locks.get(_session_scope_key(context), {}).get(key)
            if current is None:
                return False
            current_owner, expires_at = current
            if expires_at < time.time() or current_owner != owner:
                return False
            self._locks[_session_scope_key(context)][key] = (owner, time.time() + ttl_seconds)
            return True

    def release_turn_lock(self, context: RuntimeContext | None, *, owner: str) -> None:
        key = context.conversation_id if context and context.conversation_id else "local"
        with self._lock:
            locks = self._locks.get(_session_scope_key(context), {})
            current = locks.get(key)
            if current is not None and current[0] == owner:
                locks.pop(key, None)

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.setdefault(_session_scope_key(context), []).append(copy.deepcopy(dict(event)))

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[Any]:
        with self._lock:
            events = copy.deepcopy(self._events.get(_session_scope_key(context), []))
        if limit is None:
            return events
        if limit <= 0:
            return []
        return events[-limit:]

    def search(self, context: RuntimeContext | None, query: str) -> list[Any]:
        with self._lock:
            events = copy.deepcopy(self._events.get(_session_scope_key(context), []))
        return [event for event in events if query in repr(event)]


class LocalCronBackend:
    """In-process, RuntimeContext-scoped cron model and worker-safe scheduler.

    This is the local compatibility/test backend: it keeps the M2 CRUD storage
    contract while growing the native cron surface M8 requires — context/delivery
    binding, due-claim leasing that survives worker death via timeout, and run
    recording that preserves silent and error-alert semantics. State is held in
    process and partitioned by the same RuntimeContext fields a durable backend
    keys on, so two tenants never observe each other's jobs.

    Recurring vs one-shot is driven by the job's ``repeat`` (an int count or a
    ``{"times", "completed"}`` mapping): ``repeat=1`` is one-shot, ``None`` is
    forever. ``next_run_at`` (a numeric clock value) gates due-ness; absent
    means immediately due.
    """

    def __init__(self) -> None:
        self._jobs: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self._history: dict[tuple[Any, ...], dict[str, list[Any]]] = {}
        self._leases: dict[tuple[Any, ...], dict[str, tuple[str, float]]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_repeat(job: Mapping[str, Any]) -> dict[str, Any]:
        raw = job.get("repeat")
        if isinstance(raw, Mapping):
            times = raw.get("times")
            completed = raw.get("completed", 0)
        elif isinstance(raw, int):
            times = raw if raw > 0 else None
            completed = 0
        elif job.get("one_shot"):
            times, completed = 1, 0
        else:
            times, completed = None, 0
        return {"times": times, "completed": int(completed or 0)}

    @staticmethod
    def _is_done(record: Mapping[str, Any]) -> bool:
        repeat = record.get("repeat") or {}
        times = repeat.get("times")
        return times is not None and int(repeat.get("completed", 0)) >= int(times)

    def create(self, context: RuntimeContext | None, job: Mapping[str, Any]) -> str:
        with self._lock:
            self._counter += 1
            job_id = str(self._counter)
            scope = _cron_scope_key(context)
            record = {
                **copy.deepcopy(dict(job)),
                "id": job_id,
                "paused": False,
                "state": "scheduled",
                "repeat": self._normalize_repeat(job),
                "deliver": job.get("deliver", "local"),
                "binding": _cron_job_binding(context),
            }
            self._jobs.setdefault(scope, {})[job_id] = record
            self._history.setdefault(scope, {})[job_id] = []
            return job_id

    def update(self, context: RuntimeContext | None, job_id: str, job: Mapping[str, Any]) -> None:
        with self._lock:
            record = self._jobs.get(_cron_scope_key(context), {}).get(job_id)
            if record is not None:
                record.update(copy.deepcopy(dict(job)))

    def get_job(self, context: RuntimeContext | None, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(_cron_scope_key(context), {}).get(job_id)
            return copy.deepcopy(record) if record is not None else None

    def pause(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(_cron_scope_key(context), {}).get(job_id)
            if record is None:
                return
            record["paused"] = True
            record["state"] = "paused"

    def resume(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(_cron_scope_key(context), {}).get(job_id)
            if record is None:
                return
            record["paused"] = False
            if record.get("state") == "paused":
                record["state"] = "scheduled"

    def remove(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            scope = _cron_scope_key(context)
            self._jobs.get(scope, {}).pop(job_id, None)
            self._history.get(scope, {}).pop(job_id, None)
            self._leases.get(scope, {}).pop(job_id, None)

    def list_jobs(self, context: RuntimeContext | None) -> list[Any]:
        with self._lock:
            return copy.deepcopy(list(self._jobs.get(_cron_scope_key(context), {}).values()))

    def run_history(self, context: RuntimeContext | None, job_id: str) -> list[Any]:
        with self._lock:
            return copy.deepcopy(self._history.get(_cron_scope_key(context), {}).get(job_id, []))

    def _lease_is_live(self, scope: tuple[Any, ...], job_id: str, now: float) -> tuple[str, float] | None:
        lease = self._leases.get(scope, {}).get(job_id)
        if lease is None:
            return None
        _owner, expires_at = lease
        return lease if expires_at > now else None

    @staticmethod
    def _next_run_due_at(next_run_at: Any) -> float:
        if isinstance(next_run_at, (int, float)):
            return float(next_run_at)
        if isinstance(next_run_at, str):
            text = next_run_at.strip()
            if not text:
                return 0.0
            try:
                return float(text)
            except ValueError:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return float(next_run_at)

    def _is_due(self, record: Mapping[str, Any], now: float) -> bool:
        if record.get("paused") or record.get("state") in {"paused", "completed", "needs_schedule"}:
            return False
        if self._is_done(record):
            return False
        next_run_at = record.get("next_run_at")
        if next_run_at is None:
            return True
        try:
            return self._next_run_due_at(next_run_at) <= now
        except (TypeError, ValueError, OverflowError) as exc:
            if isinstance(record, dict):
                record["state"] = "needs_schedule"
                record["next_run_at_error"] = str(exc)
            return False

    def claim_due(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
        draining: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if draining:
            return []
        clock = time.time() if now is None else now
        claimed: list[dict[str, Any]] = []
        with self._lock:
            scope = _cron_scope_key(context)
            jobs = self._jobs.get(scope, {})
            leases = self._leases.setdefault(scope, {})
            for job_id, record in jobs.items():
                if not self._is_due(record, clock):
                    continue
                if self._lease_is_live(scope, job_id, clock) is not None:
                    continue
                leases[job_id] = (owner, clock + lease_seconds)
                record["state"] = "running"
                claim = copy.deepcopy(record)
                claim["owner"] = owner
                claim["lease_expires_at"] = clock + lease_seconds
                claimed.append(claim)
                if limit is not None and len(claimed) >= limit:
                    break
        return claimed

    def _require_lease(self, scope: tuple[Any, ...], job_id: str, owner: str, now: float) -> None:
        lease = self._leases.get(scope, {}).get(job_id)
        if lease is None or lease[0] != owner or lease[1] <= now:
            raise CronLeaseError(
                f"worker {owner!r} does not hold the lease for cron job {job_id!r}"
            )

    def renew_lease(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> bool:
        clock = time.time() if now is None else now
        with self._lock:
            scope = _cron_scope_key(context)
            lease = self._leases.get(scope, {}).get(job_id)
            if lease is None or lease[0] != owner or lease[1] <= clock:
                return False
            self._leases[scope][job_id] = (owner, clock + lease_seconds)
            return True

    def release_lease(self, context: RuntimeContext | None, job_id: str, *, owner: str) -> None:
        with self._lock:
            scope = _cron_scope_key(context)
            lease = self._leases.get(scope, {}).get(job_id)
            if lease is not None and lease[0] == owner:
                self._leases[scope].pop(job_id, None)

    def _finish_run(
        self,
        scope: tuple[Any, ...],
        job_id: str,
        owner: str,
        entry: Mapping[str, Any],
        next_run_at: float | None,
        now: float,
    ) -> dict[str, Any]:
        self._require_lease(scope, job_id, owner, now)
        record = self._jobs.get(scope, {}).get(job_id)
        if record is not None:
            repeat = record.setdefault("repeat", {"times": None, "completed": 0})
            repeat["completed"] = int(repeat.get("completed", 0)) + 1
            if next_run_at is not None:
                record["next_run_at"] = next_run_at
            if self._is_done(record):
                record["state"] = "completed"
            elif next_run_at is None:
                # Scheduler adapters must advance recurring schedules before the
                # next claim. Without that fail-closed state, an old due time can
                # hot-loop immediately after successful completion.
                record["state"] = "needs_schedule"
            elif not record.get("paused"):
                record["state"] = "scheduled"
        stored = copy.deepcopy(dict(entry))
        self._history.setdefault(scope, {}).setdefault(job_id, []).append(stored)
        self._leases.get(scope, {}).pop(job_id, None)
        return copy.deepcopy(stored)

    def complete_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        output: str | None = None,
        delivery_error: str | None = None,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time() if now is None else now
        silent = _cron_output_is_silent(output)
        if delivery_error:
            status, delivery, error = "delivery_error", "error", delivery_error
        elif silent:
            status, delivery, error = "success", "skipped_silent", None
        else:
            status, delivery, error = "success", "delivered", None
        entry = {
            "status": status,
            "owner": owner,
            "output_empty": output is None or not str(output).strip(),
            "silent": silent,
            "delivery": delivery,
            "error": error,
            "at": clock,
        }
        with self._lock:
            return self._finish_run(_cron_scope_key(context), job_id, owner, entry, next_run_at, clock)

    def fail_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        error: str,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time() if now is None else now
        entry = {
            "status": "error",
            "owner": owner,
            "output_empty": True,
            "silent": False,
            "delivery": "error_alert",
            "error": error,
            "at": clock,
        }
        with self._lock:
            return self._finish_run(_cron_scope_key(context), job_id, owner, entry, next_run_at, clock)


class LocalCredentialResolver:
    def __init__(self) -> None:
        self._refs: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def put_ref(self, context: RuntimeContext | None, ref: str, secret_ref: str) -> None:
        """Bind a logical credential request ref to an opaque secret ref."""

        with self._lock:
            self._refs.setdefault(_credential_scope_key(context), {})[ref] = secret_ref

    def resolve(self, context: RuntimeContext | None, ref: str) -> str | None:
        with self._lock:
            return self._refs.get(_credential_scope_key(context), {}).get(ref)


class LocalSecretStore:
    def __init__(self) -> None:
        self._secrets: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def get_secret(self, context: RuntimeContext | None, ref: str) -> str | None:
        with self._lock:
            return self._secrets.get(_credential_scope_key(context), {}).get(ref)

    def put_secret(self, context: RuntimeContext | None, ref: str, value: str) -> None:
        with self._lock:
            self._secrets.setdefault(_credential_scope_key(context), {})[ref] = value


class LocalQueueBackend:
    def __init__(self) -> None:
        self._items: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._seen_keys: dict[tuple[Any, ...], set[str]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def enqueue(
        self,
        context: RuntimeContext | None,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        with self._lock:
            scope = _runtime_scope_key(context)
            seen = self._seen_keys.setdefault(scope, set())
            if idempotency_key is not None and idempotency_key in seen:
                return idempotency_key
            if idempotency_key is not None:
                seen.add(idempotency_key)
            self._counter += 1
            receipt = idempotency_key or str(self._counter)
            self._items.setdefault(scope, []).append({"receipt": receipt, "payload": copy.deepcopy(dict(payload))})
            return receipt

    def claim(self, context: RuntimeContext | None, *, capability: str | None = None) -> Any | None:
        with self._lock:
            items = self._items.get(_runtime_scope_key(context), [])
            return copy.deepcopy(items.pop(0)) if items else None

    def ack(self, context: RuntimeContext | None, receipt: Any) -> None:
        return None

    def nack(self, context: RuntimeContext | None, receipt: Any, *, requeue: bool = True) -> None:
        if requeue and isinstance(receipt, Mapping):
            with self._lock:
                self._items.setdefault(_runtime_scope_key(context), []).append(copy.deepcopy(dict(receipt)))

    def extend_lease(self, context: RuntimeContext | None, receipt: Any, *, seconds: int) -> None:
        return None


class LocalRunLeaseBackend:
    def __init__(self) -> None:
        self._owners: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def claim(self, context: RuntimeContext | None, key: str, *, owner: str) -> bool:
        with self._lock:
            owners = self._owners.setdefault(_runtime_scope_key(context), {})
            current = owners.get(key)
            if current is not None and current != owner:
                return False
            owners[key] = owner
            return True

    def renew(self, context: RuntimeContext | None, key: str, *, owner: str) -> bool:
        with self._lock:
            return self._owners.get(_runtime_scope_key(context), {}).get(key) == owner

    def release(self, context: RuntimeContext | None, key: str, *, owner: str) -> None:
        with self._lock:
            owners = self._owners.get(_runtime_scope_key(context), {})
            if owners.get(key) == owner:
                owners.pop(key, None)

    def expire_stale(self, context: RuntimeContext | None) -> int:
        return 0


class LocalConversationRouter:
    def __init__(self) -> None:
        self._active_runs: dict[tuple[Any, ...], dict[str, str]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def resolve_conversation(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> str:
        if context is not None and context.conversation_id:
            return context.conversation_id
        return str(event.get("conversation_id") or event.get("thread_id") or "local")

    def find_active_run(self, context: RuntimeContext | None, conversation_id: str) -> str | None:
        with self._lock:
            return self._active_runs.get(_runtime_scope_key(context), {}).get(conversation_id)

    def route_turn(self, context: RuntimeContext | None, conversation_id: str, turn: Mapping[str, Any]) -> str:
        with self._lock:
            active_runs = self._active_runs.setdefault(_runtime_scope_key(context), {})
            run_id = active_runs.get(conversation_id)
            if run_id is None:
                self._counter += 1
                run_id = str(self._counter)
                active_runs[conversation_id] = run_id
            return run_id


class LocalWorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def register(self, context: RuntimeContext | None, worker: Mapping[str, Any]) -> str:
        with self._lock:
            self._counter += 1
            worker_id = str(worker.get("id") or self._counter)
            self._workers.setdefault(_runtime_scope_key(context), {})[worker_id] = {**dict(worker), "draining": False}
            return worker_id

    def heartbeat(self, context: RuntimeContext | None, worker_id: str, *, slots: Mapping[str, Any]) -> None:
        with self._lock:
            self._workers.setdefault(_runtime_scope_key(context), {}).setdefault(worker_id, {})["slots"] = dict(slots)

    def mark_draining(self, context: RuntimeContext | None, worker_id: str) -> None:
        with self._lock:
            self._workers.setdefault(_runtime_scope_key(context), {}).setdefault(worker_id, {})["draining"] = True

    def recover_expired(self, context: RuntimeContext | None) -> list[str]:
        return []


class LocalArtifactBackend:
    def __init__(self) -> None:
        self._artifacts: dict[tuple[Any, ...], dict[str, bytes]] = {}
        self._lock = threading.RLock()

    def put(self, context: RuntimeContext | None, ref: str, data: bytes) -> str:
        with self._lock:
            self._artifacts.setdefault(_runtime_scope_key(context), {})[ref] = bytes(data)
            return ref

    def get(self, context: RuntimeContext | None, ref: str) -> bytes | None:
        with self._lock:
            return self._artifacts.get(_runtime_scope_key(context), {}).get(ref)

    def list_artifacts(self, context: RuntimeContext | None) -> list[str]:
        with self._lock:
            return sorted(self._artifacts.get(_runtime_scope_key(context), {}))


class LocalAuditBackend:
    def __init__(self) -> None:
        self._events: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        self._lock = threading.RLock()

    def record(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None:
        sanitized = _redact_audit_value(event)
        with self._lock:
            self._events.setdefault(_runtime_scope_key(context), []).append(dict(sanitized))


class LocalDeliveryBackend:
    def __init__(self) -> None:
        self._delivered: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        self._lock = threading.RLock()

    def deliver(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
        with self._lock:
            self._delivered.setdefault(_runtime_scope_key(context), []).append(dict(message))


# Factories receive static options only. RuntimeContext remains a method-level
# argument on every backend contract, so cached instances cannot bind a tenant.
BackendFactory = Callable[[Mapping[str, Any]], Any]

_LOCAL_BACKEND_TYPES: dict[BackendCapability, Callable[[], Any]] = {
    BackendCapability.MEMORY: LocalMemoryBackend,
    BackendCapability.SKILL: LocalSkillBackend,
    BackendCapability.SESSION: LocalSessionBackend,
    BackendCapability.CRON: LocalCronBackend,
    BackendCapability.CREDENTIAL: LocalCredentialResolver,
    BackendCapability.SECRET: LocalSecretStore,
    BackendCapability.QUEUE: LocalQueueBackend,
    BackendCapability.RUN_LEASE: LocalRunLeaseBackend,
    BackendCapability.CONVERSATION_ROUTER: LocalConversationRouter,
    BackendCapability.WORKER_REGISTRY: LocalWorkerRegistry,
    BackendCapability.ARTIFACT: LocalArtifactBackend,
    BackendCapability.AUDIT: LocalAuditBackend,
    BackendCapability.DELIVERY: LocalDeliveryBackend,
}


def _coerce_capability(capability: BackendCapability | str) -> BackendCapability:
    if isinstance(capability, BackendCapability):
        return capability
    try:
        return BackendCapability(str(capability))
    except ValueError as exc:
        raise BackendSelectionError(f"Unknown backend capability: {capability!r}") from exc


class RuntimeBackendRegistry:
    """Selects a concrete backend per capability by config + RuntimeContext.

    Profile selection precedence (most specific wins):

    1. ``config["backends"]["capabilities"][<capability>]`` — per-capability override
    2. ``RuntimeContext.backend_profile`` — the run's deployment profile
    3. ``config["backends"]["default_profile"]`` — registry-wide default
    4. ``"local"`` — built-in compatibility default

    Each registry owns its own factory table and instance cache, so distinct
    registries never share mutable backend state.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        backends_config = {}
        if isinstance(config, Mapping):
            candidate = config.get("backends")
            if isinstance(candidate, Mapping):
                backends_config = dict(candidate)
        self._backends_config: Mapping[str, Any] = backends_config
        self._factories: dict[BackendCapability, dict[str, BackendFactory]] = {}
        self._instances: dict[tuple[BackendCapability, str], Any] = {}
        self._lock = threading.RLock()
        self._register_local_defaults()

    def _register_local_defaults(self) -> None:
        for capability, local_type in _LOCAL_BACKEND_TYPES.items():
            self.register(
                capability,
                lambda options, _type=local_type: _type(),
                profile=_DEFAULT_PROFILE,
            )

    def register(
        self,
        capability: BackendCapability | str,
        factory: BackendFactory,
        *,
        profile: str = _DEFAULT_PROFILE,
    ) -> None:
        cap = _coerce_capability(capability)
        with self._lock:
            self._factories.setdefault(cap, {})[profile] = factory
            self._instances.pop((cap, profile), None)

    def resolve_profile(
        self,
        capability: BackendCapability | str,
        context: RuntimeContext | None,
    ) -> str:
        cap = _coerce_capability(capability)
        overrides = self._backends_config.get("capabilities")
        if isinstance(overrides, Mapping):
            override = overrides.get(cap.value)
            if override:
                return str(override)
        if context is not None and context.backend_profile:
            return context.backend_profile
        default = self._backends_config.get("default_profile")
        if default:
            return str(default)
        return _DEFAULT_PROFILE

    def _capability_options(self, capability: BackendCapability) -> Mapping[str, Any]:
        options = self._backends_config.get("options")
        if isinstance(options, Mapping):
            capability_options = options.get(capability.value)
            if isinstance(capability_options, Mapping):
                return capability_options
        return {}

    def get(
        self,
        capability: BackendCapability | str,
        context: RuntimeContext | None = None,
    ) -> Any:
        cap = _coerce_capability(capability)
        profile = self.resolve_profile(cap, context)
        cache_key = (cap, profile)
        with self._lock:
            cached = self._instances.get(cache_key)
            if cached is not None:
                return cached
            factories = self._factories.get(cap, {})
            factory = factories.get(profile)
            if factory is None:
                available = sorted(factories)
                raise BackendSelectionError(
                    f"No backend registered for capability {cap.value!r} under "
                    f"profile {profile!r}. Available profiles: {available}."
                )
            instance = factory(self._capability_options(cap))
            self._instances[cache_key] = instance
            return instance
