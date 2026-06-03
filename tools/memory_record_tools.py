"""Scoped native deep-memory record tools.

These implement the ``memory_record_search`` / ``memory_record_get`` /
``memory_record_get_many`` tool surface against the ``DEEP_MEMORY`` runtime
backend capability, scoped by the currently bound RuntimeContext. Because the
backend owns scoping, a record ingested under one tenant/user/thread can never
be searched or fetched (even by stable ID) from another scope.

For AgentOps/remote profiles with no registered deep-memory adapter, backend
resolution raises ``BackendSelectionError`` and these tools FAIL CLOSED — they
return a tool error / empty hints rather than touching any local store.

This module is deliberately registry- and context-driven (rather than holding a
module-level singleton) so it is straightforward to unit test and to wire into
either the native tool registry or the agent init path. It self-registers with a
``check_fn`` gate so discovery can see the native tools while local single-user
and no-adapter profiles keep them invisible/fail-closed until agent init binds a
scoped registry.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from agent.local_memory.provider import (
    RECORD_TOOL_SCHEMAS,
    fenced_record,
    format_hints,
    search_result_payload,
)
from agent.runtime_backends import (
    BackendCapability,
    BackendSelectionError,
    RuntimeBackendRegistry,
)
from agent.runtime_context import RuntimeContext, get_current_runtime_context
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

MEMORY_RECORD_TOOL_NAMES = (
    "memory_record_search",
    "memory_record_get",
    "memory_record_get_many",
)


def _resolve(registry: RuntimeBackendRegistry, context: Optional[RuntimeContext]):
    """Resolve the scoped deep-memory backend, or raise to fail closed."""
    return registry.get(BackendCapability.DEEP_MEMORY, context)


def _context(context: Optional[RuntimeContext]) -> Optional[RuntimeContext]:
    return context if context is not None else get_current_runtime_context()


def memory_record_search(
    args: Dict[str, Any],
    *,
    registry: RuntimeBackendRegistry,
    context: Optional[RuntimeContext] = None,
    **_kwargs: Any,
) -> str:
    ctx = _context(context)
    try:
        backend = _resolve(registry, ctx)
    except BackendSelectionError as exc:
        return tool_error(f"deep memory unavailable for this profile: {exc}")
    try:
        results = backend.search(
            ctx,
            str(args.get("query") or ""),
            filters=args.get("filters") if isinstance(args.get("filters"), dict) else None,
            limit=int(args.get("limit") or 5),
            max_distance=float(args.get("max_distance") or 0.0),
            candidate_strategy=str(args.get("candidate_strategy") or "union"),
        )
    except Exception as exc:
        logger.exception("memory_record_search failed")
        return tool_error(str(exc))
    return tool_result({"results": [search_result_payload(r) for r in results]})


def memory_record_get(
    args: Dict[str, Any],
    *,
    registry: RuntimeBackendRegistry,
    context: Optional[RuntimeContext] = None,
    **_kwargs: Any,
) -> str:
    ctx = _context(context)
    try:
        backend = _resolve(registry, ctx)
    except BackendSelectionError as exc:
        return tool_error(f"deep memory unavailable for this profile: {exc}")
    rid = str(args.get("id") or "")
    try:
        rec = backend.get_record(ctx, rid)
    except Exception as exc:
        logger.exception("memory_record_get failed")
        return tool_error(str(exc))
    if not rec:
        return tool_error(f"memory record not found: {rid}")
    return tool_result(fenced_record(rec))


def memory_record_get_many(
    args: Dict[str, Any],
    *,
    registry: RuntimeBackendRegistry,
    context: Optional[RuntimeContext] = None,
    **_kwargs: Any,
) -> str:
    ctx = _context(context)
    try:
        backend = _resolve(registry, ctx)
    except BackendSelectionError as exc:
        return tool_error(f"deep memory unavailable for this profile: {exc}")
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return tool_error("ids must be a list")
    try:
        records = backend.get_many(ctx, [str(i) for i in ids])
    except Exception as exc:
        logger.exception("memory_record_get_many failed")
        return tool_error(str(exc))
    return tool_result({"records": [fenced_record(r) for r in records]})


def deep_memory_prefetch_hints(
    query: str,
    *,
    registry: RuntimeBackendRegistry,
    context: Optional[RuntimeContext] = None,
    limit: int = 5,
    max_chars: int = 1800,
) -> str:
    """Return bounded historical hints for the bound scope (fail-closed -> "").

    The host injects this string ONLY into the API-call copy of the current
    message; it does not mutate the persisted session transcript.
    """
    ctx = _context(context)
    if not query or not query.strip():
        return ""
    try:
        backend = _resolve(registry, ctx)
    except BackendSelectionError:
        return ""
    try:
        results = backend.search(ctx, query, limit=int(limit or 5))
    except Exception:
        logger.debug("deep_memory_prefetch_hints search failed", exc_info=True)
        return ""
    return format_hints(results, max_chars=int(max_chars or 1800))


# ---------------------------------------------------------------------------
# Active scoped-registry holder (bound by agent init for the native path)
# ---------------------------------------------------------------------------
#
# The scoped DEEP_MEMORY backend partitions by the RuntimeContext passed to each
# backend method, and ``RuntimeBackendRegistry.get`` selects the profile from
# ``RuntimeContext.backend_profile``. Tenant scoping is therefore fully driven by
# the *current* context resolved at dispatch time, not by which registry object
# is active — so a single process-level holder is correct and safe even under a
# concurrent multi-tenant gateway. The holder stays unset (and the native tools
# stay invisible + fail closed) for local single-user processes, where the
# MemoryManager provider path owns deep memory instead.

_active_lock = threading.RLock()
_active_registry: Optional[RuntimeBackendRegistry] = None


def set_active_deep_memory_registry(
    registry_instance: Optional[RuntimeBackendRegistry],
    context: Optional[RuntimeContext] = None,
) -> None:
    """Bind (or clear) the scoped registry the native record tools dispatch through.

    ``context`` is accepted for call-site symmetry with the other backend
    binders but is intentionally unused: per-tenant scoping is resolved from the
    live RuntimeContext at dispatch time.
    """
    global _active_registry
    with _active_lock:
        _active_registry = registry_instance


def get_active_deep_memory_registry(
    context: Optional[RuntimeContext] = None,
) -> Optional[RuntimeBackendRegistry]:
    with _active_lock:
        return _active_registry


def clear_active_deep_memory_registry() -> None:
    global _active_registry
    with _active_lock:
        _active_registry = None


def _deep_memory_native_active() -> bool:
    """check_fn: native record tools are visible only when the scoped path is bound."""
    return get_active_deep_memory_registry() is not None


def deep_memory_prefetch_for_agent(agent: Any, query: str) -> str:
    """Bounded historical hints for an agent on the scoped native path.

    Returns ``""`` unless the scoped registry is bound (local-multi / remote);
    local single-user agents keep the MemoryManager provider prefetch path. The
    host injects the returned string ONLY into the API-call copy of the current
    message — it must never be written back into the persisted transcript.
    """
    if not query or not query.strip():
        return ""
    reg = get_active_deep_memory_registry()
    if reg is None:
        return ""
    ctx = getattr(agent, "runtime_context", None) or get_current_runtime_context()
    try:
        from agent.local_memory.provider import load_deep_memory_config

        cfg = load_deep_memory_config()
    except Exception:
        cfg = {}
    if not cfg.get("prefetch_enabled", True):
        return ""
    return deep_memory_prefetch_hints(
        query,
        registry=reg,
        context=ctx,
        limit=int(cfg.get("prefetch_limit", 5) or 5),
        max_chars=int(cfg.get("prefetch_max_chars", 1800) or 1800),
    )


def _native_search(args: Dict[str, Any], **_kwargs: Any) -> str:
    reg = get_active_deep_memory_registry()
    if reg is None:
        return tool_error("deep memory is not enabled for this profile")
    return memory_record_search(args, registry=reg, context=get_current_runtime_context())


def _native_get(args: Dict[str, Any], **_kwargs: Any) -> str:
    reg = get_active_deep_memory_registry()
    if reg is None:
        return tool_error("deep memory is not enabled for this profile")
    return memory_record_get(args, registry=reg, context=get_current_runtime_context())


def _native_get_many(args: Dict[str, Any], **_kwargs: Any) -> str:
    reg = get_active_deep_memory_registry()
    if reg is None:
        return tool_error("deep memory is not enabled for this profile")
    return memory_record_get_many(args, registry=reg, context=get_current_runtime_context())


_NATIVE_HANDLERS = {
    "memory_record_search": _native_search,
    "memory_record_get": _native_get,
    "memory_record_get_many": _native_get_many,
}


def register_memory_record_tools() -> None:
    """Register the three native record tools into the global tool registry.

    Idempotent. Gated by ``check_fn`` so the tools stay invisible to agents that
    have not bound the scoped native deep-memory path (e.g. local single-user,
    where the MemoryManager provider surface owns these names instead).
    """
    by_name = {schema["name"]: schema for schema in RECORD_TOOL_SCHEMAS}
    for name in MEMORY_RECORD_TOOL_NAMES:
        registry.register(
            name=name,
            toolset="memory",
            schema=by_name[name],
            handler=_NATIVE_HANDLERS[name],
            check_fn=_deep_memory_native_active,
            emoji="🧠",
        )


# Auto-register into native tool discovery at import time. The literal
# ``registry.register(...)`` call below is what ``discover_builtin_tools`` keys on
# to treat this module as a tool provider.
registry.register(
    name="memory_record_search",
    toolset="memory",
    schema=RECORD_TOOL_SCHEMAS[0],
    handler=_native_search,
    check_fn=_deep_memory_native_active,
    emoji="🧠",
)
register_memory_record_tools()
