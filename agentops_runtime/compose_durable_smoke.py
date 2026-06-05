"""M12B compose durable smoke — exercises WORKER_REGISTRY, QUEUE, CONVERSATION_ROUTER, SECRET, MEMORY, SESSION, DEEP_MEMORY, ARTIFACT, AUDIT, DELIVERY, SKILL, and CRON via HTTP adapters.

Run against a live Compose stack (or a test harness with in-process SQLite servers)::

    python -m agentops_runtime.compose_durable_smoke

The module probes durable-backend slices:
- ``worker_fleet``           register/list two workers via WorkerRegistry
- ``queue_tenant_isolation`` tenant A enqueue/claim/ack; tenant B cannot claim tenant A's item
- ``conversation_routing``   resolve-conversation idempotency, route_turn, find_active_run
- ``secret_roundtrip``       put/get sentinel; cross-tenant get is isolated
- ``native_state_continuity`` curated memory write/read, session create/append/read, deep-memory
                               upsert/get — all three surfaces isolated between tenant A and B
- ``artifact_roundtrip``     put/get/list a durable artifact via HttpArtifactBackend + API server
                               + LocalFileArtifactBackend; tenant B cannot get or list tenant A's ref
- ``audit_roundtrip``        record two audit events, count them back through the sanitized
                               audit readback, and prove tenant B count isolation
- ``delivery_dispatch``      dispatch two outbound messages through the native delivery adapter;
                               persistence is asserted by tests through the injected backend
- ``platform_restart_resume`` synthetic Slack-shaped platform dispatch through two fresh local
                               supervisors over durable Compose HTTP backends, proving restart/resume
                               transcript continuity without live container restart claims
- ``skill_roundtrip``        project-scope skill create (approval gate), peer visibility, and
                               tenant B isolation via HttpSkillBackend + SQLiteScopedSkillStore
- ``scheduler_claim_once``   cron claim-once, watchdog fairness, run history, and tenant isolation
                               via HttpCronBackend + SQLiteCronBackend
- ``worker_fleet_scale``     optional live scaled-worker proof when AGENTOPS_SMOKE_EXPECTED_WORKERS
                               is set by the Compose smoke script

Only sanitized, JSON-safe data is reported: step names, booleans, and integer counts.
No raw IDs, tenant IDs, secret values, URLs, local paths, artifact refs, artifact bytes,
audit event payloads, delivery refs, delivery message contents, skill names, skill content,
skill descriptions, skill categories, policy strings, or backend error text is emitted.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from uuid import uuid4

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_supervisor import LocalRunSupervisor
from agentops_runtime.compose_backends import configure_compose_runtime_backends
from agentops_runtime.slack_runtime import SlackDeliveryBackend, build_slack_runtime_context, run_slack_turn

_COMPOSE_PROFILE = "compose-self-hosted"

# Logical scope labels used inside the smoke — never leaked to the report.
_SCOPE_A = "smoke-tenant-a"
_SCOPE_B = "smoke-tenant-b"
_SECRET_REF = "smoke-sentinel-ref"
_SECRET_VALUE = "durable-smoke-sentinel"
_MEMORY_SENTINEL = "smoke-curated-mem-sentinel"
_SESSION_SENTINEL = "smoke-session-sentinel"
_DEEP_MEM_SENTINEL = "smoke-deep-mem-sentinel"

# Artifact roundtrip scope labels — never leaked to the report.
_ARTIFACT_REF = "smoke/roundtrip/sentinel.bin"
_ARTIFACT_DATA = b"durable-smoke-artifact-sentinel"

# Audit/delivery roundtrip scope labels — never leaked to the report.
_AUDIT_CONVERSATION_ID = "smoke-conv-audit"
_AUDIT_EVENT_SENTINEL = "smoke-audit-event-sentinel"
_DELIVERY_CONVERSATION_ID = "smoke-conv-delivery"
_DELIVERY_MESSAGE_SENTINEL = "smoke-delivery-message-sentinel"

# Platform restart/resume scope labels — never leaked to the report.
_PLATFORM_RESTART_THREAD = "smoke-platform-restart-conv"
_PLATFORM_RESTART_TEXT = "smoke-restart-session-sentinel"
_PLATFORM_RESTART_REPLY = "smoke-platform-restart-dispatch"

# Skill roundtrip scope labels — never leaked to the report.
_SKILL_NAME = "smoke-skill-roundtrip"
_SKILL_CONTENT = "smoke-skill-content-sentinel"
_SKILL_DESCRIPTION = "smoke-skill-description-sentinel"
_SKILL_CATEGORY = "smoke-skill-category-sentinel"

# Scheduler claim-once scope labels — never leaked to the report.
_CRON_BUILDER_JOB_NAME = "smoke-cron-builder"
_CRON_WATCHDOG_JOB_NAME = "smoke-cron-watchdog"
_CRON_SCHEDULER_OWNER_1 = "smoke-cron-sched-1"
_CRON_SCHEDULER_OWNER_2 = "smoke-cron-sched-2"
_CRON_SCHEDULER_OWNER_3 = "smoke-cron-sched-3"
_CRON_SCHEDULER_OWNER_B = "smoke-cron-sched-b"
# Far-future deterministic timestamps; wall-clock schedulers cannot race these.
_CRON_FUTURE_TS = 9_999_999_999.0
_CRON_CLAIM_NOW = _CRON_FUTURE_TS + 1.0

# Reserved fleet/infrastructure scope — never collides with tenant scopes.
_FLEET_ORG_ID = "__fleet__"
_FLEET_WS_ID = "__fleet__"


def _fleet_ctx() -> RuntimeContext:
    """Reserved infrastructure RuntimeContext for fleet worker registration."""
    return RuntimeContext(
        mode="agentops",
        org_id=_FLEET_ORG_ID,
        workspace_id=_FLEET_WS_ID,
        workspace_type="infrastructure",
        user_id="__fleet__",
        conversation_id=None,
        agent_profile_id="__fleet__",
        project_id="__fleet__",
        permissions_ref="fleet-perms",
        run_id="__fleet__",
        run_type="manual",
        backend_profile=_COMPOSE_PROFILE,
        delivery_ref=None,
    )


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
    """Synthetic contract guard: register/list via the native adapter with a smoke tenant context.

    This step proves the worker-registry HTTP adapter wiring is functional.
    It is NOT live multi-container scale proof — see worker_fleet_scale for that.
    """
    ctx = _ctx(_SCOPE_A)
    backend = registry.get(BackendCapability.WORKER_REGISTRY, ctx)
    backend.register(ctx, {"capabilities": ["run"]}, ttl_seconds=300)
    backend.register(ctx, {"capabilities": ["run"]}, ttl_seconds=300)
    listed = backend.list_workers(ctx)
    count = len(listed)
    return {"step": "worker_fleet", "ok": count >= 2, "registered": 2, "listed": count, "synthetic": True}


def _step_worker_fleet_scale(
    registry: RuntimeBackendRegistry,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Gated live scale check: verify fleet worker count meets the expected minimum.

    Reads AGENTOPS_SMOKE_EXPECTED_WORKERS from environ. If unset or 0, reports
    enforced=False and ok=True (gate is off). If > 0, lists fleet workers under the
    reserved fleet scope and requires distinct count >= expected. Also asserts that
    a tenant-scoped list cannot see fleet workers.

    Only sanitized counts and booleans are reported — no raw worker IDs, hostnames,
    URLs, or backend error text.
    """
    env = environ or {}
    expected_raw = env.get("AGENTOPS_SMOKE_EXPECTED_WORKERS", "") or ""
    if not str(expected_raw).strip():
        return {"step": "worker_fleet_scale", "ok": True, "enforced": False}
    try:
        expected = int(str(expected_raw).strip())
    except (ValueError, TypeError):
        return {"step": "worker_fleet_scale", "ok": False, "enforced": True, "error": "invalid expected worker count"}

    if expected < 0:
        return {"step": "worker_fleet_scale", "ok": False, "enforced": True, "error": "invalid expected worker count"}
    if expected == 0:
        return {"step": "worker_fleet_scale", "ok": True, "enforced": False}

    fleet_ctx = _fleet_ctx()
    tenant_ctx = _ctx(_SCOPE_A)

    try:
        fleet_backend = registry.get(BackendCapability.WORKER_REGISTRY, fleet_ctx)
        fleet_backend.recover_expired(fleet_ctx)
        fleet_workers = fleet_backend.list_workers(fleet_ctx)
    except Exception:
        return {"step": "worker_fleet_scale", "ok": False, "enforced": True, "error": "fleet worker list failed"}

    smoke_fleet_run_id = env.get("AGENTOPS_SMOKE_FLEET_RUN_ID", "") or ""
    seen_ids: set[str] = set()
    for w in fleet_workers:
        worker_payload = w.get("worker") if isinstance(w, dict) else None
        if smoke_fleet_run_id:
            if not isinstance(worker_payload, dict) or worker_payload.get("smoke_fleet_run_id") != smoke_fleet_run_id:
                continue
        wid = w.get("worker_id") if isinstance(w, dict) else None
        if wid and isinstance(wid, str):
            seen_ids.add(wid)
    distinct_count = len(seen_ids)
    enough = distinct_count >= expected

    try:
        tenant_backend = registry.get(BackendCapability.WORKER_REGISTRY, tenant_ctx)
        tenant_workers = tenant_backend.list_workers(tenant_ctx)
        fleet_ids_in_tenant = sum(
            1
            for w in tenant_workers
            if isinstance(w, dict) and w.get("worker_id") in seen_ids
        )
        tenant_isolated = fleet_ids_in_tenant == 0
    except Exception:
        tenant_isolated = False

    ok = enough and tenant_isolated
    return {
        "step": "worker_fleet_scale",
        "ok": ok,
        "enforced": True,
        "expected": expected,
        "distinct_count": distinct_count,
        "enough": enough,
        "tenant_isolated": tenant_isolated,
    }


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


