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


# ---------------------------------------------------------------------------
# M12B: Curated memory endpoint routing, backend, and log sanitization
# ---------------------------------------------------------------------------


import tempfile as _tempfile


@pytest.fixture
def _api_server_curated(tmp_path):
    """Compose API server with a real SQLiteCuratedMemoryStore backend."""
    from agentops_runtime.curated_memory_api import SQLiteCuratedMemoryStore

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.memory_backend = _FakeMemoryBackend()
    server.curated_memory_backend = SQLiteCuratedMemoryStore(str(tmp_path / "curated.db"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_get_memory(_api_server_curated):
    import urllib.parse

    qs = urllib.parse.urlencode({"target": "memory", "context": "{}"})
    status = _http_get(f"{_api_server_curated}/memory?{qs}")
    assert status == 200


def test_api_service_routes_post_memory(_api_server_curated):
    body = json.dumps({"context": {}, "target": "memory", "content": "test routing", "action": "add"}).encode()
    status = _http_post(f"{_api_server_curated}/memory", body)
    assert status == 200


@pytest.mark.parametrize("alias", ["/memory;scope-leak", "/memory;"])
def test_api_service_rejects_path_parameter_alias_for_memory(_api_server_curated, alias):
    """Exact /memory routing must reject urlparse path-parameter aliases."""
    import urllib.parse

    qs = urllib.parse.urlencode({"target": "memory", "context": "{}"})
    status = _http_get(f"{_api_server_curated}{alias}?{qs}")
    assert status == 404


def test_api_service_memory_does_not_route_memory_records(_api_server_curated):
    """GET /memory must not shadow GET /memory/records/search."""
    import urllib.parse

    qs = urllib.parse.urlencode({"context": json.dumps(_CONTEXT), "query": "x"})
    status = _http_get(f"{_api_server_curated}/memory/records/search?{qs}")
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_get_memory(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    import urllib.parse

    try:
        qs = urllib.parse.urlencode({"target": "memory", "context": "{}"})
        status = _http_get(f"{base}/memory?{qs}")
        assert status == 404, f"{service_name} should return 404 for GET /memory"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_post_memory(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": {}, "content": "x"}).encode()
        status = _http_post(f"{base}/memory", body)
        assert status == 404, f"{service_name} should return 404 for POST /memory"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_curated_memory_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_CURATED_MEMORY_DB_PATH"):
        compose_services._make_curated_memory_backend({})


def test_make_curated_memory_backend_fails_closed_on_blank_path():
    with pytest.raises(ValueError):
        compose_services._make_curated_memory_backend({"AGENTOPS_CURATED_MEMORY_DB_PATH": "   "})


def test_make_curated_memory_backend_creates_store(tmp_path):
    from agentops_runtime.curated_memory_api import SQLiteCuratedMemoryStore

    db_path = str(tmp_path / "curated.db")
    backend = compose_services._make_curated_memory_backend({"AGENTOPS_CURATED_MEMORY_DB_PATH": db_path})
    assert isinstance(backend, SQLiteCuratedMemoryStore)


def test_make_curated_memory_backend_rejects_in_memory_path():
    with pytest.raises(ValueError):
        compose_services._make_curated_memory_backend({"AGENTOPS_CURATED_MEMORY_DB_PATH": ":memory:"})


def test_make_curated_memory_backend_rejects_relative_path():
    with pytest.raises(ValueError):
        compose_services._make_curated_memory_backend({"AGENTOPS_CURATED_MEMORY_DB_PATH": "relative/path.db"})


def test_get_or_create_curated_memory_backend_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(compose_services, "_curated_memory_backend_instance", None)
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError, match="AGENTOPS_CURATED_MEMORY_DB_PATH"):
        compose_services._get_or_create_curated_memory_backend()


def test_curated_memory_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"GET /memory?target=memory&context=%7B%22user_id%22%3A%22LEAKSENTINEL%22%7D HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/memory" in rendered
    assert "?<redacted>" in rendered


def test_curated_memory_log_does_not_affect_memory_records_redaction():
    rendered = compose_services._sanitize_log_message(
        '"GET /memory/records/search?context=%7B%7D&query=RECORDSENTINEL HTTP/1.1" 200 -'
    )
    assert "RECORDSENTINEL" not in rendered
    assert "/memory/records/search" in rendered
    assert "<redacted>" in rendered


# ---------------------------------------------------------------------------
# M12B: Skills endpoint routing
# ---------------------------------------------------------------------------


class _FakeSkillBackend:
    """Minimal skill backend stub for compose routing tests."""

    def list_skills(self, context, *, category=None):
        return []

    def load_skill(self, context, name, *, file_path=None, preprocess=True):
        return {"success": False, "error": f"Skill '{name}' not found."}

    def manage_skill(self, context, *, action, name, **fields):
        return {"success": True, "message": f"ok"}


@pytest.fixture
def _skill_api_server():
    """Spin up a real compose api service server with a fake skill backend."""
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.skill_backend = _FakeSkillBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("skill_path", ["/skills/list", "/skills/view", "/skills/manage"])
def test_api_service_routes_skill_post(_skill_api_server, skill_path):
    body = json.dumps({"context": _CONTEXT, "name": "x", "action": "create"}).encode()
    status = _http_post(f"{_skill_api_server}{skill_path}", body)
    assert status == 200, f"Expected 200 from {skill_path}, got {status}"


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
@pytest.mark.parametrize("skill_path", ["/skills/list", "/skills/view", "/skills/manage"])
def test_non_api_services_return_404_for_skills(service_name, skill_path):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT, "name": "x"}).encode()
        status = _http_post(f"{base}{skill_path}", body)
        assert status == 404, f"{service_name} should 404 for {skill_path}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("skill_alias", [
    "/skills/list;jsessionid=abc",
    "/skills/list;",
    "/skills/view;anything",
    "/skills/manage;x=1",
])
def test_semicolon_alias_skill_paths_return_404(_skill_api_server, skill_alias):
    body = json.dumps({"context": _CONTEXT}).encode()
    req = urllib.request.Request(f"{_skill_api_server}{skill_alias}", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 404, f"Semicolon alias {skill_alias!r} should return 404, got {status}"


def test_skill_endpoint_auth_rejected_before_body_is_read(_skill_api_server):
    """Auth check happens before body read — bad token always gets 401."""
    body = json.dumps({"context": _CONTEXT}).encode()
    req = urllib.request.Request(f"{_skill_api_server}/skills/list", data=body, method="POST")
    req.add_header("Authorization", "Bearer wrong-token")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))

    # Inject a token so the server enforces auth
    import os
    original = os.environ.copy()
    try:
        # We can't inject env easily mid-test; instead test via direct handler invocation
        from agentops_runtime import compose_services as cs
        from agentops_runtime.skills_api import handle_skill_request

        called = []

        def _spy_backend(*args, **kwargs):
            called.append(args)
            return []

        class _SpyBackend:
            def list_skills(self, context, *, category=None):
                called.append("list")
                return []

        status, resp = handle_skill_request(
            method="POST",
            path="/skills/list",
            query_string="",
            body_bytes=b"not-json",  # bad body — should never be parsed if auth fails
            auth_header="Bearer wrong-token",
            token="correct-token",
            backend=_SpyBackend(),
        )
        assert status == 401
        assert called == [], "Backend should not be called when auth fails"
    finally:
        pass


