"""Tests for the AWS-managed cloud adapter spike (M14).

This spike proves the provider-neutral runtime contracts can host an
``aws-managed`` deployment profile without leaking AWS/ECS/SQS/RDS naming into
the core contract modules and without requiring boto3, AWS credentials, or any
network access. Local/fake backends remain the default test path.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from agent.runtime_backends import (
    REQUIRED_CAPABILITIES,
    BackendCapability,
    RuntimeBackendRegistry,
)
from agent.runtime_context import RuntimeContext
from agent.runtime_supervisor import RunStatus
from agentops_runtime.aws_managed import (
    AWS_MANAGED_PROFILE,
    EcsWorkerFleetPlan,
    build_aws_managed_run_supervisor,
    build_aws_managed_test_registry,
    configure_aws_managed_runtime_backends,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_CONTRACT_MODULES = (
    "agent/runtime_backends.py",
    "agent/runtime_supervisor.py",
    "agent/runtime_context.py",
)
# Provider/service tokens that must never leak into core contract modules.
_AWS_SERVICE_TOKENS = ("ECS", "Fargate", "SQS", "RDS", "S3", "Secrets Manager", "CloudWatch")


def _aws_managed_context(run_id: str, *, job_id: str | None = None) -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="org-m14",
        workspace_id="workspace-m14",
        user_id="derek",
        conversation_id="aws-thread",
        agent_profile_id="default",
        project_id="runtime",
        run_id=run_id,
        run_type="manual",
        job_id=job_id,
        backend_profile=AWS_MANAGED_PROFILE,
    )


# --- ECS worker fleet sizing planner -------------------------------------


def test_fleet_capacity_is_tasks_times_per_task_concurrency():
    plan = EcsWorkerFleetPlan(desired_task_count=3, max_concurrent_runs=4)
    assert plan.capacity == 12


def test_scaling_desired_task_count_does_not_mutate_per_task_concurrency():
    plan = EcsWorkerFleetPlan(desired_task_count=2, max_concurrent_runs=4)

    scaled = plan.with_desired_task_count(5)

    assert scaled.desired_task_count == 5
    assert scaled.max_concurrent_runs == 4
    assert scaled.capacity == 20
    # Original plan is immutable: scaling produced a new value, not a mutation.
    assert plan.desired_task_count == 2
    assert plan.max_concurrent_runs == 4
    assert plan.capacity == 8


def test_changing_per_task_concurrency_does_not_mutate_desired_task_count():
    plan = EcsWorkerFleetPlan(desired_task_count=6, max_concurrent_runs=2)

    rescaled = plan.with_max_concurrent_runs(8)

    assert rescaled.max_concurrent_runs == 8
    assert rescaled.desired_task_count == 6
    assert rescaled.capacity == 48
    assert plan.max_concurrent_runs == 2
    assert plan.desired_task_count == 6


@pytest.mark.parametrize("desired", [0, -1, 2.5, "3"])
def test_fleet_plan_rejects_non_positive_desired_task_count(desired):
    with pytest.raises(ValueError):
        EcsWorkerFleetPlan(desired_task_count=desired, max_concurrent_runs=1)


@pytest.mark.parametrize("concurrency", [0, -4, 1.5, "2"])
def test_fleet_plan_rejects_non_positive_per_task_concurrency(concurrency):
    with pytest.raises(ValueError):
        EcsWorkerFleetPlan(desired_task_count=1, max_concurrent_runs=concurrency)


# --- aws-managed registry wiring -----------------------------------------


def test_configure_registers_every_required_capability_for_profile():
    registry = RuntimeBackendRegistry()
    configure_aws_managed_runtime_backends(registry)

    context = _aws_managed_context("run-caps")
    for capability in REQUIRED_CAPABILITIES:
        assert registry.resolve_profile(capability, context) == AWS_MANAGED_PROFILE
        assert registry.get(capability, context) is not None


def test_build_test_registry_resolves_aws_managed_profile():
    registry = build_aws_managed_test_registry()
    context = _aws_managed_context("run-build")

    lease = registry.get(BackendCapability.RUN_LEASE, context)
    audit = registry.get(BackendCapability.AUDIT, context)
    assert lease is not None
    assert audit is not None


def test_aws_managed_wiring_does_not_import_boto3_or_touch_network():
    # The spike must stay offline: importing/using the adapter never pulls boto3.
    assert "boto3" not in sys.modules
    build_aws_managed_test_registry()
    assert "boto3" not in sys.modules


# --- same worker lifecycle under aws-managed profile ---------------------


def test_local_supervisor_lifecycle_runs_under_aws_managed_profile():
    plan = EcsWorkerFleetPlan(desired_task_count=4, max_concurrent_runs=2)
    supervisor, registry = build_aws_managed_run_supervisor(
        fleet_plan=plan, worker_id="aws-managed-worker"
    )
    # The per-task supervisor honors the plan's per-task concurrency, not the
    # whole-fleet capacity.
    assert supervisor.max_concurrent_runs == 2

    context = _aws_managed_context("run-lifecycle", job_id="job-lifecycle")
    result = supervisor.run_to_completion(context, lambda: "done-aws")

    assert result.status is RunStatus.SUCCEEDED
    assert result.value == "done-aws"

    audit = registry.get(BackendCapability.AUDIT, context)
    statuses = [
        event["status"]
        for events in audit._events.values()
        for event in events
        if "status" in event
    ]
    assert statuses == ["started", "succeeded"]


# --- core contract leakage guard -----------------------------------------


def test_core_contract_modules_do_not_contain_aws_provider_strings():
    for relative in _CORE_CONTRACT_MODULES:
        source = (_REPO_ROOT / relative).read_text()
        for token in _AWS_SERVICE_TOKENS:
            pattern = re.compile(rf"\b{re.escape(token)}\b")
            assert not pattern.search(source), f"{relative} leaks AWS token {token!r}"
