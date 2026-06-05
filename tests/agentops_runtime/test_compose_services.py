"""Tests for compose self-hosted service startup wiring (M12)."""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest

from agent.runtime_backends import RuntimeBackendRegistry
from agentops_runtime import compose_services


class _FailingPsycopg2:
    def connect(self, _db_url):
        raise RuntimeError("synthetic postgres connection denied")


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
    monkeypatch.setattr(compose_services, "validate_compose_backend_registration", lambda registry, **kwargs: None, raising=False)
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


def test_readiness_backend_error_redacts_unexpected_exception_details(monkeypatch):
    def fake_configure(registry: RuntimeBackendRegistry, *, environ: dict[str, str]) -> None:
        raise RuntimeError("token=cp-secret-sentinel path=/Users/derek/secret postgresql://db")

    monkeypatch.setattr(compose_services, "configure_compose_runtime_backends", fake_configure, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")

    assert payload["ok"] is False
    assert payload["compose_backends_configured"] is False
    assert payload["backend_error"] == "compose backend wiring failed"


def test_readiness_backend_error_redacts_safe_prefix_spoofing(monkeypatch):
    def fake_configure(registry: RuntimeBackendRegistry, *, environ: dict[str, str]) -> None:
        raise RuntimeError(
            "compose backend wiring requires a control-plane base URL "
            "token=cp-secret-sentinel path=/Users/derek/secret postgresql://db"
        )

    monkeypatch.setattr(compose_services, "configure_compose_runtime_backends", fake_configure, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")

    assert payload["backend_error"] == "compose backend wiring failed"


def test_readiness_backend_error_redacts_registration_prefix_spoofing(monkeypatch):
    def fake_configure(registry: RuntimeBackendRegistry, *, environ: dict[str, str]) -> None:
        raise RuntimeError(
            "compose backend registration incomplete; missing capabilities: cp_secret_sentinel"
        )

    monkeypatch.setattr(compose_services, "configure_compose_runtime_backends", fake_configure, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")

    assert payload["backend_error"] == "compose backend wiring failed"


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


# ---------------------------------------------------------------------------
# _make_memory_backend seam tests (M5B durable store selection)
# ---------------------------------------------------------------------------


def test_make_memory_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_DEEP_MEMORY_DB_URL"):
        compose_services._make_memory_backend({})


def test_make_memory_backend_selects_sqlite_via_db_url(tmp_path):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    db_path = str(tmp_path / "seam_test.db")
    backend = compose_services._make_memory_backend({
        "AGENTOPS_DEEP_MEMORY_DB_URL": f"sqlite:///{db_path}",
    })
    assert isinstance(backend, RelationalMemoryRecordBackend)


def test_make_memory_backend_selects_sqlite_via_store_env(tmp_path):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    db_path = str(tmp_path / "seam_store.db")
    backend = compose_services._make_memory_backend({
        "AGENTOPS_DEEP_MEMORY_STORE": "sqlite",
        "AGENTOPS_DEEP_MEMORY_DB_URL": f"sqlite:///{db_path}",
    })
    assert isinstance(backend, RelationalMemoryRecordBackend)


def test_make_memory_backend_fails_closed_on_postgres_url(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", _FailingPsycopg2())
    with pytest.raises(Exception):
        compose_services._make_memory_backend({
            "AGENTOPS_DEEP_MEMORY_DB_URL": "postgresql://user:***@localhost/db",
        })


def test_make_memory_backend_selects_postgres_via_fake_psycopg(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    class FakeCursor:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def execute(self, sql, params=None):
            self.connection.executed.append((sql, params))

    class FakeConnection:
        def __init__(self):
            self.executed = []

        def cursor(self):
            return FakeCursor(self)

        def commit(self):
            pass

    class FakePsycopg2:
        def __init__(self):
            self.connection = FakeConnection()

        def connect(self, _db_url):
            return self.connection

    monkeypatch.setitem(sys.modules, "psycopg2", FakePsycopg2())

    backend = compose_services._make_memory_backend({
        "AGENTOPS_DEEP_MEMORY_DB_URL": "postgresql://user:***@postgres:5432/deepmem",
    })

    assert isinstance(backend, RelationalMemoryRecordBackend)


def test_make_memory_backend_fails_closed_on_postgres_store_without_url():
    with pytest.raises(Exception):
        compose_services._make_memory_backend({"AGENTOPS_DEEP_MEMORY_STORE": "postgres"})


def test_make_memory_backend_fails_closed_on_postgres_store_even_with_sqlite_url(tmp_path):
    with pytest.raises(Exception):
        compose_services._make_memory_backend({
            "AGENTOPS_DEEP_MEMORY_STORE": "postgres",
            "AGENTOPS_DEEP_MEMORY_DB_URL": f"sqlite:///{tmp_path / 'records.db'}",
        })


def test_make_memory_backend_fails_closed_on_sqlite_store_without_explicit_url():
    with pytest.raises(ValueError, match="AGENTOPS_DEEP_MEMORY_DB_URL"):
        compose_services._make_memory_backend({"AGENTOPS_DEEP_MEMORY_STORE": "sqlite"})


def test_make_memory_backend_fails_closed_on_unknown_store_type():
    with pytest.raises(Exception):
        compose_services._make_memory_backend({"AGENTOPS_DEEP_MEMORY_STORE": "postgress"})


def test_make_memory_backend_fails_closed_on_unsupported_db_url():
    with pytest.raises(Exception):
        compose_services._make_memory_backend({"AGENTOPS_DEEP_MEMORY_DB_URL": "mysql://localhost/deepmemory"})


def test_make_memory_backend_postgres_error_does_not_leak_password(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", _FailingPsycopg2())
    sentinel_password = "LEAKSENTINEL123"
    with pytest.raises(Exception) as exc_info:
        compose_services._make_memory_backend({
            "AGENTOPS_DEEP_MEMORY_DB_URL": f"postgresql://user:***@localhost/db",
        })
    assert sentinel_password not in str(exc_info.value)


def test_get_or_create_memory_backend_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(compose_services, "_memory_backend_instance", None)
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError, match="AGENTOPS_DEEP_MEMORY_DB_URL"):
        compose_services._get_or_create_memory_backend()


# ---------------------------------------------------------------------------
# _health_payload api deep-memory requirement (M5B)
# ---------------------------------------------------------------------------


def test_api_readiness_requires_deep_memory_db_url(monkeypatch):
    env = {
        "HERMES_RUNTIME_MODE": "agentops",
        "HERMES_BACKEND_PROFILE": "compose-self-hosted",
        "AGENTOPS_DATABASE_URL": "postgresql://agentops:pass@postgres:5432/agentops",
        "AGENTOPS_QUEUE_URL": "redis://redis:6379/0",
        "AGENTOPS_ARTIFACT_ENDPOINT": "http://minio:9000",
        "AGENTOPS_SECRET_STORE_URL": "http://local-secrets:8713",
        "AGENTOPS_API_URL": "http://api:8710",
        # AGENTOPS_DEEP_MEMORY_DB_URL intentionally absent
    }
    monkeypatch.setattr(compose_services.os, "environ", env)
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    monkeypatch.setattr(
        compose_services,
        "validate_compose_backend_registration",
        lambda registry, **kwargs: None,
        raising=False,
    )

    api_payload = compose_services._health_payload("api")
    assert api_payload["ok"] is False
    assert "AGENTOPS_DEEP_MEMORY_DB_URL" in api_payload.get("missing", [])

    # worker and scheduler must NOT require AGENTOPS_DEEP_MEMORY_DB_URL
    worker_payload = compose_services._health_payload("worker")
    assert worker_payload["ok"] is True
    scheduler_payload = compose_services._health_payload("scheduler")
    assert scheduler_payload["ok"] is True


def test_api_readiness_treats_whitespace_deep_memory_db_url_as_missing(monkeypatch):
    env = {
        "HERMES_RUNTIME_MODE": "agentops",
        "HERMES_BACKEND_PROFILE": "compose-self-hosted",
        "AGENTOPS_DATABASE_URL": "postgresql://agentops:***@postgres:5432/agentops",
        "AGENTOPS_QUEUE_URL": "redis://redis:6379/0",
        "AGENTOPS_ARTIFACT_ENDPOINT": "http://minio:9000",
        "AGENTOPS_SECRET_STORE_URL": "http://local-secrets:8713",
        "AGENTOPS_API_URL": "http://api:8710",
        "AGENTOPS_DEEP_MEMORY_DB_URL": "   ",
    }
    monkeypatch.setattr(compose_services.os, "environ", env)
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )

    api_payload = compose_services._health_payload("api")
    assert api_payload["ok"] is False
    assert "AGENTOPS_DEEP_MEMORY_DB_URL" in api_payload.get("missing", [])


# ---------------------------------------------------------------------------
# _dispatch_memory_records 503 on backend failure (M5B)
# ---------------------------------------------------------------------------


def test_memory_records_request_returns_503_when_backend_unavailable(monkeypatch):
    def _raise_backend_error():
        raise ValueError("LEAKSENTINEL password=secret")

    monkeypatch.setattr(compose_services, "_get_or_create_memory_backend", _raise_backend_error)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    # memory_backend is None by default — forces call to _get_or_create_memory_backend
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT, "record": {"text": "test"}}).encode()
        req = urllib.request.Request(f"{base}/memory/records", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 503
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=secret" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# M5B: embed_fn injection via make_relational_memory_backend seam
# ---------------------------------------------------------------------------


def _make_fake_psycopg2():
    class _FakeCursor:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            self._conn.executed.append(sql)

    class _FakeConn:
        def __init__(self):
            self.executed = []

        def cursor(self):
            return _FakeCursor(self)

        def commit(self):
            pass

    class _FakePsycopg2:
        def __init__(self):
            self.connection = _FakeConn()

        def connect(self, _url):
            return self.connection

    return _FakePsycopg2()


def test_make_memory_backend_postgres_injects_resolved_embed_fn(monkeypatch):
    """postgres _make_memory_backend must inject the resolved default embed_fn."""
    import agentops_runtime.memory_record_store as mrs

    _SENTINEL_EMBED = object()

    def _fake_load(device="auto"):
        return _SENTINEL_EMBED

    monkeypatch.setattr(mrs, "_load_default_embed_fn", _fake_load)
    monkeypatch.setitem(sys.modules, "psycopg2", _make_fake_psycopg2())

    backend = compose_services._make_memory_backend({
        "AGENTOPS_DEEP_MEMORY_DB_URL": "postgresql://user:***@postgres:5432/deepmem",
    })

    assert backend._embed_fn is _SENTINEL_EMBED


def test_make_memory_backend_postgres_embedder_failure_raises_sanitized_error(monkeypatch):
    """postgres embedder load failure must raise sanitized RuntimeError with no chained cause."""
    import agentops_runtime.memory_record_store as mrs

    _LEAK_TEXT = "LEAKSENTINEL_EMBED password=secret"
    _LEAK_DSN = f"postgresql://user:{_LEAK_TEXT}@postgres:5432/deepmem"

    def _fake_load_fail(device="auto"):
        raise ImportError(f"chromadb missing: {_LEAK_TEXT}")

    monkeypatch.setattr(mrs, "_load_default_embed_fn", _fake_load_fail)

    with pytest.raises(RuntimeError) as exc_info:
        compose_services._make_memory_backend({"AGENTOPS_DEEP_MEMORY_DB_URL": _LEAK_DSN})

    error_msg = str(exc_info.value)
    assert _LEAK_TEXT not in error_msg
    assert _LEAK_DSN not in error_msg
    assert exc_info.value.__cause__ is None


def test_make_memory_backend_sqlite_does_not_load_embedder(monkeypatch, tmp_path):
    """sqlite _make_memory_backend must not call _load_default_embed_fn; _embed_fn stays None."""
    import agentops_runtime.memory_record_store as mrs

    embed_calls = []

    def _fake_load(device="auto"):
        embed_calls.append(device)
        raise RuntimeError("must not be called for sqlite")

    monkeypatch.setattr(mrs, "_load_default_embed_fn", _fake_load)

    db_path = str(tmp_path / "no_embed_test.db")
    backend = compose_services._make_memory_backend({
        "AGENTOPS_DEEP_MEMORY_DB_URL": f"sqlite:///{db_path}",
    })

    assert embed_calls == [], "sqlite must not invoke _load_default_embed_fn"
    assert backend._embed_fn is None


# ---------------------------------------------------------------------------
# M12B: Compose backend registration/readiness gate
# ---------------------------------------------------------------------------


def test_api_readiness_fails_closed_on_partial_compose_backend_registration(monkeypatch):
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("api")

    assert payload["ok"] is False
    assert payload["compose_backends_configured"] is False
    error = payload.get("backend_error", "")
    assert "conversation_router" in error


def test_worker_readiness_fails_closed_on_partial_compose_backend_registration(monkeypatch):
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")

    assert payload["ok"] is False
    assert payload["compose_backends_configured"] is False


def test_scheduler_readiness_fails_closed_on_partial_compose_backend_registration(monkeypatch):
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("scheduler")

    assert payload["ok"] is False
    assert payload["compose_backends_configured"] is False


def test_readiness_backend_error_exposes_only_capability_names_not_secrets(monkeypatch):
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    env = dict(_ready_env())
    env["AGENTOPS_RUNTIME_TOKEN"] = "cp-secret-sentinel"
    monkeypatch.setattr(compose_services.os, "environ", env)

    payload = compose_services._health_payload("worker")

    error = payload.get("backend_error", "")
    assert "cp-secret-sentinel" not in error
    assert "postgresql://" not in error
    assert "redis://" not in error


def test_readiness_payload_omits_env_derived_runtime_values(monkeypatch):
    monkeypatch.setattr(
        compose_services,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
        raising=False,
    )
    monkeypatch.setattr(compose_services.os, "environ", _ready_env())

    payload = compose_services._health_payload("worker")
    encoded = json.dumps(payload, sort_keys=True)

    assert "runtime_mode" not in payload
    assert "backend_profile" not in payload
    assert "compose-self-hosted" not in encoded
    assert "agentops" not in encoded


# ---------------------------------------------------------------------------
# M12B: Artifact endpoint routing and backend
# ---------------------------------------------------------------------------


import base64 as _base64


class _FakeArtifactBackend:
    def __init__(self):
        self._store: dict = {}

    def _key(self, context, ref):
        if context is None:
            return (None, ref)
        d = context.to_dict()
        return (tuple(sorted((k, d.get(k)) for k in d if k != "metadata")), ref)

    def put(self, context, ref, data):
        self._store[self._key(context, ref)] = bytes(data)
        return ref

    def get(self, context, ref):
        return self._store.get(self._key(context, ref))

    def list_artifacts(self, context):
        scope = self._key(context, None)[0]
        return sorted(k[1] for k in self._store if k[0] == scope)


_ARTIFACT_SCOPE = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "conversation_id": "conv1",
    "external_channel_id": None,
    "external_thread_id": None,
    "agent_profile_id": "bot",
    "project_id": "proj1",
    "run_id": "run1",
    "run_type": "conversation",
    "job_id": None,
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
}


@pytest.fixture
def _api_server_with_artifact():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.artifact_backend = _FakeArtifactBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_artifacts_post(_api_server_with_artifact):
    import urllib.parse
    scope_json = json.dumps(_ARTIFACT_SCOPE)
    body = json.dumps({
        "scope": _ARTIFACT_SCOPE,
        "ref": "routing-test.txt",
        "data_b64": _base64.b64encode(b"hello routing").decode(),
    }).encode()
    status = _http_post(f"{_api_server_with_artifact}/artifacts", body)
    assert status == 200


def test_api_service_routes_artifacts_get(_api_server_with_artifact):
    import urllib.parse
    qs = urllib.parse.urlencode({"scope": json.dumps(_ARTIFACT_SCOPE)})
    status = _http_get(f"{_api_server_with_artifact}/artifacts?{qs}")
    assert status == 200


def test_api_service_routes_artifact_ref_get(_api_server_with_artifact):
    import urllib.parse
    qs = urllib.parse.urlencode({"scope": json.dumps(_ARTIFACT_SCOPE)})
    status = _http_get(f"{_api_server_with_artifact}/artifacts/nonexistent.txt?{qs}")
    assert status == 404


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_artifacts_post(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({
            "scope": _ARTIFACT_SCOPE,
            "ref": "x.txt",
            "data_b64": _base64.b64encode(b"x").decode(),
        }).encode()
        status = _http_post(f"{base}/artifacts", body)
        assert status == 404, f"{service_name} should return 404 for POST /artifacts"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_artifacts_get(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        import urllib.parse
        qs = urllib.parse.urlencode({"scope": json.dumps(_ARTIFACT_SCOPE)})
        status = _http_get(f"{base}/artifacts?{qs}")
        assert status == 404, f"{service_name} should return 404 for GET /artifacts"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_artifacts_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /artifacts?scope=sentinel-secret-scope HTTP/1.1" 200 -'
    )
    assert "sentinel-secret-scope" not in rendered
    assert "/artifacts" in rendered
    assert "<redacted>" in rendered


def test_artifacts_log_message_redacts_ref_path_and_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /artifacts/LEAKSENTINEL-ref.txt?scope=sentinel-secret-scope HTTP/1.1" 404 -'
    )
    assert "LEAKSENTINEL-ref.txt" not in rendered
    assert "sentinel-secret-scope" not in rendered
    assert "/artifacts/<redacted>" in rendered


def test_make_artifact_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_ARTIFACT_ROOT"):
        compose_services._make_artifact_backend({})


def test_make_artifact_backend_configured_returns_backend(tmp_path):
    from agent.runtime_artifacts_audit import LocalFileArtifactBackend
    backend = compose_services._make_artifact_backend({"AGENTOPS_ARTIFACT_ROOT": str(tmp_path)})
    assert isinstance(backend, LocalFileArtifactBackend)


def test_artifact_dispatch_returns_401_before_backend_setup_when_token_missing(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL artifact backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_artifact_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({
            "scope": _ARTIFACT_SCOPE,
            "ref": "x.txt",
            "data_b64": _base64.b64encode(b"x").decode(),
        }).encode()
        req = urllib.request.Request(f"{base}/artifacts", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 401
        assert calls == []
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=x" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_artifact_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL artifact backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_artifact_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({
            "scope": _ARTIFACT_SCOPE,
            "ref": "x.txt",
            "data_b64": _base64.b64encode(b"x").decode(),
        }).encode()
        req = urllib.request.Request(f"{base}/artifacts", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 503
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=x" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# M12B: Audit endpoint routing and backend
# ---------------------------------------------------------------------------


class _FakeAuditBackend:
    def __init__(self):
        self._events: list = []

    def record(self, context, event):
        self._events.append(dict(event))

    def list_events(self, context, *, limit=None):
        events = list(self._events)
        return events if limit is None else events[:limit]


_AUDIT_SCOPE = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "conversation_id": "conv1",
    "external_channel_id": None,
    "external_thread_id": None,
    "agent_profile_id": "bot",
    "project_id": "proj1",
    "run_id": "run1",
    "run_type": "conversation",
    "job_id": None,
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
}


@pytest.fixture
def _api_server_with_audit():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.audit_backend = _FakeAuditBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_audit_post(_api_server_with_audit):
    body = json.dumps({"scope": _AUDIT_SCOPE, "event": {"event_type": "routing_test"}}).encode()
    status = _http_post(f"{_api_server_with_audit}/audit", body)
    assert status == 200


def test_api_service_routes_audit_get(_api_server_with_audit):
    import urllib.parse
    qs = urllib.parse.urlencode({"scope": json.dumps(_AUDIT_SCOPE)})
    status = _http_get(f"{_api_server_with_audit}/audit?{qs}")
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_audit_post(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"scope": _AUDIT_SCOPE, "event": {"event_type": "x"}}).encode()
        status = _http_post(f"{base}/audit", body)
        assert status == 404, f"{service_name} should return 404 for POST /audit"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_audit_get(service_name):
    import urllib.parse
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        qs = urllib.parse.urlencode({"scope": json.dumps(_AUDIT_SCOPE)})
        status = _http_get(f"{base}/audit?{qs}")
        assert status == 404, f"{service_name} should return 404 for GET /audit"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_audit_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_ARTIFACT_ROOT"):
        compose_services._make_audit_backend({})


def test_make_audit_backend_configured_returns_backend(tmp_path):
    from agent.runtime_artifacts_audit import LocalFileAuditBackend
    backend = compose_services._make_audit_backend({"AGENTOPS_ARTIFACT_ROOT": str(tmp_path)})
    assert isinstance(backend, LocalFileAuditBackend)


def test_audit_dispatch_returns_401_before_backend_setup_when_token_missing(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL audit backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_audit_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"scope": _AUDIT_SCOPE, "event": {"event_type": "x"}}).encode()
        req = urllib.request.Request(f"{base}/audit", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 401
        assert calls == []
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=x" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_audit_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL audit backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_audit_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"scope": _AUDIT_SCOPE, "event": {"event_type": "x"}}).encode()
        req = urllib.request.Request(f"{base}/audit", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 503
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=x" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_audit_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /audit?scope=sentinel-secret-scope HTTP/1.1" 200 -'
    )
    assert "sentinel-secret-scope" not in rendered
    assert "/audit" in rendered
    assert "<redacted>" in rendered


def test_audit_log_message_redacts_raw_json_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /audit?scope={"user":"LEAKSENTINEL"} HTTP/1.1" 400 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert '"user"' not in rendered
    assert "/audit" in rendered
    assert "<redacted>" in rendered


# ---------------------------------------------------------------------------
# M12B: Sessions endpoint routing and backend
# ---------------------------------------------------------------------------


class _FakeSessionBackend:
    def create_session(self, context, **kwargs):
        return "test-session-id"

    def append_message(self, context, message):
        return 1

    def read_messages(self, context, **kwargs):
        return []

    def search(self, context, query):
        return []

    def resolve_resume_session_id(self, session_id):
        return session_id

    def claim_turn_lock(self, context, **kwargs):
        return True

    def renew_turn_lock(self, context, **kwargs):
        return True

    def release_turn_lock(self, context, **kwargs):
        pass


_SESSION_CONTEXT = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "conversation_id": "conv1",
    "agent_profile_id": "bot",
    "project_id": "proj1",
}