def test_skill_endpoint_content_length_too_large_returns_413():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.skill_backend = _FakeSkillBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # Send a small body but claim a huge Content-Length
        body = b'{"context": {}}'
        req = urllib.request.Request(f"{base}/skills/list", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(10 * 1024 * 1024 + 1))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 413
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_skill_backend_failure_returns_503_sanitized(monkeypatch):
    def _raise_backend_error():
        raise ValueError("LEAKSENTINEL db=/var/secret/db")

    monkeypatch.setattr(compose_services, "_get_or_create_skill_backend", _raise_backend_error)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    # skill_backend is None — forces call to _get_or_create_skill_backend
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT}).encode()
        req = urllib.request.Request(f"{base}/skills/list", data=body, method="POST")
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
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# M12B: _make_skill_backend path validation
# ---------------------------------------------------------------------------


def test_make_skill_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_SKILL_DB_PATH"):
        compose_services._make_skill_backend({})


def test_make_skill_backend_rejects_memory_path():
    with pytest.raises(ValueError, match=":memory:"):
        compose_services._make_skill_backend({"AGENTOPS_SKILL_DB_PATH": ":memory:"})


def test_make_skill_backend_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute"):
        compose_services._make_skill_backend({"AGENTOPS_SKILL_DB_PATH": "relative/path.db"})


