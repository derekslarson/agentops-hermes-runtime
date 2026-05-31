"""Tests for the M3 local multi-run supervisor baseline."""

from __future__ import annotations

import threading
import time

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, get_current_runtime_context
from agent.runtime_supervisor import LocalRunSupervisor, RunStatus


def _context(run_id: str, user_id: str) -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="org-m3",
        workspace_id="workspace-m3",
        user_id=user_id,
        conversation_id=f"conversation-{user_id}",
        agent_profile_id="default",
        project_id="runtime",
        run_id=run_id,
        run_type="manual",
        backend_profile="local",
    )


def test_local_supervisor_runs_two_scoped_runs_concurrently():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-1", max_concurrent_runs=2, registry=registry)
    ctx_a = _context("run-a", "derek")
    ctx_b = _context("run-b", "alex")
    ready = threading.Barrier(2, timeout=2)
    release = threading.Event()
    seen_contexts: list[str | None] = []
    seen_lock = threading.Lock()

    def task():
        ready.wait()
        with seen_lock:
            current = get_current_runtime_context()
            seen_contexts.append(current.run_id if current else None)
        release.wait(timeout=2)
        return "ok"

    handle_a = supervisor.submit(ctx_a, task)
    handle_b = supervisor.submit(ctx_b, task)

    assert ready.n_waiting == 0
    release.set()
    result_a = handle_a.result(timeout=2)
    result_b = handle_b.result(timeout=2)

    assert {result_a.value, result_b.value} == {"ok"}
    assert {result_a.context.run_id, result_b.context.run_id} == {"run-a", "run-b"}
    assert {result_a.status, result_b.status} == {RunStatus.SUCCEEDED}
    assert set(seen_contexts) == {"run-a", "run-b"}


def test_local_supervisor_isolates_state_and_survives_one_run_crashing():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-2", max_concurrent_runs=2, registry=registry)
    ctx_ok = _context("run-ok", "derek")
    ctx_fail = _context("run-fail", "alex")
    started = threading.Barrier(2, timeout=2)

    def successful_run():
        started.wait()
        memory = registry.get(BackendCapability.MEMORY, ctx_ok)
        sessions = registry.get(BackendCapability.SESSION, ctx_ok)
        artifacts = registry.get(BackendCapability.ARTIFACT, ctx_ok)
        memory.write(ctx_ok, "derek scoped memory")
        sessions.append(ctx_ok, {"message": "derek scoped session"})
        artifacts.put(ctx_ok, "result.txt", b"derek artifact")
        time.sleep(0.01)
        return "done"

    def crashing_run():
        started.wait()
        registry.get(BackendCapability.MEMORY, ctx_fail).write(ctx_fail, "alex scoped memory")
        raise RuntimeError("sentinel crash")

    ok = supervisor.submit(ctx_ok, successful_run)
    failed = supervisor.submit(ctx_fail, crashing_run)

    ok_result = ok.result(timeout=2)
    failed_result = failed.result(timeout=2)

    assert ok_result.status is RunStatus.SUCCEEDED
    assert failed_result.status is RunStatus.FAILED
    assert "sentinel crash" in failed_result.error

    memory = registry.get(BackendCapability.MEMORY, ctx_ok)
    sessions = registry.get(BackendCapability.SESSION, ctx_ok)
    artifacts = registry.get(BackendCapability.ARTIFACT, ctx_ok)
    assert memory.read(ctx_ok) == "derek scoped memory"
    assert sessions.read(ctx_ok) == [{"message": "derek scoped session"}]
    assert artifacts.get(ctx_ok, "result.txt") == b"derek artifact"
    assert artifacts.get(ctx_fail, "result.txt") is None
    assert memory.read(ctx_fail) == "alex scoped memory"

    audit = registry.get(BackendCapability.AUDIT, ctx_ok)
    ok_events = audit._events[
        (
            ctx_ok.mode,
            ctx_ok.org_id,
            ctx_ok.workspace_id,
            ctx_ok.user_id,
            ctx_ok.conversation_id,
            ctx_ok.agent_profile_id,
            ctx_ok.project_id,
            ctx_ok.run_id,
            ctx_ok.job_id,
        )
    ]
    fail_events = audit._events[
        (
            ctx_fail.mode,
            ctx_fail.org_id,
            ctx_fail.workspace_id,
            ctx_fail.user_id,
            ctx_fail.conversation_id,
            ctx_fail.agent_profile_id,
            ctx_fail.project_id,
            ctx_fail.run_id,
            ctx_fail.job_id,
        )
    ]
    assert [event["status"] for event in ok_events] == ["started", "succeeded"]
    assert [event["status"] for event in fail_events] == ["started", "failed"]