@pytest.fixture
def _api_server_with_session():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.session_backend = _FakeSessionBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_sessions_create(_api_server_with_session):
    body = json.dumps({"context": _SESSION_CONTEXT}).encode()
    status = _http_post(f"{_api_server_with_session}/sessions/create", body)
    assert status == 200


def test_api_service_routes_sessions_messages(_api_server_with_session):
    body = json.dumps({"context": _SESSION_CONTEXT}).encode()
    status = _http_post(f"{_api_server_with_session}/sessions/messages", body)
    assert status == 200


def test_api_service_routes_sessions_turn_lock_claim(_api_server_with_session):
    body = json.dumps({
        "context": _SESSION_CONTEXT,
        "owner": "worker-1",
        "ttl_seconds": 300,
    }).encode()
    status = _http_post(f"{_api_server_with_session}/sessions/turn-lock/claim", body)
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_sessions_create(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SESSION_CONTEXT}).encode()
        status = _http_post(f"{base}/sessions/create", body)
        assert status == 404, f"{service_name} should return 404 for POST /sessions/create"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_sessions_messages(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SESSION_CONTEXT}).encode()
        status = _http_post(f"{base}/sessions/messages", body)
        assert status == 404, f"{service_name} should return 404 for POST /sessions/messages"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_session_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_SESSION_DB_PATH"):
        compose_services._make_session_backend({})


