"""RED tests for M5B scoped deep-memory turn-ingest (completed-turn parity).

Proves that _sync_external_memory_for_turn routes completed user-facing turns
through the active scoped DEEP_MEMORY backend when one is registered, while
preserving all existing skip/guard semantics and preventing double-ingest.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_agent(
    *,
    runtime_context=None,
    memory_manager=None,
    session_id="sess-1",
    scoped_registry=None,
    deep_memory_config=None,
    platform=None,
):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = memory_manager
    setattr(agent, "_scoped_deep_memory_registry", scoped_registry)
    agent.session_id = session_id
    setattr(agent, "_deep_memory_config", deep_memory_config or {})
    setattr(agent, "platform", platform)
    agent.runtime_context = runtime_context or RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="thread-1",
        backend_profile="compose-self-hosted",
    )
    return agent


def _fake_registry():
    """Return a (registry, backend) pair where the backend is a MagicMock."""
    backend = MagicMock()
    backend.upsert_record.return_value = SimpleNamespace(id="mem_1", text="ok")
    registry = MagicMock(spec=RuntimeBackendRegistry)
    registry.get.return_value = backend
    return registry, backend


def _ctx(mode="agentops", user_id="alice", run_type="conversation", conversation_id="thread-1", **kw):
    return RuntimeContext(
        mode=mode,
        org_id="acme",
        user_id=user_id,
        conversation_id=conversation_id,
        backend_profile="compose-self-hosted",
        run_type=run_type,
        **kw,
    )


# ---------------------------------------------------------------------------
# Happy path: scoped backend receives a completed turn
# ---------------------------------------------------------------------------

def test_scoped_ingest_calls_upsert_record_on_completed_turn():
    """When a scoped registry is active, a completed turn calls backend.upsert_record()."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="What is the weather?",
            final_response="It is sunny.",
            interrupted=False,
        )

    backend.upsert_record.assert_called_once()
    call_kwargs = backend.upsert_record.call_args
    # First positional arg is context (RuntimeContext)
    assert isinstance(call_kwargs[0][0], RuntimeContext)
    # text must contain both user and assistant content
    text = call_kwargs[1]["text"]
    assert "What is the weather?" in text
    assert "It is sunny." in text


def test_scoped_ingest_passes_correct_scope_to_backend():
    """upsert_record receives the agent's runtime_context as first arg."""
    reg, backend = _fake_registry()
    ctx = _ctx(user_id="bob", conversation_id="thread-99")
    agent = _bare_agent(runtime_context=ctx, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Hello",
            final_response="Hi there.",
            interrupted=False,
        )

    backend.upsert_record.assert_called_once()
    ctx_arg = backend.upsert_record.call_args[0][0]
    assert ctx_arg.user_id == "bob"
    assert ctx_arg.conversation_id == "thread-99"


def test_scoped_ingest_source_is_conversation_turn():
    """upsert_record is called with source='conversation_turn'."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Tell me a joke.",
            final_response="Why did the chicken cross the road?",
            interrupted=False,
        )

    call_kwargs = backend.upsert_record.call_args[1]
    assert call_kwargs["source"] == "conversation_turn"


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

def test_scoped_ingest_skips_interrupted_turn():
    """Interrupted turns must NOT reach the scoped backend."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="What time is it?",
            final_response="It is 3pm.",
            interrupted=True,
        )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_skips_failed_turn():
    """Failed turns must NOT reach the scoped backend."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    agent._sync_external_memory_for_turn(
        original_user_message="What time is it?",
        final_response="Runtime error response",
        interrupted=False,
        failed=True,
    )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_skips_empty_user_message():
    """Empty/None user message must not be ingested."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    for msg in (None, "", "   "):
        backend.upsert_record.reset_mock()
        with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
            agent._sync_external_memory_for_turn(
                original_user_message=msg,
                final_response="Response here.",
                interrupted=False,
            )
        backend.upsert_record.assert_not_called()


