"""Runtime scoping primitives for AgentOps-capable Hermes runs.

The RuntimeContext is intentionally lightweight in M1: it records the scope
that future backend registries will use, while defaulting to local-compatible
values when AgentOps is not enabled.
"""

from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping

_RUNTIME_CONTEXT_ENV = "HERMES_RUNTIME_CONTEXT"
_VALID_MODES = {"local", "agentops"}
_VALID_RUN_TYPES = {"conversation", "event", "cron", "delegation", "manual"}

_CURRENT_RUNTIME_CONTEXT: ContextVar[RuntimeContext | None] = ContextVar(
    "hermes_runtime_context",
    default=None,
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _deepcopy_jsonish(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return copy.deepcopy(dict(value))


def _freeze_jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_jsonish(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_jsonish(item) for item in value)
    return copy.deepcopy(value)


def _thaw_jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_jsonish(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_jsonish(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Scope selector for memory, skills, sessions, credentials, and tools.

    All fields are optional except ``mode`` so local Hermes can keep its
    current behavior. Future AgentOps adapters can require stricter subsets at
    their boundaries without making local mode brittle.
    """

    mode: str = "local"
    org_id: str | None = None
    workspace_id: str | None = None
    workspace_type: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    external_channel_id: str | None = None
    external_thread_id: str | None = None
    agent_profile_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    run_type: str = "conversation"
    job_id: str | None = None
    parent_session_id: str | None = None
    permissions_ref: str | None = None
    backend_profile: str | None = None
    delivery_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = _clean(self.mode) or "local"
        if mode not in _VALID_MODES:
            raise ValueError(f"Unsupported RuntimeContext mode: {mode}")
        run_type = _clean(self.run_type) or "conversation"
        if run_type not in _VALID_RUN_TYPES:
            raise ValueError(f"Unsupported RuntimeContext run_type: {run_type}")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "run_type", run_type)
        for name in (
            "org_id",
            "workspace_id",
            "workspace_type",
            "user_id",
            "conversation_id",
            "external_channel_id",
            "external_thread_id",
            "agent_profile_id",
            "project_id",
            "run_id",
            "job_id",
            "parent_session_id",
            "permissions_ref",
            "backend_profile",
            "delivery_ref",
        ):
            object.__setattr__(self, name, _clean(getattr(self, name)))
        object.__setattr__(self, "metadata", _freeze_jsonish(_deepcopy_jsonish(self.metadata)))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "RuntimeContext":
        if payload is None:
            return cls()
        if isinstance(payload, RuntimeContext):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("RuntimeContext payload must be a mapping")
        fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
        values = {key: copy.deepcopy(value) for key, value in payload.items() if key in fields}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "org_id": self.org_id,
            "workspace_id": self.workspace_id,
            "workspace_type": self.workspace_type,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "external_channel_id": self.external_channel_id,
            "external_thread_id": self.external_thread_id,
            "agent_profile_id": self.agent_profile_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "run_type": self.run_type,
            "job_id": self.job_id,
            "parent_session_id": self.parent_session_id,
            "permissions_ref": self.permissions_ref,
            "backend_profile": self.backend_profile,
            "delivery_ref": self.delivery_ref,
            "metadata": _thaw_jsonish(self.metadata),
        }


def build_local_runtime_context(
    *,
    profile_id: str | None = None,
    session_id: str | None = None,
    platform: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    parent_session_id: str | None = None,
    run_type: str = "conversation",
    backend_profile: str | None = None,
) -> RuntimeContext:
    """Create the compatibility RuntimeContext from existing Hermes metadata."""

    workspace_type = _clean(platform) or os.environ.get("HERMES_SESSION_SOURCE") or "cli"
    return RuntimeContext(
        mode="local",
        workspace_id=_clean(chat_id),
        workspace_type=workspace_type,
        user_id=_clean(user_id),
        conversation_id=_clean(session_id) or _clean(thread_id) or _clean(chat_id),
        external_channel_id=_clean(chat_id),
        external_thread_id=_clean(thread_id),
        agent_profile_id=_clean(profile_id) or os.environ.get("HERMES_PROFILE") or "default",
        parent_session_id=_clean(parent_session_id),
        run_type=run_type,
        backend_profile=_clean(backend_profile) or "local",
    )


def _context_from_env() -> RuntimeContext | None:
    raw = os.environ.get(_RUNTIME_CONTEXT_ENV)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_RUNTIME_CONTEXT_ENV} must contain valid JSON") from exc
    return RuntimeContext.from_mapping(payload)


def _context_from_config(config: Mapping[str, Any] | None) -> RuntimeContext | None:
    if not isinstance(config, Mapping):
        return None
    agentops = config.get("agentops")
    if isinstance(agentops, Mapping) and "runtime_context" in agentops:
        runtime = agentops["runtime_context"]
        if not isinstance(runtime, Mapping):
            raise TypeError("agentops.runtime_context must be a mapping")
        return RuntimeContext.from_mapping(runtime)
    if "runtime_context" not in config:
        return None
    runtime = config.get("runtime_context")
    if not isinstance(runtime, Mapping):
        raise TypeError("runtime_context config must be a mapping")
    return RuntimeContext.from_mapping(runtime)


def _context_from_work_item(work_item: Mapping[str, Any] | None) -> RuntimeContext | None:
    if not isinstance(work_item, Mapping):
        return None
    key = "runtime_context" if "runtime_context" in work_item else "RuntimeContext" if "RuntimeContext" in work_item else None
    if key is None:
        return None
    runtime = work_item[key]
    if not isinstance(runtime, Mapping):
        raise TypeError("work item runtime_context must be a mapping")
    return RuntimeContext.from_mapping(runtime)


def resolve_runtime_context(
    *,
    explicit: RuntimeContext | Mapping[str, Any] | None = None,
    work_item: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    profile_id: str | None = None,
    session_id: str | None = None,
    platform: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    parent_session_id: str | None = None,
    run_type: str = "conversation",
    backend_profile: str | None = None,
) -> RuntimeContext:
    """Resolve RuntimeContext from explicit/work item/config/env/local inputs.

    Precedence is explicit > queued work item > config > env var > local
    compatibility context. This keeps queue payloads authoritative over process
    environment while still allowing local tests and CLI runs to default cleanly.
    """

    if explicit is not None:
        return RuntimeContext.from_mapping(explicit) if not isinstance(explicit, RuntimeContext) else explicit
    from_work = _context_from_work_item(work_item)
    if from_work is not None:
        return from_work
    from_config = _context_from_config(config)
    if from_config is not None:
        return from_config
    from_env = _context_from_env()
    if from_env is not None:
        return from_env
    return build_local_runtime_context(
        profile_id=profile_id,
        session_id=session_id,
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        parent_session_id=parent_session_id,
        run_type=run_type,
        backend_profile=backend_profile,
    )


def get_current_runtime_context() -> RuntimeContext | None:
    return _CURRENT_RUNTIME_CONTEXT.get()


def current_runtime_context() -> RuntimeContext | None:
    return get_current_runtime_context()


@contextmanager
def use_runtime_context(context: RuntimeContext | Mapping[str, Any] | None) -> Iterator[RuntimeContext | None]:
    ctx = RuntimeContext.from_mapping(context) if isinstance(context, Mapping) else context
    token = _CURRENT_RUNTIME_CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT_RUNTIME_CONTEXT.reset(token)