def test_make_skill_backend_rejects_blank_path():
    with pytest.raises(ValueError, match="AGENTOPS_SKILL_DB_PATH"):
        compose_services._make_skill_backend({"AGENTOPS_SKILL_DB_PATH": "   "})


def test_make_skill_backend_creates_sqlite_store(tmp_path):
    from agentops_runtime.skills_api import SQLiteScopedSkillStore

    db_path = str(tmp_path / "skills_seam.db")
    backend = compose_services._make_skill_backend({"AGENTOPS_SKILL_DB_PATH": db_path})
    assert isinstance(backend, SQLiteScopedSkillStore)


# ---------------------------------------------------------------------------
# M12B: _sanitize_log_message redacts /skills query strings
# ---------------------------------------------------------------------------


def test_skills_list_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /skills/list?secret=LEAKSENTINEL HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/skills/list" in rendered
    assert "?<redacted>" in rendered


def test_skills_view_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /skills/view?name=LEAKSENTINEL HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/skills/view" in rendered


def test_skills_manage_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /skills/manage?action=LEAKSENTINEL HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/skills/manage" in rendered


# ---------------------------------------------------------------------------
# M12B: Queue endpoint routing and backend
# ---------------------------------------------------------------------------


class _FakeQueueBackend:
    def __init__(self):
        self._items = []

    def enqueue(self, scope, payload, *, idempotency_key=None):
        import uuid as _uuid

        r = str(_uuid.uuid4())
        self._items.append({"receipt": r, "scope": scope, "payload": payload})
        return r

    def claim(self, scope):
        for item in self._items:
            if item["scope"] == scope:
                return {"receipt": item["receipt"], "payload": item["payload"]}
        return None

    def ack(self, scope, receipt):
        pass

    def nack(self, scope, receipt, *, requeue):
        pass

    def extend_lease(self, scope, receipt, *, seconds):
        pass


_QUEUE_CTX = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "project_id": "proj1",
    "external_channel_id": None,
    "external_thread_id": None,
    "conversation_id": "conv1",
    "user_id": "alice",
    "agent_profile_id": "bot",
    "run_type": "conversation",
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
    "delivery_ref": None,
    "run_id": "run1",
    "job_id": None,
}


@pytest.fixture
def _api_server_with_queue():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.queue_backend = _FakeQueueBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_queue_enqueue(_api_server_with_queue):
    body = json.dumps({"context": _QUEUE_CTX, "payload": {"task": "routing-test"}}).encode()
    status = _http_post(f"{_api_server_with_queue}/queue/enqueue", body)
    assert status == 200


def test_api_service_routes_queue_claim(_api_server_with_queue):
    body = json.dumps({"context": _QUEUE_CTX}).encode()
    status = _http_post(f"{_api_server_with_queue}/queue/claim", body)
    assert status == 200


def test_api_service_routes_queue_ack(_api_server_with_queue):
    body = json.dumps({"context": _QUEUE_CTX, "receipt": "fake-receipt-for-routing"}).encode()
    status = _http_post(f"{_api_server_with_queue}/queue/ack", body)
    assert status == 200


def test_api_service_routes_queue_nack(_api_server_with_queue):
    body = json.dumps({"context": _QUEUE_CTX, "receipt": "fake-receipt-for-routing", "requeue": True}).encode()
    status = _http_post(f"{_api_server_with_queue}/queue/nack", body)
    assert status == 200