def test_local_supervisor_isolates_locks_queue_idempotency_and_audit_error_secrets():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-3", max_concurrent_runs=2, registry=registry)
    ctx_a = _context("run-lock-a", "derek")
    ctx_b = _context("run-lock-b", "alex")
    lease = registry.get(BackendCapability.RUN_LEASE, ctx_a)
    queue = registry.get(BackendCapability.QUEUE, ctx_a)

    def claim_lock_and_queue(context: RuntimeContext, owner: str):
        assert lease.claim(context, "shared-lock-name", owner=owner) is True
        first = queue.enqueue(context, {"owner": owner}, idempotency_key="same-idempotency-key")
        second = queue.enqueue(context, {"owner": "duplicate"}, idempotency_key="same-idempotency-key")
        if owner == "alex":
            raise RuntimeError("token=sentinel-secret should-not-leak")
        return (first, second)

    result_a = supervisor.submit(ctx_a, lambda: claim_lock_and_queue(ctx_a, "derek")).result(timeout=2)
    result_b = supervisor.submit(ctx_b, lambda: claim_lock_and_queue(ctx_b, "alex")).result(timeout=2)

    assert result_a.status is RunStatus.SUCCEEDED
    assert result_a.value == ("same-idempotency-key", "same-idempotency-key")
    assert result_b.status is RunStatus.FAILED
    assert result_b.error is not None
    assert "sentinel-secret" in result_b.error
    assert lease.claim(ctx_a, "shared-lock-name", owner="other") is False
    assert lease.claim(ctx_b, "shared-lock-name", owner="other") is False
    assert queue.claim(ctx_a)["payload"] == {"owner": "derek"}
    payload_a = {"owner": "derek", "nested": {"value": "original"}}
    queue.enqueue(ctx_a, payload_a, idempotency_key="nested-payload")
    payload_a["nested"]["value"] = "mutated after enqueue"
    claimed_a = queue.claim(ctx_a)
    assert queue.claim(ctx_a) is None
    assert claimed_a["payload"] == {"owner": "derek", "nested": {"value": "original"}}

    assert queue.claim(ctx_b)["payload"] == {"owner": "alex"}
    assert queue.claim(ctx_b) is None

    audit = registry.get(BackendCapability.AUDIT, ctx_b)
    fail_events = audit._events[
        (
            ctx_b.mode,
            ctx_b.org_id,
            ctx_b.workspace_id,
            ctx_b.user_id,
            ctx_b.conversation_id,
            ctx_b.agent_profile_id,
            ctx_b.project_id,
            ctx_b.run_id,
            ctx_b.job_id,
        )
    ]
    failed_event = [event for event in fail_events if event["status"] == "failed"][0]
    assert "sentinel-secret" not in failed_event["error"]
    assert "token=[REDACTED]" in failed_event["error"]


def test_local_backend_reads_do_not_expose_mutable_internal_state():
    registry = RuntimeBackendRegistry()
    context = _context("run-copy", "derek")
    sessions = registry.get(BackendCapability.SESSION, context)
    cron = registry.get(BackendCapability.CRON, context)

    sessions.append(context, {"message": "original", "nested": {"value": "original"}})
    session_events = sessions.read(context)
    session_events[0]["message"] = "mutated outside lock"
    session_events[0]["nested"]["value"] = "nested mutation outside lock"
    assert sessions.read(context) == [{"message": "original", "nested": {"value": "original"}}]

    job_id = cron.create(context, {"name": "original", "nested": {"value": "original"}})
    jobs = cron.list_jobs(context)
    jobs[0]["name"] = "mutated outside lock"
    jobs[0]["nested"]["value"] = "nested mutation outside lock"
    assert cron.list_jobs(context) == [{"name": "original", "nested": {"value": "original"}, "paused": False}]
    assert cron.run_history(context, job_id) == []


def test_local_supervisor_preserves_existing_inline_local_behavior_by_default():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(registry=registry)
    context = RuntimeContext()

    result = supervisor.run_sync(context, lambda: get_current_runtime_context())

    assert result.status is RunStatus.SUCCEEDED
    assert result.context == context
    assert result.value == context
    assert supervisor.max_concurrent_runs == 1
