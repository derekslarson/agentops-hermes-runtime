from __future__ import annotations

import time

from agent.runtime_context import RuntimeContext
from agent.runtime_sessions import LocalSQLiteSessionBackend


def _context(user: str, conversation: str, *, run_id: str = "run-1") -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        user_id=user,
        conversation_id=conversation,
        agent_profile_id="default",
        project_id="agentops-runtime",
        run_id=run_id,
        backend_profile="local-multi",
    )


def test_local_sqlite_session_backend_wraps_sessiondb_lifecycle_and_transcript(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    context = _context("derek", "thread-1")

    session_id = backend.create_session(context, source="agentops-test", model="test-model")
    backend.append_message(context, {"role": "user", "content": "hello from Derek"})
    backend.append_message(
        context,
        {"role": "assistant", "content": "hi", "tool_calls": [{"id": "call-1", "function": {"name": "memory"}}]},
    )

    assert session_id.endswith("-thread-1")
    assert session_id != context.conversation_id
    assert [message["content"] for message in backend.read_messages(context)] == ["hello from Derek", "hi"]
    summary = backend.read_session(context)
    assert summary is not None
    assert summary["id"] == session_id
    assert summary["source"] == "agentops-test"
    assert summary["message_count"] == 2
    assert summary["tool_call_count"] == 1


def test_local_sqlite_session_backend_isolates_scoped_conversations_and_search(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    derek = _context("derek", "thread-1")
    alex = _context("alex", "thread-2")

    backend.create_session(derek)
    backend.create_session(alex)
    backend.append_message(derek, {"role": "user", "content": "derek private sentinel"})
    backend.append_message(alex, {"role": "user", "content": "alex private sentinel"})

    assert backend.read_messages(derek)[0]["content"] == "derek private sentinel"
    assert backend.read_messages(alex)[0]["content"] == "alex private sentinel"
    assert backend.search(derek, "alex private") == []
    derek_hits = backend.search(derek, "derek private")
    assert len(derek_hits) == 1
    assert derek_hits[0]["content"] == "derek private sentinel"


def test_local_sqlite_session_backend_isolates_same_conversation_id_across_runtime_scopes(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    alice = RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        user_id="alice",
        conversation_id="shared-thread-id",
        agent_profile_id="default",
        project_id="runtime",
        backend_profile="local-multi",
    )
    bob = RuntimeContext(
        mode="agentops",
        org_id="globex",
        workspace_id="slack-main",
        user_id="bob",
        conversation_id="shared-thread-id",
        agent_profile_id="default",
        project_id="runtime",
        backend_profile="local-multi",
    )

    alice_session = backend.create_session(alice)
    bob_session = backend.create_session(bob)
    backend.append_message(alice, {"role": "user", "content": "alice private sentinel"})
    backend.append_message(bob, {"role": "user", "content": "bob private sentinel"})

    assert alice_session != bob_session
    assert [message["content"] for message in backend.read_messages(alice)] == ["alice private sentinel"]
    assert [message["content"] for message in backend.read_messages(bob)] == ["bob private sentinel"]
    assert backend.search(alice, "bob private") == []
    assert backend.claim_turn_lock(alice, owner="worker-a", ttl_seconds=60) is True
    assert backend.claim_turn_lock(bob, owner="worker-b", ttl_seconds=60) is True


def test_local_sqlite_session_backend_prevents_payload_session_id_scope_bypass(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    derek = _context("derek", "thread-1")
    alex = _context("alex", "thread-2")
    alex_session = backend.create_session(alex)
    backend.create_session(derek)

    backend.append_message(derek, {"session_id": alex_session, "role": "user", "content": "cannot cross scope"})

    assert backend.read_messages(alex) == []
    scoped_messages = backend.read_messages(derek, session_id=alex_session)
    assert len(scoped_messages) == 1
    assert scoped_messages[0]["content"] == "cannot cross scope"


def test_local_sqlite_session_backend_prevents_same_scope_scoped_id_conversation_bypass(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    first = _context("derek", "thread-1")
    second = _context("derek", "thread-2")
    first_session = backend.create_session(first)
    backend.create_session(second)
    backend.append_message(first, {"role": "user", "content": "thread one private sentinel"})

    assert backend.read_messages(second) == []
    assert backend.read_messages(second, session_id=first_session) == []


def test_local_sqlite_session_backend_resume_lineage_survives_reopen(tmp_path):
    db_path = tmp_path / "state.db"
    parent_context = _context("derek", "parent-thread", run_id="run-parent")
    child_context = _context("derek", "child-thread", run_id="run-child")

    first_backend = LocalSQLiteSessionBackend(db_path=db_path)
    parent_id = first_backend.create_session(parent_context)
    child_id = first_backend.create_session(child_context, parent_session_id=parent_id)
    first_backend.append_message(child_context, {"role": "user", "content": "resumed on another worker"})
    first_backend.end_session(parent_context, "compression")
    first_backend.close()

    restarted_backend = LocalSQLiteSessionBackend(db_path=db_path)

    assert restarted_backend.resolve_resume_session_id(parent_id) == child_id
    assert restarted_backend.read_messages(child_context)[0]["content"] == "resumed on another worker"


def test_local_sqlite_session_backend_turn_lock_is_context_scoped(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    derek = _context("derek", "thread-1")
    alex = _context("alex", "thread-2")

    assert backend.claim_turn_lock(derek, owner="worker-a", ttl_seconds=60) is True
    assert backend.claim_turn_lock(derek, owner="worker-b", ttl_seconds=60) is False
    assert backend.claim_turn_lock(alex, owner="worker-b", ttl_seconds=60) is True

    assert backend.renew_turn_lock(derek, owner="worker-a", ttl_seconds=60) is True
    assert backend.renew_turn_lock(derek, owner="worker-b", ttl_seconds=60) is False
    backend.release_turn_lock(derek, owner="worker-a")
    assert backend.claim_turn_lock(derek, owner="worker-b", ttl_seconds=60) is True


def test_local_sqlite_session_backend_expired_turn_lock_cannot_be_renewed(tmp_path):
    backend = LocalSQLiteSessionBackend(db_path=tmp_path / "state.db")
    context = _context("derek", "thread-1")

    assert backend.claim_turn_lock(context, owner="worker-a", ttl_seconds=0.01) is True
    time.sleep(0.02)

    assert backend.renew_turn_lock(context, owner="worker-a", ttl_seconds=60) is False
    assert backend.claim_turn_lock(context, owner="worker-b", ttl_seconds=60) is True
