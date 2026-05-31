"""Tests for the M2 backend contracts and runtime backend registry.

These prove the registry selects local-compatible backends by default,
honors RuntimeContext/config-driven profile selection, supports
fake/test backend injection, exposes every required capability, keeps
provider-specific names out of the core surface, and never shares mutable
state across isolated registry instances.
"""

import re
from pathlib import Path

import pytest

from agent.runtime_backends import (
    REQUIRED_CAPABILITIES,
    ArtifactBackend,
    AuditBackend,
    BackendCapability,
    BackendSelectionError,
    ConversationRouter,
    CredentialResolver,
    CronBackend,
    DeliveryBackend,
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
    LocalSkillBackend,
    LocalWorkerRegistry,
    MemoryBackend,
    QueueBackend,
    RunLeaseBackend,
    RuntimeBackendRegistry,
    SecretStore,
    SessionBackend,
    SkillBackend,
    WorkerRegistry,
)
from agent.runtime_context import RuntimeContext


_CAPABILITY_PROTOCOLS = {
    BackendCapability.MEMORY: MemoryBackend,
    BackendCapability.SKILL: SkillBackend,
    BackendCapability.SESSION: SessionBackend,
    BackendCapability.CRON: CronBackend,
    BackendCapability.CREDENTIAL: CredentialResolver,
    BackendCapability.SECRET: SecretStore,
    BackendCapability.QUEUE: QueueBackend,
    BackendCapability.RUN_LEASE: RunLeaseBackend,
    BackendCapability.CONVERSATION_ROUTER: ConversationRouter,
    BackendCapability.WORKER_REGISTRY: WorkerRegistry,
    BackendCapability.ARTIFACT: ArtifactBackend,
    BackendCapability.AUDIT: AuditBackend,
    BackendCapability.DELIVERY: DeliveryBackend,
}

_CAPABILITY_LOCAL_TYPES = {
    BackendCapability.MEMORY: LocalMemoryBackend,
    BackendCapability.SKILL: LocalSkillBackend,
    BackendCapability.SESSION: LocalSessionBackend,
    BackendCapability.CRON: LocalCronBackend,
    BackendCapability.CREDENTIAL: LocalCredentialResolver,
    BackendCapability.SECRET: LocalSecretStore,
    BackendCapability.QUEUE: LocalQueueBackend,
    BackendCapability.RUN_LEASE: LocalRunLeaseBackend,
    BackendCapability.CONVERSATION_ROUTER: LocalConversationRouter,
    BackendCapability.WORKER_REGISTRY: LocalWorkerRegistry,
    BackendCapability.ARTIFACT: LocalArtifactBackend,
    BackendCapability.AUDIT: LocalAuditBackend,
    BackendCapability.DELIVERY: LocalDeliveryBackend,
}


def test_required_capabilities_cover_the_full_contract_set():
    expected = {
        "memory",
        "skill",
        "session",
        "cron",
        "credential",
        "secret",
        "queue",
        "run_lease",
        "conversation_router",
        "worker_registry",
        "artifact",
        "audit",
        "delivery",
    }
    assert {capability.value for capability in REQUIRED_CAPABILITIES} == expected
    assert set(_CAPABILITY_LOCAL_TYPES) == set(REQUIRED_CAPABILITIES)


def test_local_defaults_resolve_local_backends_for_every_capability():
    registry = RuntimeBackendRegistry()

    for capability, local_type in _CAPABILITY_LOCAL_TYPES.items():
        backend = registry.get(capability)
        assert isinstance(backend, local_type)


def test_resolved_backends_satisfy_their_contract_protocols():
    registry = RuntimeBackendRegistry()

    for capability, protocol in _CAPABILITY_PROTOCOLS.items():
        backend = registry.get(capability)
        assert isinstance(backend, protocol)


def test_default_profile_is_local_without_context_or_config():
    registry = RuntimeBackendRegistry()

    for capability in REQUIRED_CAPABILITIES:
        assert registry.resolve_profile(capability, None) == "local"


def test_runtime_context_backend_profile_selects_registered_profile():
    registry = RuntimeBackendRegistry()

    class FakeMemoryBackend:
        def read(self, context, *, target="memory"):
            return None

        def write(self, context, content, *, target="memory", action="add"):
            return None

    registry.register(
        BackendCapability.MEMORY,
        lambda options: FakeMemoryBackend(),
        profile="fake-remote",
    )

    remote_context = RuntimeContext(mode="agentops", backend_profile="fake-remote")
    local_context = RuntimeContext(mode="local", backend_profile="local")

    remote_backend = registry.get(BackendCapability.MEMORY, remote_context)
    local_backend = registry.get(BackendCapability.MEMORY, local_context)

    assert isinstance(remote_backend, FakeMemoryBackend)
    assert isinstance(remote_backend, MemoryBackend)
    # The same registry serves the local backend when the context selects it.
    assert isinstance(local_backend, LocalMemoryBackend)


def test_config_default_profile_drives_selection():
    class FakeQueueBackend:
        def enqueue(self, context, payload, *, idempotency_key=None):
            return None

        def claim(self, context, *, capability=None):
            return None

        def ack(self, context, receipt):
            return None

        def nack(self, context, receipt, *, requeue=True):
            return None

        def extend_lease(self, context, receipt, *, seconds):
            return None

    registry = RuntimeBackendRegistry(config={"backends": {"default_profile": "fake"}})
    registry.register(
        BackendCapability.QUEUE,
        lambda options: FakeQueueBackend(),
        profile="fake",
    )

    assert isinstance(registry.get(BackendCapability.QUEUE), FakeQueueBackend)


