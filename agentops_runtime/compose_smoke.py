"""Compose self-hosted smoke-test backend assembly helpers (M12).

The production Compose profile wires provider-neutral HTTP adapters from service
configuration.  These helpers build the same registry-selection shape with
shared in-process contract backends so focused tests can prove distributed
semantics without depending on Docker, network services, or cloud-specific code.
"""

from __future__ import annotations

from agent.runtime_backends import (
    BackendCapability,
    LocalArtifactBackend,
    LocalAuditBackend,
    LocalConversationRouter,
    LocalCredentialResolver,
    LocalCronBackend,
    LocalDeliveryBackend,
    LocalMemoryBackend,
    LocalQueueBackend,
    LocalRunLeaseBackend,
    LocalSecretStore,
    LocalSessionBackend,
    LocalWorkerRegistry,
    RuntimeBackendRegistry,
)
from agent.runtime_skills import ScopedSkillBackend

_COMPOSE_PROFILE = "compose-self-hosted"


def create_compose_smoke_registry() -> RuntimeBackendRegistry:
    """Return a registry whose Compose profile uses shared contract backends.

    The returned registry models multiple Compose worker/scheduler processes
    talking to the same durable control-plane stores: every capability is
    registered under ``compose-self-hosted`` with one shared backend instance.
    RuntimeContext still supplies tenant/user/thread scope on each backend call,
    so the smoke tests exercise the same isolation and profile-selection seams as
    the HTTP-backed Compose services while remaining hermetic and fast.
    """

    registry = RuntimeBackendRegistry()
    shared_backends = {
        BackendCapability.MEMORY: LocalMemoryBackend(),
        BackendCapability.SKILL: ScopedSkillBackend(),
        BackendCapability.SESSION: LocalSessionBackend(),
        BackendCapability.CRON: LocalCronBackend(),
        BackendCapability.CREDENTIAL: LocalCredentialResolver(),
        BackendCapability.SECRET: LocalSecretStore(),
        BackendCapability.QUEUE: LocalQueueBackend(),
        BackendCapability.RUN_LEASE: LocalRunLeaseBackend(),
        BackendCapability.CONVERSATION_ROUTER: LocalConversationRouter(),
        BackendCapability.WORKER_REGISTRY: LocalWorkerRegistry(),
        BackendCapability.ARTIFACT: LocalArtifactBackend(),
        BackendCapability.AUDIT: LocalAuditBackend(),
        BackendCapability.DELIVERY: LocalDeliveryBackend(),
    }
    for capability, backend in shared_backends.items():
        registry.register(capability, lambda _options, backend=backend: backend, profile=_COMPOSE_PROFILE)
    return registry


__all__ = ["create_compose_smoke_registry"]
