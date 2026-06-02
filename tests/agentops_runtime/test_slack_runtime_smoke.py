"""M13 Slack multi-user/thread end-to-end runtime smoke tests.

These exercise the full hermetic Slack ingress path: a Slack event maps to a
RuntimeContext, the turn routes through the selected ConversationRouter, the
scoped turn is processed by a LocalRunSupervisor worker against the shared
Compose contract backends, and the reply is delivered back to the correct Slack
thread metadata. No real Slack SDK, credentials, or network are used.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.runtime_backends import BackendCapability
from agent.runtime_context import get_current_runtime_context
from agent.runtime_supervisor import LocalRunSupervisor, RunStatus
from agentops_runtime.compose_smoke import create_compose_smoke_registry
from agentops_runtime.slack_runtime import (
    SlackDeliveryBackend,
    build_slack_runtime_context,
    run_slack_turn,
)

_SHARED_AGENTOPS = {
    "enabled": True,
    "org_id": "org_acme",
    "project_id": "proj_support",
    "agent_profile_id": "support-bot",
    "backend_profile": "compose-self-hosted",
}


def _slack_event(*, user: str, thread_ts: str, ts: str) -> dict:
    return {
        "team": "T_acme",
        "channel": "C_support",
        "user": user,
        "ts": ts,
        "thread_ts": thread_ts,
    }


def _context(event: Mapping[str, Any]):
    return build_slack_runtime_context(event, config={"agentops": _SHARED_AGENTOPS})


def _recording_delivery() -> tuple[SlackDeliveryBackend, list[tuple[Any, ...]]]:
    sent: list[tuple[Any, ...]] = []

    def send_like_native_slack_adapter(channel, text, reply_to=None, metadata=None):
        sent.append((channel, text, reply_to, metadata))

    return SlackDeliveryBackend(send_like_native_slack_adapter), sent


def _seed_shared_project_skill(registry, context) -> None:
    skill_backend = registry.get(BackendCapability.SKILL, context)
    skill_backend.manage_skill(
        context,
        action="create",
        name="acme-style",
        scope="project",
        content="Use Acme support style.",
        allow_shared_write=True,
    )


def _make_handler(registry, context, *, preference: str, reply: str):
    """Build a turn handler that writes native memory and loads the shared skill."""

    def handler(transcript: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        memory = registry.get(BackendCapability.MEMORY, context)
        memory.write(context, preference)
        loaded = registry.get(BackendCapability.SKILL, context).load_skill(context, "acme-style")
        return {"role": "assistant", "content": reply, "skill": loaded.get("content")}

    return handler


def test_two_slack_users_in_different_threads_get_isolated_memory_and_sessions_with_shared_skill():
    registry = create_compose_smoke_registry()
    worker = LocalRunSupervisor(worker_id="slack-worker", max_concurrent_runs=2, registry=registry)

    derek_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000100")
    alex_event = _slack_event(user="U_alex", thread_ts="1710000500.000000", ts="1710000500.000100")
    derek = _context(derek_event)
    alex = _context(alex_event)
    _seed_shared_project_skill(registry, derek)

    derek_delivery, derek_sent = _recording_delivery()
    alex_delivery, alex_sent = _recording_delivery()

    derek_result = run_slack_turn(
        derek_event,
        context=derek,
        text="Derek likes concise updates",
        handler=_make_handler(registry, derek, preference="Derek likes concise updates", reply="On it, Derek."),
        supervisor=worker,
        registry=registry,
        delivery=derek_delivery,
    )
    alex_result = run_slack_turn(
        alex_event,
        context=alex,
        text="Alex prefers detailed updates",
        handler=_make_handler(registry, alex, preference="Alex prefers detailed updates", reply="Sure, Alex."),
        supervisor=worker,
        registry=registry,
        delivery=alex_delivery,
    )

    assert derek_result.run.status is RunStatus.SUCCEEDED
    assert alex_result.run.status is RunStatus.SUCCEEDED

    # Isolated native per-user/thread memory.
    assert registry.get(BackendCapability.MEMORY, derek).read(derek) == "Derek likes concise updates"
    assert registry.get(BackendCapability.MEMORY, alex).read(alex) == "Alex prefers detailed updates"

    # Isolated native session transcripts.
    derek_transcript = registry.get(BackendCapability.SESSION, derek).read(derek)
    alex_transcript = registry.get(BackendCapability.SESSION, alex).read(alex)
    assert [m["content"] for m in derek_transcript] == ["Derek likes concise updates", "On it, Derek."]
    assert [m["content"] for m in alex_transcript] == ["Alex prefers detailed updates", "Sure, Alex."]

    # Shared project skill loads in both threads.
    assert derek_result.run.value[0]["skill"] == "Use Acme support style."
    assert alex_result.run.value[0]["skill"] == "Use Acme support style."

    # Replies delivered to the correct Slack thread metadata, never the other thread.
    assert derek_sent == [
        ("C_support", "On it, Derek.", None, {"thread_id": "1710000000.000000", "thread_ts": "1710000000.000000"})
    ]
    assert alex_sent == [
        ("C_support", "Sure, Alex.", None, {"thread_id": "1710000500.000000", "thread_ts": "1710000500.000000"})
    ]
    worker.shutdown()


def test_followup_slack_message_in_same_thread_routes_to_same_warm_run():
    registry = create_compose_smoke_registry()
    worker = LocalRunSupervisor(worker_id="slack-worker", max_concurrent_runs=2, registry=registry)

    first_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000100")
    context = _context(first_event)
    _seed_shared_project_skill(registry, context)
    delivery, sent = _recording_delivery()

    first = run_slack_turn(
        first_event,
        context=context,
        text="first",
        handler=_make_handler(registry, context, preference="first", reply="ack first"),
        supervisor=worker,
        registry=registry,
        delivery=delivery,
    )
    followup_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000200")
    followup = run_slack_turn(
        followup_event,
        context=context,
        text="second",
        handler=_make_handler(registry, context, preference="second", reply="ack second"),
        supervisor=worker,
        registry=registry,
        delivery=delivery,
    )

    assert first.route.routed_to_active_run is False
    assert followup.route.routed_to_active_run is True
    assert first.route.run_id == followup.route.run_id

    # Both turns delivered to the same Slack thread, in order.
    assert [text for _, text, _, _ in sent] == ["ack first", "ack second"]
    assert {meta["thread_ts"] for _, _, _, meta in sent} == {"1710000000.000000"}
    worker.shutdown()


def test_run_slack_turn_executes_turns_under_the_selected_warm_run_context():
    registry = create_compose_smoke_registry()
    worker = LocalRunSupervisor(worker_id="slack-worker", max_concurrent_runs=2, registry=registry)

    first_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000100")
    context = _context(first_event)
    _seed_shared_project_skill(registry, context)
    delivery, _sent = _recording_delivery()

    observed_run_ids: list[str | None] = []

    def observing_handler(reply: str):
        def handler(transcript: list[Mapping[str, Any]]) -> Mapping[str, Any]:
            ambient = get_current_runtime_context()
            observed_run_ids.append(ambient.run_id if ambient is not None else None)
            return {"role": "assistant", "content": reply}

        return handler

    first = run_slack_turn(
        first_event,
        context=context,
        text="first",
        handler=observing_handler("ack first"),
        supervisor=worker,
        registry=registry,
        delivery=delivery,
    )
    followup_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000200")
    followup = run_slack_turn(
        followup_event,
        context=context,
        text="second",
        handler=observing_handler("ack second"),
        supervisor=worker,
        registry=registry,
        delivery=delivery,
    )

    assert followup.route.routed_to_active_run is True
    assert first.route.run_id == followup.route.run_id

    # The turns must actually execute under the routed warm-run context, not
    # merely match router bookkeeping: the ambient RuntimeContext observed by the
    # handler carries the selected warm run_id for both the first turn and the
    # follow-up routed to that same warm run.
    assert observed_run_ids[0] is not None
    assert observed_run_ids[0] == first.route.run_id
    assert observed_run_ids[1] == followup.route.run_id
    assert observed_run_ids[0] == observed_run_ids[1]
    worker.shutdown()


def test_worker_restart_between_slack_turns_resumes_remote_session_state():
    registry = create_compose_smoke_registry()
    event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000100")
    context = _context(event)
    _seed_shared_project_skill(registry, context)
    delivery, sent = _recording_delivery()

    first_worker = LocalRunSupervisor(worker_id="slack-worker-first", max_concurrent_runs=1, registry=registry)
    first = run_slack_turn(
        event,
        context=context,
        text="hello",
        handler=_make_handler(registry, context, preference="hello", reply="hi there"),
        supervisor=first_worker,
        registry=registry,
        delivery=delivery,
    )
    assert first.run.status is RunStatus.SUCCEEDED
    first_worker.shutdown()

    seen_transcripts: list[list[str]] = []

    def resume_handler(transcript: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        seen_transcripts.append([m["content"] for m in transcript])
        return {"role": "assistant", "content": "welcome back"}

    restarted_worker = LocalRunSupervisor(worker_id="slack-worker-restarted", max_concurrent_runs=1, registry=registry)
    followup_event = _slack_event(user="U_derek", thread_ts="1710000000.000000", ts="1710000000.000200")
    second = run_slack_turn(
        followup_event,
        context=context,
        text="still there?",
        handler=resume_handler,
        supervisor=restarted_worker,
        registry=registry,
        delivery=delivery,
    )

    assert second.run.status is RunStatus.SUCCEEDED
    # The restarted worker saw the durable transcript from the first worker.
    assert seen_transcripts == [["hello", "hi there", "still there?"]]
    assert [m["content"] for m in registry.get(BackendCapability.SESSION, context).read(context)] == [
        "hello",
        "hi there",
        "still there?",
        "welcome back",
    ]
    restarted_worker.shutdown()
