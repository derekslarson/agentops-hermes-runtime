"""AWS-managed cloud adapter spike (M14).

Isolates all AWS-specific naming for the ``aws-managed`` deployment profile so
the provider-neutral core contracts (``agent/runtime_backends.py``,
``agent/runtime_supervisor.py``, ``agent/runtime_context.py``) stay free of
ECS/Fargate/SQS/RDS/S3/Secrets Manager/CloudWatch references.

This is a SPIKE, not a real AWS deployment:

* Profile registration goes through the existing ``RuntimeBackendRegistry``
  contracts using the local/fake backends as the default test path. No boto3,
  AWS credentials, network, or Terraform are required or imported.
* ``EcsWorkerFleetPlan`` proves the ECS desired task count scales independently
  from per-task run concurrency: whole-fleet ``capacity`` is the product of the
  two, but changing one never mutates the other (the plan is immutable, so each
  rescale yields a new value).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ClassVar, Mapping

from agent.runtime_backends import (
    _LOCAL_BACKEND_TYPES,
    BackendCapability,
    RuntimeBackendRegistry,
)
from agent.runtime_context import RuntimeContext
from agent.runtime_supervisor import LocalRunSupervisor, RunResult

AWS_MANAGED_PROFILE = "aws-managed"
_DEEP_MEMORY_DB_URL_ENV = "AGENTOPS_DEEP_MEMORY_DB_URL"
_DEEP_MEMORY_DB_URL_CONFIG_KEY = "deep_memory_db_url"

# Handler receives the bound RuntimeContext and the queued payload.
AwsManagedHandler = Callable[[RuntimeContext, Mapping[str, Any]], Any]


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


@dataclass(frozen=True, slots=True)
class EcsWorkerFleetPlan:
    """Sizing for an ECS worker fleet running the runtime worker lifecycle.

    ``desired_task_count`` is the number of ECS tasks the service runs;
    ``max_concurrent_runs`` is the per-task run-slot bound each worker enforces
    locally. The two scale independently — fleet ``capacity`` is their product,
    but rescaling one returns a new plan and never mutates the other.
    """

    desired_task_count: int
    max_concurrent_runs: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "desired_task_count", _positive_int("desired_task_count", self.desired_task_count)
        )
        object.__setattr__(
            self, "max_concurrent_runs", _positive_int("max_concurrent_runs", self.max_concurrent_runs)
        )

    @property
    def capacity(self) -> int:
        """Total concurrent run capacity across the fleet."""

        return self.desired_task_count * self.max_concurrent_runs

    def with_desired_task_count(self, desired_task_count: int) -> "EcsWorkerFleetPlan":
        """Return a new plan scaled to ``desired_task_count`` ECS tasks."""

        return replace(self, desired_task_count=desired_task_count)

    def with_max_concurrent_runs(self, max_concurrent_runs: int) -> "EcsWorkerFleetPlan":
        """Return a new plan with a different per-task run-slot bound."""

        return replace(self, max_concurrent_runs=max_concurrent_runs)


@dataclass(frozen=True, slots=True)
class AwsManagedWorkItem:
    """An AWS-shaped queued work item (SQS/EventBridge envelope) to execute.

    ``message_id`` is the non-secret queue message identifier. ``receipt_handle``
    is the SQS delete/visibility credential and is deliberately kept off the
    :class:`RuntimeContext` so it never reaches scope metadata or audit — only
    the non-secret ``message_id`` is surfaced as ``work_item_id``.
    """

    message_id: str
    run_type: str
    run_id: str
    org_id: str
    workspace_id: str
    user_id: str
    conversation_id: str
    agent_profile_id: str
    project_id: str
    job_id: str | None = None
    receipt_handle: str | None = field(default=None, repr=False)
    delivery_ref: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    backend_profile: str = AWS_MANAGED_PROFILE

    def __post_init__(self) -> None:
        if self.backend_profile != AWS_MANAGED_PROFILE:
            raise ValueError("backend_profile must be aws-managed")

    def to_context(self) -> RuntimeContext:
        """Map the queued item to an ``aws-managed`` agentops RuntimeContext."""

        return RuntimeContext(
            mode="agentops",
            org_id=self.org_id,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            agent_profile_id=self.agent_profile_id,
            project_id=self.project_id,
            run_id=self.run_id,
            run_type=self.run_type,
            job_id=self.job_id,
            backend_profile=self.backend_profile,
            delivery_ref=self.delivery_ref,
            metadata={"work_item_id": self.message_id},
        )


def _agentops_section(config: object | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    section = config.get("agentops")
    return section if isinstance(section, Mapping) else {}


@dataclass(frozen=True, repr=False, slots=True)
class _DeepMemoryDbUrlSecret:
    """Non-printing DB URL holder used by lazy backend factories.

    The raw DSN is deliberately not kept as a string in the factory defaults,
    registry options, or process-global adapter map. Factories decode it only
    when constructing the backend.
    """

    _encoded: tuple[int, ...]
    _mask: ClassVar[int] = 73

    @classmethod
    def from_url(cls, db_url: str) -> "_DeepMemoryDbUrlSecret":
        return cls(tuple(ord(char) ^ cls._mask for char in db_url))

    def reveal(self) -> str:
        return "".join(chr(value ^ self._mask) for value in self._encoded)

    def __repr__(self) -> str:
        return "_DeepMemoryDbUrlSecret([REDACTED])"



def _resolve_deep_memory_db_url(
    config: object | None,
    environ: Mapping[str, str] | None,
) -> str:
    agentops_cfg = _agentops_section(config)
    env = environ if environ is not None else os.environ
    for candidate in (
        env.get(_DEEP_MEMORY_DB_URL_ENV),
        agentops_cfg.get(_DEEP_MEMORY_DB_URL_CONFIG_KEY),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _unregister_registry_deep_memory(registry: RuntimeBackendRegistry, profile: str) -> None:
    """Remove an aws-managed DEEP_MEMORY binding from a registry without touching local defaults."""

    lock = getattr(registry, "_lock")
    with lock:
        factories = registry._factories.get(BackendCapability.DEEP_MEMORY)
        if factories is not None:
            factories.pop(profile, None)
        registry._instances.pop((BackendCapability.DEEP_MEMORY, profile), None)


def configure_aws_managed_runtime_backends(
    registry: RuntimeBackendRegistry,
    *,
    profile: str = AWS_MANAGED_PROFILE,
    config: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Register backends for the ``aws-managed`` profile on ``registry``.

    Registers local-compatible backends for all required capabilities.
    For DEEP_MEMORY: if a DB URL is resolved from ``config`` or ``environ``,
    registers a lazy ``RelationalMemoryRecordBackend`` factory; otherwise
    DEEP_MEMORY is left unregistered so the profile fails closed without
    any local/fake fallback.
    """

    for capability, local_type in _LOCAL_BACKEND_TYPES.items():
        registry.register(
            capability,
            lambda options, _type=local_type: _type(),
            profile=profile,
        )

    db_url = _resolve_deep_memory_db_url(config, environ)
    if not db_url:
        _unregister_registry_deep_memory(registry, profile)
        return

    db_url_secret = _DeepMemoryDbUrlSecret.from_url(db_url)

    def _deep_memory_factory(options, _secret=db_url_secret):
        from agentops_runtime.memory_record_store import make_relational_memory_backend
        return make_relational_memory_backend(_secret.reveal())

    registry.register(BackendCapability.DEEP_MEMORY, _deep_memory_factory, profile=profile)


