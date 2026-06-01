"""Tests for RuntimeContext-scoped credential resolution (M9)."""

import os
from types import SimpleNamespace

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, use_runtime_context
from agent.runtime_credentials import (
    CredentialResolutionError,
    RuntimeCredentialBroker,
    get_active_credential_broker,
    set_active_credential_broker,
)


def _ctx(user: str, *, run_id: str = "run-1") -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="eng",
        user_id=user,
        project_id="runtime",
        agent_profile_id="builder",
        run_id=run_id,
        backend_profile="local",
    )


def test_resolves_credential_by_capability_ref_without_exposing_secret_in_repr():
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    credentials = registry.get(BackendCapability.CREDENTIAL, context)
    secrets = registry.get(BackendCapability.SECRET, context)

    credentials.put_ref(context, "model:openrouter/default", "secret:model/openrouter/default")
    secrets.put_secret(context, "secret:model/openrouter/default", "sk-live-secret")

    handle = RuntimeCredentialBroker(registry).resolve(
        context,
        capability="model:openrouter",
        ref="default",
    )

    assert handle.reveal() == "sk-live-secret"
    assert handle.secret_ref == "secret:model/openrouter/default"
    assert "sk-live-secret" not in repr(handle)
    assert "sk-live-secret" not in str(handle)


def test_runtime_context_scopes_credential_resolution_by_user_and_not_run_id():
    registry = RuntimeBackendRegistry()
    derek_first = _ctx("derek", run_id="run-1")
    derek_second = _ctx("derek", run_id="run-2")
    alex = _ctx("alex", run_id="run-3")
    credentials = registry.get(BackendCapability.CREDENTIAL, derek_first)
    secrets = registry.get(BackendCapability.SECRET, derek_first)

    credentials.put_ref(derek_first, "tool:github/default", "secret:github/derek")
    secrets.put_secret(derek_first, "secret:github/derek", "ghp-derek")
    credentials.put_ref(alex, "tool:github/default", "secret:github/alex")
    secrets.put_secret(alex, "secret:github/alex", "ghp-alex")

    broker = RuntimeCredentialBroker(registry)

    assert broker.resolve(derek_second, capability="tool:github", ref="default").reveal() == "ghp-derek"
    assert broker.resolve(alex, capability="tool:github", ref="default").reveal() == "ghp-alex"


def test_runtime_context_scopes_credential_storage_by_backend_profile_and_permissions_ref():
    registry = RuntimeBackendRegistry()
    from agent.runtime_backends import LocalCredentialResolver, LocalSecretStore

    registry.register(BackendCapability.CREDENTIAL, lambda options: LocalCredentialResolver(), profile="compose")
    registry.register(BackendCapability.SECRET, lambda options: LocalSecretStore(), profile="compose")
    registry.register(BackendCapability.CREDENTIAL, lambda options: LocalCredentialResolver(), profile="aws")
    registry.register(BackendCapability.SECRET, lambda options: LocalSecretStore(), profile="aws")
    allow = RuntimeContext.from_mapping({**_ctx("derek").to_dict(), "backend_profile": "compose", "permissions_ref": "allow"})
    deny = RuntimeContext.from_mapping({**allow.to_dict(), "permissions_ref": "deny"})
    aws = RuntimeContext.from_mapping({**allow.to_dict(), "backend_profile": "aws"})
    credentials = registry.get(BackendCapability.CREDENTIAL, allow)
    secrets = registry.get(BackendCapability.SECRET, allow)
    credentials.put_ref(allow, "model:openrouter/default", "secret:openrouter/allow")
    secrets.put_secret(allow, "secret:openrouter/allow", "allow-token")
    broker = RuntimeCredentialBroker(registry)

    assert broker.resolve(allow, capability="model:openrouter", ref="default").reveal() == "allow-token"
    with pytest.raises(CredentialResolutionError, match="credential ref not available"):
        broker.resolve(deny, capability="model:openrouter", ref="default")
    with pytest.raises(CredentialResolutionError, match="credential ref not available"):
        broker.resolve(aws, capability="model:openrouter", ref="default")


