"""RuntimeContext-scoped deep-memory backend: partitioning + fail-closed.

These tests exercise the M5A runtime layer that wraps the local flat deep-memory
store behind the pluggable ``DEEP_MEMORY`` backend capability:

* local single-user mode keeps the current single ``$HERMES_HOME/deep-memory``
  store (cross-session recall for the one user),
* local-multi mode partitions record storage/search by RuntimeContext so there
  is no cross-user/org/project/thread record leakage, and
* AgentOps/remote profiles fail closed (explicit error) rather than silently
  falling back to an unscoped local Chroma store.
"""
from __future__ import annotations

import pytest

from agent.runtime_backends import (
    BackendCapability,
    BackendSelectionError,
    LocalDeepMemoryBackend,
    MemoryRecordBackend,
    REQUIRED_CAPABILITIES,
    RuntimeBackendRegistry,
)
from agent.runtime_context import RuntimeContext


def _ingest(backend, context, *, text, user_facing=True):
    return backend.upsert_record(
        context,
        text=text,
        metadata={"type": "conversation_turn"},
        source="unit",
        source_uri=f"unit://{text[:12]}",
        record_kind="conversation_turn",
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


def test_deep_memory_is_not_a_required_core_capability():
    # Deep memory is an optional, additively-registered capability. It must NOT
    # be in the MVP-required contract set (otherwise AgentOps profiles would be
    # forced to provide a remote adapter for it instead of failing closed).
    assert BackendCapability.DEEP_MEMORY not in REQUIRED_CAPABILITIES


def test_local_default_backend_satisfies_record_protocol():
    registry = RuntimeBackendRegistry()
    backend = registry.get(BackendCapability.DEEP_MEMORY)
    assert isinstance(backend, LocalDeepMemoryBackend)
    assert isinstance(backend, MemoryRecordBackend)


def test_local_single_user_uses_one_shared_store(tmp_path):
    # partition=False: every context resolves to the SAME store rooted exactly at
    # the base dir, preserving current $HERMES_HOME/deep-memory behaviour.
    backend = LocalDeepMemoryBackend(base_dir=tmp_path / "deep-memory", partition=False)
    ctx_a = _ctx(user_id="alice", conversation_id="t1", backend_profile="local")
    ctx_b = _ctx(user_id="bob", conversation_id="t2", backend_profile="local")

    rec = _ingest(backend, ctx_a, text="The shared single-user store keeps periwinkle notes.")
    # A different context sees the same single-user store (no partitioning).
    assert backend.get_record(ctx_b, rec.id) is not None
    assert backend.search(ctx_b, "periwinkle notes", limit=3)
    # Stored exactly at the base dir (current path contract).
    assert (tmp_path / "deep-memory" / "chroma.sqlite3").exists()
    backend.close()


def test_local_multi_partitions_by_user(tmp_path):
    backend = LocalDeepMemoryBackend(base_dir=tmp_path / "deep-memory", partition=True)
    alice = _ctx(user_id="alice")
    bob = _ctx(user_id="bob")

    rec = _ingest(backend, alice, text="Alice's secret marmalade deployment runbook.")

    # Cross-user isolation: bob cannot search OR fetch alice's record by ID.
    assert backend.search(bob, "marmalade deployment runbook", limit=5) == []
    assert backend.get_record(bob, rec.id) is None
    assert backend.get_many(bob, [rec.id]) == []

    # Alice still sees her own record.
    assert backend.get_record(alice, rec.id) is not None
    assert backend.search(alice, "marmalade deployment runbook", limit=5)
    backend.close()


def test_local_multi_partitions_by_thread(tmp_path):
    backend = LocalDeepMemoryBackend(base_dir=tmp_path / "deep-memory", partition=True)
    thread_a = _ctx(conversation_id="thread-a")
    thread_b = _ctx(conversation_id="thread-b")

    rec = _ingest(backend, thread_a, text="Thread-a private chartreuse handshake details.")

    # Same user/org/project but a different thread cannot reach thread-a's record.
    assert backend.get_record(thread_b, rec.id) is None
    assert backend.search(thread_b, "chartreuse handshake", limit=5) == []
    assert backend.get_record(thread_a, rec.id) is not None
    backend.close()


def test_local_multi_isolates_org_and_project(tmp_path):
    backend = LocalDeepMemoryBackend(base_dir=tmp_path / "deep-memory", partition=True)
    org_a = _ctx(org_id="org-a")
    org_b = _ctx(org_id="org-b")
    rec = _ingest(backend, org_a, text="Org-a confidential vermilion ledger entry.")
    assert backend.get_record(org_b, rec.id) is None
    assert backend.search(org_b, "vermilion ledger", limit=5) == []
    backend.close()


def test_agentops_profile_fails_closed_without_remote_adapter():
    # No remote deep-memory adapter registered: an AgentOps/remote profile must
    # raise an explicit selection error rather than silently using an unscoped
    # local Chroma store.
    registry = RuntimeBackendRegistry()
    ctx = _ctx(backend_profile="agentops")
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_agentops_without_profile_or_default_fails_closed():
    # Regression for the fail-open blocker: an AgentOps context with NO
    # backend_profile AND no registry-wide default_profile must still fail
    # closed for DEEP_MEMORY. Previously resolve_profile fell back to the
    # built-in "local" compatibility profile, which DOES have a DEEP_MEMORY
    # factory, so the registry silently returned an unscoped LocalDeepMemory
    # store instead of raising.
    registry = RuntimeBackendRegistry()
    ctx = RuntimeContext(mode="agentops", org_id="acme", user_id="alice")
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_local_mode_without_profile_keeps_local_backend():
    # The fail-closed guard must NOT disturb the local single-user path: a
    # local-mode context with no explicit profile still resolves the built-in
    # local deep-memory backend.
    registry = RuntimeBackendRegistry()
    ctx = RuntimeContext(mode="local")
    backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
    assert isinstance(backend, LocalDeepMemoryBackend)


def test_default_profile_agentops_also_fails_closed():
    registry = RuntimeBackendRegistry(
        {"backends": {"default_profile": "compose-self-hosted"}}
    )
    # Context carries no backend_profile, so the registry-wide default applies.
    ctx = RuntimeContext(mode="agentops", org_id="acme", user_id="alice")
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_agentops_explicit_local_backend_profile_fails_closed():
    # Explicit selection path 1: context.backend_profile == "local". A non-local
    # context must NOT resolve to the unpartitioned built-in local store.
    registry = RuntimeBackendRegistry()
    ctx = RuntimeContext(
        mode="agentops", org_id="acme", user_id="alice", backend_profile="local"
    )
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_agentops_config_default_profile_local_fails_closed():
    # Explicit selection path 2: config backends.default_profile == "local".
    registry = RuntimeBackendRegistry({"backends": {"default_profile": "local"}})
    ctx = RuntimeContext(mode="agentops", org_id="acme", user_id="alice")
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_agentops_capability_override_local_fails_closed():
    # Explicit selection path 3: config backends.capabilities.deep_memory ==
    # "local" (per-capability override wins over a safe backend_profile).
    registry = RuntimeBackendRegistry(
        {"backends": {"capabilities": {"deep_memory": "local"}}}
    )
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        backend_profile="compose-self-hosted",
    )
    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.DEEP_MEMORY, ctx)


