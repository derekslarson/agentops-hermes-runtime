"""Native registration + agent-init binding for the scoped DEEP_MEMORY path (M5A).

These prove the M5A "native tool registration + agent-init binding" blocker:

* ``memory_record_search`` / ``memory_record_get`` / ``memory_record_get_many``
  are auto-registered into the native ``tools/registry`` discovery, but fail
  closed (and stay invisible) unless the scoped native deep-memory path is bound.
* ``_bind_deep_memory_backend`` leaves the MemoryManager provider path in charge
  for local single-user, binds the scoped backend + native tools for local-multi
  (and remote profiles that registered an adapter), and fails closed for
  AgentOps/remote profiles without an adapter — never opening an unscoped store
  and never double-registering the provider's tool names.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import tools.memory_record_tools as mrt
from agent.agent_init import _bind_deep_memory_backend
from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, use_runtime_context
from tools.registry import registry


MEMORY_RECORD_TOOL_NAMES = (
    "memory_record_search",
    "memory_record_get",
    "memory_record_get_many",
)


def _config(tmp_path):
    return {"backends": {"options": {"deep_memory": {"storage_dir": str(tmp_path / "store")}}}}


def _agent(ctx):
    return SimpleNamespace(
        runtime_context=ctx,
        tools=[],
        valid_tool_names=set(),
        enabled_toolsets=None,
    )


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


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


def test_record_tools_are_auto_registered_in_native_registry():
    # Importing the module must register the three native tool handlers into the
    # tools/registry so they are discoverable and dispatchable.
    for name in MEMORY_RECORD_TOOL_NAMES:
        assert registry.get_entry(name) is not None


def test_record_tool_dispatch_fails_closed_without_active_binding():
    mrt.clear_active_deep_memory_registry()
    out = json.loads(registry.dispatch("memory_record_search", {"query": "anything"}))
    assert "error" in out
    out = json.loads(registry.dispatch("memory_record_get", {"id": "mem_x"}))
    assert "error" in out


def test_skip_memory_scrubs_stale_native_record_tools_and_registry(tmp_path):
    from run_agent import AIAgent

    mrt.set_active_deep_memory_registry(RuntimeBackendRegistry(_config(tmp_path)))
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*MEMORY_RECORD_TOOL_NAMES)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["memory"],
        )

    assert mrt.get_active_deep_memory_registry() is None
    assert getattr(agent, "_scoped_deep_memory_registry", "sentinel") is None
    valid_tool_names = getattr(agent, "valid_tool_names")
    for name in MEMORY_RECORD_TOOL_NAMES:
        assert name not in valid_tool_names


def test_deep_memory_enabled_false_disables_scoped_native_binding(tmp_path):
    from run_agent import AIAgent

    ctx = _ctx(backend_profile="local-multi")
    cfg = {"deep_memory": {"enabled": False}, "backends": _config(tmp_path)["backends"]}
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*MEMORY_RECORD_TOOL_NAMES)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            runtime_context=ctx,
            enabled_toolsets=["memory"],
        )

    assert mrt.get_active_deep_memory_registry() is None
    assert getattr(agent, "_scoped_deep_memory_registry", "sentinel") is None
    valid_tool_names = getattr(agent, "valid_tool_names")
    for name in MEMORY_RECORD_TOOL_NAMES:
        assert name not in valid_tool_names


def test_local_single_user_keeps_provider_path_no_native_tools(tmp_path):
    mrt.clear_active_deep_memory_registry()
    agent = _agent(RuntimeContext(mode="local"))
    _bind_deep_memory_backend(agent, _config(tmp_path))
    # Provider path owns deep memory for local single-user: no scoped binding and
    # no native record tools injected (so they cannot double-register).
    assert mrt.get_active_deep_memory_registry() is None
    assert "memory_record_search" not in agent.valid_tool_names


def test_local_multi_binds_scoped_backend_and_injects_native_tools(tmp_path):
    mrt.clear_active_deep_memory_registry()
    ctx = _ctx()
    agent = _agent(ctx)
    _bind_deep_memory_backend(agent, _config(tmp_path))

    # Scoped binding active and the three tools injected into the agent surface.
    reg = mrt.get_active_deep_memory_registry()
    assert reg is not None
    for name in MEMORY_RECORD_TOOL_NAMES:
        assert name in agent.valid_tool_names

    # Ingest under alice through the bound registry, then prove scoped dispatch.
    alice = _ctx(user_id="alice")
    bob = _ctx(user_id="bob")
    backend = reg.get(BackendCapability.DEEP_MEMORY, alice)
    rec = backend.upsert_record(
        alice,
        text="Alice scoped magenta rollout verbatim record.",
        metadata={"type": "conversation_turn"},
        source="unit",
        source_uri="unit://alice-1",
        record_kind="conversation_turn",
    )

    with use_runtime_context(alice):
        got = json.loads(registry.dispatch("memory_record_get", {"id": rec.id}))
        assert "<memory-record>" in got["text"]
        assert "magenta rollout" in got["text"].lower()
    with use_runtime_context(bob):
        got = json.loads(registry.dispatch("memory_record_get", {"id": rec.id}))
        assert "error" in got
        search = json.loads(registry.dispatch("memory_record_search", {"query": "magenta rollout"}))
        assert search["results"] == []
    backend.close()


def test_agentops_without_adapter_fails_closed_no_binding(tmp_path):
    mrt.clear_active_deep_memory_registry()
    ctx = _ctx(backend_profile="agentops")
    agent = _agent(ctx)
    _bind_deep_memory_backend(agent, _config(tmp_path))
    # No adapter registered for the agentops profile: fail closed, no binding, no
    # native tools, and no fallback to an unscoped local store.
    assert mrt.get_active_deep_memory_registry() is None
    assert "memory_record_search" not in agent.valid_tool_names


def _preload_record_tool_schemas(agent):
    # Simulate native tool discovery having already loaded the memory_record_*
    # schemas into this agent's surface (their check_fn saw a stale active
    # registry left by a previous successful bind in the same process).
    from agent.local_memory.provider import RECORD_TOOL_SCHEMAS

    by_name = {s["name"]: s for s in RECORD_TOOL_SCHEMAS}
    for name in MEMORY_RECORD_TOOL_NAMES:
        agent.tools.append({"type": "function", "function": dict(by_name[name])})
        agent.valid_tool_names.add(name)


def test_remote_no_adapter_scrubs_stale_record_tools_after_prior_bind(tmp_path):
    # Long-lived process: a prior successful scoped bind left the module-global
    # active deep-memory registry set, and discovery already loaded the native
    # record tools onto THIS agent. A later AgentOps/remote no-adapter bind must
    # fail closed AND scrub the stale tools — never leave them exposed.
    from agent.runtime_backends import RuntimeBackendRegistry

    mrt.set_active_deep_memory_registry(RuntimeBackendRegistry(_config(tmp_path)))
    try:
        ctx = _ctx(backend_profile="agentops")  # no adapter registered
        agent = _agent(ctx)
        _preload_record_tool_schemas(agent)

        _bind_deep_memory_backend(agent, _config(tmp_path))

        assert mrt.get_active_deep_memory_registry() is None
        tool_names = {
            t.get("function", {}).get("name") for t in agent.tools if isinstance(t, dict)
        }
        for name in MEMORY_RECORD_TOOL_NAMES:
            assert name not in tool_names
            assert name not in agent.valid_tool_names
    finally:
        mrt.clear_active_deep_memory_registry()


def test_local_single_user_scrubs_stale_record_tools_after_prior_bind(tmp_path):
    # The local single-user provider path must also scrub native record tools
    # left exposed by stale global state, so the provider surface is not shadowed
    # by double-registered scoped tool names.
    from agent.runtime_backends import RuntimeBackendRegistry

    mrt.set_active_deep_memory_registry(RuntimeBackendRegistry(_config(tmp_path)))
    try:
        agent = _agent(RuntimeContext(mode="local"))
        _preload_record_tool_schemas(agent)

        _bind_deep_memory_backend(agent, _config(tmp_path))

        assert mrt.get_active_deep_memory_registry() is None
        tool_names = {
            t.get("function", {}).get("name") for t in agent.tools if isinstance(t, dict)
        }
        for name in MEMORY_RECORD_TOOL_NAMES:
            assert name not in tool_names
            assert name not in agent.valid_tool_names
    finally:
        mrt.clear_active_deep_memory_registry()


def test_local_single_user_preserves_provider_record_tools(tmp_path):
    # Local single-user keeps the MemoryManager LocalDeepMemoryProvider in charge:
    # its provider-owned memory_record_* schemas are legitimate and must SURVIVE
    # binding. The stale-tool scrub may only remove duplicates, leaving each
    # provider-owned record tool present exactly once.
    from agent.local_memory.provider import RECORD_TOOL_SCHEMAS
    from agent.runtime_backends import RuntimeBackendRegistry

    class _FakeMemoryManager:
        def get_all_tool_schemas(self):
            return [dict(s) for s in RECORD_TOOL_SCHEMAS]

    mrt.set_active_deep_memory_registry(RuntimeBackendRegistry(_config(tmp_path)))
    try:
        agent = _agent(RuntimeContext(mode="local"))
        agent._memory_manager = _FakeMemoryManager()
        # Stale discovery loaded the names before provider injection.
        _preload_record_tool_schemas(agent)

        _bind_deep_memory_backend(agent, _config(tmp_path))

        assert mrt.get_active_deep_memory_registry() is None
        names = [
            t.get("function", {}).get("name") for t in agent.tools if isinstance(t, dict)
        ]
        for name in MEMORY_RECORD_TOOL_NAMES:
            # Present as the provider-owned surface (not lost) ...
            assert name in names
            assert name in agent.valid_tool_names
            # ... and exactly once (no stale duplicate).
            assert names.count(name) == 1
    finally:
        mrt.clear_active_deep_memory_registry()


def test_remote_with_registered_adapter_binds_native_tools(tmp_path):
    # A non-local AgentOps context whose profile has a process-registered remote
    # DEEP_MEMORY adapter must bind the scoped backend + inject the native record
    # tools — WITHOUT opening an unscoped local store. This proves the adapter
    # registration hook is applied to the agent-init registry before resolution.
    from agent.runtime_backends import (
        LocalDeepMemoryBackend,
        clear_deep_memory_adapters,
        register_deep_memory_adapter,
    )

    class FakeRemoteDeepMemory:
        def __init__(self):
            self.records = {}

        def upsert_record(self, context, *, text, **kw):
            from agent.local_memory.store import MemoryRecord, stable_record_id, text_hash

            rid = stable_record_id(context.user_id, text)
            rec = MemoryRecord(id=rid, text=text, text_hash=text_hash(text))
            self.records[(context.org_id, context.user_id, rid)] = rec
            return rec

        def search(self, context, query, **kw):
            return []

        def get_record(self, context, record_id):
            return self.records.get((context.org_id, context.user_id, record_id))

        def get_many(self, context, ids):
            return [r for r in (self.get_record(context, i) for i in ids) if r]

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    register_deep_memory_adapter(
        "compose-self-hosted", lambda options: FakeRemoteDeepMemory()
    )
    try:
        ctx = _ctx(backend_profile="compose-self-hosted")
        agent = _agent(ctx)
        _bind_deep_memory_backend(agent, _config(tmp_path))

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        for name in MEMORY_RECORD_TOOL_NAMES:
            assert name in agent.valid_tool_names

        backend = reg.get(BackendCapability.DEEP_MEMORY, ctx)
        # The registered remote adapter is bound — NOT an unscoped local store.
        assert isinstance(backend, FakeRemoteDeepMemory)
        assert not isinstance(backend, LocalDeepMemoryBackend)
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_enabled_toolsets_excluding_memory_skips_injection(tmp_path):
    mrt.clear_active_deep_memory_registry()
    ctx = _ctx()
    agent = _agent(ctx)
    agent.enabled_toolsets = ["files"]  # memory intentionally excluded
    _bind_deep_memory_backend(agent, _config(tmp_path))
    assert "memory_record_search" not in agent.valid_tool_names


# ── M5B: aws-managed config/env-driven deep-memory binding ───────────────────


def _aws_ctx(**overrides):
    base = dict(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="thread-1",
        backend_profile="aws-managed",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def _aws_config(db_url):
    return {"agentops": {"deep_memory_db_url": db_url}}


def _without_aws_deep_memory_env():
    return patch.dict(
        os.environ,
        {k: v for k, v in os.environ.items() if k != "AGENTOPS_DEEP_MEMORY_DB_URL"},
        clear=True,
    )


def test_aws_managed_config_db_url_binds_relational_backend(tmp_path):
    """Configured aws-managed DB URL (config key) binds RelationalMemoryRecordBackend."""
    from agent.runtime_backends import clear_deep_memory_adapters
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_test.db"
        ctx = _aws_ctx()
        agent = _agent(ctx)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent, _aws_config(db_url))

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None, "registry must be bound for aws-managed with DB URL"
        backend = reg.get(BackendCapability.DEEP_MEMORY, ctx)
        assert isinstance(backend, RelationalMemoryRecordBackend)
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_env_db_url_binds_relational_backend(tmp_path):
    """Configured aws-managed DB URL (env var) binds RelationalMemoryRecordBackend."""
    from agent.runtime_backends import clear_deep_memory_adapters
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_env_test.db"
        ctx = _aws_ctx()
        agent = _agent(ctx)
        with patch.dict(os.environ, {"AGENTOPS_DEEP_MEMORY_DB_URL": db_url}, clear=True):
            _bind_deep_memory_backend(agent, {})

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        backend = reg.get(BackendCapability.DEEP_MEMORY, ctx)
        assert isinstance(backend, RelationalMemoryRecordBackend)
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_binds_native_tools(tmp_path):
    """Configured aws-managed binding injects memory_record_* tools into the agent surface."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_tools.db"
        ctx = _aws_ctx()
        agent = _agent(ctx)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent, _aws_config(db_url))

        for name in MEMORY_RECORD_TOOL_NAMES:
            assert name in agent.valid_tool_names, f"{name} must be injected for aws-managed"
    finally:
        try:
            reg = mrt.get_active_deep_memory_registry()
            if reg is not None:
                reg.get(BackendCapability.DEEP_MEMORY, _aws_ctx()).close()
        except Exception:
            pass
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_scoped_isolation_across_users(tmp_path):
    """Records ingested under one user/conversation are not visible to another."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        db_url = f"sqlite:///{tmp_path}/aws_scope.db"
        alice_ctx = _aws_ctx(user_id="alice", conversation_id="thread-alice")
        agent = _agent(alice_ctx)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent, _aws_config(db_url))

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        backend = reg.get(BackendCapability.DEEP_MEMORY, alice_ctx)
        rec = backend.upsert_record(
            alice_ctx,
            text="Alice azure sequoia private aws record.",
            metadata={"type": "conversation_turn"},
            source="unit",
            source_uri="unit://aws-scope-1",
            record_kind="conversation_turn",
        )

        bob_ctx = _aws_ctx(user_id="bob", conversation_id="thread-bob")
        assert backend.get_record(bob_ctx, rec.id) is None, "bob must not see alice's record"
        assert backend.search(bob_ctx, "azure sequoia private aws") == [], "bob must not search alice's record"
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_unconfigured_fails_closed_no_tools(tmp_path):
    """aws-managed profile with NO DB URL must fail closed: no binding, no native tools."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        ctx = _aws_ctx()
        agent = _agent(ctx)
        env_without_url = {k: v for k, v in os.environ.items() if k != "AGENTOPS_DEEP_MEMORY_DB_URL"}
        with patch.dict(os.environ, env_without_url, clear=True):
            _bind_deep_memory_backend(agent, {})

        assert mrt.get_active_deep_memory_registry() is None
        for name in MEMORY_RECORD_TOOL_NAMES:
            assert name not in agent.valid_tool_names
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_explicit_process_global_adapter_not_overwritten(tmp_path):
    """An explicit process-global aws-managed adapter must NOT be overwritten by the config seam."""
    from agent.runtime_backends import (
        clear_deep_memory_adapters,
        register_deep_memory_adapter,
    )

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        sentinel = object()

        def _sentinel_factory(options):
            return sentinel

        register_deep_memory_adapter("aws-managed", _sentinel_factory)

        db_url = f"sqlite:///{tmp_path}/aws_nowrote.db"
        ctx = _aws_ctx()
        agent = _agent(ctx)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent, _aws_config(db_url))

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        backend = reg.get(BackendCapability.DEEP_MEMORY, ctx)
        assert backend is sentinel, "explicit process-global adapter must not be overwritten"
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()


