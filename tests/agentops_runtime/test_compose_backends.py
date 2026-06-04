"""Tests for compose-self-hosted backend wiring (M12)."""

from __future__ import annotations

import pytest

from agent.runtime_artifacts_audit import HttpArtifactBackend, HttpAuditBackend
from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_cron_http import HttpCronBackend
from agent.runtime_memory_http import HttpMemoryBackend
from agent.runtime_memory_record_http import HttpMemoryRecordBackend
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
    assert isinstance(registry.get(BackendCapability.DEEP_MEMORY, context), HttpMemoryRecordBackend)
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


def test_compose_required_capabilities_equals_required_capabilities_plus_deep_memory():
    from agent.runtime_backends import REQUIRED_CAPABILITIES
    from agentops_runtime.compose_backends import COMPOSE_REQUIRED_CAPABILITIES

    assert COMPOSE_REQUIRED_CAPABILITIES == REQUIRED_CAPABILITIES | {BackendCapability.DEEP_MEMORY}
    assert BackendCapability.DEEP_MEMORY in COMPOSE_REQUIRED_CAPABILITIES


def test_missing_compose_capabilities_reports_nine_durable_surfaces_after_partial_wiring():
    from agentops_runtime.compose_backends import missing_compose_capabilities

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )
    missing = missing_compose_capabilities(registry)
    missing_values = {cap.value for cap in missing}
    assert missing_values == {
        "conversation_router", "credential", "delivery", "queue",
        "run_lease", "secret", "session", "skill", "worker_registry",
    }
    assert [cap.value for cap in missing] == sorted(missing_values)


def test_missing_compose_capabilities_result_contains_no_secrets_or_urls():
    from agentops_runtime.compose_backends import missing_compose_capabilities

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal:8710",
            "AGENTOPS_RUNTIME_TOKEN": "cp-secret-sentinel",
        },
    )
    missing = missing_compose_capabilities(registry)
    result_str = str(missing)
    for sensitive in ("cp-secret-sentinel", "api.internal", "8710", "password", "postgresql://"):
        assert sensitive not in result_str


def test_validate_compose_backend_registration_fails_closed_on_partial_registration():
    from agentops_runtime.compose_backends import validate_compose_backend_registration

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )
    with pytest.raises(ValueError) as exc_info:
        validate_compose_backend_registration(registry)
    error_msg = str(exc_info.value)
    assert "conversation_router" in error_msg


def test_validate_compose_backend_registration_error_exposes_only_capability_names():
    from agentops_runtime.compose_backends import validate_compose_backend_registration

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal:8710",
            "AGENTOPS_RUNTIME_TOKEN": "cp-secret-sentinel",
        },
    )
    with pytest.raises(ValueError) as exc_info:
        validate_compose_backend_registration(registry)
    error_msg = str(exc_info.value)
    for sensitive in (
        "cp-secret-sentinel",
        "api.internal",
        "8710",
        "postgresql://",
        "/Users/",
        "compose-self-hosted",
        "profile",
    ):
        assert sensitive not in error_msg, f"{sensitive!r} leaked into error"


def test_validate_compose_backend_registration_passes_after_full_registration():
    from agentops_runtime.compose_backends import (
        missing_compose_capabilities,
        validate_compose_backend_registration,
    )

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )
    for cap in missing_compose_capabilities(registry):
        registry.register(cap, lambda opts: object(), profile=_PROFILE)
    validate_compose_backend_registration(registry)
