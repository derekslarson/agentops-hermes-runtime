from __future__ import annotations

import pytest

from agent.runtime_context import RuntimeContext


@pytest.fixture
def slack_event() -> dict:
    return {
        "team": "T_acme",
        "channel": "C_support",
        "user": "U_derek",
        "ts": "1710000000.000100",
        "thread_ts": "1710000000.000000",
    }


def test_slack_event_maps_workspace_thread_user_to_agentops_runtime_context(slack_event):
    from agentops_runtime.slack_runtime import build_slack_runtime_context

    context = build_slack_runtime_context(
        slack_event,
        config={
            "agentops": {
                "enabled": True,
                "org_id": "org_acme",
                "project_id": "proj_support",
                "agent_profile_id": "support-bot",
                "backend_profile": "compose-self-hosted",
                "permissions_ref": "perm_slack_support",
            }
        },
    )

    assert isinstance(context, RuntimeContext)
    assert context.mode == "agentops"
    assert context.workspace_type == "slack"
    assert context.org_id == "org_acme"
    assert context.workspace_id == "T_acme"
    assert context.user_id == "U_derek"
    assert context.external_channel_id == "C_support"
    assert context.external_thread_id == "1710000000.000000"
    assert context.conversation_id == "slack:T_acme:C_support:1710000000.000000"
    assert context.project_id == "proj_support"
    assert context.agent_profile_id == "support-bot"
    assert context.backend_profile == "compose-self-hosted"
    assert context.permissions_ref == "perm_slack_support"
    assert context.delivery_ref == "slack:T_acme:C_support:1710000000.000000"
    assert context.metadata["slack_message_ts"] == "1710000000.000100"


def test_slack_runtime_context_uses_message_ts_as_root_thread_and_keeps_users_separate(slack_event):
    from agentops_runtime.slack_runtime import build_slack_runtime_context

    root_event = dict(slack_event)
    root_event.pop("thread_ts")

    derek = build_slack_runtime_context(root_event, config={"agentops": {"enabled": True}})
    alex = build_slack_runtime_context({**root_event, "user": "U_alex"}, config={"agentops": {"enabled": True}})

    assert derek.external_thread_id == "1710000000.000100"
    assert derek.conversation_id == "slack:T_acme:C_support:1710000000.000100"
    assert derek.user_id == "U_derek"
    assert alex.conversation_id == derek.conversation_id
    assert alex.user_id == "U_alex"
    assert derek.to_dict() != alex.to_dict()


def test_slack_runtime_context_is_disabled_unless_agentops_enabled(slack_event):
    from agentops_runtime.slack_runtime import build_slack_runtime_context

    assert build_slack_runtime_context(slack_event, config={}) is None
    assert build_slack_runtime_context(slack_event, config={"agentops": {"enabled": False}}) is None


def test_slack_runtime_context_rejects_missing_required_scope(slack_event):
    from agentops_runtime.slack_runtime import build_slack_runtime_context

    broken = dict(slack_event)
    broken.pop("team")

    with pytest.raises(ValueError, match="team"):
        build_slack_runtime_context(broken, config={"agentops": {"enabled": True}})


def test_slack_turn_routing_reuses_active_run_for_followups_in_same_thread(slack_event):
    from agent.runtime_backends import BackendCapability, LocalConversationRouter, RuntimeBackendRegistry
    from agentops_runtime.slack_runtime import build_slack_runtime_context, route_slack_turn

    registry = RuntimeBackendRegistry()
    registry.register(
        BackendCapability.CONVERSATION_ROUTER,
        lambda options: LocalConversationRouter(),
        profile="compose-self-hosted",
    )
    context = build_slack_runtime_context(slack_event, config={"agentops": {"enabled": True}})

    first = route_slack_turn(slack_event, context=context, text="first", registry=registry)
    followup = route_slack_turn(
        {**slack_event, "ts": "1710000000.000200"},
        context=context,
        text="followup",
        registry=registry,
    )

    assert first.conversation_id == "slack:T_acme:C_support:1710000000.000000"
    assert first.run_id == followup.run_id
    assert first.routed_to_active_run is False
    assert followup.routed_to_active_run is True
    assert followup.turn["text"] == "followup"
    assert followup.turn["delivery_ref"] == context.delivery_ref


def test_slack_delivery_backend_targets_context_thread_without_leaking_other_threads(slack_event):
    from agentops_runtime.slack_runtime import SlackDeliveryBackend, build_slack_runtime_context

    deliveries = []

    def send_like_native_slack_adapter(channel, text, reply_to=None, metadata=None):
        deliveries.append((channel, text, reply_to, metadata))

    backend = SlackDeliveryBackend(send_like_native_slack_adapter)
    thread_context = build_slack_runtime_context(slack_event, config={"agentops": {"enabled": True}})
    other_context = build_slack_runtime_context(
        {**slack_event, "ts": "1710000001.000000", "thread_ts": "1710000001.000000"},
        config={"agentops": {"enabled": True}},
    )

    backend.deliver(thread_context, {"text": "thread reply"})
    backend.deliver(other_context, {"text": "other reply"})

    assert deliveries == [
        ("C_support", "thread reply", None, {"thread_id": "1710000000.000000", "thread_ts": "1710000000.000000"}),
        ("C_support", "other reply", None, {"thread_id": "1710000001.000000", "thread_ts": "1710000001.000000"}),
    ]


def test_slack_delivery_backend_executes_async_native_sender(slack_event):
    from agentops_runtime.slack_runtime import SlackDeliveryBackend, build_slack_runtime_context

    deliveries = []

    async def async_send(channel, text, reply_to=None, metadata=None):
        deliveries.append((channel, text, reply_to, metadata))

    backend = SlackDeliveryBackend(async_send)
    context = build_slack_runtime_context(slack_event, config={"agentops": {"enabled": True}})

    backend.deliver(context, {"text": "async reply"})

    assert deliveries == [
        ("C_support", "async reply", None, {"thread_id": "1710000000.000000", "thread_ts": "1710000000.000000"})
    ]


def test_slack_turn_routing_default_registry_handles_default_compose_profile(slack_event):
    from agentops_runtime.slack_runtime import build_slack_runtime_context, route_slack_turn

    context = build_slack_runtime_context(slack_event, config={"agentops": {"enabled": True}})

    first = route_slack_turn(slack_event, context=context, text="first")
    followup = route_slack_turn({**slack_event, "ts": "1710000000.000200"}, context=context, text="followup")

    assert first.run_id == followup.run_id
    assert followup.routed_to_active_run is True
