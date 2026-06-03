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
import os
import re
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from agent.runtime_context import RuntimeContext

_DEFAULT_PROFILE = "local"
_LOCAL_MULTI_PROFILE = "local-multi"
_SENSITIVE_EVENT_KEYS = ("secret", "token", "password", "api_key", "apikey", "credential", "authorization", "auth")
_PATH_EVENT_KEYS = ("path", "file", "filename", "dirname", "cwd", "workdir", "directory")
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)\b(secret|token|password|api[_-]?key|x-api-key|credential|authorization|auth)\b[\"']?\s*[:=]\s*[\"']?[^\"'\s,;}]+"
)
_BEARER_TEXT_PATTERN = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+")
_PATH_TEXT_PATTERN = re.compile(r"(?:/Users/[^\s,;]+|/private/[^\s,;]+|/tmp/[^\s,;]+)")
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
        context.permissions_ref,
        context.backend_profile,
    )


def _provider_from_credential_request(ref: str) -> str | None:
    """Extract the provider name from a logical model credential request."""

    prefix = "model:"
    suffix = "/default"
    if not ref.startswith(prefix) or not ref.endswith(suffix):
        return None
    provider = ref[len(prefix) : -len(suffix)].strip().lower()
    return provider or None


def _local_provider_env_vars(provider: str) -> tuple[str, ...]:
    if provider == "openrouter":
        return ("OPENROUTER_API_KEY",)
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return ()

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig:
        return ()
    return tuple(str(name) for name in pconfig.api_key_env_vars)


def _load_local_env_file() -> Mapping[str, str]:
    try:
        from hermes_cli.config import load_env

        return load_env()
    except Exception:
        return {}


def _load_local_auth_store() -> Mapping[str, Any]:
    try:
        from hermes_cli.auth import _load_auth_store

        return _load_auth_store()
    except Exception:
        return {}


def _local_auth_provider_payload(provider: str) -> Mapping[str, Any] | None:
    store = _load_local_auth_store()
    payload = store.get(provider)
    if not isinstance(payload, Mapping):
        providers = store.get("providers")
        if isinstance(providers, Mapping):
            payload = providers.get(provider)
    return payload if isinstance(payload, Mapping) else None