def test_aws_managed_secret_safety_dsn_not_in_factory_repr(tmp_path):
    """The raw DSN/password must not appear in the factory repr/defaults/closure."""
    from agent.runtime_backends import clear_deep_memory_adapters

    mrt.clear_active_deep_memory_registry()
    clear_deep_memory_adapters()
    try:
        sentinel_password = "SENTINEL_SECRET_PASS_12345"
        db_url = f"sqlite:///{tmp_path}/sec.db?dummy={sentinel_password}"
        ctx = _aws_ctx()
        agent = _agent(ctx)
        with _without_aws_deep_memory_env():
            _bind_deep_memory_backend(agent, {"agentops": {"deep_memory_db_url": db_url}})

        reg = mrt.get_active_deep_memory_registry()
        assert reg is not None
        cap_factories = getattr(reg, "_factories", {})
        dm_factories = cap_factories.get(BackendCapability.DEEP_MEMORY, {})
        factory = dm_factories.get("aws-managed")
        assert factory is not None, "factory must be registered"

        exposed = "\n".join(
            repr(value)
            for value in (
                factory,
                getattr(factory, "__defaults__", None),
                getattr(factory, "__kwdefaults__", None),
                getattr(factory, "__closure__", None),
                getattr(factory, "__dict__", None),
            )
        )
        assert sentinel_password not in exposed
        assert db_url not in exposed
    finally:
        clear_deep_memory_adapters()
        mrt.clear_active_deep_memory_registry()
