"""Slack-to-RuntimeContext mapping for AgentOps gateway runs."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.runtime_context import RuntimeContext
from agent.runtime_backends import BackendCapability, LocalConversationRouter, RuntimeBackendRegistry

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


_DEFAULT_ROUTE_REGISTRY_LOCK = threading.RLock()
_DEFAULT_ROUTE_REGISTRY = RuntimeBackendRegistry()
_DEFAULT_ROUTE_PROFILES: set[str] = set()


def _default_route_registry_for(context: RuntimeContext) -> RuntimeBackendRegistry:
    """Return a process-local router registry for direct helper use.

    Production control-plane/gateway paths should inject their configured
    registry. The default keeps this helper usable in local tests and small
    compose-profile smoke paths without creating a new empty router on every
    turn.
    """

    profile = context.backend_profile or "local"
    with _DEFAULT_ROUTE_REGISTRY_LOCK:
        if profile != "local" and profile not in _DEFAULT_ROUTE_PROFILES:
            _DEFAULT_ROUTE_REGISTRY.register(
                BackendCapability.CONVERSATION_ROUTER,
                lambda options: LocalConversationRouter(),
                profile=profile,
            )
            _DEFAULT_ROUTE_PROFILES.add(profile)
    return _DEFAULT_ROUTE_REGISTRY


@dataclass(frozen=True, slots=True)
class SlackTurnRoute:
    """Conversation-router decision for an incoming Slack turn."""

    conversation_id: str
    run_id: str
    routed_to_active_run: bool
    turn: Mapping[str, Any]


class SlackDeliveryBackend:
    """Delivery backend that maps RuntimeContext scope back to Slack threads.

    The callable is injected by the gateway/control plane so this backend stays
    free of Slack SDK imports and secrets. It receives the channel id, text, and
    metadata shaped for the native Slack adapter's ``send(..., metadata=...)``
    path.
    """

    def __init__(self, sender: Callable[..., Any]) -> None:
        self._sender = sender

    def deliver(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> None:
        if context is None or context.workspace_type != "slack":
            raise ValueError("Slack delivery requires a Slack RuntimeContext")
        channel_id = _clean(context.external_channel_id)
        thread_ts = _clean(context.external_thread_id)
        if not channel_id or not thread_ts:
            raise ValueError("Slack delivery requires channel and thread identifiers")
        text = _clean(message.get("text")) or _clean(message.get("content")) or ""
        metadata = {"thread_id": thread_ts, "thread_ts": thread_ts}
        result = self._sender(channel_id, text, metadata=metadata)
        if inspect.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)


def route_slack_turn(
    event: Mapping[str, Any],
    *,
    context: RuntimeContext,
    text: str,
    registry: RuntimeBackendRegistry | None = None,
) -> SlackTurnRoute:
    """Route an incoming Slack turn through the runtime conversation router.

    This is the AgentOps ingress seam for warm Slack conversations: the selected
    ``ConversationRouter`` resolves the Slack conversation, reports whether a
    warm run already owns it, and routes the turn to that run or creates a new
    run according to the backend contract.
    """

    if context is None:
        raise ValueError("Slack turn routing requires a RuntimeContext")
    backend_registry = registry or _default_route_registry_for(context)
    router = backend_registry.get(BackendCapability.CONVERSATION_ROUTER, context)
    conversation_id = router.resolve_conversation(context, event)
    active_run = router.find_active_run(context, conversation_id)
    turn = {
        "platform": "slack",
        "text": text,
        "user_id": context.user_id,
        "channel_id": context.external_channel_id,
        "thread_ts": context.external_thread_id,
        "message_ts": _slack_value(event, "ts", "trigger_id"),
        "delivery_ref": context.delivery_ref,
    }
    run_id = router.route_turn(context, conversation_id, turn)
    return SlackTurnRoute(
        conversation_id=conversation_id,
        run_id=run_id,
        routed_to_active_run=active_run is not None and active_run == run_id,
        turn=turn,
    )


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
