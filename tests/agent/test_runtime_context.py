import json
from types import MappingProxyType

import pytest

from agent.runtime_context import (
    RuntimeContext,
    build_local_runtime_context,
    current_runtime_context,
    get_current_runtime_context,
    resolve_runtime_context,
    use_runtime_context,
)


def test_absent_agentops_context_defaults_to_local_compatibility(monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_CONTEXT", raising=False)

    ctx = resolve_runtime_context(
        profile_id="default",
        session_id="sess-1",
        platform="cli",
    )

    assert ctx.mode == "local"
    assert ctx.agent_profile_id == "default"
    assert ctx.conversation_id == "sess-1"
    assert ctx.workspace_type == "cli"
    assert ctx.run_type == "conversation"


def test_runtime_context_loads_from_env_json(monkeypatch):
    monkeypatch.setenv(
        "HERMES_RUNTIME_CONTEXT",
        json.dumps(
            {
                "mode": "agentops",
                "org_id": "org-a",
                "workspace_id": "ws-a",
                "workspace_type": "slack",
                "user_id": "user-a",
                "conversation_id": "thread-a",
                "run_id": "run-a",
                "run_type": "event",
                "backend_profile": "compose-self-hosted",
                "metadata": {"source": "env"},
            }
        ),
    )

    ctx = resolve_runtime_context(profile_id="ignored", session_id="ignored", platform="cli")

    assert ctx == RuntimeContext(
        mode="agentops",
        org_id="org-a",
        workspace_id="ws-a",
        workspace_type="slack",
        user_id="user-a",
        conversation_id="thread-a",
        run_id="run-a",
        run_type="event",
        backend_profile="compose-self-hosted",
        metadata={"source": "env"},
    )


def test_runtime_context_precedence_explicit_then_work_item_then_config_then_env(monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_CONTEXT", json.dumps({"mode": "agentops", "org_id": "env"}))
    config = {"agentops": {"runtime_context": {"mode": "agentops", "org_id": "config"}}}
    work_item = {"runtime_context": {"mode": "agentops", "org_id": "work"}}
    explicit = {"mode": "agentops", "org_id": "explicit"}

    assert resolve_runtime_context(config=config).org_id == "config"
    assert resolve_runtime_context(config=config, work_item=work_item).org_id == "work"
    assert resolve_runtime_context(config=config, work_item=work_item, explicit=explicit).org_id == "explicit"


def test_malformed_work_item_or_config_runtime_context_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_RUNTIME_CONTEXT", raising=False)

    with pytest.raises(TypeError, match="work item runtime_context"):
        resolve_runtime_context(work_item={"runtime_context": "not-a-mapping"})

    with pytest.raises(TypeError, match="runtime_context config"):
        resolve_runtime_context(config={"runtime_context": "not-a-mapping"})

    with pytest.raises(TypeError, match="agentops.runtime_context"):
        resolve_runtime_context(config={"agentops": {"runtime_context": "not-a-mapping"}})


def test_runtime_contexts_do_not_share_mutable_metadata():
    first = RuntimeContext.from_mapping({"metadata": {"labels": ["one"]}})
    second = RuntimeContext.from_mapping({"metadata": {"labels": ["two"]}})

    first_snapshot = first.to_dict()
    first_snapshot["metadata"]["labels"].append("mutated")

    assert isinstance(first.metadata, MappingProxyType)
    assert first.to_dict()["metadata"] == {"labels": ["one"]}
    assert second.to_dict()["metadata"] == {"labels": ["two"]}


def test_runtime_context_metadata_is_immutable():
    ctx = RuntimeContext.from_mapping({"metadata": {"labels": ["one"]}})

    with pytest.raises(TypeError):
        ctx.metadata["other"] = "blocked"  # type: ignore[index]

    with pytest.raises(AttributeError):
        ctx.metadata["labels"].append("blocked")  # type: ignore[attr-defined]

    assert ctx.to_dict()["metadata"] == {"labels": ["one"]}


def test_contextvar_scopes_current_runtime_context():
    outer = build_local_runtime_context(profile_id="default", session_id="outer")
    inner = build_local_runtime_context(profile_id="default", session_id="inner")

    assert get_current_runtime_context() is None
    with use_runtime_context(outer):
        assert current_runtime_context() == outer
        with use_runtime_context(inner):
            assert get_current_runtime_context() == inner
        assert get_current_runtime_context() == outer
    assert get_current_runtime_context() is None


def test_tool_dispatch_sees_bound_runtime_context(monkeypatch):
    import model_tools
    from model_tools import handle_function_call

    ctx = RuntimeContext(mode="agentops", org_id="org-tool", conversation_id="conv-tool")

    monkeypatch.setattr(model_tools, "coerce_tool_args", lambda _name, args: args)
    monkeypatch.setattr(model_tools, "_AGENT_LOOP_TOOLS", set())
    monkeypatch.setattr(model_tools.registry, "get_schema", lambda _name: None)
    def _dispatch_with_context(*_args, **_kwargs):
        active = get_current_runtime_context()
        return json.dumps({"org": active.org_id if active else None})

    monkeypatch.setattr(model_tools.registry, "dispatch", _dispatch_with_context)

    result = json.loads(handle_function_call("dummy_tool", {}, runtime_context=ctx))

    assert result == {"org": "org-tool"}
    assert get_current_runtime_context() is None


def test_aiagent_initializes_distinct_runtime_context_instances(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.delenv("HERMES_RUNTIME_CONTEXT", raising=False)

    a = AIAgent(
        model="test-model",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        session_id="session-a",
        platform="cli",
        user_id="user-a",
    )
    b = AIAgent(
        model="test-model",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        session_id="session-b",
        platform="telegram",
        user_id="user-b",
    )

    assert a.runtime_context.conversation_id == "session-a"
    assert b.runtime_context.conversation_id == "session-b"
    assert a.runtime_context.workspace_type == "cli"
    assert b.runtime_context.workspace_type == "telegram"
    assert a.runtime_context is not b.runtime_context


def test_aiagent_generated_session_id_updates_local_runtime_context(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.delenv("HERMES_RUNTIME_CONTEXT", raising=False)

    agent = AIAgent(
        model="test-model",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        platform="cli",
    )

    assert getattr(agent, "runtime_context").mode == "local"
    assert getattr(agent, "runtime_context").conversation_id == getattr(agent, "session_id")


def test_aiagent_malformed_config_runtime_context_fails_closed(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.delenv("HERMES_RUNTIME_CONTEXT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"runtime_context": "not-a-mapping"},
    )

    with pytest.raises(TypeError, match="runtime_context config"):
        AIAgent(
            model="test-model",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            platform="cli",
        )
