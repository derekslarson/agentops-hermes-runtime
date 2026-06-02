"""Tests for compose self-hosted service startup wiring (M12)."""

from __future__ import annotations

from agent.runtime_backends import RuntimeBackendRegistry
from agentops_runtime import compose_services


def _ready_env() -> dict[str, str]:
    return {
        "HERMES_RUNTIME_MODE": "agentops",
        "HERMES_BACKEND_PROFILE": "compose-self-hosted",
        "AGENTOPS_DATABASE_URL": "postgresql://agentops:pass@postgres:5432/agentops",
        "AGENTOPS_QUEUE_URL": "redis://redis:6379/0",
        "AGENTOPS_ARTIFACT_ENDPOINT": "http://minio:9000",
        "AGENTOPS_SECRET_STORE_URL": "http://local-secrets:8713",
        "AGENTOPS_API_URL": "http://api:8710",
    }


def test_worker_readiness_configures_compose_runtime_backends(monkeypatch):
    calls: list[tuple[RuntimeBackendRegistry, dict[str, str]]] = []

    def fake_configure(registry: RuntimeBackendRegistry, *, environ: dict[str, str]) -> None:
        calls.append((registry, environ))

    monkeypatch.setattr(compose_services, "configure_compose_runtime_backends", fake_configure, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")

    assert payload["ok"] is True
    assert payload["compose_backends_configured"] is True
    assert len(calls) == 1
    registry, environ = calls[0]
    assert isinstance(registry, RuntimeBackendRegistry)
    assert environ["AGENTOPS_API_URL"] == "http://api:8710"


def test_readiness_fails_closed_when_compose_backend_wiring_fails(monkeypatch):
    def fake_configure(registry: RuntimeBackendRegistry, *, environ: dict[str, str]) -> None:
        raise ValueError("compose backend wiring requires a control-plane base URL")

    monkeypatch.setattr(compose_services, "configure_compose_runtime_backends", fake_configure, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("scheduler")

    assert payload["ok"] is False
    assert payload["compose_backends_configured"] is False
    assert "compose backend wiring requires" in payload["backend_error"]
