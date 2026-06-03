"""Tests for compose self-hosted service startup wiring (M12)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

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


# ---------------------------------------------------------------------------
# Memory records routing (M5B)
# ---------------------------------------------------------------------------


class _FakeMemoryBackend:
    """Minimal MemoryRecordBackend stub for routing tests."""

    def upsert_record(self, context, *, text, **kwargs):
        from agent.local_memory.store import MemoryRecord
        import hashlib

        rid = kwargs.get("record_id") or "mem_test"
        return MemoryRecord(
            id=rid,
            text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
        )

    def search(self, context, query, **kwargs):
        return []

    def get_record(self, context, record_id):
        return None

    def get_many(self, context, ids):
        return []


@pytest.fixture
def _api_server():
    """Spin up a real compose api service server with a fake memory backend."""
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.memory_backend = _FakeMemoryBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _make_non_api_server(service_name: str) -> tuple[compose_services._Server, threading.Thread]:
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = service_name
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _http_get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _http_post(url: str, body: bytes, content_type: str = "application/json") -> int:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


_CONTEXT = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "user_id": "alice",
    "conversation_id": "conv1",
    "agent_profile_id": "bot",
    "project_id": "proj1",
}


def test_api_service_routes_memory_records_upsert(_api_server):
    body = json.dumps({"context": _CONTEXT, "record": {"text": "routing test record"}}).encode()
    status = _http_post(f"{_api_server}/memory/records", body)
    assert status == 200


def test_api_service_routes_memory_records_search(_api_server):
    import urllib.parse

    qs = urllib.parse.urlencode({"context": json.dumps(_CONTEXT), "query": "routing"})
    status = _http_get(f"{_api_server}/memory/records/search?{qs}")
    assert status == 200


def test_api_service_routes_memory_records_get(_api_server):
    import urllib.parse

    qs = urllib.parse.urlencode({"context": json.dumps(_CONTEXT), "id": "mem_x"})
    status = _http_get(f"{_api_server}/memory/records/get?{qs}")
    assert status == 200


def test_api_service_routes_memory_records_get_many(_api_server):
    body = json.dumps({"context": _CONTEXT, "ids": []}).encode()
    status = _http_post(f"{_api_server}/memory/records/get_many", body)
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_memory_records_upsert(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT, "record": {"text": "x"}}).encode()
        status = _http_post(f"{base}/memory/records", body)
        assert status == 404, f"{service_name} should return 404 for POST /memory/records"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_memory_records_search(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        import urllib.parse

        qs = urllib.parse.urlencode({"context": json.dumps(_CONTEXT), "query": "x"})
        status = _http_get(f"{base}/memory/records/search?{qs}")
        assert status == 404, f"{service_name} should return 404 for GET /memory/records/search"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_healthz_remains_open_on_api_service(_api_server):
    status = _http_get(f"{_api_server}/healthz")
    # 200 or 503 are both valid (depends on env); 404 would be wrong
    assert status in {200, 503}


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_healthz_remains_open_on_non_api_services(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status = _http_get(f"{base}/healthz")
        assert status in {200, 503}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_memory_records_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /memory/records/search?context=%7B%7D&query=sentinel-secret-query HTTP/1.1" 200 -'
    )

    assert "sentinel-secret-query" not in rendered
    assert "/memory/records/search" in rendered
    assert "<redacted>" in rendered
