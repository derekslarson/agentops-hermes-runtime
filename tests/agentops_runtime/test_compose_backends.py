"""Tests for compose-self-hosted backend wiring (M12)."""

from __future__ import annotations

import pytest

from agent.runtime_artifacts_audit import HttpArtifactBackend, HttpAuditBackend
from agent.runtime_backends import BackendCapability, LocalWorkerRegistry, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_conversation_router_http import HttpConversationRouter
from agent.runtime_credential_http import HttpCredentialResolver
from agent.runtime_cron_http import HttpCronBackend
from agent.runtime_delivery_http import HttpDeliveryBackend
from agent.runtime_memory_http import HttpMemoryBackend
from agent.runtime_memory_record_http import HttpMemoryRecordBackend
from agent.runtime_run_lease_http import HttpRunLeaseBackend
from agent.runtime_queue_http import HttpQueueBackend
from agent.runtime_secret_http import HttpSecretStoreBackend
from agent.runtime_session_http import HttpSessionBackend
from agent.runtime_worker_registry_http import HttpWorkerRegistry
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
    assert isinstance(registry.get(BackendCapability.CONVERSATION_ROUTER, context), HttpConversationRouter)
    assert isinstance(registry.get(BackendCapability.RUN_LEASE, context), HttpRunLeaseBackend)
    assert isinstance(registry.get(BackendCapability.WORKER_REGISTRY, context), HttpWorkerRegistry)
    assert isinstance(registry.get(BackendCapability.SESSION, context), HttpSessionBackend)
    assert isinstance(registry.get(BackendCapability.DELIVERY, context), HttpDeliveryBackend)
    assert isinstance(registry.get(BackendCapability.QUEUE, context), HttpQueueBackend)
    assert isinstance(registry.get(BackendCapability.SECRET, context), HttpSecretStoreBackend)
    assert isinstance(registry.get(BackendCapability.CREDENTIAL, context), HttpCredentialResolver)


def test_worker_registry_uses_compose_backend_for_supervisor_none_context():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )

    assert isinstance(registry.get(BackendCapability.WORKER_REGISTRY, None), HttpWorkerRegistry)


def test_worker_registry_context_profile_override_beats_compose_default_profile():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )
    context = RuntimeContext(mode="local", backend_profile="local")

    assert isinstance(registry.get(BackendCapability.WORKER_REGISTRY, context), LocalWorkerRegistry)


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


def test_conversation_router_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_CONVERSATION_ROUTER_URL": "https://router.internal",
        },
    )

    router_opts = registry._capability_options(BackendCapability.CONVERSATION_ROUTER)
    assert router_opts["base_url"] == "https://router.internal"


def test_conversation_router_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"conversation_router_url": "https://config-router.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_CONVERSATION_ROUTER_URL": "https://env-router.internal"},
    )

    router_opts = registry._capability_options(BackendCapability.CONVERSATION_ROUTER)
    assert router_opts["base_url"] == "https://config-router.internal"


def test_run_lease_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_RUN_LEASE_URL": "https://leases.internal",
        },
    )

    run_lease_opts = registry._capability_options(BackendCapability.RUN_LEASE)
    assert run_lease_opts["base_url"] == "https://leases.internal"


def test_run_lease_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"run_lease_url": "https://config-leases.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_RUN_LEASE_URL": "https://env-leases.internal"},
    )

    run_lease_opts = registry._capability_options(BackendCapability.RUN_LEASE)
    assert run_lease_opts["base_url"] == "https://config-leases.internal"


def test_worker_registry_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_WORKER_REGISTRY_URL": "https://workers.internal",
        },
    )

    worker_opts = registry._capability_options(BackendCapability.WORKER_REGISTRY)
    assert worker_opts["base_url"] == "https://workers.internal"


def test_session_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_SESSION_URL": "https://sessions.internal",
        },
    )

    session_opts = registry._capability_options(BackendCapability.SESSION)
    assert session_opts["base_url"] == "https://sessions.internal"


def test_session_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"session_url": "https://config-sessions.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_SESSION_URL": "https://env-sessions.internal"},
    )

    session_opts = registry._capability_options(BackendCapability.SESSION)
    assert session_opts["base_url"] == "https://config-sessions.internal"


def test_worker_registry_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"worker_registry_url": "https://config-workers.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_WORKER_REGISTRY_URL": "https://env-workers.internal"},
    )

    worker_opts = registry._capability_options(BackendCapability.WORKER_REGISTRY)
    assert worker_opts["base_url"] == "https://config-workers.internal"


def test_delivery_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_DELIVERY_URL": "https://delivery.internal",
        },
    )

    delivery_opts = registry._capability_options(BackendCapability.DELIVERY)
    assert delivery_opts["base_url"] == "https://delivery.internal"


def test_delivery_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"delivery_url": "https://config-delivery.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_DELIVERY_URL": "https://env-delivery.internal"},
    )

    delivery_opts = registry._capability_options(BackendCapability.DELIVERY)
    assert delivery_opts["base_url"] == "https://config-delivery.internal"


def test_queue_backend_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_QUEUE_BACKEND_URL": "https://queue.internal",
        },
    )

    queue_opts = registry._capability_options(BackendCapability.QUEUE)
    assert queue_opts["base_url"] == "https://queue.internal"