def test_per_capability_config_override_beats_context_profile():
    class FakeMemoryBackend:
        def read(self, context, *, target="memory"):
            return None

        def write(self, context, content, *, target="memory", action="add"):
            return None

    registry = RuntimeBackendRegistry(
        config={"backends": {"capabilities": {"memory": "fake"}}}
    )
    registry.register(
        BackendCapability.MEMORY,
        lambda options: FakeMemoryBackend(),
        profile="fake",
    )

    context = RuntimeContext(mode="agentops", backend_profile="local")

    assert isinstance(registry.get(BackendCapability.MEMORY, context), FakeMemoryBackend)
    assert isinstance(registry.get(BackendCapability.SESSION, context), LocalSessionBackend)


def test_capability_options_are_passed_to_the_factory_without_context_binding():
    seen = {}

    def factory(options):
        seen["options"] = options
        return LocalMemoryBackend()

    registry = RuntimeBackendRegistry(
        config={"backends": {"options": {"memory": {"scope": "user"}}}}
    )
    registry.register(BackendCapability.MEMORY, factory, profile="local")

    registry.get(BackendCapability.MEMORY, RuntimeContext(mode="agentops", org_id="org-opts"))

    assert seen["options"] == {"scope": "user"}


def test_register_replaces_cached_backend_for_profile():
    registry = RuntimeBackendRegistry()
    original = registry.get(BackendCapability.MEMORY)

    registry.register(BackendCapability.MEMORY, lambda options: LocalMemoryBackend(), profile="local")
    replacement = registry.get(BackendCapability.MEMORY)

    assert replacement is not original


def test_unregistered_profile_fails_closed():
    registry = RuntimeBackendRegistry()
    context = RuntimeContext(mode="agentops", backend_profile="not-registered")

    with pytest.raises(BackendSelectionError):
        registry.get(BackendCapability.MEMORY, context)


def test_string_capability_names_are_accepted():
    registry = RuntimeBackendRegistry()

    assert isinstance(registry.get("memory"), LocalMemoryBackend)

    with pytest.raises(BackendSelectionError):
        registry.get("not-a-capability")


def test_backend_instances_are_cached_within_a_registry():
    registry = RuntimeBackendRegistry()

    first = registry.get(BackendCapability.MEMORY)
    second = registry.get(BackendCapability.MEMORY)

    assert first is second


def test_isolated_registries_do_not_share_mutable_state():
    registry_a = RuntimeBackendRegistry()
    registry_b = RuntimeBackendRegistry()

    context = RuntimeContext(mode="agentops", org_id="org-iso", conversation_id="conv-iso")

    memory_a = registry_a.get(BackendCapability.MEMORY, context)
    memory_b = registry_b.get(BackendCapability.MEMORY, context)

    assert memory_a is not memory_b

    memory_a.write(context, "registry-a-only")

    assert memory_a.read(context) == "registry-a-only"
    assert memory_b.read(context) is None


def test_local_backends_partition_state_by_runtime_context():
    registry = RuntimeBackendRegistry()
    derek = RuntimeContext(mode="agentops", org_id="acme", user_id="derek", conversation_id="thread-1")
    alex = RuntimeContext(mode="agentops", org_id="acme", user_id="alex", conversation_id="thread-2")

    memory = registry.get(BackendCapability.MEMORY, derek)
    memory.write(derek, "derek-only")
    memory.write(alex, "alex-only")
    assert memory.read(derek) == "derek-only"
    assert memory.read(alex) == "alex-only"

    sessions = registry.get(BackendCapability.SESSION, derek)
    sessions.append(derek, {"message": "derek"})
    sessions.append(alex, {"message": "alex"})
    assert sessions.read(derek) == [{"message": "derek"}]
    assert sessions.read(alex) == [{"message": "alex"}]
    assert sessions.read(derek, limit=0) == []

    artifacts = registry.get(BackendCapability.ARTIFACT, derek)
    artifacts.put(derek, "result", b"derek")
    artifacts.put(alex, "result", b"alex")
    assert artifacts.get(derek, "result") == b"derek"
    assert artifacts.get(alex, "result") == b"alex"


def test_local_audit_backend_redacts_sensitive_event_keys():
    registry = RuntimeBackendRegistry()
    context = RuntimeContext(mode="agentops", org_id="acme", conversation_id="audit")
    audit = registry.get(BackendCapability.AUDIT, context)

    audit.record(
        context,
        {
            "action": "credential_resolution",
            "api_key": "sentinel-secret",
            "nested": {"access_token": "sentinel-token", "safe": "kept"},
        },
    )

    stored = audit._events[(context.mode, context.org_id, context.workspace_id, context.user_id, context.conversation_id, context.agent_profile_id, context.project_id, context.run_id, context.job_id)][0]
    assert stored == {
        "action": "credential_resolution",
        "api_key": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]", "safe": "kept"},
    }


def test_no_provider_specific_names_leak_into_core_module():
    import agent.runtime_backends as backends_module

    source = Path(backends_module.__file__).read_text().lower()

    forbidden = [
        "aws",
        "gcp",
        "azure",
        "postgres",
        "postgresql",
        "dynamodb",
        "sqs",
        "sns",
        "rds",
        "ecs",
        "fargate",
        "cloudwatch",
        "eventbridge",
        "firestore",
        "pubsub",
        "cloud run",
        "cloudrun",
        "secrets manager",
        "secretsmanager",
        "bedrock",
        "vault",
        "redis",
        "minio",
        "sqlite",
    ]

    leaked = [token for token in forbidden if re.search(rf"\b{re.escape(token)}\b", source)]

    assert leaked == []
