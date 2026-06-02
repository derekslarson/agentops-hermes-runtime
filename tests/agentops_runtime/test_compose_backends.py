"""Tests for compose-self-hosted backend wiring (M12)."""

from __future__ import annotations

import pytest

from agent.runtime_artifacts_audit import HttpArtifactBackend, HttpAuditBackend
from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_cron_http import HttpCronBackend
from agent.runtime_memory_http import HttpMemoryBackend
from agentops_runtime.compose_backends import configure_compose_runtime_backends

_PROFILE = "compose-self-hosted"


def _context() -> RuntimeContext:
    return RuntimeContext(mode="agentops", backend_profile=_PROFILE)


def test_registers_http_backends_for_each_capability():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )

    context = _context()
    assert isinstance(registry.get(BackendCapability.MEMORY, context), HttpMemoryBackend)
    assert isinstance(registry.get(BackendCapability.CRON, context), HttpCronBackend)
    assert isinstance(registry.get(BackendCapability.ARTIFACT, context), HttpArtifactBackend)
    assert isinstance(registry.get(BackendCapability.AUDIT, context), HttpAuditBackend)


def test_per_capability_url_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_MEMORY_URL": "https://memory.internal",
        },
    )

    memory_opts = registry._capability_options(BackendCapability.MEMORY)
    cron_opts = registry._capability_options(BackendCapability.CRON)
    assert memory_opts["base_url"] == "https://memory.internal"
    assert cron_opts["base_url"] == "https://api.internal"


def test_fails_closed_when_api_url_missing():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError):
        configure_compose_runtime_backends(registry, environ={})


def test_fails_closed_when_url_contains_credentials():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError):
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://user:pass@api.internal"}
        )


def test_fails_closed_when_url_contains_query_or_fragment():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError):
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api.internal/?token=abc"}
        )
    with pytest.raises(ValueError):
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api.internal/#frag"}
        )


def test_control_plane_token_is_option_not_embedded_in_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_RUNTIME_TOKEN": "cp-secret-token",
        },
    )

    memory_opts = registry._capability_options(BackendCapability.MEMORY)
    assert memory_opts["token"] == "cp-secret-token"
    assert "cp-secret-token" not in memory_opts["base_url"]

    backend = registry.get(BackendCapability.MEMORY, _context())
    headers = backend._headers()
    assert headers["Authorization"] == "Bearer cp-secret-token"


def test_reconfiguring_without_token_clears_stale_token_option():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_RUNTIME_TOKEN": "old-token"},
    )

    configure_compose_runtime_backends(registry, environ={"AGENTOPS_API_URL": "https://api.internal"})

    assert "token" not in registry._capability_options(BackendCapability.MEMORY)


def test_app_and_integration_secrets_are_not_passed_into_options():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_RUNTIME_TOKEN": "cp-secret-token",
            "SLACK_BOT_TOKEN": "xoxb-app-secret",
            "OPENAI_API_KEY": "sk-app-secret",
        },
    )

    for capability in (
        BackendCapability.MEMORY,
        BackendCapability.CRON,
        BackendCapability.ARTIFACT,
        BackendCapability.AUDIT,
    ):
        options = registry._capability_options(capability)
        assert set(options) <= {"base_url", "token", "timeout"}
        assert "xoxb-app-secret" not in options.values()
        assert "sk-app-secret" not in options.values()


def test_config_url_overrides_environment():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"api_url": "https://config.internal"}},
        environ={"AGENTOPS_API_URL": "https://env.internal"},
    )

    assert registry._capability_options(BackendCapability.MEMORY)["base_url"] == "https://config.internal"
