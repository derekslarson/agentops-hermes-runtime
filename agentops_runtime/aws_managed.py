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

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from agent.runtime_backends import (
    _LOCAL_BACKEND_TYPES,
    BackendCapability,
    RuntimeBackendRegistry,
)
from agent.runtime_context import RuntimeContext
from agent.runtime_supervisor import LocalRunSupervisor, RunResult

AWS_MANAGED_PROFILE = "aws-managed"

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


def configure_aws_managed_runtime_backends(
    registry: RuntimeBackendRegistry,
    *,
    profile: str = AWS_MANAGED_PROFILE,
) -> None:
    """Register backends for the ``aws-managed`` profile on ``registry``.

    The spike registers the same local/fake backends the registry uses for its
    built-in ``local`` default, scoped under ``profile``. This proves the
    profile is selectable through the provider-neutral contracts without any
    AWS SDK, credentials, or network dependency. Real managed AWS backends
    (durable lease/queue/audit stores) replace these factories later.
    """

    for capability, local_type in _LOCAL_BACKEND_TYPES.items():
        registry.register(
            capability,
            lambda options, _type=local_type: _type(),
            profile=profile,
        )


def build_aws_managed_test_registry(
    config: object | None = None,
) -> RuntimeBackendRegistry:
    """Create a registry with the ``aws-managed`` profile registered."""

    registry = RuntimeBackendRegistry(config if isinstance(config, dict) else None)
    configure_aws_managed_runtime_backends(registry)
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
    "run_aws_managed_work_item",
]