def test_agentops_local_multi_profile_is_not_rejected():
    # The fail-closed guard must reject only "local", never "local-multi": a
    # non-local context with the scoped local-multi profile still resolves the
    # partitioning backend.
    registry = RuntimeBackendRegistry()
    ctx = RuntimeContext(
        mode="agentops", org_id="acme", user_id="alice", backend_profile="local-multi"
    )
    backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
    assert isinstance(backend, LocalDeepMemoryBackend)


def test_local_mode_explicit_local_profile_keeps_local_backend():
    # A genuine local-mode context that explicitly selects "local" is preserved.
    registry = RuntimeBackendRegistry()
    ctx = RuntimeContext(mode="local", backend_profile="local")
    backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
    assert isinstance(backend, LocalDeepMemoryBackend)


def test_builtin_local_never_bound_via_mode_adapter_for_non_local():
    # Even with a DEEP_MEMORY adapter registered under the deployment mode
    # ("agentops"), selecting the built-in unpartitioned "local" backend for a
    # non-local context must fail closed UNCONDITIONALLY — redirecting "local" to
    # context.mode is insufficient because a mode adapter would then be silently
    # substituted. Covers all three explicit-local paths plus the implicit
    # fallback; the mode adapter must never be bound by any of them.
    from agent.runtime_backends import (
        apply_deep_memory_adapters,
        clear_deep_memory_adapters,
        register_deep_memory_adapter,
    )

    class _FakeModeAdapter:
        def upsert_record(self, *a, **k):
            return None

        def search(self, *a, **k):
            return []

        def get_record(self, *a, **k):
            return None

        def get_many(self, *a, **k):
            return []

    selections = [
        ({}, dict(backend_profile="local")),  # 1) ctx.backend_profile == "local"
        ({"default_profile": "local"}, dict()),  # 2) config default_profile == "local"
        (  # 3) per-capability override == "local"
            {"capabilities": {"deep_memory": "local"}},
            dict(backend_profile="compose-self-hosted"),
        ),
        ({}, dict()),  # 4) implicit fallback to built-in "local"
    ]
    register_deep_memory_adapter("agentops", lambda options: _FakeModeAdapter())
    try:
        for backends_cfg, ctx_kwargs in selections:
            registry = RuntimeBackendRegistry({"backends": backends_cfg})
            apply_deep_memory_adapters(registry)  # mode adapter present under "agentops"
            ctx = RuntimeContext(
                mode="agentops", org_id="acme", user_id="alice", **ctx_kwargs
            )
            with pytest.raises(BackendSelectionError):
                registry.get(BackendCapability.DEEP_MEMORY, ctx)
    finally:
        clear_deep_memory_adapters()