def test_scoped_ingest_skips_empty_assistant_response():
    """Empty/None final_response must not be ingested."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    for resp in (None, "", "   "):
        backend.upsert_record.reset_mock()
        with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
            agent._sync_external_memory_for_turn(
                original_user_message="Hello",
                final_response=resp,
                interrupted=False,
            )
        backend.upsert_record.assert_not_called()


@pytest.mark.parametrize("run_type", ["cron", "delegation", "manual"])
def test_scoped_ingest_skips_non_user_facing_run_types(run_type):
    """Cron, delegation, and manual traces must not be ingested by default."""
    reg, backend = _fake_registry()
    ctx = _ctx(run_type=run_type)
    agent = _bare_agent(runtime_context=ctx, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Scheduled task output",
            final_response="Done.",
            interrupted=False,
        )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_allows_event_run_type():
    """Event run type is a user-facing turn and should be ingested."""
    reg, backend = _fake_registry()
    ctx = _ctx(run_type="event")
    agent = _bare_agent(runtime_context=ctx, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Incoming event",
            final_response="Processed.",
            interrupted=False,
        )

    backend.upsert_record.assert_called_once()


def test_scoped_ingest_respects_sync_turns_false():
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg, deep_memory_config={"sync_turns": False})

    agent._sync_external_memory_for_turn(
        original_user_message="Do not store this turn",
        final_response="Acknowledged.",
        interrupted=False,
    )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_respects_custom_ingest_run_types():
    reg, backend = _fake_registry()
    ctx = _ctx(run_type="conversation")
    agent = _bare_agent(
        runtime_context=ctx,
        scoped_registry=reg,
        deep_memory_config={"sync_turns": True, "ingest_run_types": ["event"]},
    )

    agent._sync_external_memory_for_turn(
        original_user_message="Conversation should be disabled by config",
        final_response="Acknowledged.",
        interrupted=False,
    )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_respects_sync_platforms():
    reg, backend = _fake_registry()
    agent = _bare_agent(
        scoped_registry=reg,
        platform="cli",
        deep_memory_config={"sync_turns": True, "sync_platforms": ["telegram"]},
    )

    agent._sync_external_memory_for_turn(
        original_user_message="CLI turn should be disabled by config",
        final_response="Acknowledged.",
        interrupted=False,
    )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_respects_deep_memory_enabled_false():
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg, deep_memory_config={"enabled": False})

    agent._sync_external_memory_for_turn(
        original_user_message="Deep memory disabled",
        final_response="Acknowledged.",
        interrupted=False,
    )

    backend.upsert_record.assert_not_called()


def test_scoped_ingest_no_op_when_registry_absent():
    """When no scoped registry is active, no-op (do not fall back to local)."""
    agent = _bare_agent()
    # No patch — get_active_deep_memory_registry returns None in default state
    from tools.memory_record_tools import clear_active_deep_memory_registry
    clear_active_deep_memory_registry()
    # Must not raise, must not touch any local store
    agent._sync_external_memory_for_turn(
        original_user_message="Hello",
        final_response="Hi.",
        interrupted=False,
    )


# ---------------------------------------------------------------------------
# Best-effort: backend exception must not block the response
# ---------------------------------------------------------------------------

def test_scoped_ingest_backend_exception_does_not_propagate():
    """Backend errors are best-effort — must not raise out of _sync_external_memory_for_turn."""
    reg, backend = _fake_registry()
    backend.upsert_record.side_effect = RuntimeError("backend unreachable")
    agent = _bare_agent(scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        # Must not raise.
        agent._sync_external_memory_for_turn(
            original_user_message="Hello",
            final_response="Hi.",
            interrupted=False,
        )

    backend.upsert_record.assert_called_once()


def test_scoped_ingest_redaction_failure_skips_without_raw_text_leak():
    """Redaction failures fail closed before raw text can reach the backend."""
    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=reg)

    with (
        patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg),
        patch("agent.local_memory.provider._redact", side_effect=RuntimeError("redactor down")),
    ):
        agent._sync_external_memory_for_turn(
            original_user_message="SENTINEL_FAKE_TOKEN_VALUE should not leak",
            final_response="I will not store it.",
            interrupted=False,
        )

    backend.upsert_record.assert_not_called()


# ---------------------------------------------------------------------------
# No double-ingest: local MemoryManager path stays separate
# ---------------------------------------------------------------------------

def test_no_double_ingest_local_memory_manager_path_unaffected():
    """When _memory_manager is set and no scoped registry is active, only _memory_manager is called.
    This proves the local single-user path does not double-ingest via the scoped path.
    """
    from tools.memory_record_tools import clear_active_deep_memory_registry
    clear_active_deep_memory_registry()

    mm = MagicMock()
    agent = _bare_agent(memory_manager=mm)

    # local mode, no scoped registry
    agent._sync_external_memory_for_turn(
        original_user_message="Local user says hi",
        final_response="Local response",
        interrupted=False,
    )

    mm.sync_all.assert_called_once()
    mm.queue_prefetch_all.assert_called_once()


def test_scoped_ingest_still_runs_when_remote_agent_has_memory_manager():
    """Remote/local-multi agents still ingest through their bound scoped backend."""
    reg, backend = _fake_registry()
    mm = MagicMock()
    agent = _bare_agent(memory_manager=mm, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Local user says hi",
            final_response="Local response",
            interrupted=False,
        )

    mm.sync_all.assert_called_once()
    mm.queue_prefetch_all.assert_called_once()
    backend.upsert_record.assert_called_once()


def test_scoped_ingest_ignores_stale_global_registry_when_agent_not_bound():
    """An agent without its own scoped binding must not use a stale global registry."""
    from tools.memory_record_tools import clear_active_deep_memory_registry, set_active_deep_memory_registry

    reg, backend = _fake_registry()
    agent = _bare_agent(scoped_registry=None)

    try:
        set_active_deep_memory_registry(reg)
        agent._sync_external_memory_for_turn(
            original_user_message="Hello from an unbound agent",
            final_response="No scoped ingest should happen.",
            interrupted=False,
        )
    finally:
        clear_active_deep_memory_registry()

    backend.upsert_record.assert_not_called()


def test_no_double_ingest_scoped_path_does_not_call_memory_manager():
    """When a scoped registry is active and _memory_manager is None, only the backend is called."""
    reg, backend = _fake_registry()
    agent = _bare_agent(memory_manager=None, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent._sync_external_memory_for_turn(
            original_user_message="Scoped user says hi",
            final_response="Scoped response",
            interrupted=False,
        )

    backend.upsert_record.assert_called_once()


# ---------------------------------------------------------------------------
# Tenant/scope separation
# ---------------------------------------------------------------------------

def test_tenant_scope_separation_for_ingest_path():
    """Two agents with different runtime_contexts call upsert_record with separate scope keys."""
    reg, backend = _fake_registry()

    ctx_alice = _ctx(user_id="alice", conversation_id="thread-alice")
    ctx_bob = _ctx(user_id="bob", conversation_id="thread-bob")
    agent_alice = _bare_agent(runtime_context=ctx_alice, scoped_registry=reg)
    agent_bob = _bare_agent(runtime_context=ctx_bob, scoped_registry=reg)

    with patch("tools.memory_record_tools.get_active_deep_memory_registry", return_value=reg):
        agent_alice._sync_external_memory_for_turn(
            original_user_message="Alice speaks",
            final_response="Response to Alice",
            interrupted=False,
        )
        agent_bob._sync_external_memory_for_turn(
            original_user_message="Bob speaks",
            final_response="Response to Bob",
            interrupted=False,
        )

    assert backend.upsert_record.call_count == 2
    calls = backend.upsert_record.call_args_list
    alice_ctx = calls[0][0][0]
    bob_ctx = calls[1][0][0]
    assert alice_ctx.user_id == "alice"
    assert bob_ctx.user_id == "bob"
    # Scopes are distinct
    assert alice_ctx.user_id != bob_ctx.user_id
    assert alice_ctx.conversation_id != bob_ctx.conversation_id


def test_tenant_scoped_backend_search_stays_scoped(tmp_path):
    """Proves get/search behavior remains scoped: alice's records not visible to bob."""
    from agent.runtime_backends import LocalDeepMemoryBackend

    backend = LocalDeepMemoryBackend(
        base_dir=str(tmp_path / "store"),
        partition=True,
    )

    alice = RuntimeContext(
        mode="local",
        org_id="acme",
        user_id="alice",
        conversation_id="thread-a",
    )
    bob = RuntimeContext(
        mode="local",
        org_id="acme",
        user_id="bob",
        conversation_id="thread-b",
    )

    rec = backend.upsert_record(
        alice,
        text="Alice private crimson record",
        source="test",
    )
    assert rec is not None

    # Bob cannot find alice's record by ID or search
    found = backend.get_record(bob, rec.id)
    assert found is None

    results = backend.search(bob, "crimson")
    assert results == []

    backend.close()
