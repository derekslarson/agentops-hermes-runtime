"""M12 Compose self-hosted distributed semantics smoke tests."""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping

from agent.runtime_backends import BackendCapability
from agent.runtime_context import RuntimeContext
from agent.runtime_supervisor import LocalRunSupervisor, RunStatus
from agentops_runtime.compose_smoke import create_compose_smoke_registry


def _ctx(
    *,
    run_id: str,
    user_id: str,
    conversation_id: str,
    run_type: str = "conversation",
    job_id: str | None = None,
) -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="org-compose",
        workspace_id="workspace-compose",
        user_id=user_id,
        conversation_id=conversation_id,
        agent_profile_id="default",
        project_id="project-compose",
        run_id=run_id,
        run_type=run_type,
        job_id=job_id,
        backend_profile="compose-self-hosted",
        delivery_ref="thread://compose",
    )


def test_compose_smoke_registry_supports_horizontal_workers_with_multiple_slots():
    registry = create_compose_smoke_registry()
    worker_a = LocalRunSupervisor(worker_id="compose-worker-a", max_concurrent_runs=2, registry=registry)
    worker_b = LocalRunSupervisor(worker_id="compose-worker-b", max_concurrent_runs=2, registry=registry)
    contexts = [
        _ctx(run_id=f"run-{idx}", user_id=f"user-{idx}", conversation_id=f"thread-{idx}", run_type="manual")
        for idx in range(4)
    ]
    started = threading.Barrier(4, timeout=3)
    release = threading.Event()
    observed: list[str] = []
    observed_lock = threading.Lock()

    def run(context: RuntimeContext) -> str:
        started.wait()
        with observed_lock:
            observed.append(context.run_id or "")
        release.wait(timeout=3)
        return context.run_id or ""

    handles = [
        worker_a.submit(contexts[0], lambda context=contexts[0]: run(context)),
        worker_a.submit(contexts[1], lambda context=contexts[1]: run(context)),
        worker_b.submit(contexts[2], lambda context=contexts[2]: run(context)),
        worker_b.submit(contexts[3], lambda context=contexts[3]: run(context)),
    ]
    release.set()
    results = [handle.result(timeout=3) for handle in handles]

    assert {result.status for result in results} == {RunStatus.SUCCEEDED}
    assert {result.value for result in results} == {"run-0", "run-1", "run-2", "run-3"}
    assert set(observed) == {"run-0", "run-1", "run-2", "run-3"}
    worker_a.shutdown()
    worker_b.shutdown()


def test_compose_smoke_registry_isolates_user_thread_memory_sessions_and_shares_project_skills():
    registry = create_compose_smoke_registry()
    supervisor = LocalRunSupervisor(worker_id="compose-worker", max_concurrent_runs=2, registry=registry)
    derek = _ctx(run_id="run-derek", user_id="derek", conversation_id="thread-derek")
    alex = _ctx(run_id="run-alex", user_id="alex", conversation_id="thread-alex")
    skill_backend = registry.get(BackendCapability.SKILL, derek)
    skill_backend.manage_skill(
        derek,
        action="create",
        name="acme-style",
        scope="project",
        content="Use Acme engineering style.",
        allow_shared_write=True,
    )

    def turn(context: RuntimeContext, preference: str) -> Mapping[str, Any]:
        memory = registry.get(BackendCapability.MEMORY, context)
        sessions = registry.get(BackendCapability.SESSION, context)
        memory.write(context, preference)
        sessions.append(context, {"role": "user", "content": preference})
        loaded_skill = registry.get(BackendCapability.SKILL, context).load_skill(context, "acme-style")
        return {"memory": memory.read(context), "skill": loaded_skill.get("content")}

    derek_result = supervisor.submit(derek, lambda: turn(derek, "Derek likes concise updates")).result(timeout=3)
    alex_result = supervisor.submit(alex, lambda: turn(alex, "Alex prefers detailed updates")).result(timeout=3)

    assert derek_result.status is RunStatus.SUCCEEDED
    assert alex_result.status is RunStatus.SUCCEEDED
    assert derek_result.value == {"memory": "Derek likes concise updates", "skill": "Use Acme engineering style."}
    assert alex_result.value == {"memory": "Alex prefers detailed updates", "skill": "Use Acme engineering style."}
    assert registry.get(BackendCapability.MEMORY, derek).read(derek) == "Derek likes concise updates"
    assert registry.get(BackendCapability.MEMORY, alex).read(alex) == "Alex prefers detailed updates"
    assert registry.get(BackendCapability.SESSION, derek).read(derek) == [
        {"role": "user", "content": "Derek likes concise updates"}
    ]
    assert registry.get(BackendCapability.SESSION, alex).read(alex) == [
        {"role": "user", "content": "Alex prefers detailed updates"}
    ]
    supervisor.shutdown()


