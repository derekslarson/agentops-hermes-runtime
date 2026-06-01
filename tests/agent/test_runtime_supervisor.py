"""Tests for the M3 local multi-run supervisor baseline."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Mapping

from agent.runtime_backends import BackendCapability, LocalAuditBackend, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, get_current_runtime_context
from agent.runtime_sessions import LocalSQLiteSessionBackend
from agent.runtime_supervisor import LocalRunSupervisor, RunResult, RunStatus


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
    assert [event["status"] for event in ok_events if "status" in event] == ["started", "succeeded"]
    assert [event["status"] for event in fail_events if "status" in event] == ["started", "failed"]


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
    failed_event = [event for event in fail_events if event.get("status") == "failed"][0]
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
    fresh = cron.list_jobs(context)
    assert fresh[0]["name"] == "original"
    assert fresh[0]["nested"] == {"value": "original"}
    assert cron.run_history(context, job_id) == []


class _FakeRemoteSessionBackend:
    def __init__(self) -> None:
        self.created: list[tuple[RuntimeContext | None, str | None]] = []
        self.messages: dict[tuple[str | None, str | None, str | None, str | None, str | None], list[dict[str, Any]]] = {}
        self.children: dict[str, str] = {}
        self.locks: dict[tuple[str | None, str | None, str | None, str | None, str | None], tuple[str, float]] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _scope(context: RuntimeContext | None) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        if context is None:
            return (None, None, None, None, None)
        return (context.org_id, context.workspace_id, context.user_id, context.project_id, context.conversation_id)

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
        resolved = session_id or (context.conversation_id if context else "local") or "local"
        with self.lock:
            self.created.append((context, resolved))
            self.messages.setdefault(self._scope(context), [])
            if parent_session_id:
                self.children[parent_session_id] = resolved
        return resolved

    def append_message(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
        with self.lock:
            self.messages.setdefault(self._scope(context), []).append(copy.deepcopy(dict(message)))

    def read_messages(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.lock:
            messages = copy.deepcopy(self.messages.get(self._scope(context), []))
        if limit is None:
            return messages
        if limit <= 0:
            return []
        return messages[-limit:]

    def resolve_resume_session_id(self, session_id: str) -> str:
        with self.lock:
            return self.children.get(session_id, session_id)

    def claim_turn_lock(self, context: RuntimeContext | None, *, owner: str, ttl_seconds: float = 300.0) -> bool:
        now = time.time()
        with self.lock:
            current = self.locks.get(self._scope(context))
            if current is not None and current[1] >= now and current[0] != owner:
                return False
            self.locks[self._scope(context)] = (owner, now + ttl_seconds)
            return True

    def renew_turn_lock(self, context: RuntimeContext | None, *, owner: str, ttl_seconds: float = 300.0) -> bool:
        now = time.time()
        with self.lock:
            current = self.locks.get(self._scope(context))
            if current is None or current[0] != owner or current[1] < now:
                return False
            self.locks[self._scope(context)] = (owner, now + ttl_seconds)
            return True

    def release_turn_lock(self, context: RuntimeContext | None, *, owner: str) -> None:
        with self.lock:
            current = self.locks.get(self._scope(context))
            if current is not None and current[0] == owner:
                self.locks.pop(self._scope(context), None)

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None:
        self.append_message(context, event)

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.read_messages(context, limit=limit)

    def search(self, context: RuntimeContext | None, query: str) -> list[dict[str, Any]]:
        return [message for message in self.read_messages(context) if query in repr(message)]


def _remote_context(run_id: str, conversation_id: str = "thread-remote") -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="org-remote",
        workspace_id="workspace-remote",
        user_id="derek",
        conversation_id=conversation_id,
        agent_profile_id="default",
        project_id="runtime",
        run_id=run_id,
        run_type="conversation",
        backend_profile="compose-self-hosted",
    )


def test_fake_remote_session_backend_contract_supports_append_read_search_resume_lineage_through_registry():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    parent = _remote_context("run-parent", conversation_id="parent-thread")
    child = _remote_context("run-child", conversation_id="child-thread")
    sessions = registry.get(BackendCapability.SESSION, parent)

    parent_id = sessions.create_session(parent)
    child_id = sessions.create_session(child, parent_session_id=parent_id)
    sessions.append_message(child, {"role": "user", "content": "remote resume sentinel"})
    sessions.append(child, {"role": "tool", "content": "remote tool sentinel"})

    assert sessions.resolve_resume_session_id(parent_id) == child_id
    assert [message["content"] for message in sessions.read_messages(child)] == [
        "remote resume sentinel",
        "remote tool sentinel",
    ]
    assert [message["content"] for message in sessions.search(child, "tool sentinel")] == ["remote tool sentinel"]
    assert sessions.search(parent, "remote resume sentinel") == []


def test_supervisor_process_turn_strips_payload_session_id_before_writing_sqlite_backend(tmp_path):
    registry = RuntimeBackendRegistry()
    sqlite_backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    registry.register(BackendCapability.SESSION, lambda options: sqlite_backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-sqlite", registry=registry)
    context = _remote_context("run-sqlite", conversation_id="canonical-thread")

    result = supervisor.process_turn(
        context,
        {"session_id": "attacker-thread", "role": "user", "content": "canonical payload sentinel"},
        lambda transcript: {"session_id": "attacker-thread", "role": "assistant", "content": f"saw {len(transcript)}"},
    )

    assert result.status is RunStatus.SUCCEEDED
    assert [message["content"] for message in sqlite_backend.read_messages(context)] == [
        "canonical payload sentinel",
        "saw 1",
    ]
    assert sqlite_backend.read_messages(context, session_id="attacker-thread") == []


def test_supervisor_process_turn_writes_transcript_to_registry_selected_session_backend():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-remote-a", registry=registry)
    context = _remote_context("run-remote-a")

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "remote user turn sentinel"},
        lambda transcript: {"role": "assistant", "content": f"saw {len(transcript)} message"},
    )

    assert result.status is RunStatus.SUCCEEDED
    assert [message["content"] for message in backend.read_messages(context)] == [
        "remote user turn sentinel",
        "saw 1 message",
    ]
    assert backend.created == [(context, context.conversation_id)]
    assert backend.locks == {}


def test_supervisor_process_turn_can_resume_remote_conversation_on_different_worker():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    first_worker = LocalRunSupervisor(worker_id="worker-one", registry=registry)
    second_worker = LocalRunSupervisor(worker_id="worker-two", registry=registry)
    first_context = _remote_context("run-first", conversation_id="thread-survives-restart")
    second_context = _remote_context("run-second", conversation_id="thread-survives-restart")
    observed_transcripts: list[list[str]] = []

    first_worker.process_turn(
        first_context,
        {"role": "user", "content": "first worker persisted this"},
        lambda transcript: {"role": "assistant", "content": "first response"},
    )

    result = second_worker.process_turn(
        second_context,
        {"role": "user", "content": "second worker resumes"},
        lambda transcript: observed_transcripts.append([message["content"] for message in transcript])
        or {"role": "assistant", "content": "second response"},
    )

    assert result.status is RunStatus.SUCCEEDED
    assert observed_transcripts == [["first worker persisted this", "first response", "second worker resumes"]]
    assert [message["content"] for message in backend.read_messages(second_context)] == [
        "first worker persisted this",
        "first response",
        "second worker resumes",
        "second response",
    ]


def test_supervisor_process_turn_without_registry_fails_cleanly_without_mutation():
    supervisor = LocalRunSupervisor(worker_id="worker-no-registry")
    context = _remote_context("run-no-registry")
    handler_calls: list[Any] = []

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "should not persist"},
        lambda transcript: handler_calls.append(transcript) or {"role": "assistant", "content": "nope"},
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert handler_calls == []


def test_supervisor_process_turn_fails_when_turn_lock_held_by_other_worker():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-late", registry=registry)
    context = _remote_context("run-contended")
    assert backend.claim_turn_lock(context, owner="worker-incumbent") is True
    handler_calls: list[Any] = []

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "should not persist"},
        lambda transcript: handler_calls.append(transcript) or {"role": "assistant", "content": "nope"},
    )

    assert result.status is RunStatus.FAILED
    assert handler_calls == []
    assert backend.read_messages(context) == []
    assert backend.created == []
    assert backend.claim_turn_lock(context, owner="worker-incumbent") is True


def test_supervisor_process_turn_blocks_concurrent_same_worker_turn_until_lock_released():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-shared", registry=registry)
    context = _remote_context("run-concurrent")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()
    first_result: list[RunResult] = []

    def first_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=2)
        return {"role": "assistant", "content": "first done"}

    thread = threading.Thread(
        target=lambda: first_result.append(
            supervisor.process_turn(context, {"role": "user", "content": "first inbound"}, first_handler)
        )
    )
    thread.start()
    assert first_handler_entered.wait(timeout=2)

    second_handler_calls: list[Any] = []
    second_result = supervisor.process_turn(
        context,
        {"role": "user", "content": "second should wait or fail"},
        lambda transcript: second_handler_calls.append(transcript) or {"role": "assistant", "content": "second done"},
    )
    release_first_handler.set()
    thread.join(timeout=2)

    assert second_result.status is RunStatus.FAILED
    assert second_handler_calls == []
    assert first_result[0].status is RunStatus.SUCCEEDED
    assert [message["content"] for message in backend.read_messages(context)] == ["first inbound", "first done"]


def test_supervisor_warm_conversation_reuses_active_run_until_idle_timeout():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-warm", max_concurrent_runs=2, registry=registry)
    first_context = _remote_context("run-warm-first", conversation_id="thread-warm")
    second_context = _remote_context("run-warm-second", conversation_id="thread-warm")
    third_context = _remote_context("run-warm-third", conversation_id="thread-warm")
    observed_run_ids: list[str | None] = []

    def handler(transcript):
        current = get_current_runtime_context()
        observed_run_ids.append(current.run_id if current else None)
        return {"role": "assistant", "content": f"reply {len(transcript)}"}

    first = supervisor.submit_warm_turn(
        first_context,
        {"role": "user", "content": "first inbound"},
        handler,
        idle_timeout_seconds=10.0,
        now=100.0,
    )
    second = supervisor.submit_warm_turn(
        second_context,
        {"role": "user", "content": "second inbound before idle"},
        handler,
        idle_timeout_seconds=10.0,
        now=105.0,
    )
    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second.result(timeout=2).status is RunStatus.SUCCEEDED
    third = supervisor.submit_warm_turn(
        third_context,
        {"role": "user", "content": "third inbound after idle"},
        handler,
        idle_timeout_seconds=10.0,
        now=116.0,
    )

    assert third.result(timeout=2).status is RunStatus.SUCCEEDED
    assert observed_run_ids == ["run-warm-first", "run-warm-first", "run-warm-third"]
    assert [message["content"] for message in backend.read_messages(third_context)] == [
        "first inbound",
        "reply 1",
        "second inbound before idle",
        "reply 3",
        "third inbound after idle",
        "reply 5",
    ]


def test_supervisor_warm_conversation_does_not_expire_while_turn_is_active():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-warm-active", max_concurrent_runs=2, registry=registry)
    first_context = _remote_context("run-warm-active-first", conversation_id="thread-warm-active")
    second_context = _remote_context("run-warm-active-second", conversation_id="thread-warm-active")
    handler_started = threading.Event()
    release_handler = threading.Event()
    observed_run_ids: list[str | None] = []

    def handler(transcript):
        current = get_current_runtime_context()
        observed_run_ids.append(current.run_id if current else None)
        handler_started.set()
        release_handler.wait(timeout=2)
        return {"role": "assistant", "content": "ok"}

    first = supervisor.submit_warm_turn(
        first_context,
        {"role": "user", "content": "first slow turn"},
        handler,
        idle_timeout_seconds=10.0,
        now=100.0,
    )
    assert handler_started.wait(timeout=2)
    assert supervisor.reap_idle_conversations(now=116.0) == 0
    second = supervisor.submit_warm_turn(
        second_context,
        {"role": "user", "content": "follow-up while first still active"},
        handler,
        idle_timeout_seconds=10.0,
        now=116.0,
    )

    release_handler.set()
    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second.result(timeout=2).status is RunStatus.SUCCEEDED
    assert observed_run_ids == ["run-warm-active-first", "run-warm-active-first"]


def test_supervisor_warm_conversation_starts_fresh_run_when_permissions_change():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-warm-permissions", max_concurrent_runs=2, registry=registry)
    first_context = RuntimeContext(**{**_remote_context("run-warm-broad").to_dict(), "permissions_ref": "broad"})
    second_context = RuntimeContext(**{**_remote_context("run-warm-narrow").to_dict(), "permissions_ref": "narrow"})
    observed: list[tuple[str | None, str | None]] = []

    def handler(transcript):
        current = get_current_runtime_context()
        observed.append((current.run_id if current else None, current.permissions_ref if current else None))
        return {"role": "assistant", "content": "ok"}

    first = supervisor.submit_warm_turn(
        first_context,
        {"role": "user", "content": "first"},
        handler,
        idle_timeout_seconds=10.0,
        now=100.0,
    )
    second = supervisor.submit_warm_turn(
        second_context,
        {"role": "user", "content": "second with narrower permission"},
        handler,
        idle_timeout_seconds=10.0,
        now=105.0,
    )

    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second.result(timeout=2).status is RunStatus.SUCCEEDED
    assert observed == [("run-warm-broad", "broad"), ("run-warm-narrow", "narrow")]


def test_supervisor_warm_conversation_expires_at_idle_timeout_boundary():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-warm-boundary", max_concurrent_runs=2, registry=registry)
    first_context = _remote_context("run-warm-boundary-first", conversation_id="thread-warm-boundary")
    second_context = _remote_context("run-warm-boundary-second", conversation_id="thread-warm-boundary")
    observed_run_ids: list[str | None] = []

    def handler(transcript):
        current = get_current_runtime_context()
        observed_run_ids.append(current.run_id if current else None)
        return {"role": "assistant", "content": "ok"}

    first = supervisor.submit_warm_turn(
        first_context,
        {"role": "user", "content": "first"},
        handler,
        idle_timeout_seconds=10.0,
        now=100.0,
    )
    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert supervisor.reap_idle_conversations(now=110.0) == 1
    second = supervisor.submit_warm_turn(
        second_context,
        {"role": "user", "content": "second at boundary"},
        handler,
        idle_timeout_seconds=10.0,
        now=110.0,
    )

    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second.result(timeout=2).status is RunStatus.SUCCEEDED
    assert observed_run_ids == ["run-warm-boundary-first", "run-warm-boundary-second"]


def test_supervisor_warm_turn_rejects_non_conversation_run_type():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-warm-event", max_concurrent_runs=2, registry=registry)
    context = RuntimeContext(**{**_remote_context("run-warm-event").to_dict(), "run_type": "event"})
    handler_called = False

    def handler(transcript):
        nonlocal handler_called
        handler_called = True
        return {"role": "assistant", "content": "must not run"}

    handle = supervisor.submit_warm_turn(
        context,
        {"role": "user", "content": "event should not be warm"},
        handler,
        idle_timeout_seconds=10.0,
        now=100.0,
    )

    result = handle.result(timeout=2)
    assert result.status is RunStatus.FAILED
    assert result.error == "warm turns require RuntimeContext.run_type='conversation'"
    assert handler_called is False
    assert supervisor.reap_idle_conversations(now=111.0) == 0


def test_supervisor_warm_turn_after_drain_does_not_record_active_conversation():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-warm-drain", max_concurrent_runs=2, registry=registry)
    context = _remote_context("run-warm-drain", conversation_id="thread-warm-drain")

    drain_result = supervisor.request_drain()
    handle = supervisor.submit_warm_turn(
        context,
        {"role": "user", "content": "ignored while draining"},
        lambda transcript: {"role": "assistant", "content": "should not run"},
        idle_timeout_seconds=10.0,
        now=100.0,
    )

    assert drain_result.status is RunStatus.SUCCEEDED
    result = handle.result(timeout=2)
    assert result.status is RunStatus.FAILED
    assert result.error == "worker is draining and cannot accept new runs"
    assert supervisor.reap_idle_conversations(now=111.0) == 0
    assert backend.read_messages(context) == []


def test_supervisor_submit_turn_queues_busy_same_conversation_in_order():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-turn-queue", max_concurrent_runs=2, registry=registry)
    context = _remote_context("run-queued", conversation_id="thread-queued")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()
    second_seen: list[list[str]] = []

    def first_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=2)
        return {"role": "assistant", "content": "first response"}

    first = supervisor.submit_turn(context, {"role": "user", "content": "first inbound"}, first_handler)
    assert first_handler_entered.wait(timeout=2)
    second = supervisor.submit_turn(
        context,
        {"role": "user", "content": "second inbound"},
        lambda transcript: second_seen.append([message["content"] for message in transcript])
        or {"role": "assistant", "content": "second response"},
    )

    time.sleep(0.02)
    assert not second.done()
    release_first_handler.set()

    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second.result(timeout=2).status is RunStatus.SUCCEEDED
    assert second_seen == [["first inbound", "first response", "second inbound"]]
    assert [message["content"] for message in backend.read_messages(context)] == [
        "first inbound",
        "first response",
        "second inbound",
        "second response",
    ]
    assert supervisor._turn_queues == {}


def test_supervisor_submit_turn_backlog_for_one_conversation_does_not_block_another_conversation():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-turn-fairness", max_concurrent_runs=2, registry=registry)
    busy_context = _remote_context("run-busy", conversation_id="thread-busy")
    other_context = _remote_context("run-other", conversation_id="thread-other")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()
    other_handler_called = threading.Event()

    def busy_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=5)
        return {"role": "assistant", "content": "busy response"}

    first = supervisor.submit_turn(busy_context, {"role": "user", "content": "busy first"}, busy_handler)
    assert first_handler_entered.wait(timeout=2)
    queued_same_conversation = supervisor.submit_turn(
        busy_context,
        {"role": "user", "content": "busy second"},
        lambda transcript: {"role": "assistant", "content": "busy second response"},
    )

    other = supervisor.submit_turn(
        other_context,
        {"role": "user", "content": "other inbound"},
        lambda transcript: other_handler_called.set() or {"role": "assistant", "content": "other response"},
    )

    assert other_handler_called.wait(timeout=0.2)
    assert other.result(timeout=2).status is RunStatus.SUCCEEDED
    assert not queued_same_conversation.done()

    release_first_handler.set()
    assert first.result(timeout=2).status is RunStatus.SUCCEEDED
    assert queued_same_conversation.result(timeout=2).status is RunStatus.SUCCEEDED
    assert supervisor._turn_queues == {}


def test_supervisor_drain_fails_queued_turns_without_starting_them():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-turn-drain", max_concurrent_runs=2, registry=registry)
    context = _remote_context("run-drain-queued", conversation_id="thread-drain-queued")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()
    queued_handler_called = threading.Event()

    def first_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=2)
        return {"role": "assistant", "content": "active response"}

    active = supervisor.submit_turn(context, {"role": "user", "content": "active inbound"}, first_handler)
    assert first_handler_entered.wait(timeout=2)
    queued = supervisor.submit_turn(
        context,
        {"role": "user", "content": "queued inbound"},
        lambda transcript: queued_handler_called.set() or {"role": "assistant", "content": "must not run"},
    )

    drain_result = supervisor.request_drain()
    release_first_handler.set()

    assert drain_result.status is RunStatus.SUCCEEDED
    assert active.result(timeout=2).status is RunStatus.SUCCEEDED
    queued_result = queued.result(timeout=2)
    assert queued_result.status is RunStatus.FAILED
    assert queued_result.error == "worker is draining and cannot accept queued turn"
    assert not queued_handler_called.is_set()
    assert [message["content"] for message in backend.read_messages(context)] == ["active inbound", "active response"]
    assert supervisor._turn_queues == {}


def test_supervisor_drain_cancels_queued_turn_head_that_has_not_started():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-drain-pending", max_concurrent_runs=1, registry=registry)
    context = _remote_context("run-drain-pending", conversation_id="thread-drain-pending")
    executor_busy = threading.Event()
    release_executor = threading.Event()
    queued_handler_called = threading.Event()

    busy = supervisor.submit(
        context,
        lambda: executor_busy.set() or release_executor.wait(timeout=2) or "released",
    )
    assert executor_busy.wait(timeout=2)
    queued = supervisor.submit_turn(
        context,
        {"role": "user", "content": "queued while executor saturated"},
        lambda transcript: queued_handler_called.set() or {"role": "assistant", "content": "must not run"},
    )

    drain_result = supervisor.request_drain()

    queued_result = queued.result(timeout=2)
    assert drain_result.status is RunStatus.SUCCEEDED
    assert queued_result.status is RunStatus.FAILED
    assert queued_result.error == "worker is draining and cannot accept queued turn"
    assert not queued_handler_called.is_set()
    assert backend.read_messages(context) == []
    assert supervisor._turn_queues == {}

    release_executor.set()
    assert busy.result(timeout=2).status is RunStatus.SUCCEEDED


def test_supervisor_shutdown_fails_queued_turns_without_creating_replacement_executor():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-turn-shutdown", max_concurrent_runs=2, registry=registry)
    context = _remote_context("run-shutdown-queued", conversation_id="thread-shutdown-queued")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()
    queued_handler_called = threading.Event()

    def first_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=2)
        return {"role": "assistant", "content": "active response"}

    active = supervisor.submit_turn(context, {"role": "user", "content": "active inbound"}, first_handler)
    assert first_handler_entered.wait(timeout=2)
    queued = supervisor.submit_turn(
        context,
        {"role": "user", "content": "queued inbound"},
        lambda transcript: queued_handler_called.set() or {"role": "assistant", "content": "must not run"},
    )

    release_first_handler.set()
    supervisor.shutdown(wait=True)

    assert active.result(timeout=2).status is RunStatus.SUCCEEDED
    queued_result = queued.result(timeout=2)
    assert queued_result.status is RunStatus.FAILED
    assert queued_result.error == "worker is shutting down and cannot accept queued turn"
    assert not queued_handler_called.is_set()
    assert supervisor._executor is None
    assert supervisor._turn_queues == {}


def test_supervisor_shutdown_is_terminal_for_new_run_and_turn_paths():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-terminal-shutdown", registry=registry)
    context = _remote_context("run-after-shutdown")

    supervisor.shutdown(wait=True)

    submitted = supervisor.submit(context, lambda: "must not run").result(timeout=2)
    inline = supervisor.run_sync(context, lambda: "must not run")
    turn = supervisor.process_turn(
        context,
        {"role": "user", "content": "must not persist"},
        lambda transcript: {"role": "assistant", "content": "must not run"},
    )

    assert submitted.status is RunStatus.FAILED
    assert inline.status is RunStatus.FAILED
    assert turn.status is RunStatus.FAILED
    assert submitted.error == "worker is shutting down and cannot accept new runs"
    assert inline.error == "worker is shutting down and cannot accept new runs"
    assert turn.error == "worker is shutting down and cannot accept new runs"


def test_supervisor_shutdown_cancels_queued_turn_head_that_has_not_started():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-cancel-pending", max_concurrent_runs=1, registry=registry)
    context = _remote_context("run-pending-head", conversation_id="thread-pending-head")
    executor_busy = threading.Event()
    release_executor = threading.Event()
    queued_handler_called = threading.Event()

    busy = supervisor.submit(
        context,
        lambda: executor_busy.set() or release_executor.wait(timeout=2) or "released",
    )
    assert executor_busy.wait(timeout=2)
    queued = supervisor.submit_turn(
        context,
        {"role": "user", "content": "queued while executor saturated"},
        lambda transcript: queued_handler_called.set() or {"role": "assistant", "content": "must not run"},
    )

    supervisor.shutdown(wait=False)

    queued_result = queued.result(timeout=2)
    assert queued_result.status is RunStatus.FAILED
    assert queued_result.error == "worker is shutting down and cannot accept queued turn"
    assert not queued_handler_called.is_set()
    assert backend.read_messages(context) == []
    assert supervisor._turn_queues == {}

    release_executor.set()
    assert busy.result(timeout=2).status is RunStatus.SUCCEEDED


def test_supervisor_submit_turn_applies_per_conversation_backpressure():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(
        worker_id="worker-turn-backpressure",
        max_concurrent_runs=2,
        registry=registry,
        max_queued_turns_per_conversation=1,
    )
    context = _remote_context("run-backpressure", conversation_id="thread-backpressure")
    first_handler_entered = threading.Event()
    release_first_handler = threading.Event()

    def first_handler(transcript):
        first_handler_entered.set()
        release_first_handler.wait(timeout=2)
        return {"role": "assistant", "content": "active response"}

    active = supervisor.submit_turn(context, {"role": "user", "content": "active inbound"}, first_handler)
    assert first_handler_entered.wait(timeout=2)
    rejected = supervisor.submit_turn(
        context,
        {"role": "user", "content": "overflow inbound"},
        lambda transcript: {"role": "assistant", "content": "must not run"},
    )

    rejected_result = rejected.result(timeout=2)
    assert rejected_result.status is RunStatus.FAILED
    assert rejected_result.error == "conversation turn queue is full"

    release_first_handler.set()
    assert active.result(timeout=2).status is RunStatus.SUCCEEDED
    assert supervisor._turn_queues == {}


def test_supervisor_process_turn_releases_lock_and_preserves_raw_error_when_handler_raises():
    backend = _FakeRemoteSessionBackend()
    audit = LocalAuditBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    registry.register(BackendCapability.AUDIT, lambda options: audit, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-raise", registry=registry)
    context = _remote_context("run-raise")

    def boom(transcript):
        raise RuntimeError("token=sentinel-secret boom")

    result = supervisor.process_turn(context, {"role": "user", "content": "inbound"}, boom)

    assert result.status is RunStatus.FAILED
    assert "sentinel-secret" in result.error
    assert backend.locks == {}
    assert [message["content"] for message in backend.read_messages(context)] == ["inbound"]

    fail_events = audit._events[
        (
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
    ]
    failed_event = [event for event in fail_events if event.get("status") == "failed"][0]
    assert "sentinel-secret" not in failed_event["error"]
    assert "token=[REDACTED]" in failed_event["error"]


def test_supervisor_process_turn_rejects_agentops_conversation_without_conversation_id():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-missing-conversation", registry=registry)
    context = RuntimeContext(
        mode="agentops",
        org_id="org-remote",
        workspace_id="workspace-remote",
        user_id="derek",
        conversation_id=None,
        agent_profile_id="default",
        project_id="runtime",
        run_id="run-missing-conversation",
        run_type="conversation",
        backend_profile="local",
    )
    handler_calls: list[Any] = []

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "should not persist"},
        lambda transcript: handler_calls.append(transcript) or {"role": "assistant", "content": "nope"},
    )

    assert result.status is RunStatus.FAILED
    assert result.error == "agentops conversation turns require RuntimeContext.conversation_id"
    assert handler_calls == []


def test_supervisor_process_turn_default_session_backend_resumes_same_conversation_across_run_ids():
    registry = RuntimeBackendRegistry()
    first_worker = LocalRunSupervisor(worker_id="worker-default-one", registry=registry)
    second_worker = LocalRunSupervisor(worker_id="worker-default-two", registry=registry)
    first_context = _remote_context("run-default-first", conversation_id="thread-default-backend")
    second_context = _remote_context("run-default-second", conversation_id="thread-default-backend")
    first_context = RuntimeContext(**{**first_context.to_dict(), "backend_profile": "local"})
    second_context = RuntimeContext(**{**second_context.to_dict(), "backend_profile": "local"})
    observed_transcripts: list[list[str]] = []

    first_result = first_worker.process_turn(
        first_context,
        {"role": "user", "content": "default backend first turn"},
        lambda transcript: {"role": "assistant", "content": "default backend first response"},
    )
    second_result = second_worker.process_turn(
        second_context,
        {"role": "user", "content": "default backend second turn"},
        lambda transcript: observed_transcripts.append([message["content"] for message in transcript])
        or {"role": "assistant", "content": "default backend second response"},
    )

    assert first_result.status is RunStatus.SUCCEEDED
    assert second_result.status is RunStatus.SUCCEEDED
    assert observed_transcripts == [
        ["default backend first turn", "default backend first response", "default backend second turn"]
    ]


def test_supervisor_process_turn_fails_before_outbound_write_when_turn_lock_expires_mid_handler():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-expiring", registry=registry)
    context = _remote_context("run-expiring")

    def slow_handler(transcript):
        time.sleep(0.02)
        return {"role": "assistant", "content": "stale response must not append"}

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "inbound before expiry"},
        slow_handler,
        lock_ttl_seconds=0.001,
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "lock expired or was lost" in result.error
    assert [message["content"] for message in backend.read_messages(context)] == ["inbound before expiry"]
    assert backend.locks == {}


def test_supervisor_process_turn_appends_iterable_of_outbound_messages():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-iter", registry=registry)
    context = _remote_context("run-iter")

    result = supervisor.process_turn(
        context,
        {"role": "user", "content": "inbound"},
        lambda transcript: [
            {"role": "assistant", "content": "tool call"},
            {"role": "tool", "content": "tool result"},
        ],
    )

    assert result.status is RunStatus.SUCCEEDED
    assert [message["content"] for message in backend.read_messages(context)] == [
        "inbound",
        "tool call",
        "tool result",
    ]
    assert backend.locks == {}


def test_supervisor_process_turn_with_none_handler_result_appends_only_inbound():
    backend = _FakeRemoteSessionBackend()
    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.SESSION, lambda options: backend, profile="compose-self-hosted")
    supervisor = LocalRunSupervisor(worker_id="worker-none", registry=registry)
    context = _remote_context("run-none")

    result = supervisor.process_turn(context, {"role": "user", "content": "inbound"}, lambda transcript: None)

    assert result.status is RunStatus.SUCCEEDED
    assert [message["content"] for message in backend.read_messages(context)] == ["inbound"]
    assert backend.locks == {}


def test_local_supervisor_drain_prevents_new_runs_and_finishes_active_runs():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(worker_id="worker-drain", max_concurrent_runs=1, registry=registry)
    context = _context("run-drain", "derek")
    entered = threading.Event()
    release = threading.Event()

    def run_until_released():
        entered.set()
        release.wait(timeout=2)
        return "finished-before-drain"

    handle = supervisor.submit(context, run_until_released)
    assert entered.wait(timeout=2)

    supervisor.request_drain()
    release.set()
    supervisor.shutdown(wait=True)

    result = handle.result(timeout=2)
    assert result.status is RunStatus.SUCCEEDED
    assert result.value == "finished-before-drain"
    assert supervisor.draining is True
    worker_registry = registry.get(BackendCapability.WORKER_REGISTRY, None)
    assert worker_registry.list_workers(None)[0]["draining"] is True

    blocked = supervisor.submit(context, lambda: "must not start")
    blocked_result = blocked.result(timeout=2)
    assert blocked_result.status is RunStatus.FAILED
    assert blocked_result.error == "worker is draining and cannot accept new runs"


def test_local_supervisor_registers_capacity_capabilities_and_idle_slots():
    registry = RuntimeBackendRegistry()
    LocalRunSupervisor(
        worker_id="worker-capacity",
        max_concurrent_runs=3,
        registry=registry,
        capabilities=("conversation", "cron"),
    )

    workers = registry.get(BackendCapability.WORKER_REGISTRY, None).list_workers(None)
    assert workers == [
        {
            "id": "worker-capacity",
            "capacity": 3,
            "max_concurrent_runs": 3,
            "capabilities": ["conversation", "cron"],
            "draining": False,
            "slots": {"active": 0, "available": 3},
            "last_heartbeat": workers[0]["last_heartbeat"],
            "expires_at": workers[0]["expires_at"],
        }
    ]


def test_local_supervisor_request_drain_fails_closed_when_registry_rejects_drain():
    class FailingWorkerRegistry:
        def register(self, context, worker, *, now=None, ttl_seconds=None):
            return str(worker["id"])

        def heartbeat(self, context, worker_id, *, slots, now=None, ttl_seconds=None):
            return None

        def mark_draining(self, context, worker_id):
            raise RuntimeError("registry unavailable")

        def recover_expired(self, context, *, now=None):
            return []

        def list_workers(self, context):
            return []

    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.WORKER_REGISTRY, lambda options: FailingWorkerRegistry(), profile="local")
    supervisor = LocalRunSupervisor(worker_id="worker-fail-drain", registry=registry)

    result = supervisor.request_drain()

    assert result.status is RunStatus.FAILED
    assert result.error == "RuntimeError: registry unavailable"
    assert supervisor.draining is True

    blocked = supervisor.submit(_context("run-after-failed-drain", "derek"), lambda: "must not start")
    blocked_result = blocked.result(timeout=2)
    assert blocked_result.status is RunStatus.FAILED
    assert blocked_result.error == "worker is draining and cannot accept new runs"


def test_local_supervisor_preserves_existing_inline_local_behavior_by_default():
    registry = RuntimeBackendRegistry()
    supervisor = LocalRunSupervisor(registry=registry)
    context = RuntimeContext()

    result = supervisor.run_sync(context, lambda: get_current_runtime_context())

    assert result.status is RunStatus.SUCCEEDED
    assert result.context == context
    assert result.value == context
    assert supervisor.max_concurrent_runs == 1