def test_api_service_routes_queue_extend_lease(_api_server_with_queue):
    body = json.dumps({"context": _QUEUE_CTX, "receipt": "fake-receipt-for-routing", "seconds": 30}).encode()
    status = _http_post(f"{_api_server_with_queue}/queue/extend-lease", body)
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_queue_enqueue(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _QUEUE_CTX, "payload": {"x": 1}}).encode()
        status = _http_post(f"{base}/queue/enqueue", body)
        assert status == 404, f"{service_name} should return 404 for POST /queue/enqueue"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("queue_alias", [
    "/queue/enqueue;anything",
    "/queue/enqueue;",
    "/queue/claim;x=1",
    "/queue/ack;jsessionid=abc",
])
def test_semicolon_alias_queue_paths_return_404_before_backend(_api_server_with_queue, queue_alias):
    body = json.dumps({"context": _QUEUE_CTX, "payload": {"x": 1}}).encode()
    req = urllib.request.Request(f"{_api_server_with_queue}{queue_alias}", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 404, f"Semicolon alias {queue_alias!r} should return 404, got {status}"


def test_queue_prefix_lookalike_returns_404(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL queue backend failure")

    monkeypatch.setattr(compose_services, "_get_or_create_queue_backend", _raise, raising=False)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status = _http_post(f"{base}/queueXYZ", b"{}")
        assert status == 404
        assert calls == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_queue_dispatch_returns_401_before_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL queue backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_queue_backend", _raise, raising=False)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _QUEUE_CTX, "payload": {"x": 1}}).encode()
        req = urllib.request.Request(f"{base}/queue/enqueue", data=body, method="POST")
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


def test_queue_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL queue backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_queue_backend", _raise, raising=False)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _QUEUE_CTX, "payload": {"x": 1}}).encode()
        req = urllib.request.Request(f"{base}/queue/enqueue", data=body, method="POST")
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


def test_queue_dispatch_rejects_oversized_body_before_backend(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL queue backend failure")

    monkeypatch.setattr(compose_services, "_get_or_create_queue_backend", _raise, raising=False)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /queue/enqueue HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {compose_services._MAX_QUEUE_REQUEST_BODY_BYTES + 1}\r\n"
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


def test_make_queue_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_QUEUE_DB_PATH"):
        compose_services._make_queue_backend({})


def test_make_queue_backend_rejects_memory_path():
    with pytest.raises(ValueError, match=":memory:"):
        compose_services._make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": ":memory:"})


def test_make_queue_backend_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute"):
        compose_services._make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": "relative/path.db"})


def test_make_queue_backend_rejects_blank_path():
    with pytest.raises(ValueError, match="AGENTOPS_QUEUE_DB_PATH"):
        compose_services._make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": "   "})


def test_make_queue_backend_creates_sqlite_backend(tmp_path):
    from agentops_runtime.queue_api import SQLiteQueueBackend

    db_path = str(tmp_path / "queue_seam.db")
    backend = compose_services._make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": db_path})
    assert isinstance(backend, SQLiteQueueBackend)


def test_get_or_create_queue_backend_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(compose_services, "_queue_backend_instance", None, raising=False)
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError, match="AGENTOPS_QUEUE_DB_PATH"):
        compose_services._get_or_create_queue_backend()


def test_queue_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /queue/enqueue?LEAKSENTINEL=secret HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/queue/enqueue" in rendered
    assert "?<redacted>" in rendered


@pytest.mark.parametrize(
    "raw_request",
    [
        '"POST /queue/ack/LEAKSENTINEL-receipt HTTP/1.1" 404 -',
        '"POST /queue/enqueue;receipt=LEAKSENTINEL HTTP/1.1" 404 -',
        '"POST /queue%2Fack%2FLEAKSENTINEL-receipt HTTP/1.1" 404 -',
        '"POST /queueXYZ/LEAKSENTINEL-receipt HTTP/1.1" 404 -',
    ],
)
def test_queue_log_message_redacts_invalid_path_receipt_material(raw_request):
    rendered = compose_services._sanitize_log_message(raw_request)
    assert "LEAKSENTINEL" not in rendered
    assert "/queue/<redacted>" in rendered


# ---------------------------------------------------------------------------
# M12B: Run-lease endpoint routing and backend
# ---------------------------------------------------------------------------


class _FakeRunLeaseBackend:
    def claim(self, scope, key, *, owner, now=None, lease_seconds=None):
        return True

    def renew(self, scope, key, *, owner, now=None, lease_seconds=None):
        return True

    def release(self, scope, key, *, owner):
        pass

    def request_cancel(self, scope, key, *, requester=None):
        pass

    def is_cancelled(self, scope, key):
        return False

    def clear_cancel(self, scope, key):
        pass

    def expire_stale(self, scope, *, now=None):
        return 0


_RUN_LEASE_CTX = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "project_id": "proj1",
    "external_channel_id": None,
    "external_thread_id": None,
    "conversation_id": "conv1",
    "user_id": "alice",
    "agent_profile_id": "bot",
    "run_type": "conversation",
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
    "delivery_ref": None,
}


@pytest.fixture
def _api_server_with_run_lease():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.run_lease_backend = _FakeRunLeaseBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_service_routes_run_lease_claim(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/claim", body)
    assert status == 200


def test_api_service_routes_run_lease_renew(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/renew", body)
    assert status == 200


def test_api_service_routes_run_lease_release(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/release", body)
    assert status == 200


def test_api_service_routes_run_lease_request_cancel(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX, "requester": "sup"}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/request-cancel", body)
    assert status == 200


def test_api_service_routes_run_lease_is_cancelled(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/is-cancelled", body)
    assert status == 200


def test_api_service_routes_run_lease_clear_cancel(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/run-001/clear-cancel", body)
    assert status == 200


def test_api_service_routes_run_lease_expire_stale(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leases/expire-stale", body)
    assert status == 200


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_run_lease_claim(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
        status = _http_post(f"{base}/run-leases/run-001/claim", body)
        assert status == 404, f"{service_name} should return 404 for POST /run-leases/<key>/claim"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("service_name", ["worker", "scheduler", "local-secrets"])
def test_non_api_services_return_404_for_run_lease_expire_stale(service_name):
    server, thread = _make_non_api_server(service_name)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _RUN_LEASE_CTX}).encode()
        status = _http_post(f"{base}/run-leases/expire-stale", body)
        assert status == 404, f"{service_name} should return 404 for POST /run-leases/expire-stale"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_run_lease_prefix_lookalike_returns_404(_api_server_with_run_lease):
    body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
    status = _http_post(f"{_api_server_with_run_lease}/run-leasesXYZ/run-001/claim", body)
    assert status == 404


def test_run_lease_log_message_redacts_key_path():
    rendered = compose_services._sanitize_log_message(
        '"POST /run-leases/LEAKSENTINEL-run-key/claim HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL-run-key" not in rendered
    assert "/run-leases/<redacted>/claim" in rendered


def test_run_lease_expire_stale_log_message_not_redacted():
    rendered = compose_services._sanitize_log_message(
        '"POST /run-leases/expire-stale HTTP/1.1" 200 -'
    )
    assert "/run-leases/expire-stale" in rendered


def test_run_lease_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /run-leases/run-001/claim?LEAKSENTINEL=x HTTP/1.1" 200 -'
    )
    assert "LEAKSENTINEL" not in rendered


def test_run_lease_log_message_redacts_semicolon_alias_material():
    rendered = compose_services._sanitize_log_message(
        '"POST /run-leases/run-001/claim;LEAKSENTINEL HTTP/1.1" 404 -'
    )
    assert "LEAKSENTINEL" not in rendered
    assert "/run-leases/<redacted>/claim" in rendered


@pytest.mark.parametrize(
    "raw_request",
    [
        '"POST /run-leases;LEAKSENTINEL/run-001/claim HTTP/1.1" 404 -',
        '"POST /run-leasesXYZ/LEAKSENTINEL-run-key/claim HTTP/1.1" 404 -',
        '"POST /run-leases%2FLEAKSENTINEL-run-key%2Fclaim HTTP/1.1" 404 -',
    ],
)
def test_run_lease_log_message_redacts_rejected_prefix_and_encoded_material(raw_request):
    rendered = compose_services._sanitize_log_message(raw_request)
    assert "LEAKSENTINEL" not in rendered
    assert "/run-leases" in rendered
    assert "<redacted>" in rendered


def test_make_run_lease_backend_fails_closed_when_unconfigured():
    with pytest.raises(ValueError, match="AGENTOPS_RUN_LEASE_DB_PATH"):
        compose_services._make_run_lease_backend({})


def test_make_run_lease_backend_rejects_memory_path():
    with pytest.raises(ValueError, match=":memory:"):
        compose_services._make_run_lease_backend({"AGENTOPS_RUN_LEASE_DB_PATH": ":memory:"})


def test_make_run_lease_backend_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute"):
        compose_services._make_run_lease_backend({"AGENTOPS_RUN_LEASE_DB_PATH": "relative/path.db"})


def test_make_run_lease_backend_creates_sqlite_backend(tmp_path):
    from agentops_runtime.run_leases_api import SQLiteRunLeaseBackend

    db_path = str(tmp_path / "run_lease_seam.db")
    backend = compose_services._make_run_lease_backend({"AGENTOPS_RUN_LEASE_DB_PATH": db_path})
    assert isinstance(backend, SQLiteRunLeaseBackend)


def test_run_lease_invalid_semicolon_route_404s_before_backend_construction(monkeypatch):
    monkeypatch.delenv("AGENTOPS_RUN_LEASE_DB_PATH", raising=False)
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _RUN_LEASE_CTX, "owner": "w"}).encode()
        status = _http_post(f"{base}/run-leases/run-001/claim;LEAKSENTINEL", body)
        assert status == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
