"""Slack-to-RuntimeContext mapping for AgentOps gateway runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.runtime_context import RuntimeContext

_REQUIRED_EVENT_KEYS = ("team", "channel", "user", "ts")
_SLACK_ALIASES = {
    "team": ("team", "team_id"),
    "channel": ("channel", "channel_id"),
    "user": ("user", "user_id"),
    # Slack message events include ``ts``; slash commands do not, but they do
    # include a unique ``trigger_id`` that is suitable as the one-shot command
    # conversation identifier.
    "ts": ("ts", "trigger_id"),
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _agentops_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    agentops = config.get("agentops")
    return agentops if isinstance(agentops, Mapping) else {}


def _is_enabled(agentops: Mapping[str, Any]) -> bool:
    return agentops.get("enabled") is True or agentops.get("mode") == "agentops"


def _slack_value(event: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _clean(event.get(key))
        if value:
            return value
    return None


def build_slack_runtime_context(
    event: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> RuntimeContext | None:
    """Build an AgentOps RuntimeContext from a Slack event/command payload.

    Returns ``None`` unless ``config.agentops.enabled`` is true (or
    ``config.agentops.mode == "agentops"``), preserving ordinary local gateway
    behavior. Slack's workspace/team, channel, thread, and user identifiers are
    mapped directly to RuntimeContext scope fields so native Hermes backends can
    isolate per-user memory while sharing the same conversation/thread scope.
    """

    if not isinstance(event, Mapping):
        raise TypeError("Slack event payload must be a mapping")

    agentops = _agentops_config(config)
    if not _is_enabled(agentops):
        return None

    values = {key: _slack_value(event, *_SLACK_ALIASES[key]) for key in _REQUIRED_EVENT_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(f"Slack RuntimeContext requires {', '.join(missing)}")

    team_id = values["team"]
    channel_id = values["channel"]
    user_id = values["user"]
    message_ts = values["ts"]
    thread_ts = _slack_value(event, "thread_ts") or message_ts
    conversation_id = f"slack:{team_id}:{channel_id}:{thread_ts}"
    delivery_ref = _clean(agentops.get("delivery_ref")) or conversation_id

    return RuntimeContext(
        mode="agentops",
        org_id=_clean(agentops.get("org_id")) or team_id,
        workspace_id=team_id,
        workspace_type="slack",
        user_id=user_id,
        conversation_id=conversation_id,
        external_channel_id=channel_id,
        external_thread_id=thread_ts,
        agent_profile_id=_clean(agentops.get("agent_profile_id")) or _clean(agentops.get("profile_id")) or "default",
        project_id=_clean(agentops.get("project_id")),
        run_type="conversation",
        permissions_ref=_clean(agentops.get("permissions_ref")),
        backend_profile=_clean(agentops.get("backend_profile")) or "compose-self-hosted",
        delivery_ref=delivery_ref,
        metadata={
            "slack_team_id": team_id,
            "slack_channel_id": channel_id,
            "slack_thread_ts": thread_ts,
            "slack_message_ts": message_ts,
            "slack_is_root_message": thread_ts == message_ts,
        },
    )
