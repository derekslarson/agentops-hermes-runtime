"""Scoped deep-memory prefetch injection seam (M5A).

Proves the M5A "prefetch injection seam" blocker: under the scoped native path
(local-multi / remote), bounded historical hints are produced for the bound
RuntimeContext and injected ONLY into the API-call copy of the current user
message — the persisted transcript message object is never mutated. Hints stay
scoped (one user/thread cannot see another's) and fail closed (empty string) when
no scoped path is bound (local single-user keeps its MemoryManager prefetch).
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import tools.memory_record_tools as mrt
from agent.agent_init import _bind_deep_memory_backend
from agent.conversation_loop import apply_current_turn_user_injections
from agent.memory_manager import build_memory_context_block
from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, use_runtime_context
from tools.memory_record_tools import deep_memory_prefetch_for_agent


def _config(tmp_path):
    return {"backends": {"options": {"deep_memory": {"storage_dir": str(tmp_path / "store")}}}}


def _ctx(**overrides):
    base = dict(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="thread-1",
        backend_profile="local-multi",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def _ingest(registry, context, text):
    backend = registry.get(BackendCapability.DEEP_MEMORY, context)
    return backend.upsert_record(
        context,
        text=text,
        metadata={"type": "conversation_turn"},
        source="unit",
        source_uri=f"unit://{text[:12]}",
        record_kind="conversation_turn",
    )


def test_prefetch_for_agent_returns_bounded_hints_under_scoped_path(tmp_path):
    mrt.clear_active_deep_memory_registry()
    registry = RuntimeBackendRegistry(_config(tmp_path))
    mrt.set_active_deep_memory_registry(registry)
    alice = _ctx(user_id="alice")
    _ingest(registry, alice, "Alice teal sprocket rollout plan: ship Tuesday.")
    agent = SimpleNamespace(runtime_context=alice)

    with use_runtime_context(alice):
        hints = deep_memory_prefetch_for_agent(agent, "teal sprocket rollout plan")
    assert "Deep memory hints" in hints
    assert "teal sprocket" in hints.lower()
    mrt.clear_active_deep_memory_registry()


def test_prefetch_for_agent_is_scoped_across_users(tmp_path):
    mrt.clear_active_deep_memory_registry()
    registry = RuntimeBackendRegistry(_config(tmp_path))
    mrt.set_active_deep_memory_registry(registry)
    alice = _ctx(user_id="alice")
    bob = _ctx(user_id="bob")
    _ingest(registry, alice, "Alice teal sprocket rollout plan: ship Tuesday.")
    agent_bob = SimpleNamespace(runtime_context=bob)

    with use_runtime_context(bob):
        hints = deep_memory_prefetch_for_agent(agent_bob, "teal sprocket rollout plan")
    assert hints == ""
    mrt.clear_active_deep_memory_registry()


def test_prefetch_for_agent_empty_without_binding():
    mrt.clear_active_deep_memory_registry()
    agent = SimpleNamespace(runtime_context=RuntimeContext(mode="local"))
    assert deep_memory_prefetch_for_agent(agent, "anything") == ""


def test_prefetch_for_agent_empty_for_blank_query(tmp_path):
    mrt.clear_active_deep_memory_registry()
    registry = RuntimeBackendRegistry(_config(tmp_path))
    mrt.set_active_deep_memory_registry(registry)
    agent = SimpleNamespace(runtime_context=_ctx())
    assert deep_memory_prefetch_for_agent(agent, "   ") == ""
    mrt.clear_active_deep_memory_registry()


def test_injection_does_not_mutate_persisted_message():
    persisted = {"role": "user", "content": "What did we decide about the rollout?"}
    api_copy = persisted.copy()
    apply_current_turn_user_injections(api_copy, ["<memory-context>HINTS</memory-context>"])
    assert "HINTS" in api_copy["content"]
    # The persisted transcript message object must be unchanged.
    assert persisted["content"] == "What did we decide about the rollout?"


def test_injection_noop_for_empty_injections():
    api_copy = {"role": "user", "content": "hi"}
    apply_current_turn_user_injections(api_copy, [])
    assert api_copy["content"] == "hi"


def test_scoped_hint_flows_into_api_copy_only(tmp_path):
    # End-to-end of the seam: scoped prefetch -> fenced injection -> API copy only.
    mrt.clear_active_deep_memory_registry()
    registry = RuntimeBackendRegistry(_config(tmp_path))
    mrt.set_active_deep_memory_registry(registry)
    alice = _ctx(user_id="alice")
    _ingest(registry, alice, "Alice teal sprocket rollout plan: ship Tuesday.")
    agent = SimpleNamespace(runtime_context=alice)

    persisted = {"role": "user", "content": "Remind me about the rollout."}
    api_copy = persisted.copy()
    with use_runtime_context(alice):
        hints = deep_memory_prefetch_for_agent(agent, "rollout")
        injections = [build_memory_context_block(hints)] if hints else []
        apply_current_turn_user_injections(api_copy, injections)

    assert injections, "scoped prefetch should yield hints"
    assert api_copy["content"] != persisted["content"]
    assert persisted["content"] == "Remind me about the rollout."
    mrt.clear_active_deep_memory_registry()


# ── M5B: aws-managed prefetch wiring ─────────────────────────────────────────


def _aws_ctx_pf(**overrides):
    base = dict(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="thread-1",
        backend_profile="aws-managed",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def _aws_agent(ctx):
    return SimpleNamespace(
        runtime_context=ctx,
        tools=[],
        valid_tool_names=set(),
        enabled_toolsets=None,
    )


def _without_aws_deep_memory_env():
    return patch.dict(
        os.environ,
        {k: v for k, v in os.environ.items() if k != "AGENTOPS_DEEP_MEMORY_DB_URL"},
        clear=True,
    )


def test_aws_managed_prefetch_yields_hints_for_ingested_record(tmp_path):
    """aws-managed binding + ingested record yields prefetch hints for same scope."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_pf.db"
        alice = _aws_ctx_pf(user_id="alice")
        agent_ns = _aws_agent(alice)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent_ns, {"agentops": {"deep_memory_db_url": db_url}})

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        backend = reg.get(BackendCapability.DEEP_MEMORY, alice)
        backend.upsert_record(
            alice,
            text="Alice magenta turboencabulator aws project discussion.",
            metadata={"type": "conversation_turn"},
            source="unit",
            source_uri="unit://aws-pf-1",
            record_kind="conversation_turn",
        )

        with use_runtime_context(alice):
            hints = deep_memory_prefetch_for_agent(agent_ns, "magenta turboencabulator aws")
        assert "Deep memory hints" in hints
        assert "turboencabulator" in hints.lower()
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_prefetch_empty_for_different_user(tmp_path):
    """aws-managed binding + ingested record yields empty hints for a different user."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_pf2.db"
        alice = _aws_ctx_pf(user_id="alice")
        bob = _aws_ctx_pf(user_id="bob", conversation_id="thread-bob")
        agent_alice = _aws_agent(alice)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent_alice, {"agentops": {"deep_memory_db_url": db_url}})

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        backend = reg.get(BackendCapability.DEEP_MEMORY, alice)
        backend.upsert_record(
            alice,
            text="Alice teal sprocket aws deployment.",
            metadata={"type": "conversation_turn"},
            source="unit",
            source_uri="unit://aws-pf-scope",
            record_kind="conversation_turn",
        )

        agent_bob = SimpleNamespace(runtime_context=bob)
        with use_runtime_context(bob):
            hints = deep_memory_prefetch_for_agent(agent_bob, "teal sprocket aws deployment")
        assert hints == "", "bob must not see alice's hints"
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()