def register_aws_managed_deep_memory_adapter(
    config: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Register a DEEP_MEMORY adapter factory for the aws-managed profile process-wide.

    No-ops when unconfigured (no resolved DB URL). When configured, registers a
    lazy ``RelationalMemoryRecordBackend`` factory via ``register_deep_memory_adapter``
    so ``apply_deep_memory_adapters`` binds it during native agent_init. Does not
    store the DSN in registry options or metadata.
    """

    from agent.runtime_backends import register_deep_memory_adapter, unregister_deep_memory_adapter

    db_url = _resolve_deep_memory_db_url(config, environ)
    if not db_url:
        unregister_deep_memory_adapter(AWS_MANAGED_PROFILE)
        return
    db_url_secret = _DeepMemoryDbUrlSecret.from_url(db_url)

    def _factory(options, _secret=db_url_secret):
        from agentops_runtime.memory_record_store import make_relational_memory_backend
        return make_relational_memory_backend(_secret.reveal())

    register_deep_memory_adapter(AWS_MANAGED_PROFILE, _factory)


def build_aws_managed_test_registry(
    config: object | None = None,
) -> RuntimeBackendRegistry:
    """Create a registry with the ``aws-managed`` profile registered.

    Registers all required capabilities with local-compatible backends.
    DEEP_MEMORY is not registered here — use ``apply_deep_memory_adapters``
    after calling ``register_aws_managed_deep_memory_adapter`` if needed.
    """

    registry = RuntimeBackendRegistry(config if isinstance(config, dict) else None)
    configure_aws_managed_runtime_backends(registry, environ={})
    return registry


def build_aws_managed_run_supervisor(
    *,
    fleet_plan: EcsWorkerFleetPlan,
    worker_id: str = "aws-managed-worker",
) -> tuple[LocalRunSupervisor, RuntimeBackendRegistry]:
    """Build a worker + registry that run the lifecycle under ``aws-managed``.

    The supervisor is bounded by the plan's per-task ``max_concurrent_runs`` (a
    single ECS task's slots), independent of the fleet-wide ``desired_task_count``.
    Returns the supervisor and the registry it was wired against.
    """

    registry = build_aws_managed_test_registry()
    supervisor = LocalRunSupervisor(
        worker_id=worker_id,
        max_concurrent_runs=fleet_plan.max_concurrent_runs,
        registry=registry,
    )
    return supervisor, registry


def run_aws_managed_work_item(
    work_item: AwsManagedWorkItem,
    handler: AwsManagedHandler,
    *,
    supervisor: LocalRunSupervisor | None = None,
    registry: RuntimeBackendRegistry | None = None,
    worker_id: str = "aws-managed-worker",
    max_concurrent_runs: int = 1,
) -> RunResult:
    """Run an AWS-shaped queued item through the shared runtime lifecycle.

    The item is mapped to an ``aws-managed`` :class:`RuntimeContext` and executed
    via :meth:`LocalRunSupervisor.run_to_completion`, so it claims the same
    per-run lease and records the same lifecycle audit as the local and Compose
    paths. When no supervisor is supplied one is built against the ``aws-managed``
    registry with the given per-task ``max_concurrent_runs`` bound — independent
    of any whole-fleet ``desired_task_count``. The handler receives the bound
    context and the queued payload.
    """

    context = work_item.to_context()
    if supervisor is None:
        if registry is None:
            registry = build_aws_managed_test_registry()
        supervisor = LocalRunSupervisor(
            worker_id=worker_id,
            max_concurrent_runs=max_concurrent_runs,
            registry=registry,
        )
    payload = dict(work_item.payload)
    return supervisor.run_to_completion(context, lambda: handler(context, payload))


__all__ = [
    "AWS_MANAGED_PROFILE",
    "AwsManagedWorkItem",
    "EcsWorkerFleetPlan",
    "build_aws_managed_run_supervisor",
    "build_aws_managed_test_registry",
    "configure_aws_managed_runtime_backends",
    "register_aws_managed_deep_memory_adapter",
    "run_aws_managed_work_item",
]
