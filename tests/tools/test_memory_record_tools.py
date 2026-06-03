"""Scoped deep-memory record tools: tenant/user/thread isolation + fail-closed.

These prove the M5A acceptance criterion that ``memory_record_search`` /
``memory_record_get`` / ``memory_record_get_many`` return only scoped historical
data and cannot fetch another tenant/user/thread's record by ID — through the
tool dispatch layer, driven by the currently bound RuntimeContext (not just the
backend in isolation).
"""
from __future__ import annotations

import json

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, use_runtime_context
from tools.memory_record_tools import (
    deep_memory_prefetch_hints,
    memory_record_get,
    memory_record_get_many,
    memory_record_search,
)


def _registry(tmp_path):
    return RuntimeBackendRegistry(
        {"backends": {"options": {"deep_memory": {"storage_dir": str(tmp_path / "store")}}}}
    )


def _ctx(**overrides):
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-ws",
        user_id="alice",
        conversation_id="thread-1",
        agent_profile_id="support-bot",
        project_id="runtime-mvp",
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


def test_cross_user_record_get_is_isolated_by_bound_context(tmp_path):
    registry = _registry(tmp_path)
    alice = _ctx(user_id="alice")
    bob = _ctx(user_id="bob")
    rec = _ingest(registry, alice, "Alice's saffron rollout checklist verbatim.")

    # Bob (bound) cannot fetch alice's record by ID, even knowing the exact ID.
    with use_runtime_context(bob):
        got = json.loads(memory_record_get({"id": rec.id}, registry=registry))
        assert "error" in got
        many = json.loads(memory_record_get_many({"ids": [rec.id]}, registry=registry))
        assert many["records"] == []
        search = json.loads(memory_record_search({"query": "saffron rollout checklist", "limit": 5}, registry=registry))
        assert search["results"] == []

    # Alice (bound) gets her own fenced verbatim record.
    with use_runtime_context(alice):
        got = json.loads(memory_record_get({"id": rec.id}, registry=registry))
        assert "<memory-record>" in got["text"]
        assert "saffron rollout checklist" in got["text"].lower()


def test_cross_thread_record_get_is_isolated(tmp_path):
    registry = _registry(tmp_path)
    thread_a = _ctx(conversation_id="thread-a")
    thread_b = _ctx(conversation_id="thread-b")
    rec = _ingest(registry, thread_a, "Thread-a only cobalt incident timeline.")

    with use_runtime_context(thread_b):
        got = json.loads(memory_record_get({"id": rec.id}, registry=registry))
        assert "error" in got
    with use_runtime_context(thread_a):
        got = json.loads(memory_record_get({"id": rec.id}, registry=registry))
        assert "cobalt incident" in got["text"].lower()


def test_prefetch_hints_are_scoped_to_bound_context(tmp_path):
    registry = _registry(tmp_path)
    alice = _ctx(user_id="alice")
    bob = _ctx(user_id="bob")
    _ingest(registry, alice, "Alice indigo migration plan: ship the teal sprocket Tuesday.")

    with use_runtime_context(alice):
        hints = deep_memory_prefetch_hints("indigo migration teal sprocket", registry=registry)
        assert "Deep memory hints" in hints
        assert "teal sprocket" in hints.lower()
    # Automatic prefetch for bob must not surface alice's history.
    with use_runtime_context(bob):
        hints = deep_memory_prefetch_hints("indigo migration teal sprocket", registry=registry)
        assert hints == ""


def test_search_returns_bounded_excerpt_not_full_verbatim(tmp_path):
    # memory_record_search must expose only bounded excerpts, never the full
    # verbatim record (which could carry a secret-like tail). The full record is
    # only available, fenced, via memory_record_get / get_many.
    registry = _registry(tmp_path)
    alice = _ctx(user_id="alice")
    tail = "ZZTAILSECRETSENTINEL42"
    body = "alpenglow ridge survey notes " + ("padding token " * 500) + tail
    rec = _ingest(registry, alice, body)

    with use_runtime_context(alice):
        raw = memory_record_search({"query": "alpenglow ridge survey", "limit": 1}, registry=registry)
        search = json.loads(raw)
        assert search["results"], "record should be found"
        hit = search["results"][0]
        # The bounded excerpt is present; the full verbatim text / secret tail is not.
        assert hit["id"] == rec.id
        assert "excerpt" in hit and hit["excerpt"]
        assert tail not in raw
        assert "text" not in hit or tail not in str(hit.get("text"))

        # The full fenced record (with the tail) is still reachable by ID.
        got = json.loads(memory_record_get({"id": rec.id}, registry=registry))
        assert tail in got["text"]
        assert "<memory-record>" in got["text"]


def test_tools_fail_closed_for_agentops_profile_without_adapter(tmp_path):
    registry = _registry(tmp_path)
    # backend_profile=agentops has no registered deep-memory adapter -> the tool
    # must fail closed with an explicit error rather than touch local storage.
    ctx = _ctx(backend_profile="agentops")
    with use_runtime_context(ctx):
        got = json.loads(memory_record_get({"id": "mem_whatever"}, registry=registry))
        assert "error" in got
        search = json.loads(memory_record_search({"query": "anything"}, registry=registry))
        assert "error" in search
        hints = deep_memory_prefetch_hints("anything", registry=registry)
        assert hints == ""
