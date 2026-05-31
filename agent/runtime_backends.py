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
import threading
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agent.runtime_context import RuntimeContext

_DEFAULT_PROFILE = "local"
_SENSITIVE_EVENT_KEYS = ("secret", "token", "password", "api_key", "apikey", "credential")


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


def _redact_audit_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if any(marker in str(key).lower() for marker in _SENSITIVE_EVENT_KEYS)
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


@runtime_checkable
class MemoryBackend(Protocol):
    """Scoped store for the native memory tool."""

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
    """Scoped source for listing and loading skill content."""

    def list_skills(self, context: RuntimeContext | None) -> list[str]: ...

    def load_skill(self, context: RuntimeContext | None, name: str) -> str | None: ...


@runtime_checkable
class SessionBackend(Protocol):
    """Durable conversation/session persistence."""

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None: ...

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[Any]: ...

    def search(self, context: RuntimeContext | None, query: str) -> list[Any]: ...


@runtime_checkable
class CronBackend(Protocol):
    """Scheduled/autonomous job storage and run history."""

    def create(self, context: RuntimeContext | None, job: Mapping[str, Any]) -> str: ...

    def update(self, context: RuntimeContext | None, job_id: str, job: Mapping[str, Any]) -> None: ...

    def pause(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def resume(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def remove(self, context: RuntimeContext | None, job_id: str) -> None: ...

    def list_jobs(self, context: RuntimeContext | None) -> list[Any]: ...

    def run_history(self, context: RuntimeContext | None, job_id: str) -> list[Any]: ...


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
    def __init__(self) -> None:
        self._skills: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def list_skills(self, context: RuntimeContext | None) -> list[str]:
        with self._lock:
            return sorted(self._skills.get(_runtime_scope_key(context), {}))

    def load_skill(self, context: RuntimeContext | None, name: str) -> str | None:
        with self._lock:
            return self._skills.get(_runtime_scope_key(context), {}).get(name)


class LocalSessionBackend:
    def __init__(self) -> None:
        self._events: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        self._lock = threading.RLock()

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.setdefault(_runtime_scope_key(context), []).append(copy.deepcopy(dict(event)))

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[Any]:
        with self._lock:
            events = copy.deepcopy(self._events.get(_runtime_scope_key(context), []))
        if limit is None:
            return events
        if limit <= 0:
            return []
        return events[-limit:]

    def search(self, context: RuntimeContext | None, query: str) -> list[Any]:
        with self._lock:
            events = copy.deepcopy(self._events.get(_runtime_scope_key(context), []))
        return [event for event in events if query in repr(event)]


class LocalCronBackend:
    def __init__(self) -> None:
        self._jobs: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self._history: dict[tuple[Any, ...], dict[str, list[Any]]] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def create(self, context: RuntimeContext | None, job: Mapping[str, Any]) -> str:
        with self._lock:
            self._counter += 1
            job_id = str(self._counter)
            scope = _runtime_scope_key(context)
            self._jobs.setdefault(scope, {})[job_id] = {**copy.deepcopy(dict(job)), "paused": False}
            self._history.setdefault(scope, {})[job_id] = []
            return job_id

    def update(self, context: RuntimeContext | None, job_id: str, job: Mapping[str, Any]) -> None:
        with self._lock:
            self._jobs.setdefault(_runtime_scope_key(context), {}).setdefault(job_id, {}).update(copy.deepcopy(dict(job)))

    def pause(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            self._jobs.setdefault(_runtime_scope_key(context), {}).setdefault(job_id, {})["paused"] = True

    def resume(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            self._jobs.setdefault(_runtime_scope_key(context), {}).setdefault(job_id, {})["paused"] = False

    def remove(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock:
            scope = _runtime_scope_key(context)
            self._jobs.get(scope, {}).pop(job_id, None)
            self._history.get(scope, {}).pop(job_id, None)

    def list_jobs(self, context: RuntimeContext | None) -> list[Any]:
        with self._lock:
            return copy.deepcopy(list(self._jobs.get(_runtime_scope_key(context), {}).values()))

    def run_history(self, context: RuntimeContext | None, job_id: str) -> list[Any]:
        with self._lock:
            return copy.deepcopy(self._history.get(_runtime_scope_key(context), {}).get(job_id, []))


class LocalCredentialResolver:
    def __init__(self) -> None:
        self._refs: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def resolve(self, context: RuntimeContext | None, ref: str) -> str | None:
        with self._lock:
            return self._refs.get(_runtime_scope_key(context), {}).get(ref)


class LocalSecretStore:
    def __init__(self) -> None:
        self._secrets: dict[tuple[Any, ...], dict[str, str]] = {}
        self._lock = threading.RLock()

    def get_secret(self, context: RuntimeContext | None, ref: str) -> str | None:
        with self._lock:
            return self._secrets.get(_runtime_scope_key(context), {}).get(ref)

    def put_secret(self, context: RuntimeContext | None, ref: str, value: str) -> None:
        with self._lock:
            self._secrets.setdefault(_runtime_scope_key(context), {})[ref] = value


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