def test_local_runtime_credential_resolution_can_read_existing_env_provider_secret(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-openrouter-token")
    registry = RuntimeBackendRegistry()
    context = RuntimeContext(mode="local", user_id="derek", project_id="runtime", backend_profile="local")

    handle = RuntimeCredentialBroker(registry).resolve(
        context,
        capability="model:openrouter",
        ref="default",
    )

    assert handle.secret_ref == "env:OPENROUTER_API_KEY"
    assert handle.reveal() == "local-openrouter-token"


def test_local_runtime_credential_resolution_can_read_existing_profile_provider_secret(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "agent.runtime_backends._load_local_auth_store",
        lambda: {"providers": {"openrouter": {"api_key": "profile-openrouter-token"}}},
    )
    registry = RuntimeBackendRegistry()
    context = RuntimeContext(mode="local", user_id="derek", project_id="runtime", backend_profile="local")

    handle = RuntimeCredentialBroker(registry).resolve(
        context,
        capability="model:openrouter",
        ref="default",
    )

    assert handle.secret_ref == "profile:openrouter:api_key"
    assert handle.reveal() == "profile-openrouter-token"


def test_resolution_audit_records_refs_and_metadata_never_secret_values():
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    credentials = registry.get(BackendCapability.CREDENTIAL, context)
    secrets = registry.get(BackendCapability.SECRET, context)
    audit = registry.get(BackendCapability.AUDIT, context)

    credentials.put_ref(context, "model:anthropic/default", "secret:model/anthropic/default")
    secrets.put_secret(context, "secret:model/anthropic/default", "sk-ant-secret")

    RuntimeCredentialBroker(registry).resolve(context, capability="model:anthropic", ref="default")

    stored_events = audit._events[(
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.conversation_id,
        context.agent_profile_id,
        context.project_id,
        context.run_id,
        context.job_id,
    )]
    assert stored_events == [
        {
            "action": "credential.resolve",
            "capability": "model:anthropic",
            "ref": "default",
            "resolved_ref": "secret:model/anthropic/default",
            "success": True,
        }
    ]
    assert "sk-ant-secret" not in repr(stored_events)


def test_secret_values_are_available_only_inside_scoped_env(monkeypatch):
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    credentials = registry.get(BackendCapability.CREDENTIAL, context)
    secrets = registry.get(BackendCapability.SECRET, context)
    credentials.put_ref(context, "tool:linear/default", "secret:linear/default")
    secrets.put_secret(context, "secret:linear/default", "lin-secret")
    monkeypatch.setenv("LINEAR_API_KEY", "outer")

    broker = RuntimeCredentialBroker(registry)

    assert os.environ["LINEAR_API_KEY"] == "outer"
    with broker.scoped_env(context, {"LINEAR_API_KEY": ("tool:linear", "default")}):
        assert os.environ["LINEAR_API_KEY"] == "lin-secret"
    assert os.environ["LINEAR_API_KEY"] == "outer"


def test_missing_ref_fails_closed_and_audit_still_omits_secret_values():
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    audit = registry.get(BackendCapability.AUDIT, context)

    with pytest.raises(CredentialResolutionError, match="credential ref not available"):
        RuntimeCredentialBroker(registry).resolve(context, capability="model:missing", ref="default")

    stored = audit._events[(
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.conversation_id,
        context.agent_profile_id,
        context.project_id,
        context.run_id,
        context.job_id,
    )][0]
    assert stored == {
        "action": "credential.resolve",
        "capability": "model:missing",
        "ref": "default",
        "resolved_ref": None,
        "success": False,
        "error": "credential ref not available",
    }


def test_credential_pool_seeds_provider_from_runtime_broker_without_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-env-token-must-not-win")
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    registry.get(BackendCapability.CREDENTIAL, context).put_ref(
        context,
        "model:openrouter/default",
        "secret:model/openrouter/default",
    )
    registry.get(BackendCapability.SECRET, context).put_secret(
        context,
        "secret:model/openrouter/default",
        "runtime-openrouter-token",
    )
    broker = RuntimeCredentialBroker(registry)
    set_active_credential_broker(broker, context=context)

    try:
        with use_runtime_context(context):
            from agent.credential_pool import load_pool

            pool = load_pool("openrouter")
            entry = pool.select()
    finally:
        set_active_credential_broker(None, context=context)

    assert entry is not None
    assert entry.source == "runtime:model:openrouter/default"
    assert entry.access_token == "runtime-openrouter-token"
    assert entry.base_url == "https://openrouter.ai/api/v1"
    auth_json = tmp_path / "hermes" / "auth.json"
    if auth_json.exists():
        assert "runtime-openrouter-token" not in auth_json.read_text()


def test_agentops_missing_runtime_credential_ref_does_not_fall_back_to_local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-env-token-must-not-leak")
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    set_active_credential_broker(RuntimeCredentialBroker(registry), context=context)

    try:
        with use_runtime_context(context):
            from agent.credential_pool import load_pool

            pool = load_pool("openrouter")
    finally:
        set_active_credential_broker(None, context=context)

    assert pool.select() is None


def test_active_credential_broker_lookup_is_stable_across_run_replacement():
    first = _ctx("derek", run_id="run-1")
    replacement = _ctx("derek", run_id="run-2")
    broker = RuntimeCredentialBroker(RuntimeBackendRegistry())
    set_active_credential_broker(broker, context=first)

    try:
        assert get_active_credential_broker(replacement) is broker
    finally:
        set_active_credential_broker(None, context=first)


def test_agent_init_binds_configured_agentops_credential_broker():
    from agent.agent_init import _bind_agentops_credential_broker

    context = _ctx("derek")
    set_active_credential_broker(None, context=context)
    agent = SimpleNamespace(runtime_context=context)

    try:
        _bind_agentops_credential_broker(agent, {})
        assert get_active_credential_broker(context) is not None
    finally:
        set_active_credential_broker(None, context=context)


def test_active_credential_broker_lookup_is_partitioned_by_backend_profile_and_permissions_ref():
    compose = _ctx("derek")
    compose = RuntimeContext.from_mapping({**compose.to_dict(), "backend_profile": "compose", "permissions_ref": "policy-a"})
    aws = RuntimeContext.from_mapping({**compose.to_dict(), "backend_profile": "aws", "permissions_ref": "policy-a"})
    restricted = RuntimeContext.from_mapping({**compose.to_dict(), "backend_profile": "compose", "permissions_ref": "policy-b"})
    broker = RuntimeCredentialBroker(RuntimeBackendRegistry())
    set_active_credential_broker(broker, context=compose)

    try:
        assert get_active_credential_broker(compose) is broker
        assert get_active_credential_broker(aws) is None
        assert get_active_credential_broker(restricted) is None
    finally:
        set_active_credential_broker(None, context=compose)


def test_agentops_load_pool_does_not_touch_local_sources_when_no_runtime_broker(monkeypatch):
    from agent.credential_pool import load_pool

    context = _ctx("derek")
    monkeypatch.setattr("agent.credential_pool.read_credential_pool", lambda provider: (_ for _ in ()).throw(AssertionError("local auth read")))
    monkeypatch.setattr("agent.credential_pool.write_credential_pool", lambda provider, entries: (_ for _ in ()).throw(AssertionError("local auth write")))

    with use_runtime_context(context):
        pool = load_pool("openrouter")

    assert pool.select() is None


def test_agentops_secret_store_rejects_ambient_env_and_profile_secret_refs(monkeypatch):
    registry = RuntimeBackendRegistry()
    context = _ctx("derek")
    secret_store = registry.get(BackendCapability.SECRET, context)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setattr(
        "agent.runtime_backends._load_local_auth_store",
        lambda: {"providers": {"openrouter": {"api_key": "profile-must-not-leak"}}},
    )

    assert secret_store.get_secret(context, "env:OPENROUTER_API_KEY") is None
    assert secret_store.get_secret(context, "profile:openrouter:api_key") is None


def test_agentops_runtime_provider_resolution_fails_closed_instead_of_falling_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.auth import AuthError
    from hermes_cli import runtime_provider

    context = _ctx("derek")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: {"provider": "openrouter"})

    with use_runtime_context(context):
        with pytest.raises(AuthError, match="AgentOps runtime credential"):
            runtime_provider.resolve_runtime_provider(requested="openrouter")