def _local_provider_profile_secret(provider: str) -> str | None:
    payload = _local_auth_provider_payload(provider)
    if payload is None:
        return None
    for key in ("api_key", "access_token", "token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return key
    return None


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
            str(key): _redact_audit_mapping_item(str(key), item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_audit_value(item) for item in value)
    if isinstance(value, str):
        return _redact_audit_text(value)
    return value


def _redact_audit_text(value: str) -> str:
    value = _BEARER_TEXT_PATTERN.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = _SENSITIVE_TEXT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _PATH_TEXT_PATTERN.sub("[REDACTED_PATH]", value)


def _redact_audit_mapping_item(key: str, value: Any) -> Any:
    lowered = key.lower()
    normalized = "".join(ch for ch in lowered if ch.isalnum())
    if value is not None and (
        any(marker in lowered for marker in _SENSITIVE_EVENT_KEYS)
        or any("".join(ch for ch in marker if ch.isalnum()) in normalized for marker in _SENSITIVE_EVENT_KEYS)
    ):
        return "[REDACTED]"
    if value is not None and (
        any(marker == lowered or lowered.endswith(f"_{marker}") for marker in _PATH_EVENT_KEYS)
        or any("".join(ch for ch in marker if ch.isalnum()) in normalized for marker in _PATH_EVENT_KEYS)
    ):
        return "[REDACTED_PATH]"
    if isinstance(value, str):
        if lowered == "ref" or lowered.endswith("_ref"):
            return value
        return _redact_audit_text(value)
    return _redact_audit_value(value)


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
    # Optional, additively-registered capability (M5A). Deep memory is the
    # separate historical completed-turn recall surface; it is NOT part of the
    # MVP-required contract set so AgentOps/remote profiles without a registered
    # adapter fail closed rather than falling back to an unscoped local store.
    DEEP_MEMORY = "deep_memory"


_AUDITED_BACKEND_METHODS: dict[BackendCapability, tuple[str, ...]] = {
    BackendCapability.MEMORY: ("write",),
    BackendCapability.SKILL: ("list_skills", "load_skill", "manage_skill"),
    BackendCapability.SESSION: (
        "create_session",
        "append_message",
        "read_messages",
        "append",
        "read",
        "search",
        "claim_turn_lock",
        "renew_turn_lock",
        "release_turn_lock",
    ),
    BackendCapability.CRON: (
        "create",
        "update",
        "pause",
        "resume",
        "remove",
        "claim_due",
        "renew_lease",
        "release_lease",
        "complete_run",
        "fail_run",
    ),
    BackendCapability.QUEUE: ("enqueue", "claim", "ack", "nack", "extend_lease"),
    BackendCapability.RUN_LEASE: (
        "claim",
        "renew",
        "release",
        "request_cancel",
        "is_cancelled",
        "clear_cancelled",
        "expire_stale",
    ),
    BackendCapability.CONVERSATION_ROUTER: ("resolve_conversation", "find_active_run", "route_turn"),
    BackendCapability.WORKER_REGISTRY: ("register", "heartbeat", "mark_draining", "recover_expired", "list_workers"),
    BackendCapability.ARTIFACT: ("put", "get", "list_artifacts"),
    BackendCapability.DELIVERY: ("deliver",),
}


# The MVP-required runtime backend contract. Every required capability has a
# local default and must be satisfiable under every deployment profile.
# ``DEEP_MEMORY`` is intentionally excluded: it is an optional capability that
# fails closed on profiles without a registered adapter.
REQUIRED_CAPABILITIES: frozenset[BackendCapability] = frozenset(
    cap for cap in BackendCapability if cap is not BackendCapability.DEEP_MEMORY
)


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
class MemoryRecordBackend(Protocol):
    """Scoped backend for deep-memory historical records.

    This is the ``DEEP_MEMORY`` capability contract — a separate surface from the
    curated ``MemoryBackend`` above. Every method takes the RuntimeContext first
    and the backend owns scoping: a record ingested under one tenant/user/thread
    must never be searchable or fetchable (even by stable ID) under another. The
    native ``memory_record_search`` / ``memory_record_get`` / ``memory_record_get_many``
    tools dispatch through this contract using the current bound RuntimeContext.
    """

    def upsert_record(
        self,
        context: RuntimeContext | None,
        *,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: float | None = None,
        record_kind: str = "verbatim",
        parent_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        record_id: str | None = None,
    ) -> Any: ...

    def search(
        self,
        context: RuntimeContext | None,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> list[Any]: ...

    def get_record(self, context: RuntimeContext | None, record_id: str) -> Any: ...

    def get_many(self, context: RuntimeContext | None, ids: Iterable[str]) -> list[Any]: ...


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

    def claim(
        self,
        context: RuntimeContext | None,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool: ...

    def renew(
        self,
        context: RuntimeContext | None,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool: ...

    def release(self, context: RuntimeContext | None, key: str, *, owner: str) -> None: ...

    def request_cancel(self, context: RuntimeContext | None, key: str, *, requester: str) -> None: ...

    def is_cancelled(self, context: RuntimeContext | None, key: str) -> bool: ...

    def clear_cancelled(self, context: RuntimeContext | None, key: str) -> None: ...

    def expire_stale(self, context: RuntimeContext | None, *, now: float | None = None) -> int: ...


@runtime_checkable
class ConversationRouter(Protocol):
    """Maps external messaging events to conversations and active runs."""

    def resolve_conversation(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> str: ...

    def find_active_run(self, context: RuntimeContext | None, conversation_id: str) -> str | None: ...

    def route_turn(self, context: RuntimeContext | None, conversation_id: str, turn: Mapping[str, Any]) -> str: ...


@runtime_checkable
class WorkerRegistry(Protocol):
    """Tracks fleet capacity and lifecycle."""

    def register(
        self,
        context: RuntimeContext | None,
        worker: Mapping[str, Any],
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> str: ...

    def heartbeat(
        self,
        context: RuntimeContext | None,
        worker_id: str,
        *,
        slots: Mapping[str, Any],
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> None: ...

    def mark_draining(self, context: RuntimeContext | None, worker_id: str) -> None: ...

    def recover_expired(self, context: RuntimeContext | None, *, now: float | None = None) -> list[str]: ...

    def list_workers(self, context: RuntimeContext | None) -> list[Mapping[str, Any]]: ...


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


def _deep_memory_scope_key(context: RuntimeContext | None) -> tuple[Any, ...]:
    """Durable tenant/user/project/thread scope for deep-memory records.

    Reuses the session-scope shape (excludes per-execution ``run_id``/``job_id``)
    so a restarted/rescheduled worker resolves the same partition while another
    user/org/project/thread stays isolated. Including ``conversation_id`` gives
    thread-level isolation in multi-tenant mode.
    """

    return _session_scope_key(context)


class LocalDeepMemoryBackend:
    """RuntimeContext-scoped wrapper over the local flat deep-memory store.

    ``partition=False`` (the ``local`` single-user profile) keeps one shared store
    rooted exactly at the base dir (the current ``$HERMES_HOME/deep-memory``
    behaviour, with cross-session recall for the single user). ``partition=True``
    (the ``local-multi`` profile) gives each RuntimeContext scope its own store
    subdirectory so there is no cross-user/org/project/thread record leakage.

    The ChromaDB-backed store is imported lazily and created on first use per
    scope, so importing this module (and constructing the backend) never pulls in
    the optional Chroma/ONNX dependencies.
    """

    def __init__(
        self,
        *,
        base_dir: Any = None,
        partition: bool = False,
        embedding_device: str = "auto",
    ) -> None:
        self._base_dir = base_dir
        self._partition = bool(partition)
        self._embedding_device = embedding_device or "auto"
        self._stores: dict[tuple[Any, ...], Any] = {}
        self._lock = threading.RLock()

    def _resolve_base_dir(self):
        from pathlib import Path

        if self._base_dir:
            return Path(self._base_dir)
        from agent.local_memory.provider import load_deep_memory_config, resolve_storage_dir
        from hermes_constants import get_hermes_home

        cfg = load_deep_memory_config()
        return resolve_storage_dir(str(cfg.get("storage_dir")), str(get_hermes_home()))

    def _scope_key(self, context: RuntimeContext | None) -> tuple[Any, ...]:
        if not self._partition:
            return ()
        return _deep_memory_scope_key(context)

    @staticmethod
    def _scope_dirname(scope: tuple[Any, ...]) -> str:
        import hashlib

        digest = hashlib.sha256(repr(scope).encode("utf-8", errors="replace")).hexdigest()
        return "scope_" + digest[:32]

    def _store_for(self, context: RuntimeContext | None):
        from pathlib import Path

        scope = self._scope_key(context)
        with self._lock:
            store = self._stores.get(scope)
            if store is not None:
                return store
            from agent.local_memory.store import LocalMemoryStore

            base = Path(self._resolve_base_dir())
            root = base if not self._partition else base / self._scope_dirname(scope)
            store = LocalMemoryStore(root, embedding_device=self._embedding_device)
            self._stores[scope] = store
            return store

    def upsert_record(
        self,
        context: RuntimeContext | None,
        *,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: float | None = None,
        record_kind: str = "verbatim",
        parent_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        record_id: str | None = None,
    ) -> Any:
        return self._store_for(context).upsert_record(
            text=text,
            metadata=dict(metadata) if metadata else None,
            source=source,
            source_uri=source_uri,
            timestamp=timestamp,
            record_kind=record_kind,
            parent_id=parent_id,
            provenance=dict(provenance) if provenance else None,
            record_id=record_id,
        )

    def search(
        self,
        context: RuntimeContext | None,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> list[Any]:
        return self._store_for(context).search(
            query,
            filters=dict(filters) if filters else None,
            limit=limit,
            max_distance=max_distance,
            candidate_strategy=candidate_strategy,
        )

    def get_record(self, context: RuntimeContext | None, record_id: str) -> Any:
        return self._store_for(context).get_record(record_id)

    def get_many(self, context: RuntimeContext | None, ids: Iterable[str]) -> list[Any]:
        return self._store_for(context).get_many(ids)

    def stats(self, context: RuntimeContext | None) -> Mapping[str, Any]:
        return self._store_for(context).stats()

    def close(self) -> None:
        with self._lock:
            for store in self._stores.values():
                try:
                    store.close()
                except Exception:
                    pass
            self._stores.clear()


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
                updates = copy.deepcopy(dict(job))
                updates.pop("binding", None)
                record.update(updates)

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
                record["next_run_at_error"] = _redact_audit_text(str(exc))
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
            delivery_error = _redact_audit_text(delivery_error)
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
            "error": _redact_audit_text(error),
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
            explicit = self._refs.get(_credential_scope_key(context), {}).get(ref)
        if explicit:
            return explicit

        provider = _provider_from_credential_request(ref)
        if provider is None or getattr(context, "mode", None) == "agentops":
            return None
        env_file = _load_local_env_file()
        for env_var in _local_provider_env_vars(provider):
            if env_file.get(env_var) or os.environ.get(env_var):
                return f"env:{env_var}"
        profile_key = _local_provider_profile_secret(provider)
        if profile_key:
            return f"profile:{provider}:{profile_key}"
        return None


class LocalSecretStore:
    def __init__(self) -> None:
        self._secrets: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def get_secret(self, context: RuntimeContext | None, ref: str) -> str | None:
        if ref.startswith("env:"):
            if getattr(context, "mode", None) == "agentops":
                return None
            env_var = ref.split(":", 1)[1]
            value = _load_local_env_file().get(env_var) or os.environ.get(env_var)
            return value.strip() if isinstance(value, str) and value.strip() else None
        if ref.startswith("profile:"):
            if getattr(context, "mode", None) == "agentops":
                return None
            _, provider, key = ref.split(":", 2)
            payload = _local_auth_provider_payload(provider)
            if isinstance(payload, Mapping):
                value = payload.get(key)
                return value.strip() if isinstance(value, str) and value.strip() else None
            return None
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
        self._owners: dict[tuple[Any, ...], dict[str, tuple[str, float | None]]] = {}
        self._cancelled: dict[tuple[Any, ...], set[str]] = {}
        self._lock = threading.RLock()

    def claim(
        self,
        context: RuntimeContext | None,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        clock = time.time() if now is None else now
        expires_at = None if lease_seconds is None else clock + lease_seconds
        with self._lock:
            owners = self._owners.setdefault(_runtime_scope_key(context), {})
            current = owners.get(key)
            if current is not None:
                current_owner, current_expires_at = current
                if current_expires_at is not None and current_expires_at <= clock:
                    owners.pop(key, None)
                elif current_owner != owner:
                    return False
            owners[key] = (owner, expires_at)
            return True

    def renew(
        self,
        context: RuntimeContext | None,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        clock = time.time() if now is None else now
        with self._lock:
            current = self._owners.get(_runtime_scope_key(context), {}).get(key)
            if current is None:
                return False
            current_owner, current_expires_at = current
            if current_owner != owner or (current_expires_at is not None and current_expires_at <= clock):
                return False
            next_expires_at = current_expires_at if lease_seconds is None else clock + lease_seconds
            self._owners.setdefault(_runtime_scope_key(context), {})[key] = (owner, next_expires_at)
            return True

    def release(self, context: RuntimeContext | None, key: str, *, owner: str) -> None:
        with self._lock:
            owners = self._owners.get(_runtime_scope_key(context), {})
            current = owners.get(key)
            if current is not None and current[0] == owner:
                owners.pop(key, None)

    def request_cancel(self, context: RuntimeContext | None, key: str, *, requester: str) -> None:
        del requester
        with self._lock:
            self._cancelled.setdefault(_runtime_scope_key(context), set()).add(key)

    def is_cancelled(self, context: RuntimeContext | None, key: str) -> bool:
        with self._lock:
            return key in self._cancelled.get(_runtime_scope_key(context), set())

    def clear_cancelled(self, context: RuntimeContext | None, key: str) -> None:
        with self._lock:
            cancelled = self._cancelled.get(_runtime_scope_key(context))
            if cancelled is None:
                return
            cancelled.discard(key)
            if not cancelled:
                self._cancelled.pop(_runtime_scope_key(context), None)

    def expire_stale(self, context: RuntimeContext | None, *, now: float | None = None) -> int:
        clock = time.time() if now is None else now
        with self._lock:
            owners = self._owners.get(_runtime_scope_key(context), {})
            stale = [key for key, (_owner, expires_at) in owners.items() if expires_at is not None and expires_at <= clock]
            for key in stale:
                owners.pop(key, None)
            return len(stale)


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

    def register(
        self,
        context: RuntimeContext | None,
        worker: Mapping[str, Any],
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> str:
        clock = time.time() if now is None else now
        expires_at = None if ttl_seconds is None else clock + ttl_seconds
        with self._lock:
            self._counter += 1
            worker_id = str(worker.get("id") or self._counter)
            record = copy.deepcopy(dict(worker))
            record.update(
                {
                    "id": worker_id,
                    "draining": False,
                    "last_heartbeat": clock,
                    "expires_at": expires_at,
                }
            )
            self._workers.setdefault(_runtime_scope_key(context), {})[worker_id] = record
            return worker_id

    def heartbeat(
        self,
        context: RuntimeContext | None,
        worker_id: str,
        *,
        slots: Mapping[str, Any],
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        clock = time.time() if now is None else now
        expires_at = None if ttl_seconds is None else clock + ttl_seconds
        with self._lock:
            workers = self._workers.setdefault(_runtime_scope_key(context), {})
            if worker_id not in workers:
                raise KeyError(f"worker {worker_id} is not registered")
            worker = workers[worker_id]
            worker["slots"] = copy.deepcopy(dict(slots))
            worker["last_heartbeat"] = clock
            worker["expires_at"] = expires_at

    def mark_draining(self, context: RuntimeContext | None, worker_id: str) -> None:
        with self._lock:
            workers = self._workers.setdefault(_runtime_scope_key(context), {})
            if worker_id not in workers:
                raise KeyError(f"worker {worker_id} is not registered")
            workers[worker_id]["draining"] = True

    def recover_expired(self, context: RuntimeContext | None, *, now: float | None = None) -> list[str]:
        clock = time.time() if now is None else now
        with self._lock:
            workers = self._workers.setdefault(_runtime_scope_key(context), {})
            expired = [
                worker_id
                for worker_id, worker in workers.items()
                if worker.get("expires_at") is not None and worker["expires_at"] <= clock
            ]
            for worker_id in expired:
                workers.pop(worker_id, None)
            return sorted(expired)

    def list_workers(self, context: RuntimeContext | None) -> list[Mapping[str, Any]]:
        with self._lock:
            workers = self._workers.get(_runtime_scope_key(context), {})
            return [copy.deepcopy(workers[worker_id]) for worker_id in sorted(workers)]


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


# Process-level DEEP_MEMORY adapter registry. Deep memory has no MVP-required
# local default for non-local profiles (it fails closed), so a deployment that
# wants remote/compose deep memory registers a cloud/database-agnostic adapter
# factory here at startup, keyed by deployment profile. Agent init applies these
# to each per-agent registry before resolving, giving "remote-with-adapter" a
# real binding path without baking any provider/service name into this module.
_DEEP_MEMORY_ADAPTER_FACTORIES: dict[str, "BackendFactory"] = {}
_deep_memory_adapter_lock = threading.RLock()


def register_deep_memory_adapter(profile: str, factory: "BackendFactory") -> None:
    """Register a DEEP_MEMORY adapter factory for ``profile`` process-wide.

    The factory receives static capability options and returns any object
    satisfying :class:`MemoryRecordBackend`. Stays cloud/database agnostic: no
    provider/service name appears here. Re-registering a profile replaces it.
    """

    with _deep_memory_adapter_lock:
        _DEEP_MEMORY_ADAPTER_FACTORIES[str(profile)] = factory


def clear_deep_memory_adapters() -> None:
    """Drop all process-registered DEEP_MEMORY adapter factories."""

    with _deep_memory_adapter_lock:
        _DEEP_MEMORY_ADAPTER_FACTORIES.clear()


def apply_deep_memory_adapters(registry: "RuntimeBackendRegistry") -> None:
    """Register every process-level DEEP_MEMORY adapter onto ``registry``.

    Called before resolution so a non-local profile with a registered adapter
    binds the scoped backend instead of failing closed. Profiles without an
    adapter remain unregistered and still fail closed.
    """

    with _deep_memory_adapter_lock:
        items = list(_DEEP_MEMORY_ADAPTER_FACTORIES.items())
    for profile, factory in items:
        registry.register(BackendCapability.DEEP_MEMORY, factory, profile=profile)


def _coerce_capability(capability: BackendCapability | str) -> BackendCapability:
    if isinstance(capability, BackendCapability):
        return capability
    try:
        return BackendCapability(str(capability))
    except ValueError as exc:
        raise BackendSelectionError(f"Unknown backend capability: {capability!r}") from exc


class _AuditedBackendProxy:
    """Audit wrapper used only for backend instances that cannot be mutated in place."""

    def __init__(
        self,
        registry: "RuntimeBackendRegistry",
        capability: BackendCapability,
        instance: Any,
        method_names: tuple[str, ...],
    ) -> None:
        self._runtime_audit_registry = registry
        self._runtime_audit_capability = capability
        self._runtime_audit_instance = instance
        self._runtime_audit_method_names = method_names

    def __getattr__(self, name: str) -> Any:
        original_methods = getattr(self._runtime_audit_instance, "_runtime_audit_original_methods", {})
        attr = original_methods.get(name) if isinstance(original_methods, dict) else None
        if attr is None:
            attr = getattr(self._runtime_audit_instance, name)
        if name not in self._runtime_audit_method_names or not callable(attr):
            return attr

        def audited_method(*args: Any, **kwargs: Any) -> Any:
            context = args[0] if args and (args[0] is None or isinstance(args[0], RuntimeContext)) else kwargs.get("context")
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                self._runtime_audit_registry._record_backend_audit(
                    self._runtime_audit_capability,
                    name,
                    context,
                    success=False,
                    error=exc,
                )
                raise
            self._runtime_audit_registry._record_backend_audit(
                self._runtime_audit_capability,
                name,
                context,
                success=True,
            )
            return result

        return audited_method


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
        # Optional deep-memory capability. Registered only under the local
        # single-user and local-multi profiles; AgentOps/remote profiles
        # intentionally have NO local default so they fail closed unless a
        # remote/durable adapter is explicitly registered.
        self.register(
            BackendCapability.DEEP_MEMORY,
            lambda options: LocalDeepMemoryBackend(
                base_dir=options.get("storage_dir"),
                partition=bool(options.get("partition", False)),
                embedding_device=options.get("embedding_device", "auto"),
            ),
            profile=_DEFAULT_PROFILE,
        )
        self.register(
            BackendCapability.DEEP_MEMORY,
            lambda options: LocalDeepMemoryBackend(
                base_dir=options.get("storage_dir"),
                partition=True,
                embedding_device=options.get("embedding_device", "auto"),
            ),
            profile=_LOCAL_MULTI_PROFILE,
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
        candidate: str | None = None
        overrides = self._backends_config.get("capabilities")
        if isinstance(overrides, Mapping):
            override = overrides.get(cap.value)
            if override:
                candidate = str(override)
        if candidate is None and context is not None and context.backend_profile:
            candidate = context.backend_profile
        if candidate is None:
            default = self._backends_config.get("default_profile")
            if default:
                candidate = str(default)
        if candidate is None:
            candidate = _DEFAULT_PROFILE
        return self._guard_deep_memory_failclosed(cap, context, candidate)

    @staticmethod
    def _guard_deep_memory_failclosed(
        cap: BackendCapability,
        context: RuntimeContext | None,
        candidate: str,
    ) -> str:
        """Fail closed when a non-local DEEP_MEMORY context selects built-in local.

        The built-in ``local`` DEEP_MEMORY backend is unpartitioned (one shared
        scope), so a non-local (AgentOps/remote) context must never resolve to it
        — whether by the implicit fallback OR an *explicit* ``local`` selection
        (context ``backend_profile``, config ``default_profile``, or the
        per-capability override). Raise ``BackendSelectionError`` unconditionally
        rather than redirecting to the deployment ``mode``: a process-level adapter
        can be registered under any profile string (including the mode), so a
        redirect could silently substitute it for the unpartitioned local store.
        ``local-multi`` (scoped multi-user partitioning) is preserved, as is a
        remote/compose adapter selected by an explicit non-``local`` profile (even
        one equal to the mode string); genuine local-mode contexts keep ``local``.
        """
        if (
            cap is BackendCapability.DEEP_MEMORY
            and candidate == _DEFAULT_PROFILE
            and context is not None
            and context.mode
            and context.mode != "local"
        ):
            raise BackendSelectionError(
                f"Deep memory fails closed for non-local mode {context.mode!r}: the "
                f"built-in unpartitioned {_DEFAULT_PROFILE!r} deep-memory backend "
                f"cannot serve a non-local context. Register a scoped adapter under "
                f"an explicit deployment profile instead."
            )
        return candidate

    def _capability_options(self, capability: BackendCapability) -> Mapping[str, Any]:
        options = self._backends_config.get("options")
        if isinstance(options, Mapping):
            capability_options = options.get(capability.value)
            if isinstance(capability_options, Mapping):
                return capability_options
        return {}

    def set_capability_options(
        self,
        capability: BackendCapability | str,
        options: Mapping[str, Any],
        *,
        merge: bool = True,
    ) -> None:
        """Set static factory options for a backend capability.

        Backend factory options are deployment configuration, not tenant scope.
        Updating them invalidates cached instances for the capability so the next
        ``get(...)`` call constructs a backend from the new options.
        """

        cap = _coerce_capability(capability)
        new_options = dict(options)
        with self._lock:
            backends_config = dict(self._backends_config)
            existing_options = backends_config.get("options")
            all_options = dict(existing_options) if isinstance(existing_options, Mapping) else {}
            if merge:
                current = all_options.get(cap.value)
                merged = dict(current) if isinstance(current, Mapping) else {}
                merged.update(new_options)
                all_options[cap.value] = merged
            else:
                all_options[cap.value] = new_options
            backends_config["options"] = all_options
            self._backends_config = backends_config
            for cache_key in [key for key in self._instances if key[0] == cap]:
                self._instances.pop(cache_key, None)

    def _instrument_for_audit(self, capability: BackendCapability, instance: Any) -> Any:
        method_names = _AUDITED_BACKEND_METHODS.get(capability)
        if not method_names:
            return instance
        registry_id = id(self)
        wrapped_registry_id = getattr(instance, "_runtime_audit_registry_id", None)
        if wrapped_registry_id is not None and wrapped_registry_id != registry_id:
            return _AuditedBackendProxy(self, capability, instance, method_names)
        wrapped = set(getattr(instance, "_runtime_audit_wrapped_methods", set()))
        original_methods = dict(getattr(instance, "_runtime_audit_original_methods", {}))
        for method_name in method_names:
            if method_name in wrapped or not hasattr(instance, method_name):
                continue
            original = getattr(instance, method_name)
            if not callable(original):
                continue

            original_methods.setdefault(method_name, original)
            def audited_method(*args: Any, _method_name: str = method_name, _original: Callable[..., Any] = original, **kwargs: Any) -> Any:
                context = args[0] if args and (args[0] is None or isinstance(args[0], RuntimeContext)) else kwargs.get("context")
                try:
                    result = _original(*args, **kwargs)
                except Exception as exc:
                    self._record_backend_audit(capability, _method_name, context, success=False, error=exc)
                    raise
                self._record_backend_audit(capability, _method_name, context, success=True)
                return result

            try:
                setattr(instance, method_name, audited_method)
            except (AttributeError, TypeError):
                return _AuditedBackendProxy(self, capability, instance, method_names)
            wrapped.add(method_name)
        try:
            setattr(instance, "_runtime_audit_wrapped_methods", wrapped)
            setattr(instance, "_runtime_audit_original_methods", original_methods)
            setattr(instance, "_runtime_audit_registry_id", registry_id)
        except Exception:
            return instance
        return instance

    def _record_backend_audit(
        self,
        capability: BackendCapability,
        method_name: str,
        context: RuntimeContext | None,
        *,
        success: bool,
        error: Exception | None = None,
    ) -> None:
        try:
            audit = self.get(BackendCapability.AUDIT, context)
            event: dict[str, Any] = {
                "action": f"{capability.value}.{method_name}",
                "capability": capability.value,
                "method": method_name,
                "success": success,
            }
            if error is not None:
                event["error"] = f"{type(error).__name__}: {error}"
            audit.record(context, _redact_audit_value(event))
        except Exception:
            return None

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
            instance = self._instrument_for_audit(cap, instance)
            self._instances[cache_key] = instance
            return instance
