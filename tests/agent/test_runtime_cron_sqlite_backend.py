"""Durable SQLite cron backend tests for M8.

These tests cover the first local durable/control-plane-like CronBackend: state
survives backend instance restarts, RuntimeContext scopes remain isolated, and
worker-safe leases prevent duplicate execution across scheduler/worker processes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.runtime_backends import CronLeaseError
from agent.runtime_context import RuntimeContext
from agent.runtime_cron_sqlite import SQLiteCronBackend


def _ctx(**overrides) -> RuntimeContext:
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
        delivery_ref="delivery:telegram:home",
        permissions_ref="secret:perm-ref-should-not-leak",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def _backend(path: Path) -> SQLiteCronBackend:
    return SQLiteCronBackend(path)


def test_jobs_history_and_leases_survive_backend_restart(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    first = _backend(db_path)
    job_id = first.create(context, {"prompt": "durable", "schedule": "30m", "next_run_at": 100.0})

    claimed = first.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0)
    assert [job["id"] for job in claimed] == [job_id]

    second = _backend(db_path)
    assert second.get_job(context, job_id)["prompt"] == "durable"
    assert second.claim_due(context, owner="worker-b", now=160.0, lease_seconds=60.0) == []

    reclaimed = second.claim_due(context, owner="worker-b", now=220.0, lease_seconds=60.0)
    assert [job["id"] for job in reclaimed] == [job_id]
    entry = second.complete_run(context, job_id, owner="worker-b", output="done", now=230.0, next_run_at=300.0)
    assert entry["delivery"] == "delivered"

    third = _backend(db_path)
    assert third.run_history(context, job_id) == [entry]
    assert third.get_job(context, job_id)["next_run_at"] == 300.0


def test_runtime_context_scopes_are_isolated_and_binding_omits_secret_refs(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    backend = _backend(db_path)
    derek = _ctx(user_id="derek", conversation_id="thread-derek")
    alex = _ctx(user_id="alex", conversation_id="thread-alex")

    derek_id = backend.create(derek, {"prompt": "derek", "schedule": "30m"})
    backend.create(alex, {"prompt": "alex", "schedule": "30m"})

    restarted = _backend(db_path)
    assert [job["prompt"] for job in restarted.list_jobs(derek)] == ["derek"]
    assert [job["prompt"] for job in restarted.list_jobs(alex)] == ["alex"]
    binding = restarted.get_job(derek, derek_id)["binding"]
    assert binding["delivery_ref"] == "delivery:telegram:home"
    assert "permissions_ref" not in binding
    assert "perm-ref-should-not-leak" not in repr(restarted.list_jobs(derek))


def test_secret_bearing_job_payload_and_update_fields_are_redacted(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    job_id = backend.create(
        context,
        {
            "prompt": "safe",
            "schedule": "30m",
            "permissions_ref": "secret:perm-payload-should-not-leak",
            "nested": {"api_token": "token-payload-should-not-leak"},
        },
    )

    backend.update(
        context,
        job_id,
        {
            "binding": {"permissions_ref": "secret:update-binding-should-not-leak"},
            "credential_ref": "credential-update-should-not-leak",
        },
    )

    restarted = _backend(db_path)
    stored = repr(restarted.get_job(context, job_id))
    assert "perm-payload-should-not-leak" not in stored
    assert "token-payload-should-not-leak" not in stored
    assert "update-binding-should-not-leak" not in stored
    assert "credential-update-should-not-leak" not in stored
    assert "permissions_ref" not in restarted.get_job(context, job_id)["binding"]


def test_cron_scope_survives_new_run_and_job_ids_for_same_conversation(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    created_under_run = _ctx(run_id="run-a", job_id="scheduler-a")
    resumed_under_run = _ctx(run_id="run-b", job_id="scheduler-b")
    backend = _backend(db_path)
    job_id = backend.create(created_under_run, {"prompt": "durable conversation job", "schedule": "30m", "next_run_at": 100.0})

    restarted = _backend(db_path)
    assert restarted.get_job(resumed_under_run, job_id)["prompt"] == "durable conversation job"
    assert [job["id"] for job in restarted.claim_due(resumed_under_run, owner="worker-b", now=150.0)] == [job_id]


def test_paused_and_one_shot_state_survive_restart(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    paused_id = backend.create(context, {"prompt": "paused", "schedule": "30m", "next_run_at": 100.0})
    backend.pause(context, paused_id)
    one_shot_id = backend.create(context, {"prompt": "once", "schedule": "once", "next_run_at": 100.0, "repeat": 1})
    backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0)
    backend.complete_run(context, one_shot_id, owner="worker-a", output="done", now=160.0)

    restarted = _backend(db_path)
    assert restarted.get_job(context, paused_id)["state"] == "paused"
    assert restarted.get_job(context, one_shot_id)["state"] == "completed"
    assert restarted.claim_due(context, owner="worker-b", now=220.0) == []


def test_silent_and_delivery_error_history_survives_restart(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    silent_id = backend.create(context, {"prompt": "silent", "schedule": "30m", "next_run_at": 100.0})
    error_id = backend.create(context, {"prompt": "delivery error", "schedule": "30m", "next_run_at": 100.0})
    backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0, limit=2)
    silent_entry = backend.complete_run(context, silent_id, owner="worker-a", output="   ", now=160.0, next_run_at=300.0)
    error_entry = backend.complete_run(
        context,
        error_id,
        owner="worker-a",
        output="hello",
        delivery_error="route failed",
        now=161.0,
        next_run_at=300.0,
    )

    restarted = _backend(db_path)
    assert restarted.run_history(context, silent_id) == [silent_entry]
    assert restarted.run_history(context, error_id) == [error_entry]
    assert silent_entry["delivery"] == "skipped_silent"
    assert error_entry["delivery"] == "error"


def test_secret_looking_errors_are_redacted_before_persistence(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    delivery_id = backend.create(context, {"prompt": "delivery", "schedule": "30m", "next_run_at": 100.0})
    failure_id = backend.create(context, {"prompt": "failure", "schedule": "30m", "next_run_at": 100.0})
    malformed_id = backend.create(context, {"prompt": "bad schedule", "schedule": "30m", "next_run_at": "token-secret-should-not-leak"})
    backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0, limit=2)

    backend.complete_run(
        context,
        delivery_id,
        owner="worker-a",
        output="hello",
        delivery_error="failed with token=delivery-secret-should-not-leak",
        now=160.0,
        next_run_at=300.0,
    )
    backend.fail_run(
        context,
        failure_id,
        owner="worker-a",
        error="api_key=failure-secret-should-not-leak",
        now=161.0,
        next_run_at=300.0,
    )
    backend.claim_due(context, owner="worker-b", now=170.0)

    restarted = _backend(db_path)
    persisted = repr(restarted.list_jobs(context)) + repr(restarted.run_history(context, delivery_id)) + repr(restarted.run_history(context, failure_id))
    assert "delivery-secret-should-not-leak" not in persisted
    assert "failure-secret-should-not-leak" not in persisted
    assert "token-secret-should-not-leak" not in persisted
    assert restarted.get_job(context, malformed_id)["next_run_at_error"] == "[REDACTED]"


def test_malformed_next_run_at_fails_closed_across_restart(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    malformed_id = backend.create(context, {"prompt": "broken", "schedule": "30m", "next_run_at": "not-a-date"})
    healthy_id = backend.create(context, {"prompt": "healthy", "schedule": "30m", "next_run_at": 100.0})

    assert [job["id"] for job in backend.claim_due(context, owner="worker-a", now=150.0)] == [healthy_id]

    restarted = _backend(db_path)
    malformed = restarted.get_job(context, malformed_id)
    assert malformed["state"] == "needs_schedule"
    assert "next_run_at_error" in malformed
    reclaimed_ids = [job["id"] for job in restarted.claim_due(context, owner="worker-b", now=220.0)]
    assert reclaimed_ids == [healthy_id]
    assert malformed_id not in reclaimed_ids


def test_expired_lease_cannot_complete_after_restart(tmp_path: Path):
    db_path = tmp_path / "cron.sqlite3"
    context = _ctx()
    backend = _backend(db_path)
    job_id = backend.create(context, {"prompt": "p", "schedule": "30m", "next_run_at": 100.0})
    backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0)

    restarted = _backend(db_path)
    with pytest.raises(CronLeaseError):
        restarted.complete_run(context, job_id, owner="worker-a", output="too late", now=211.0)
