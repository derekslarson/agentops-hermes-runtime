"""Process-level holder for the active cron backend (M8 routing seam).

The native ``cronjob`` tool keeps its existing local ``cron.jobs`` behavior by
default. When a run is scoped to an AgentOps profile and a scoped
:class:`~agent.runtime_backends.CronBackend` has been bound for that context,
the tool routes its native job-management operations through that backend
instead. This module is the binding point, mirroring the equivalent skill
backend holder so the selection is keyed to the active RuntimeContext's
tenant/run identity and never leaks across concurrent runs.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.runtime_backends import CronBackend
    from agent.runtime_context import RuntimeContext

_active_lock = threading.RLock()
_active_backend_default: ContextVar["CronBackend | None"] = ContextVar(
    "hermes_active_cron_backend",
    default=None,
)
_active_backends_by_context: dict[tuple[Any, ...], "CronBackend | None"] = {}


def _active_context_key(context: "RuntimeContext | None") -> tuple[Any, ...] | None:
    if context is None:
        return None
    return (
        getattr(context, "mode", None),
        getattr(context, "org_id", None),
        getattr(context, "workspace_id", None),
        getattr(context, "project_id", None),
        getattr(context, "user_id", None),
        getattr(context, "conversation_id", None),
        getattr(context, "agent_profile_id", None),
        getattr(context, "run_id", None),
        getattr(context, "job_id", None),
        getattr(context, "backend_profile", None),
    )


def set_active_cron_backend(
    backend: "CronBackend | None",
    context: "RuntimeContext | None" = None,
) -> None:
    """Bind the cron backend native surfaces route through in AgentOps mode.

    When a RuntimeContext is supplied (or currently active), the binding is keyed
    to that context's tenant/run identity. A no-context binding is the
    compatibility fallback for local tests and single-context callers, but
    context-specific bindings always win and do not leak across concurrent runs.
    """

    if context is None:
        from agent.runtime_context import get_runtime_context_for_surface

        context = get_runtime_context_for_surface("cron")
    key = _active_context_key(context)
    if key is None:
        _active_backend_default.set(backend)
        return
    with _active_lock:
        _active_backends_by_context[key] = backend


def get_active_cron_backend(context: "RuntimeContext | None" = None) -> "CronBackend | None":
    if context is None:
        from agent.runtime_context import get_runtime_context_for_surface

        context = get_runtime_context_for_surface("cron")
    key = _active_context_key(context)
    if key is None:
        return _active_backend_default.get()
    with _active_lock:
        if key in _active_backends_by_context:
            return _active_backends_by_context[key]
    return _active_backend_default.get()


def clear_active_cron_backend() -> None:
    _active_backend_default.set(None)
    with _active_lock:
        _active_backends_by_context.clear()