def test_queue_backend_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"queue_backend_url": "https://config-queue.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_QUEUE_BACKEND_URL": "https://env-queue.internal"},
    )

    queue_opts = registry._capability_options(BackendCapability.QUEUE)
    assert queue_opts["base_url"] == "https://config-queue.internal"


def test_agentops_queue_url_infra_dsn_ignored_for_http_adapter():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_QUEUE_URL": "redis://infra-queue.internal:6379",
        },
    )

    queue_opts = registry._capability_options(BackendCapability.QUEUE)
    assert queue_opts["base_url"] == "https://api.internal"


def test_credential_resolver_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_CREDENTIAL_RESOLVER_URL": "https://credentials.internal",
        },
    )

    credential_opts = registry._capability_options(BackendCapability.CREDENTIAL)
    assert credential_opts["base_url"] == "https://credentials.internal"


def test_credential_resolver_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"credential_resolver_url": "https://config-credentials.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_CREDENTIAL_RESOLVER_URL": "https://env-credentials.internal"},
    )

    credential_opts = registry._capability_options(BackendCapability.CREDENTIAL)
    assert credential_opts["base_url"] == "https://config-credentials.internal"


def test_secret_store_url_env_overrides_api_url():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        environ={
            "AGENTOPS_API_URL": "https://api.internal",
            "AGENTOPS_SECRET_STORE_URL": "https://secrets.internal",
        },
    )

    secret_opts = registry._capability_options(BackendCapability.SECRET)
    assert secret_opts["base_url"] == "https://secrets.internal"


def test_secret_store_config_key_overrides_env():
    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry,
        config={"agentops": {"secret_store_url": "https://config-secrets.internal"}},
        environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_SECRET_STORE_URL": "https://env-secrets.internal"},
    )

    secret_opts = registry._capability_options(BackendCapability.SECRET)
    assert secret_opts["base_url"] == "https://config-secrets.internal"


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
            registry, environ={"AGENTOPS_API_URL": "https://api.internal/?debug=1"}
        )
    with pytest.raises(ValueError):
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api.internal/#frag"}
        )


@pytest.mark.parametrize("url", ["https://api.internal?", "https://api.internal#"])
def test_fails_closed_when_url_contains_empty_query_or_fragment_delimiter(url):
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError, match="query or fragment"):
        configure_compose_runtime_backends(registry, environ={"AGENTOPS_API_URL": url})


def test_fails_closed_when_url_contains_invalid_port_without_retaining_raw_url():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api.internal:secret-port-sentinel"}
        )

    assert "secret-port-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fails_closed_when_url_contains_malformed_host_without_retaining_raw_url():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://[api-host-sentinel]"}
        )

    text = str(exc_info.value)
    assert "api-host-sentinel" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fails_closed_when_url_contains_whitespace_without_retaining_raw_url():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api internal/control"}
        )

    assert "api internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fails_closed_when_url_contains_del_control_without_retaining_raw_url():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry, environ={"AGENTOPS_API_URL": "https://api\x7finternal/control"}
        )

    assert "api\x7finternal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    "url",
    [" https://api.internal", "https://api.internal ", "\thttps://api.internal", "https://api.internal\n"],
)
def test_fails_closed_when_url_has_leading_or_trailing_whitespace_without_normalizing(url):
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(registry, environ={"AGENTOPS_API_URL": url})

    assert "api.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


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


def test_fails_closed_when_control_plane_token_contains_control_characters():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry,
            environ={
                "AGENTOPS_API_URL": "https://api.internal",
                "AGENTOPS_RUNTIME_TOKEN": "cp-secret-token\r\nX-Leak: yes",
            },
        )

    assert "cp-secret-token" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fails_closed_when_control_plane_token_has_trailing_control_character():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry,
            environ={
                "AGENTOPS_API_URL": "https://api.internal",
                "AGENTOPS_RUNTIME_TOKEN": "cp-secret-token\n",
            },
        )

    assert "cp-secret-token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fails_closed_when_control_plane_token_is_only_control_characters():
    registry = RuntimeBackendRegistry()
    with pytest.raises(ValueError) as exc_info:
        configure_compose_runtime_backends(
            registry,
            environ={"AGENTOPS_API_URL": "https://api.internal", "AGENTOPS_RUNTIME_TOKEN": "\n"},
        )

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


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
        BackendCapability.CONVERSATION_ROUTER,
        BackendCapability.RUN_LEASE,
        BackendCapability.WORKER_REGISTRY,
        BackendCapability.SESSION,
        BackendCapability.DELIVERY,
        BackendCapability.SECRET,
        BackendCapability.CREDENTIAL,
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


def test_missing_compose_capabilities_reports_only_skill_after_partial_wiring():
    from agentops_runtime.compose_backends import missing_compose_capabilities

    registry = RuntimeBackendRegistry()
    configure_compose_runtime_backends(
        registry, environ={"AGENTOPS_API_URL": "https://api.internal:8710"}
    )
    missing = missing_compose_capabilities(registry)
    missing_values = {cap.value for cap in missing}
    assert missing_values == {"skill"}
    assert "credential" not in missing_values
    assert "secret" not in missing_values
    assert "delivery" not in missing_values
    assert "session" not in missing_values
    assert "conversation_router" not in missing_values
    assert "run_lease" not in missing_values
    assert "worker_registry" not in missing_values
    assert "queue" not in missing_values
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
    assert "skill" in error_msg


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
