"""Cron backend worker execution tests for M8.

These tests prove the backend contract can drive actual scheduler/worker execution
through the same logical job model: due claims, drain, recurring/one-shot state,
and delivery routing are handled outside the storage adapter so local, Compose,
and cloud backends can share the same worker path.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.runtime_backends import LocalCronBackend
from agent.runtime_context import RuntimeContext
from agent.runtime_cron_sqlite import SQLiteCronBackend
from agent.runtime_cron_worker import CronWorkerResult, run_due_cron_jobs


class RecordingDeliveryBackend:
    def __init__(self) -> None:
        self.messages: list[tuple[RuntimeContext | None, Mapping[str, Any]]] = []

    def deliver(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
        self.messages.append((context, dict(message)))


def _ctx(**overrides: Any) -> RuntimeContext:
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="agentops-runtime",
        run_type="cron",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery:slack:thread-1",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def test_worker_claims_due_job_executes_and_routes_delivery_with_context() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    delivery = RecordingDeliveryBackend()
    job_id = backend.create(
        context,
        {
            "prompt": "summarize",
            "schedule": "30m",
            "next_run_at": 100.0,
            "deliver": "origin",
            "next_run_at_after_run": 200.0,
        },
    )

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=lambda job: CronWorkerResult(output=f"done {job['prompt']}", next_run_at=job["next_run_at_after_run"]),
        delivery_backend=delivery,
        now=150.0,
        clock=lambda: 150.0,
    )

    assert [result.job_id for result in results] == [job_id]
    assert results[0].status == "success"
    assert delivery.messages == [
        (
            context,
            {
                "kind": "cron_result",
                "job_id": job_id,
                "delivery_ref": "delivery:slack:thread-1",
                "deliver": "origin",
                "content": "done summarize",
                "binding": backend.get_job(context, job_id)["binding"],
            },
        )
    ]
    assert backend.run_history(context, job_id)[0]["delivery"] == "delivered"
    assert backend.get_job(context, job_id)["state"] == "scheduled"
    assert backend.get_job(context, job_id)["next_run_at"] == 200.0


def test_worker_preserves_silent_one_shot_and_failure_semantics() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    delivery = RecordingDeliveryBackend()
    marker_id = backend.create(context, {"prompt": "marker", "schedule": "once", "next_run_at": 100.0, "repeat": 1})
    silent_id = backend.create(context, {"prompt": "silent", "schedule": "once", "next_run_at": 100.0, "repeat": 1})
    failing_id = backend.create(context, {"prompt": "fail", "schedule": "once", "next_run_at": 100.0, "repeat": 1})

    def runner(job: Mapping[str, Any]) -> str:
        if job["id"] == marker_id:
            return "[SILENT]"
        if job["id"] == silent_id:
            return "   "
        raise RuntimeError("api_key=cron-secret-should-not-leak")

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=runner,
        delivery_backend=delivery,
        now=150.0,
        clock=lambda: 150.0,
        limit=3,
    )

    assert {result.job_id: result.status for result in results} == {marker_id: "success", silent_id: "success", failing_id: "error"}
    assert delivery.messages == []
    assert backend.run_history(context, marker_id)[0]["delivery"] == "skipped_silent"
    assert backend.run_history(context, silent_id)[0]["delivery"] == "skipped_silent"
    failed_history = backend.run_history(context, failing_id)[0]
    assert failed_history["delivery"] == "error_alert"
    assert "cron-secret-should-not-leak" not in repr(failed_history)


def test_worker_drain_and_existing_lease_skip_new_execution() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    job_id = backend.create(context, {"prompt": "p", "schedule": "30m", "next_run_at": 100.0})
    calls: list[str] = []

    drained = run_due_cron_jobs(
        context,
        backend=backend,
        owner="draining-worker",
        runner=lambda job: calls.append(job["id"]) or "done",
        now=150.0,
        clock=lambda: 150.0,
        draining=True,
    )
    assert drained == []
    assert calls == []

    assert [job["id"] for job in backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0)] == [job_id]
    blocked = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-b",
        runner=lambda job: calls.append(job["id"]) or "done",
        now=160.0,
        clock=lambda: 160.0,
    )
    assert blocked == []
    assert calls == []

    reclaimed = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-b",
        runner=lambda job: calls.append(job["id"]) or CronWorkerResult(output="done", next_run_at=300.0),
        now=220.0,
        clock=lambda: 220.0,
    )
    assert [result.job_id for result in reclaimed] == [job_id]
    assert calls == [job_id]


def test_worker_execution_uses_durable_sqlite_backend_after_restart(tmp_path) -> None:
    context = _ctx()
    db_path = tmp_path / "cron.sqlite3"
    first = SQLiteCronBackend(db_path)
    job_id = first.create(
        context,
        {
            "prompt": "durable",
            "schedule": "30m",
            "next_run_at": 100.0,
            "deliver": "origin",
            "next_run_at_after_run": 300.0,
        },
    )

    restarted = SQLiteCronBackend(db_path)
    results = run_due_cron_jobs(
        context,
        backend=restarted,
        owner="worker-a",
        runner=lambda job: CronWorkerResult(output="durable output", next_run_at=job["next_run_at_after_run"]),
        now=150.0,
        clock=lambda: 150.0,
    )

    assert [result.job_id for result in results] == [job_id]
    final_backend = SQLiteCronBackend(db_path)
    assert final_backend.run_history(context, job_id)[0]["delivery"] == "delivered"
    assert final_backend.get_job(context, job_id)["next_run_at"] == 300.0


def test_worker_does_not_deliver_if_lease_is_released_before_delivery() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    delivery = RecordingDeliveryBackend()
    job_id = backend.create(context, {"prompt": "released", "schedule": "30m", "next_run_at": 100.0})

    def runner(job: Mapping[str, Any]) -> CronWorkerResult:
        backend.release_lease(context, job["id"], owner="worker-a")
        return CronWorkerResult(output="should not deliver", next_run_at=300.0)

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=runner,
        delivery_backend=delivery,
        now=150.0,
        clock=lambda: 160.0,
        lease_seconds=60.0,
    )

    assert [(result.job_id, result.status) for result in results] == [(job_id, "lease_lost")]
    assert delivery.messages == []
    assert backend.run_history(context, job_id) == []


def test_worker_does_not_complete_with_stale_pre_delivery_timestamp() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    job_id = backend.create(context, {"prompt": "slow delivery", "schedule": "30m", "next_run_at": 100.0})
    times = iter([160.0, 200.0, 230.0])

    class SlowDelivery(RecordingDeliveryBackend):
        def deliver(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
            super().deliver(context, message)
            next(times)

    slow_delivery = SlowDelivery()

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=lambda job: CronWorkerResult(output="delivered before stale complete", next_run_at=300.0),
        delivery_backend=slow_delivery,
        now=150.0,
        clock=lambda: next(times),
        lease_seconds=60.0,
    )

    assert [(result.job_id, result.status) for result in results] == [(job_id, "lease_lost")]
    assert len(slow_delivery.messages) == 1
    assert backend.run_history(context, job_id) == []


def test_worker_does_not_complete_or_deliver_after_lease_expires(monkeypatch) -> None:
    context = _ctx()
    backend = LocalCronBackend()
    delivery = RecordingDeliveryBackend()
    job_id = backend.create(context, {"prompt": "slow", "schedule": "30m", "next_run_at": 100.0})
    times = iter([150.0, 220.0])
    monkeypatch.setattr("agent.runtime_cron_worker.time.time", lambda: next(times))

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=lambda job: CronWorkerResult(output="too late", next_run_at=300.0),
        delivery_backend=delivery,
        lease_seconds=60.0,
    )

    assert [(result.job_id, result.status) for result in results] == [(job_id, "lease_lost")]
    assert delivery.messages == []
    assert backend.run_history(context, job_id) == []
    assert backend.claim_due(context, owner="worker-b", now=221.0)


def test_paused_jobs_are_not_executed() -> None:
    context = _ctx()
    backend = LocalCronBackend()
    job_id = backend.create(context, {"prompt": "paused", "schedule": "30m", "next_run_at": 100.0})
    backend.pause(context, job_id)

    results = run_due_cron_jobs(
        context,
        backend=backend,
        owner="worker-a",
        runner=lambda job: "should not run",
        now=150.0,
        clock=lambda: 150.0,
    )

    assert results == []
    assert backend.run_history(context, job_id) == []