def _step_artifact_roundtrip(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Prove tenant A can put/get/list a durable artifact; tenant B cannot get or list the same ref."""
    ctx_a = _ctx(_SCOPE_A)
    ctx_b = _ctx(_SCOPE_B)
    backend_a = registry.get(BackendCapability.ARTIFACT, ctx_a)
    backend_b = registry.get(BackendCapability.ARTIFACT, ctx_b)

    stored_ref = backend_a.put(ctx_a, _ARTIFACT_REF, _ARTIFACT_DATA)
    put_ok = bool(stored_ref)

    retrieved = backend_a.get(ctx_a, _ARTIFACT_REF)
    get_ok = retrieved == _ARTIFACT_DATA

    listed = backend_a.list_artifacts(ctx_a)
    list_ok = stored_ref in listed

    b_get = backend_b.get(ctx_b, stored_ref)
    b_list = backend_b.list_artifacts(ctx_b)
    tenant_b_isolated = b_get is None and stored_ref not in b_list

    ok = put_ok and get_ok and list_ok and tenant_b_isolated
    return {
        "step": "artifact_roundtrip",
        "ok": ok,
        "put_ok": put_ok,
        "get_ok": get_ok,
        "list_ok": list_ok,
        "tenant_b_isolated": tenant_b_isolated,
    }


def _audit_ctx(scope: str, run_suffix: str) -> RuntimeContext:
    base = _ctx(scope)
    return RuntimeContext(
        mode=base.mode,
        org_id=base.org_id,
        workspace_id=base.workspace_id,
        workspace_type=base.workspace_type,
        user_id=base.user_id,
        conversation_id=f"{_AUDIT_CONVERSATION_ID}-{run_suffix}",
        agent_profile_id=base.agent_profile_id,
        project_id=base.project_id,
        permissions_ref=base.permissions_ref,
        run_id=f"{base.run_id}-{run_suffix}",
        run_type=base.run_type,
        backend_profile=base.backend_profile,
        delivery_ref=base.delivery_ref,
    )


def _delivery_ctx(scope: str) -> RuntimeContext:
    base = _ctx(scope)
    return RuntimeContext(
        mode=base.mode,
        org_id=base.org_id,
        workspace_id=base.workspace_id,
        workspace_type=base.workspace_type,
        user_id=base.user_id,
        conversation_id=_DELIVERY_CONVERSATION_ID,
        agent_profile_id=base.agent_profile_id,
        project_id=base.project_id,
        permissions_ref=base.permissions_ref,
        run_id=base.run_id,
        run_type=base.run_type,
        backend_profile=base.backend_profile,
        delivery_ref=base.delivery_ref,
    )


def _step_audit_roundtrip(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Record and count tenant-scoped audit events without exposing event payloads."""
    run_suffix = uuid4().hex
    ctx_a = _audit_ctx(_SCOPE_A, run_suffix)
    ctx_b = _audit_ctx(_SCOPE_B, run_suffix)
    backend_a = registry.get(BackendCapability.AUDIT, ctx_a)
    backend_b = registry.get(BackendCapability.AUDIT, ctx_b)

    try:
        before_a = int(backend_a.count_events(ctx_a))
        before_b = int(backend_b.count_events(ctx_b))
    except Exception:
        return {
            "step": "audit_roundtrip",
            "ok": False,
            "record_ok": False,
            "counted": 0,
            "tenant_b_isolated": False,
        }

    try:
        backend_a.record(ctx_a, {"event": _AUDIT_EVENT_SENTINEL, "ordinal": 1})
        backend_a.record(ctx_a, {"event": _AUDIT_EVENT_SENTINEL, "ordinal": 2})
    except Exception:
        return {
            "step": "audit_roundtrip",
            "ok": False,
            "record_ok": False,
            "counted": before_a,
            "tenant_b_isolated": False,
        }

    try:
        count_a = int(backend_a.count_events(ctx_a))
        count_b = int(backend_b.count_events(ctx_b))
    except Exception:
        return {
            "step": "audit_roundtrip",
            "ok": False,
            "record_ok": False,
            "counted": 0,
            "tenant_b_isolated": False,
        }

    record_ok = before_a == 0 and count_a == 2
    tenant_b_isolated = before_b == 0 and count_b == 0
    ok = record_ok and tenant_b_isolated
    return {
        "step": "audit_roundtrip",
        "ok": ok,
        "record_ok": record_ok,
        "counted": count_a,
        "tenant_b_isolated": tenant_b_isolated,
    }


def _step_delivery_dispatch(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Dispatch outbound delivery payloads through the native delivery adapter."""
    ctx_a = _delivery_ctx(_SCOPE_A)
    backend = registry.get(BackendCapability.DELIVERY, ctx_a)
    dispatched = 0
    try:
        backend.deliver(ctx_a, {"kind": "smoke", "body": _DELIVERY_MESSAGE_SENTINEL, "ordinal": 1})
        dispatched += 1
        backend.deliver(ctx_a, {"kind": "smoke", "body": _DELIVERY_MESSAGE_SENTINEL, "ordinal": 2})
        dispatched += 1
    except Exception:
        return {
            "step": "delivery_dispatch",
            "ok": False,
            "dispatched": dispatched,
            "read_isolation_applicable": False,
        }
    return {
        "step": "delivery_dispatch",
        "ok": dispatched == 2,
        "dispatched": dispatched,
        "read_isolation_applicable": False,
    }


def _slack_event(
    *,
    team: str = "T_restart_a",
    channel: str = "C_restart",
    user: str = "U_restart",
    ts: str,
    thread_ts: str = _PLATFORM_RESTART_THREAD,
) -> dict[str, str]:
    """Return a Slack-shaped event used only inside the sanitized smoke step."""

    return {
        "team": team,
        "channel": channel,
        "user": user,
        "thread_ts": thread_ts,
        "ts": ts,
    }


def _platform_restart_config(scope: str) -> dict[str, dict[str, Any]]:
    return {
        "agentops": {
            "enabled": True,
            "org_id": f"org-smoke-platform-restart-{scope}",
            "project_id": f"proj-smoke-platform-restart-{scope}",
            "agent_profile_id": "smoke-platform-agent",
            "permissions_ref": "smoke-platform-perms",
            "backend_profile": _COMPOSE_PROFILE,
        }
    }


def _step_platform_restart_resume(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Synthetic Slack platform restart/resume proof over durable Compose HTTP backends.

    This step intentionally does not claim live container restart coverage. It
    drives Slack-shaped turns through the native Slack runtime helper, uses two
    fresh ``LocalRunSupervisor`` instances to simulate worker restart, and
    verifies durable router/session continuity without exposing platform IDs,
    run/session IDs, transcript text, URLs, paths, delivery refs, or backend
    errors in the report.
    """

    config = _platform_restart_config("a")
    thread_ts = f"{_PLATFORM_RESTART_THREAD}-{uuid4().hex}"
    first_event = _slack_event(ts="1.000100", thread_ts=thread_ts)
    followup_event = _slack_event(ts="1.000200", thread_ts=thread_ts)
    context = build_slack_runtime_context(first_event, config=config)
    followup_context = build_slack_runtime_context(followup_event, config=config)
    b_context = build_slack_runtime_context(
        _slack_event(team="T_restart_b", user="U_restart_b", ts="1.000300", thread_ts=thread_ts),
        config=_platform_restart_config("b"),
    )
    if context is None or followup_context is None or b_context is None:
        return {
            "step": "platform_restart_resume",
            "ok": False,
            "routing_ok": False,
            "restart_ok": False,
            "session_resumed": False,
            "dispatch_ok": False,
            "synthetic": True,
        }

    sent: list[tuple[Any, ...]] = []

    def _recording_sender(channel: str, text: str, reply_to: Any = None, metadata: Any = None) -> None:
        sent.append((channel, text, reply_to, metadata))

    delivery = SlackDeliveryBackend(_recording_sender)
    first_worker = LocalRunSupervisor(worker_id="platform-restart-worker-a", max_concurrent_runs=1, registry=registry)
    second_worker: LocalRunSupervisor | None = None
    second_handler_saw_prior = False

    def _first_handler(transcript: list[Any]) -> dict[str, Any]:
        return {"role": "assistant", "content": _PLATFORM_RESTART_REPLY}

    def _second_handler(transcript: list[Any]) -> dict[str, Any]:
        nonlocal second_handler_saw_prior
        contents = [str(item.get("content") or "") for item in transcript if isinstance(item, dict)]
        second_handler_saw_prior = _PLATFORM_RESTART_TEXT in contents and _PLATFORM_RESTART_REPLY in contents
        return {"role": "assistant", "content": _PLATFORM_RESTART_REPLY}

    try:
        first = run_slack_turn(
            first_event,
            context=context,
            text=_PLATFORM_RESTART_TEXT,
            handler=_first_handler,
            supervisor=first_worker,
            registry=registry,
            delivery=delivery,
            result_timeout=10.0,
        )
        first_worker.shutdown()
        second_worker = LocalRunSupervisor(worker_id="platform-restart-worker-b", max_concurrent_runs=1, registry=registry)
        followup = run_slack_turn(
            followup_event,
            context=followup_context,
            text=_PLATFORM_RESTART_TEXT,
            handler=_second_handler,
            supervisor=second_worker,
            registry=registry,
            delivery=delivery,
            result_timeout=10.0,
        )

        sessions_a = registry.get(BackendCapability.SESSION, context)
        sessions_b = registry.get(BackendCapability.SESSION, b_context)
        transcript_a = sessions_a.read_messages(context)
        transcript_b = sessions_b.read_messages(b_context)
        search_b = sessions_b.search(b_context, _PLATFORM_RESTART_TEXT)
    except Exception:
        return {
            "step": "platform_restart_resume",
            "ok": False,
            "routing_ok": False,
            "restart_ok": False,
            "session_resumed": False,
            "dispatch_ok": False,
            "synthetic": True,
        }
    finally:
        first_worker.shutdown()
        if second_worker is not None:
            second_worker.shutdown()

    first_ok = first.run.error is None and bool(first.run.value)
    followup_ok = followup.run.error is None and bool(followup.run.value)
    routing_ok = (
        first.route.routed_to_active_run is False
        and followup.route.routed_to_active_run is True
        and bool(first.route.run_id)
        and first.route.run_id == followup.route.run_id
    )
    transcript_count = len(transcript_a)
    session_resumed = second_handler_saw_prior and transcript_count >= 4 and len(transcript_b) == 0 and len(search_b) == 0
    dispatch_same_thread = (
        len(sent) == 2
        and all(item[0] == context.external_channel_id for item in sent)
        and all(isinstance(item[3], dict) and item[3].get("thread_ts") == context.external_thread_id for item in sent)
    )
    dispatch_ok = dispatch_same_thread and len(first.delivered) == 1 and len(followup.delivered) == 1
    restart_ok = first_ok and followup_ok and session_resumed
    ok = routing_ok and restart_ok and dispatch_ok
    return {
        "step": "platform_restart_resume",
        "ok": ok,
        "routing_ok": routing_ok,
        "restart_ok": restart_ok,
        "session_resumed": session_resumed,
        "dispatch_ok": dispatch_ok,
        "tenant_b_isolated": len(transcript_b) == 0 and len(search_b) == 0,
        "transcript_count": transcript_count,
        "dispatch_count": len(sent),
        "synthetic": True,
    }


def _step_skill_roundtrip(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Prove project-scope skill approval gate, peer visibility, and tenant B isolation."""
    ctx_author = _ctx(_SCOPE_A)
    ctx_approver = RuntimeContext(
        mode=ctx_author.mode,
        org_id=ctx_author.org_id,
        workspace_id=ctx_author.workspace_id,
        workspace_type=ctx_author.workspace_type,
        user_id=ctx_author.user_id,
        conversation_id=ctx_author.conversation_id,
        agent_profile_id=ctx_author.agent_profile_id,
        project_id=ctx_author.project_id,
        permissions_ref=ctx_author.permissions_ref,
        run_id=ctx_author.run_id,
        run_type=ctx_author.run_type,
        backend_profile=ctx_author.backend_profile,
        delivery_ref=ctx_author.delivery_ref,
        metadata={"skill_write_approved": True},
    )
    ctx_peer = RuntimeContext(
        mode="agentops",
        org_id=ctx_author.org_id,
        workspace_id=ctx_author.workspace_id,
        workspace_type="team",
        user_id="user-smoke-peer-a",
        conversation_id="smoke-conv-peer",
        agent_profile_id="smoke-agent",
        project_id=ctx_author.project_id,
        permissions_ref="smoke-perms",
        run_id="run-smoke-peer-a",
        run_type="manual",
        backend_profile=_COMPOSE_PROFILE,
        delivery_ref="thread://smoke/peer-a",
    )
    ctx_b = _ctx(_SCOPE_B)

    backend_author = registry.get(BackendCapability.SKILL, ctx_author)
    backend_approver = registry.get(BackendCapability.SKILL, ctx_approver)
    backend_peer = registry.get(BackendCapability.SKILL, ctx_peer)
    backend_b = registry.get(BackendCapability.SKILL, ctx_b)

    try:
        backend_approver.manage_skill(ctx_approver, action="delete", name=_SKILL_NAME, scope="project")
    except Exception:
        pass

    unapproved_result = backend_author.manage_skill(
        ctx_author,
        action="create",
        name=_SKILL_NAME,
        scope="project",
        content=_SKILL_CONTENT,
        description=_SKILL_DESCRIPTION,
        category=_SKILL_CATEGORY,
    )
    unapproved_load = backend_author.load_skill(ctx_author, _SKILL_NAME)
    unapproved_list = backend_author.list_skills(ctx_author)
    unapproved_names = [s.get("name") for s in unapproved_list if isinstance(s, dict)]
    unapproved_absent = (
        (unapproved_load is None or unapproved_load.get("success") is not True)
        and _SKILL_NAME not in unapproved_names
    )
    unapproved_write_denied = unapproved_result.get("success") is not True and unapproved_absent

    approved_result = backend_approver.manage_skill(
        ctx_approver,
        action="create",
        name=_SKILL_NAME,
        scope="project",
        content=_SKILL_CONTENT,
        description=_SKILL_DESCRIPTION,
        category=_SKILL_CATEGORY,
    )
    approved_create_ok = approved_result.get("success") is True

    author_load = backend_author.load_skill(ctx_author, _SKILL_NAME)
    author_list = backend_author.list_skills(ctx_author)
    author_names = [s.get("name") for s in author_list if isinstance(s, dict)]
    author_read_ok = (
        _SKILL_NAME in author_names
        and author_load is not None
        and author_load.get("success") is True
        and _SKILL_CONTENT in (author_load.get("content") or "")
    )

    peer_list = backend_peer.list_skills(ctx_peer)
    peer_load = backend_peer.load_skill(ctx_peer, _SKILL_NAME)
    peer_names = [s.get("name") for s in peer_list if isinstance(s, dict)]
    shared_peer_visible = (
        _SKILL_NAME in peer_names
        and peer_load is not None
        and peer_load.get("success") is True
        and _SKILL_CONTENT in (peer_load.get("content") or "")
    )

    b_list = backend_b.list_skills(ctx_b)
    b_load = backend_b.load_skill(ctx_b, _SKILL_NAME)
    b_names = [s.get("name") for s in b_list if isinstance(s, dict)]
    tenant_b_isolated = _SKILL_NAME not in b_names and (b_load is None or b_load.get("success") is not True)

    try:
        backend_approver.manage_skill(ctx_approver, action="delete", name=_SKILL_NAME, scope="project")
    except Exception:
        pass

    ok = unapproved_write_denied and approved_create_ok and author_read_ok and shared_peer_visible and tenant_b_isolated
    return {
        "step": "skill_roundtrip",
        "ok": ok,
        "unapproved_write_denied": unapproved_write_denied,
        "approved_create_ok": approved_create_ok,
        "author_read_ok": author_read_ok,
        "shared_peer_visible": shared_peer_visible,
        "tenant_b_isolated": tenant_b_isolated,
    }


def _step_scheduler_claim_once(registry: RuntimeBackendRegistry) -> dict[str, Any]:
    """Prove native cron claim/lease/history end-to-end with two-tenant isolation.

    Creates two due jobs for tenant A, proves fair claim-once semantics across two
    scheduler owners, completes one job and verifies durable run history, then
    proves tenant B is isolated from both claims and history reads.
    """
    ctx_a = _ctx(_SCOPE_A)
    ctx_b = _ctx(_SCOPE_B)
    cron_a = registry.get(BackendCapability.CRON, ctx_a)
    cron_b = registry.get(BackendCapability.CRON, ctx_b)
    created_job_ids: list[str] = []

    def _claimed_id(claim: Any) -> str:
        if not isinstance(claim, dict):
            return ""
        return str(claim.get("id") or claim.get("job_id") or "")

    try:
        builder_id = cron_a.create(ctx_a, {
            "name": _CRON_BUILDER_JOB_NAME,
            "schedule": "*/5 * * * *",
            "prompt": _CRON_BUILDER_JOB_NAME,
            "next_run_at": _CRON_FUTURE_TS,
        })
        created_job_ids.append(str(builder_id))

        b_pre_claims = cron_b.claim_due(
            ctx_b, owner=_CRON_SCHEDULER_OWNER_B, now=_CRON_CLAIM_NOW, limit=1
        )
        tenant_b_claim_isolated = len(b_pre_claims) == 0

        owner1_claims = cron_a.claim_due(
            ctx_a, owner=_CRON_SCHEDULER_OWNER_1, now=_CRON_CLAIM_NOW, limit=1
        )
        claimed_job_id = _claimed_id(owner1_claims[0]) if owner1_claims else ""
        claimed_once = len(owner1_claims) == 1 and claimed_job_id == str(builder_id)

        # Create the unrelated due watchdog after the builder lease is held so
        # the smoke proves owner 2 cannot reclaim the builder while still being
        # able to claim a different due job, without depending on backend order.
        watchdog_id = cron_a.create(ctx_a, {
            "name": _CRON_WATCHDOG_JOB_NAME,
            "schedule": "*/5 * * * *",
            "prompt": _CRON_WATCHDOG_JOB_NAME,
            "next_run_at": _CRON_FUTURE_TS,
        })
        created_job_ids.append(str(watchdog_id))

        owner2_claims = cron_a.claim_due(
            ctx_a, owner=_CRON_SCHEDULER_OWNER_2, now=_CRON_CLAIM_NOW, limit=1
        )
        owner2_job_id = _claimed_id(owner2_claims[0]) if owner2_claims else ""
        watchdog_claimed = (
            len(owner2_claims) == 1
            and owner2_job_id == str(watchdog_id)
            and owner2_job_id != claimed_job_id
        )

        third_claims = cron_a.claim_due(
            ctx_a, owner=_CRON_SCHEDULER_OWNER_3, now=_CRON_CLAIM_NOW
        )
        no_extra_claims = len(third_claims) == 0

        history_recorded = False
        completion_recorded = False
        b_history: list[Any] = []
        if claimed_job_id:
            try:
                cron_a.complete_run(
                    ctx_a,
                    claimed_job_id,
                    owner=_CRON_SCHEDULER_OWNER_1,
                    now=_CRON_CLAIM_NOW + 1.0,
                    next_run_at=_CRON_CLAIM_NOW + 3600.0,
                )
                completion_recorded = True
            except Exception:
                pass
            history_a = cron_a.run_history(ctx_a, claimed_job_id)
            history_recorded = len(history_a) > 0
            b_history = cron_b.run_history(ctx_b, claimed_job_id)

        b_claims = cron_b.claim_due(ctx_b, owner=_CRON_SCHEDULER_OWNER_B, now=_CRON_CLAIM_NOW)
        tenant_b_isolated = tenant_b_claim_isolated and len(b_claims) == 0 and len(b_history) == 0

        ok = (
            claimed_once
            and watchdog_claimed
            and no_extra_claims
            and completion_recorded
            and history_recorded
            and tenant_b_isolated
        )
        return {
            "step": "scheduler_claim_once",
            "ok": ok,
            "claimed_once": claimed_once,
            "watchdog_claimed": watchdog_claimed,
            "no_extra_claims": no_extra_claims,
            "completion_recorded": completion_recorded,
            "history_recorded": history_recorded,
            "tenant_b_isolated": tenant_b_isolated,
        }
    finally:
        for jid in created_job_ids:
            try:
                cron_a.remove(ctx_a, jid)
            except Exception:
                pass


def run_durable_smoke(*, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Run all durable smoke steps and return a sanitized, JSON-safe report dict."""
    try:
        registry = RuntimeBackendRegistry()
        configure_compose_runtime_backends(registry, environ=environ)
    except Exception:
        return {"ok": False, "error": "compose backend wiring failed"}

    _env_for_scale = environ if environ is not None else dict(os.environ)

    def _scale_step(reg: RuntimeBackendRegistry) -> dict[str, Any]:
        return _step_worker_fleet_scale(reg, environ=_env_for_scale)

    _scale_step.__name__ = "_step_worker_fleet_scale"

    steps: list[dict[str, Any]] = []
    all_ok = True
    for step_fn in (
        _step_worker_fleet,
        _step_queue_tenant_isolation,
        _step_conversation_routing,
        _step_secret_roundtrip,
        _step_native_state_continuity,
        _step_artifact_roundtrip,
        _step_audit_roundtrip,
        _step_delivery_dispatch,
        _step_platform_restart_resume,
        _step_skill_roundtrip,
        _step_scheduler_claim_once,
        _scale_step,
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