def test_agentops_runtime_provider_auto_does_not_use_local_provider_autodetection(monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli import runtime_provider

    context = _ctx("derek")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-select-provider")
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: {"provider": "auto"})
    monkeypatch.setattr(
        runtime_provider,
        "resolve_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local provider autodetection")),
    )

    with use_runtime_context(context):
        with pytest.raises(AuthError, match="AgentOps runtime credential"):
            runtime_provider.resolve_runtime_provider(requested="auto")


def test_agentops_runtime_provider_config_context_does_not_read_local_pool_sources(monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli import runtime_provider

    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setattr(
        runtime_provider,
        "load_config",
        lambda: {"runtime_context": _ctx("derek").to_dict(), "model": {"provider": "openrouter"}},
    )
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: {"provider": "openrouter"})
    monkeypatch.setattr("agent.credential_pool.read_credential_pool", lambda provider: (_ for _ in ()).throw(AssertionError("local auth read")))

    with pytest.raises(AuthError, match="AgentOps runtime credential"):
        runtime_provider.resolve_runtime_provider(requested="openrouter")


def test_agentops_runtime_pool_status_updates_do_not_persist_raw_runtime_secret(monkeypatch):
    from agent.credential_pool import CredentialPool, PooledCredential

    writes: list[object] = []
    monkeypatch.setattr("agent.credential_pool.write_credential_pool", lambda provider, entries: writes.append(entries))
    pool = CredentialPool(
        "openrouter",
        [
            PooledCredential.from_dict(
                "openrouter",
                {
                    "source": "runtime:model:openrouter/default",
                    "access_token": "runtime-secret-must-not-persist",
                    "base_url": "https://openrouter.ai/api/v1",
                },
            )
        ],
        persist=False,
    )

    pool.mark_exhausted_and_rotate(status_code=429)

    assert writes == []
