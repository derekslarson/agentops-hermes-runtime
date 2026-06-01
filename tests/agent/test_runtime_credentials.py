"""Tests for RuntimeContext-scoped credential resolution (M9)."""

import os

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_credentials import CredentialResolutionError, RuntimeCredentialBroker


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
