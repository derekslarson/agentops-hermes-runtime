"""M12B compose durable smoke — exercises WORKER_REGISTRY, QUEUE, CONVERSATION_ROUTER, SECRET, MEMORY, SESSION, DEEP_MEMORY via HTTP adapters.

Run against a live Compose stack (or a test harness with in-process SQLite servers)::

    python -m agentops_runtime.compose_durable_smoke

The module probes five durable-backend slices:
- ``worker_fleet``           register/list two workers via WorkerRegistry
- ``queue_tenant_isolation`` tenant A enqueue/claim/ack; tenant B cannot claim tenant A's item
- ``conversation_routing``   resolve-conversation idempotency, route_turn, find_active_run
- ``secret_roundtrip``       put/get sentinel; cross-tenant get is isolated
- ``native_state_continuity`` curated memory write/read, session create/append/read, deep-memory
                               upsert/get — all three surfaces isolated between tenant A and B

Only sanitized, JSON-safe data is reported: step names, booleans, and integer counts.
No raw IDs, tenant IDs, secret values, URLs, local paths, or backend error text is emitted.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agentops_runtime.compose_backends import configure_compose_runtime_backends

_COMPOSE_PROFILE = "compose-self-hosted"

# Logical scope labels used inside the smoke — never leaked to the report.
_SCOPE_A = "smoke-tenant-a"
_SCOPE_B = "smoke-tenant-b"
_SECRET_REF = "smoke-sentinel-ref"
_SECRET_VALUE = "durable-smoke-sentinel"
_MEMORY_SENTINEL = "smoke-curated-mem-sentinel"
_SESSION_SENTINEL = "smoke-session-sentinel"
_DEEP_MEM_SENTINEL = "smoke-deep-mem-sentinel"


def _ctx(scope_label: str, conversation_id: str = "smoke-conv-1") -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id=f"org-{scope_label}",
        workspace_id=f"ws-{scope_label}",
        workspace_type="team",
        user_id=f"user-{scope_label}",
        conversation_id=conversation_id,
        agent_profile_id="smoke-agent",
        project_id=f"proj-{scope_label}",
        permissions_ref="smoke-perms",
        run_id=f"run-{scope_label}",
        run_type="manual",
        backend_profile=_COMPOSE_PROFILE,
        delivery_ref=f"thread://smoke/{scope_label}",
    )


def _step_worker_fleet(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    ctx = _ctx(_SCOPE_A)
    backend = registry.get(BackendCapability.WORKER_REGISTRY, ctx)
    backend.register(ctx, {"capabilities": ["run"]}, ttl_seconds=300)
    backend.register(ctx, {"capabilities": ["run"]}, ttl_seconds=300)
    listed = backend.list_workers(ctx)
    count = len(listed)
    return {"step": "worker_fleet", "ok": count >= 2, "registered": 2, "listed": count}


def _step_queue_tenant_isolation(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    ctx_a = _ctx(_SCOPE_A)
    ctx_b = _ctx(_SCOPE_B)
    backend_a = registry.get(BackendCapability.QUEUE, ctx_a)
    backend_b = registry.get(BackendCapability.QUEUE, ctx_b)

    backend_a.enqueue(ctx_a, {"task": "smoke"})

    claimed_b = backend_b.claim(ctx_b)
    tenant_b_isolated = claimed_b is None

    claimed_a = backend_a.claim(ctx_a)
    tenant_a_claimed = claimed_a is not None
    if claimed_a is not None:
        receipt = getattr(claimed_a, "receipt", None) or (
            claimed_a["receipt"] if isinstance(claimed_a, dict) else claimed_a
        )
        backend_a.ack(ctx_a, receipt)

    ok = tenant_a_claimed and tenant_b_isolated
    return {
        "step": "queue_tenant_isolation",
        "ok": ok,
        "tenant_a_claimed": tenant_a_claimed,
        "tenant_b_isolated": tenant_b_isolated,
    }


def _step_conversation_routing(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    ctx = _ctx(_SCOPE_A, conversation_id="smoke-conv-routing")
    backend = registry.get(BackendCapability.CONVERSATION_ROUTER, ctx)

    conv_id_1 = backend.resolve_conversation(ctx, {"event_type": "smoke"})
    conv_id_2 = backend.resolve_conversation(ctx, {"event_type": "smoke"})
    resolve_stable = conv_id_1 == conv_id_2 and bool(conv_id_1)

    run_id = backend.route_turn(ctx, conv_id_1, {"type": "user", "text": "smoke"})
    routed = bool(run_id)

    active = backend.find_active_run(ctx, conv_id_1)
    active_run_found = bool(active)

    ok = resolve_stable and routed and active_run_found
    return {
        "step": "conversation_routing",
        "ok": ok,
        "resolve_stable": resolve_stable,
        "routed": routed,
        "active_run_found": active_run_found,
    }


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("content") or item.get("excerpt") or "")
    return str(getattr(item, "text", "") or getattr(item, "content", "") or getattr(item, "excerpt", "") or "")


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or item.get("record_id") or "")
    return str(getattr(item, "id", "") or getattr(item, "record_id", "") or "")


def _text_matches(item: Any, text: str) -> bool:
    item_text = _item_text(item)
    return text in item_text or bool(item_text and item_text in text)


def _item_session_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("session_id") or "")
    return str(getattr(item, "session_id", "") or "")


def _contains_session_message(items: list[Any], *, session_id: str, text: str) -> bool:
    return any(_item_session_id(item) == session_id and _text_matches(item, text) for item in items)


def _contains_text(items: list[Any], text: str) -> bool:
    return any(_text_matches(item, text) for item in items)


def _contains_record(items: list[Any], *, record_id: str, text: str) -> bool:
    return any(_item_id(item) == record_id and _text_matches(item, text) for item in items)


def _record_matches(item: Any, *, record_id: str, text: str) -> bool:
    return _item_id(item) == record_id and _text_matches(item, text)


def _step_secret_roundtrip(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    ctx_a = _ctx(_SCOPE_A)
    ctx_b = _ctx(_SCOPE_B)
    backend_a = registry.get(BackendCapability.SECRET, ctx_a)
    backend_b = registry.get(BackendCapability.SECRET, ctx_b)

    backend_a.put_secret(ctx_a, _SECRET_REF, _SECRET_VALUE)
    retrieved = backend_a.get_secret(ctx_a, _SECRET_REF)
    put_ok = retrieved == _SECRET_VALUE
    get_ok = put_ok

    cross_tenant = backend_b.get_secret(ctx_b, _SECRET_REF)
    cross_tenant_isolated = cross_tenant is None

    ok = put_ok and get_ok and cross_tenant_isolated
    return {
        "step": "secret_roundtrip",
        "ok": ok,
        "put_ok": put_ok,
        "get_ok": get_ok,
        "cross_tenant_isolated": cross_tenant_isolated,
    }


def _step_native_state_continuity(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    ctx_a = _ctx(_SCOPE_A)
    ctx_b = _ctx(_SCOPE_B)

    mem_a = registry.get(BackendCapability.MEMORY, ctx_a)
    mem_b = registry.get(BackendCapability.MEMORY, ctx_b)
    mem_a.write(ctx_a, _MEMORY_SENTINEL, target="memory", action="add")
    read_a = mem_a.read(ctx_a, target="memory")
    read_b = mem_b.read(ctx_b, target="memory")
    curated_memory_ok = read_a is not None and _MEMORY_SENTINEL in (read_a or "")
    curated_b_isolated = read_b is None

    sess_a = registry.get(BackendCapability.SESSION, ctx_a)
    sess_b = registry.get(BackendCapability.SESSION, ctx_b)
    session_id = sess_a.create_session(ctx_a)
    sess_a.append_message(ctx_a, {"role": "user", "content": _SESSION_SENTINEL})
    msgs_a = sess_a.read_messages(ctx_a, session_id=session_id)
    msgs_b = sess_b.read_messages(ctx_b, session_id=session_id)
    search_a = sess_a.search(ctx_a, _SESSION_SENTINEL)
    search_b = sess_b.search(ctx_b, _SESSION_SENTINEL)
    session_read_ok = _contains_session_message(msgs_a, session_id=session_id, text=_SESSION_SENTINEL)
    session_search_ok = _contains_session_message(search_a, session_id=session_id, text=_SESSION_SENTINEL)
    session_ok = session_read_ok and session_search_ok
    session_b_isolated = len(msgs_b) == 0 and len(search_b) == 0

    dm_a = registry.get(BackendCapability.DEEP_MEMORY, ctx_a)
    dm_b = registry.get(BackendCapability.DEEP_MEMORY, ctx_b)
    record = dm_a.upsert_record(ctx_a, text=_DEEP_MEM_SENTINEL, source="smoke")
    record_id = record.id
    get_a = dm_a.get_record(ctx_a, record_id)
    many_a = dm_a.get_many(ctx_a, [record_id])
    search_a = dm_a.search(ctx_a, _DEEP_MEM_SENTINEL, limit=1)
    get_b = dm_b.get_record(ctx_b, record_id)
    many_b = dm_b.get_many(ctx_b, [record_id])
    search_b = dm_b.search(ctx_b, _DEEP_MEM_SENTINEL, limit=1)
    deep_memory_ok = (
        get_a is not None
        and _record_matches(get_a, record_id=record_id, text=_DEEP_MEM_SENTINEL)
        and _contains_record(many_a, record_id=record_id, text=_DEEP_MEM_SENTINEL)
        and _contains_record(search_a, record_id=record_id, text=_DEEP_MEM_SENTINEL)
    )
    deep_mem_b_isolated = get_b is None and len(many_b) == 0 and len(search_b) == 0

    tenant_b_isolated = curated_b_isolated and session_b_isolated and deep_mem_b_isolated
    ok = curated_memory_ok and session_ok and deep_memory_ok and tenant_b_isolated
    return {
        "step": "native_state_continuity",
        "ok": ok,
        "curated_memory_ok": curated_memory_ok,
        "session_ok": session_ok,
        "deep_memory_ok": deep_memory_ok,
        "tenant_b_isolated": tenant_b_isolated,
    }


def run_durable_smoke(*, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Run all durable smoke steps and return a sanitized, JSON-safe report dict."""
    try:
        registry = RuntimeBackendRegistry()
        configure_compose_runtime_backends(registry, environ=environ)
    except Exception:
        return {"ok": False, "error": "compose backend wiring failed"}

    steps: list[dict[str, Any]] = []
    all_ok = True
    for step_fn in (
        _step_worker_fleet,
        _step_queue_tenant_isolation,
        _step_conversation_routing,
        _step_secret_roundtrip,
        _step_native_state_continuity,
    ):
        try:
            step = step_fn(registry)
        except Exception:
            step = {"step": step_fn.__name__.removeprefix("_step_"), "ok": False, "error": "step failed"}
        steps.append(step)
        if not step.get("ok"):
            all_ok = False

    return {"ok": all_ok, "steps": steps}


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    _ = argv if argv is not None else sys.argv[1:]
    report = run_durable_smoke(environ=environ)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