def test_explicit_mode_profile_adapter_still_binds_for_non_local():
    # The fix must NOT block a legitimately registered adapter selected by an
    # explicit profile that happens to equal the mode string: when the context
    # explicitly selects profile "agentops", the registered adapter binds.
    from agent.runtime_backends import (
        apply_deep_memory_adapters,
        clear_deep_memory_adapters,
        register_deep_memory_adapter,
    )

    class _FakeModeAdapter:
        def upsert_record(self, *a, **k):
            return None

        def search(self, *a, **k):
            return []

        def get_record(self, *a, **k):
            return None

        def get_many(self, *a, **k):
            return []

    register_deep_memory_adapter("agentops", lambda options: _FakeModeAdapter())
    try:
        registry = RuntimeBackendRegistry()
        apply_deep_memory_adapters(registry)
        ctx = RuntimeContext(
            mode="agentops", org_id="acme", user_id="alice", backend_profile="agentops"
        )
        backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
        assert isinstance(backend, _FakeModeAdapter)
    finally:
        clear_deep_memory_adapters()


def test_remote_deep_memory_adapter_registers_behind_same_contract(tmp_path):
    # A compose/cloud profile can register a durable adapter behind the same
    # DEEP_MEMORY contract; selection then returns it instead of failing closed.
    registry = RuntimeBackendRegistry()

    class FakeRemoteDeepMemory:
        def __init__(self):
            self.records = {}

        def upsert_record(self, context, *, text, **kw):
            from agent.local_memory.store import MemoryRecord, stable_record_id, text_hash

            rid = stable_record_id(context.user_id, text)
            rec = MemoryRecord(id=rid, text=text, text_hash=text_hash(text))
            self.records[(context.user_id, rid)] = rec
            return rec

        def search(self, context, query, **kw):
            return []

        def get_record(self, context, record_id):
            return self.records.get((context.user_id, record_id))

        def get_many(self, context, ids):
            return [r for r in (self.get_record(context, i) for i in ids) if r]

    registry.register(
        BackendCapability.DEEP_MEMORY,
        lambda options: FakeRemoteDeepMemory(),
        profile="compose-self-hosted",
    )
    ctx = _ctx(backend_profile="compose-self-hosted")
    backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
    assert isinstance(backend, FakeRemoteDeepMemory)
    assert isinstance(backend, MemoryRecordBackend)


def test_registry_local_multi_profile_partitions(tmp_path):
    # The registry exposes a distinct ``local-multi`` profile that partitions by
    # RuntimeContext, selectable via context.backend_profile.
    registry = RuntimeBackendRegistry(
        {"backends": {"options": {"deep_memory": {"storage_dir": str(tmp_path / "store")}}}}
    )
    alice = _ctx(user_id="alice", backend_profile="local-multi")
    bob = _ctx(user_id="bob", backend_profile="local-multi")
    backend = registry.get(BackendCapability.DEEP_MEMORY, alice)
    rec = _ingest(backend, alice, text="Registry-routed amber telemetry note for alice.")
    # Same backend instance (cached per profile), partitioned internally by ctx.
    assert registry.get(BackendCapability.DEEP_MEMORY, bob) is backend
    assert backend.get_record(bob, rec.id) is None
    assert backend.get_record(alice, rec.id) is not None
    backend.close()
