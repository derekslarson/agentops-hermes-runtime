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
