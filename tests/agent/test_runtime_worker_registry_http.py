"""Tests for the M12B HTTP worker-registry adapter.

Covers:
- register/heartbeat/mark_draining return expected types; recover_expired/list_workers return lists
- malformed responses fail closed (missing field, wrong type, non-list, non-object items)
- worker_id is URL-encoded in path; raw IDs never appear in error messages
- context scope payload includes tenant fields; None context returns {} and does not crash
- recursive redaction removes secret-ish keys from worker/slots without mutating caller input
- base URL validation rejects unsafe configs; timeout rejects non-finite/non-positive
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token/worker-id leakage; logical endpoint labels used
- registry integration via register_http_worker_registry_backend
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_worker_registry_http import (
    HttpWorkerRegistry,
    register_http_worker_registry_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _RegistryHandler(BaseHTTPRequestHandler):
    server: "_RegistryServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        if self.server.force_status is not None:
            self.send_error(self.server.force_status)
            return

        if self.server.raw_response is not None:
            raw = str(self.server.raw_response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        parts = self.path.strip("/").split("/")

        if len(parts) == 2 and parts[0] == "workers" and parts[1] == "register":
            if self.server.register_bad_worker_id is not _UNSET:
                self._json({"worker_id": self.server.register_bad_worker_id})
                return
            if self.server.register_missing_worker_id:
                self._json({})
                return
            self._json({"worker_id": self.server.register_result})
            return

        if len(parts) == 3 and parts[0] == "workers" and parts[2] == "heartbeat":
            self._json({})
            return

        if len(parts) == 3 and parts[0] == "workers" and parts[2] == "drain":
            self._json({})
            return

        if len(parts) == 2 and parts[0] == "workers" and parts[1] == "recover-expired":
            if self.server.recover_bad_response is not _UNSET:
                self._json({"recovered": self.server.recover_bad_response})
                return
            if self.server.recover_missing:
                self._json({})
                return
            self._json({"recovered": self.server.recover_result})
            return

        if len(parts) == 2 and parts[0] == "workers" and parts[1] == "list":
            if self.server.list_bad_response is not _UNSET:
                self._json({"workers": self.server.list_bad_response})
                return
            if self.server.list_missing:
                self._json({})
                return
            self._json({"workers": self.server.list_result})
            return

        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


_UNSET = object()


class _RegistryServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    register_result: str
    register_bad_worker_id: Any
    register_missing_worker_id: bool
    recover_result: list[str]
    recover_bad_response: Any
    recover_missing: bool
    list_result: list[dict[str, Any]]
    list_bad_response: Any
    list_missing: bool
    force_status: int | None
    raw_response: str | None


@pytest.fixture
def registry_server():
    server = _RegistryServer(("127.0.0.1", 0), _RegistryHandler)
    server.requests = []
    server.register_result = "worker-abc"
    server.register_bad_worker_id = _UNSET
    server.register_missing_worker_id = False
    server.recover_result = []
    server.recover_bad_response = _UNSET
    server.recover_missing = False
    server.list_result = []
    server.list_bad_response = _UNSET
    server.list_missing = False
    server.force_status = None
    server.raw_response = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _ctx(**overrides: Any) -> RuntimeContext:
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="agentops-runtime",
        run_type="conversation",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery:slack:home",
    )
    base.update(overrides)
    return RuntimeContext(**base)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_posts_worker_and_returns_worker_id(registry_server):
    registry_server.register_result = "worker-42"
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="sentinel-token")
    worker_id = backend.register(_ctx(), {"id": "w-1", "capacity": 4})

    assert worker_id == "worker-42"
    assert len(registry_server.requests) == 1
    method, path, auth, body = registry_server.requests[0]
    assert method == "POST"
    assert path == "/workers/register"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body["context"]["org_id"] == "acme"
    assert "worker" in body


def test_register_with_none_context_sends_empty_scope(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    worker_id = backend.register(None, {"id": "w-none"})

    assert isinstance(worker_id, str)
    _, _, _, body = registry_server.requests[0]
    assert body.get("context") == {} or "context" not in body


def test_register_raises_on_missing_worker_id_field(registry_server):
    registry_server.register_missing_worker_id = True
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


def test_register_raises_on_empty_worker_id(registry_server):
    registry_server.register_bad_worker_id = ""
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


def test_register_raises_on_non_string_worker_id_int(registry_server):
    registry_server.register_bad_worker_id = 42
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


def test_register_raises_on_non_string_worker_id_none(registry_server):
    registry_server.register_bad_worker_id = None
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


def test_register_redacts_secret_keys_in_worker_payload(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    worker = {"id": "w-1", "token": "secret-sentinel", "capacity": 4}
    original = copy.deepcopy(worker)
    backend.register(_ctx(), worker)

    assert worker == original, "caller dict must not be mutated"
    _, _, _, body = registry_server.requests[0]
    worker_sent = body.get("worker", {})
    assert "secret-sentinel" not in repr(worker_sent)


def test_register_redacts_nested_secret_keys_in_worker(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    worker = {"id": "w-1", "config": {"api_key": "nested-secret-sentinel", "max_tasks": 2}}
    backend.register(_ctx(), worker)

    _, _, _, body = registry_server.requests[0]
    worker_sent = body.get("worker", {})
    assert "nested-secret-sentinel" not in repr(worker_sent)


def test_register_raises_on_non_json_response(registry_server):
    registry_server.raw_response = "not-json"
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


def test_register_raises_on_non_object_json_response(registry_server):
    registry_server.raw_response = '["not", "an", "object"]'
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.register(_ctx(), {"id": "w-1"})


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_posts_to_worker_path_and_returns_none(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="sentinel-token")
    result = backend.heartbeat(_ctx(), "worker-abc", slots={"tasks": 3})

    assert result is None
    method, path, auth, body = registry_server.requests[0]
    assert method == "POST"
    assert path == "/workers/worker-abc/heartbeat"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_heartbeat_url_encodes_worker_id(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    backend.heartbeat(_ctx(), "worker/with spaces&chars", slots={})

    _, path, _, _ = registry_server.requests[0]
    assert "worker/with spaces&chars" not in path
    worker_segment = path.split("/workers/")[1].split("/")[0]
    assert "/" not in worker_segment
    assert " " not in worker_segment


def test_heartbeat_with_none_context_does_not_crash(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    result = backend.heartbeat(None, "w-1", slots={})
    assert result is None


def test_heartbeat_redacts_secret_keys_in_slots(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    slots = {"task_count": 2, "authorization": "secret-slot-sentinel"}
    original = copy.deepcopy(slots)
    backend.heartbeat(_ctx(), "w-1", slots=slots)

    assert slots == original, "caller dict must not be mutated"
    _, _, _, body = registry_server.requests[0]
    slots_sent = body.get("slots", {})
    assert "secret-slot-sentinel" not in repr(slots_sent)


# ---------------------------------------------------------------------------
# mark_draining
# ---------------------------------------------------------------------------


def test_mark_draining_posts_to_drain_path_and_returns_none(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="sentinel-token")
    result = backend.mark_draining(_ctx(), "worker-abc")

    assert result is None
    method, path, auth, body = registry_server.requests[0]
    assert method == "POST"
    assert path == "/workers/worker-abc/drain"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_mark_draining_url_encodes_worker_id(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    backend.mark_draining(_ctx(), "worker/with special")

    _, path, _, _ = registry_server.requests[0]
    assert "worker/with special" not in path
    worker_segment = path.split("/workers/")[1].split("/")[0]
    assert "/" not in worker_segment


def test_mark_draining_with_none_context_does_not_crash(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    result = backend.mark_draining(None, "w-1")
    assert result is None


# ---------------------------------------------------------------------------
# recover_expired
# ---------------------------------------------------------------------------


def test_recover_expired_returns_list_of_worker_ids(registry_server):
    registry_server.recover_result = ["w-1", "w-2"]
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="sentinel-token")
    result = backend.recover_expired(_ctx())

    assert result == ["w-1", "w-2"]
    method, path, auth, body = registry_server.requests[0]
    assert method == "POST"
    assert path == "/workers/recover-expired"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_recover_expired_returns_empty_list_when_none_expired(registry_server):
    registry_server.recover_result = []
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    assert backend.recover_expired(_ctx()) == []


def test_recover_expired_with_none_context_does_not_crash(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    result = backend.recover_expired(None)
    assert isinstance(result, list)


def test_recover_expired_raises_when_recovered_is_not_a_list(registry_server):
    registry_server.recover_bad_response = "not-a-list"
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.recover_expired(_ctx())


def test_recover_expired_raises_when_recovered_is_dict(registry_server):
    registry_server.recover_bad_response = {"key": "value"}
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.recover_expired(_ctx())


def test_recover_expired_raises_when_recovered_missing(registry_server):
    registry_server.recover_missing = True
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.recover_expired(_ctx())


def test_recover_expired_raises_when_recovered_contains_non_string(registry_server):
    registry_server.recover_bad_response = ["w-1", 42, "w-3"]
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.recover_expired(_ctx())


# ---------------------------------------------------------------------------
# list_workers
# ---------------------------------------------------------------------------


def test_list_workers_returns_list_of_worker_dicts(registry_server):
    registry_server.list_result = [{"id": "w-1", "draining": False}, {"id": "w-2", "draining": True}]
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="sentinel-token")
    result = backend.list_workers(_ctx())

    assert result == [{"id": "w-1", "draining": False}, {"id": "w-2", "draining": True}]
    method, path, auth, body = registry_server.requests[0]
    assert method == "POST"
    assert path == "/workers/list"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_list_workers_returns_empty_list_when_no_workers(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    assert backend.list_workers(_ctx()) == []


def test_list_workers_with_none_context_does_not_crash(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    result = backend.list_workers(None)
    assert isinstance(result, list)


def test_list_workers_raises_when_workers_is_not_a_list(registry_server):
    registry_server.list_bad_response = "not-a-list"
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.list_workers(_ctx())


def test_list_workers_raises_when_workers_is_missing(registry_server):
    registry_server.list_missing = True
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.list_workers(_ctx())


def test_list_workers_raises_when_items_are_not_dicts(registry_server):
    registry_server.list_bad_response = [{"id": "w-1"}, "not-a-dict", {"id": "w-3"}]
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.list_workers(_ctx())


# ---------------------------------------------------------------------------
# Context scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_tenant_fields(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    ctx = _ctx(org_id="org-xyz", workspace_id="ws-abc", user_id="u-123")
    backend.register(ctx, {"id": "w-1"})

    _, _, _, body = registry_server.requests[0]
    ctx_payload = body["context"]
    assert ctx_payload["org_id"] == "org-xyz"
    assert ctx_payload["workspace_id"] == "ws-abc"
    assert ctx_payload["user_id"] == "u-123"


def test_scope_payload_includes_profile_and_delivery_ref(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    ctx = _ctx(backend_profile="compose-self-hosted", delivery_ref="delivery:slack:home")
    backend.list_workers(ctx)

    _, _, _, body = registry_server.requests[0]
    ctx_payload = body["context"]
    assert ctx_payload.get("backend_profile") == "compose-self-hosted"
    assert ctx_payload.get("delivery_ref") == "delivery:slack:home"


def test_none_context_produces_empty_scope(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    backend.list_workers(None)

    _, _, _, body = registry_server.requests[0]
    ctx_payload = body.get("context", {})
    assert ctx_payload == {}


def test_distinct_contexts_produce_distinct_scope_payloads(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    ctx_a = _ctx(org_id="org-a", user_id="user-a")
    ctx_b = _ctx(org_id="org-b", user_id="user-b")

    backend.list_workers(ctx_a)
    backend.list_workers(ctx_b)

    scope_a = registry_server.requests[0][3]["context"]
    scope_b = registry_server.requests[1][3]["context"]
    assert scope_a["org_id"] == "org-a"
    assert scope_b["org_id"] == "org-b"


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_rejects_empty_base_url():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("")


def test_rejects_relative_url():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("/api/v1")


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("ftp://example.test")


def test_rejects_url_with_credentials():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("https://user:pass@example.test")


def test_rejects_url_with_query():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("https://example.test?token=abc")


def test_rejects_url_with_fragment():
    with pytest.raises(ValueError):
        HttpWorkerRegistry("https://example.test#section")


def test_rejects_invalid_port_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpWorkerRegistry("https://api.internal:secret-port-sentinel/control")

    assert "secret-port-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_whitespace_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpWorkerRegistry("https://api internal/control")

    assert "api internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_del_control_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpWorkerRegistry("https://api\x7finternal/control")

    assert "api\x7finternal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_invalid_timeout_nan():
    with pytest.raises(ValueError, match="timeout"):
        HttpWorkerRegistry("https://example.test", timeout=float("nan"))


def test_rejects_invalid_timeout_inf():
    with pytest.raises(ValueError, match="timeout"):
        HttpWorkerRegistry("https://example.test", timeout=float("inf"))


def test_rejects_invalid_timeout_zero():
    with pytest.raises(ValueError, match="timeout"):
        HttpWorkerRegistry("https://example.test", timeout=0)


def test_rejects_invalid_timeout_negative():
    with pytest.raises(ValueError, match="timeout"):
        HttpWorkerRegistry("https://example.test", timeout=-1)


def test_rejects_non_numeric_timeout():
    with pytest.raises(ValueError, match="timeout") as exc_info:
        HttpWorkerRegistry("https://example.test", timeout="cp-secret-token")  # type: ignore[arg-type]

    assert "cp-secret-token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token transport
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="cp-secret-sentinel")
    backend.register(_ctx(), {"id": "w-1"})

    _, path, auth, body = registry_server.requests[0]
    assert auth == "Bearer cp-secret-sentinel"
    assert "cp-secret-sentinel" not in path
    assert "cp-secret-sentinel" not in repr(body)


def test_no_authorization_header_when_no_token(registry_server):
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}")
    backend.list_workers(_ctx())

    _, _, auth, _ = registry_server.requests[0]
    assert auth is None


def test_rejects_control_character_token_without_leaking_value():
    with pytest.raises(ValueError) as exc_info:
        HttpWorkerRegistry("https://api.internal", token="cp-secret-token\r\nX-Leak: yes")

    assert "cp-secret-token" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Error sanitization / logical endpoint labels
# ---------------------------------------------------------------------------


def test_transport_errors_do_not_leak_token():
    backend = HttpWorkerRegistry("http://127.0.0.1:1", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.register(_ctx(), {"id": "w-1"})
    assert "cp-secret-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_register_transport_error_uses_logical_label():
    backend = HttpWorkerRegistry("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.register(_ctx(), {"id": "w-1"})
    msg = str(exc_info.value)
    assert "POST /workers/register" in msg
    assert "127.0.0.1" not in msg


def test_heartbeat_transport_error_uses_logical_label_without_raw_worker_id():
    backend = HttpWorkerRegistry("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.heartbeat(_ctx(), "secret-worker-id-sentinel", slots={})
    msg = str(exc_info.value)
    assert "secret-worker-id-sentinel" not in msg
    assert "POST /workers/<worker_id>/heartbeat" in msg


def test_mark_draining_transport_error_uses_logical_label_without_raw_worker_id():
    backend = HttpWorkerRegistry("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.mark_draining(_ctx(), "secret-worker-id-sentinel")
    msg = str(exc_info.value)
    assert "secret-worker-id-sentinel" not in msg
    assert "POST /workers/<worker_id>/drain" in msg


def test_recover_expired_transport_error_uses_logical_label():
    backend = HttpWorkerRegistry("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.recover_expired(_ctx())
    msg = str(exc_info.value)
    assert "POST /workers/recover-expired" in msg
    assert "tok" not in msg


def test_list_workers_transport_error_uses_logical_label():
    backend = HttpWorkerRegistry("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_workers(_ctx())
    msg = str(exc_info.value)
    assert "POST /workers/list" in msg
    assert "tok" not in msg


def test_http_error_uses_logical_label_and_does_not_retain_cause(registry_server):
    registry_server.force_status = 500
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.heartbeat(_ctx(), "secret-worker-id", slots={})
    msg = str(exc_info.value)
    assert "cp-secret-sentinel" not in msg
    assert "secret-worker-id" not in msg
    assert "POST /workers/<worker_id>/heartbeat" in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_is_not_retained_as_exception_cause(registry_server):
    registry_server.raw_response = "non-json body sentinel"
    backend = HttpWorkerRegistry(f"http://127.0.0.1:{registry_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.register(_ctx(), {"id": "w-1"})
    msg = str(exc_info.value)
    assert "non-json body sentinel" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_utf8_response_body_is_not_retained_as_exception_cause(monkeypatch):
    class _InvalidUtf8Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b"\xffraw-body-sentinel"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _InvalidUtf8Response())
    backend = HttpWorkerRegistry("https://api.internal", token="cp-secret-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        backend.register(_ctx(), {"id": "w-1"})

    msg = str(exc_info.value)
    assert "raw-body-sentinel" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_worker_registry_backend_registers_for_profile(registry_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"worker_registry": "compose-self-hosted"},
                "options": {
                    "worker_registry": {
                        "base_url": f"http://127.0.0.1:{registry_server.server_port}",
                        "token": "sentinel-token",
                    }
                },
            }
        }
    )
    register_http_worker_registry_backend(registry)

    backend = registry.get(BackendCapability.WORKER_REGISTRY, _ctx())
    assert isinstance(backend, HttpWorkerRegistry)
    result = backend.register(_ctx(), {"id": "w-1"})
    assert isinstance(result, str)


def test_register_http_worker_registry_backend_preserves_timeout_validation(registry_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"worker_registry": "compose-self-hosted"},
                "options": {
                    "worker_registry": {
                        "base_url": f"http://127.0.0.1:{registry_server.server_port}",
                        "timeout": 0,
                    }
                },
            }
        }
    )
    register_http_worker_registry_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.WORKER_REGISTRY, _ctx())