def test_compose_smoke_registry_prevents_duplicate_cron_claims_across_schedulers():
    registry = create_compose_smoke_registry()
    scheduler_a = LocalRunSupervisor(worker_id="compose-scheduler-a", max_concurrent_runs=2, registry=registry)
    scheduler_b = LocalRunSupervisor(worker_id="compose-scheduler-b", max_concurrent_runs=2, registry=registry)
    context = _ctx(run_id="cron-run", user_id="derek", conversation_id="thread-cron", run_type="cron")
    cron = registry.get(BackendCapability.CRON, context)
    long_job_id = cron.create(context, {"name": "long-builder", "next_run_at": 10.0, "repeat": 2})
    watchdog_id = cron.create(context, {"name": "watchdog", "next_run_at": 10.0, "repeat": 1})
    started = threading.Event()
    release = threading.Event()
    handled: list[str] = []
    handled_lock = threading.Lock()

    def handler(job: Mapping[str, Any]) -> str:
        with handled_lock:
            handled.append(str(job["name"]))
        if job["name"] == "long-builder":
            started.set()
            release.wait(timeout=3)
        return str(job["name"])

    stable_clock = lambda: 10.1
    long_thread = threading.Thread(
        target=lambda: scheduler_a.run_due_cron_once(
            context,
            handler,
            now=10.0,
            lease_seconds=60.0,
            limit=1,
            next_run_at=20.0,
            clock=stable_clock,
        ),
        daemon=True,
    )
    long_thread.start()
    assert started.wait(timeout=3)

    scheduler_b_results = scheduler_b.run_due_cron_once(
        context,
        handler,
        now=10.0,
        lease_seconds=60.0,
        limit=2,
        clock=stable_clock,
    )
    release.set()
    long_thread.join(timeout=3)

    assert [result.value for result in scheduler_b_results] == ["watchdog"]
    assert sorted(handled) == ["long-builder", "watchdog"]
    assert [entry["status"] for entry in cron.run_history(context, long_job_id)] == ["success"]
    assert [entry["status"] for entry in cron.run_history(context, watchdog_id)] == ["success"]
    scheduler_a.shutdown()
    scheduler_b.shutdown()


def test_compose_smoke_registry_resumes_conversation_state_after_worker_restart():
    registry = create_compose_smoke_registry()
    first_worker = LocalRunSupervisor(worker_id="compose-worker-first", max_concurrent_runs=1, registry=registry)
    first_context = _ctx(run_id="turn-1", user_id="derek", conversation_id="thread-resume")

    def first_turn(transcript: list[Mapping[str, Any]]) -> Mapping[str, str]:
        return {"role": "assistant", "content": f"saw {len(transcript)} message"}

    first = first_worker.process_turn(first_context, {"role": "user", "content": "hello"}, first_turn)
    assert first.status is RunStatus.SUCCEEDED
    first_worker.shutdown()

    restarted_worker = LocalRunSupervisor(worker_id="compose-worker-restarted", max_concurrent_runs=1, registry=registry)
    second_context = _ctx(run_id="turn-2", user_id="derek", conversation_id="thread-resume")

    def second_turn(transcript: list[Mapping[str, Any]]) -> Mapping[str, str]:
        assert [message["content"] for message in transcript] == ["hello", "saw 1 message", "again"]
        return {"role": "assistant", "content": f"saw {len(transcript)} messages"}

    second = restarted_worker.process_turn(second_context, {"role": "user", "content": "again"}, second_turn)

    assert second.status is RunStatus.SUCCEEDED
    assert registry.get(BackendCapability.SESSION, second_context).read(second_context) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "saw 1 message"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "saw 3 messages"},
    ]
    restarted_worker.shutdown()