def test_make_session_backend_returns_backend_with_valid_path(tmp_path):
    from agent.runtime_sessions import LocalSQLiteSessionBackend
    db_path = str(tmp_path / "sessions.db")
    backend = compose_services._make_session_backend({
        "AGENTOPS_SESSION_DB_PATH": db_path,
    })
    assert isinstance(backend, LocalSQLiteSessionBackend)


def test_get_or_create_session_backend_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(compose_services, "_session_backend_instance", None)
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError, match="AGENTOPS_SESSION_DB_PATH"):
        compose_services._get_or_create_session_backend()


def test_session_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL session backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_session_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SESSION_CONTEXT}).encode()
        req = urllib.request.Request(f"{base}/sessions/create", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 503
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
        assert "password=x" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_session_dispatch_returns_401_before_backend_setup_when_token_missing(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL session backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_session_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SESSION_CONTEXT}).encode()
        req = urllib.request.Request(f"{base}/sessions/create", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 401
        assert calls == []
        response_text = response_body.decode("utf-8")
        assert "LEAKSENTINEL" not in response_text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_session_dispatch_rejects_oversized_body_before_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL session backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_session_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /sessions/create HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {compose_services._MAX_SESSION_REQUEST_BODY_BYTES + 1}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response_body = sock.recv(4096).decode("utf-8", errors="replace")
        assert "413" in response_body
        assert calls == []
        assert "LEAKSENTINEL" not in response_body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_session_dispatch_rejects_negative_content_length_before_read(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL session backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_session_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /sessions/create HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: -1\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response_body = sock.recv(4096).decode("utf-8", errors="replace")
        assert "400" in response_body
        assert calls == []
        assert "LEAKSENTINEL" not in response_body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_sessions_prefix_overmatch_returns_404_without_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL session backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_session_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status = _http_post(f"{base}/sessionsXYZ", b"{}")
        assert status == 404
        assert calls == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_sessions_log_message_redacts_session_id_in_resume_target_path():
    rendered = compose_services._sanitize_log_message(
        '"POST /sessions/LEAKSENTINEL-session-id/resume-target HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL-session-id" not in rendered
    assert "/sessions/" in rendered
    assert "<redacted>" in rendered


def test_sessions_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /sessions/create?debug=LEAKSENTINEL HTTP/1.1" 400 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/sessions/create" in rendered
    assert "<redacted>" in rendered


def test_sessions_log_message_keeps_static_path_segments_unredacted():
    rendered = compose_services._sanitize_log_message(
        '"POST /sessions/turn-lock/claim HTTP/1.1" 200 -'
    )
    assert "/sessions/turn-lock/claim" in rendered


# ---------------------------------------------------------------------------
# M12B: Secrets endpoint routing and backend
# ---------------------------------------------------------------------------


class _FakeSecretBackend:
    def __init__(self):
        self._store: dict = {}

    def get(self, scope, ref):
        return self._store.get((scope, ref))

    def put(self, scope, ref, value):
        self._store[(scope, ref)] = value


_SECRET_CONTEXT_CS = {
    "mode": "agentops",
    "org_id": "org-test",
    "workspace_id": "ws-test",
    "workspace_type": "team",
    "user_id": "user-test",
    "project_id": "project-test",
    "agent_profile_id": "agent-profile-test",
    "permissions_ref": "permissions-test",
    "backend_profile": "compose-self-hosted",
}


@pytest.fixture
def _local_secrets_server():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "local-secrets"
    server.secret_backend = _FakeSecretBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_local_secrets_routes_secrets_put(_local_secrets_server):
    body = json.dumps({
        "context": _SECRET_CONTEXT_CS,
        "ref": "routing-test",
        "value": "routing-value",
    }).encode()
    req = urllib.request.Request(f"{_local_secrets_server}/secrets/put", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 200


def test_local_secrets_routes_secrets_get(_local_secrets_server):
    body = json.dumps({"context": _SECRET_CONTEXT_CS, "ref": "routing-test"}).encode()
    req = urllib.request.Request(f"{_local_secrets_server}/secrets/get", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 200


@pytest.mark.parametrize("service_name", ["api", "worker", "scheduler"])
def test_non_local_secrets_services_return_404_for_secrets_put(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({
            "context": _SECRET_CONTEXT_CS,
            "ref": "x",
            "value": "v",
        }).encode()
        status = _http_post(f"{base}/secrets/put", body)
        assert status == 404, f"{service_name} should return 404 for POST /secrets/put"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["api", "worker", "scheduler"])
def test_non_local_secrets_services_return_404_for_secrets_get(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SECRET_CONTEXT_CS, "ref": "x"}).encode()
        status = _http_post(f"{base}/secrets/get", body)
        assert status == 404, f"{service_name} should return 404 for POST /secrets/get"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_secret_dispatch_returns_401_before_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL secret backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_secret_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "local-secrets"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SECRET_CONTEXT_CS, "ref": "x", "value": "v"}).encode()
        req = urllib.request.Request(f"{base}/secrets/put", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 401
        assert calls == []
        assert "LEAKSENTINEL" not in response_body.decode("utf-8")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_secret_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL secret backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_secret_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "local-secrets"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _SECRET_CONTEXT_CS, "ref": "x", "value": "v"}).encode()
        req = urllib.request.Request(f"{base}/secrets/put", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
                response_body = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        assert status == 503
        assert "LEAKSENTINEL" not in response_body.decode("utf-8")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_secret_dispatch_rejects_oversized_body_before_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL secret backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_secret_backend", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "local-secrets"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /secrets/put HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {compose_services._MAX_SECRET_REQUEST_BODY_BYTES + 1}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response_body = sock.recv(4096).decode("utf-8", errors="replace")
        assert "413" in response_body
        assert calls == []
        assert "LEAKSENTINEL" not in response_body
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_secret_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_SECRET_STORE_PATH"):
        compose_services._make_secret_backend({})


def test_make_secret_backend_returns_backend_with_valid_path(tmp_path):
    from agentops_runtime.secrets_api import SQLiteSecretStore

    db_path = str(tmp_path / "secrets.db")
    backend = compose_services._make_secret_backend({"AGENTOPS_SECRET_STORE_PATH": db_path})
    assert isinstance(backend, SQLiteSecretStore)


def test_get_or_create_secret_backend_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(compose_services, "_secret_backend_instance", None)
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError, match="AGENTOPS_SECRET_STORE_PATH"):
        compose_services._get_or_create_secret_backend()


def test_secrets_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /secrets/get?debug=LEAKSENTINEL HTTP/1.1" 400 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/secrets/get" in rendered
    assert "<redacted>" in rendered


def test_secrets_log_message_redacts_ref_path_and_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /secrets/REFLEAKSENTINEL?debug=QUERYLEAKSENTINEL HTTP/1.1" 404 -'
    )
    assert "REFLEAKSENTINEL" not in rendered
    assert "QUERYLEAKSENTINEL" not in rendered
    assert "/secrets/<redacted>" in rendered
