"""Tests for the M12B HTTP delivery backend adapter.

Covers:
- deliver(context, message) POSTs to /delivery/deliver and returns None
- token sent only via Authorization header; never in URL, body, or errors
- None context sends empty scope {}
- context scope includes tenant/thread/profile/delivery routing fields including delivery_ref
- context scope excludes permissions_ref, run_id, and job_id
- message payload preserved verbatim; caller's dict not mutated
- bad base URLs, invalid timeout, and token control chars fail closed at construction
- HTTP/transport/non-JSON failures use logical endpoint label; suppress __cause__/__context__
- no leakage of host/path/token/response body/delivery_ref/message content in errors
- register_http_delivery_backend() registers BackendCapability.DELIVERY; timeout honored
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_delivery_http import (
    HttpDeliveryBackend,
    register_http_delivery_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _DeliveryHandler(BaseHTTPRequestHandler):
    server: "_DeliveryServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        if self.server.force_status is not None:
            self.send_error(self.server.force_status)
            return

        if self.server.raw_response is not None:
            raw = self.server.raw_response
            if isinstance(raw, bytes):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                encoded = str(raw).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            return

        self._json({})

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


class _DeliveryServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    force_status: int | None
    raw_response: Any


@pytest.fixture
def delivery_server():
    server = _DeliveryServer(("127.0.0.1", 0), _DeliveryHandler)
    server.requests = []
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
# deliver method
# ---------------------------------------------------------------------------


def test_deliver_posts_to_delivery_deliver_and_returns_none(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}", token="sentinel-token")
    result = backend.deliver(_ctx(), {"content": "hello"})

    assert result is None
    assert len(delivery_server.requests) == 1
    method, path, auth, body = delivery_server.requests[0]
    assert method == "POST"
    assert path == "/delivery/deliver"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("message") == {"content": "hello"}


def test_deliver_returns_none_on_success(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    result = backend.deliver(_ctx(), {"content": "test"})
    assert result is None


def test_deliver_without_token_sends_no_authorization_header(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    backend.deliver(_ctx(), {"content": "hello"})

    _, _, auth, _ = delivery_server.requests[0]
    assert auth is None


def test_deliver_with_none_context_sends_empty_context_scope(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    backend.deliver(None, {"content": "hello"})

    _, _, _, body = delivery_server.requests[0]
    assert body.get("context") == {}


# ---------------------------------------------------------------------------
# Message payload preservation
# ---------------------------------------------------------------------------


def test_deliver_preserves_message_verbatim_including_secret_looking_keys(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    message = {
        "content": "hello world",
        "token": "secret-value",
        "api_key": "sk-123",
        "password": "hunter2",
    }
    backend.deliver(_ctx(), message)

    _, _, _, body = delivery_server.requests[0]
    sent = body.get("message", {})
    assert sent.get("content") == "hello world"
    assert sent.get("token") == "secret-value"
    assert sent.get("api_key") == "sk-123"
    assert sent.get("password") == "hunter2"


def test_deliver_does_not_mutate_callers_message_dict(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    original = {"content": "hello", "extra": "data"}
    snapshot = dict(original)
    backend.deliver(_ctx(), original)
    assert original == snapshot


# ---------------------------------------------------------------------------
# Context scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_delivery_ref(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    ctx = _ctx(delivery_ref="delivery:slack:home")
    backend.deliver(ctx, {})

    _, _, _, body = delivery_server.requests[0]
    assert body["context"]["delivery_ref"] == "delivery:slack:home"


def test_scope_payload_includes_all_tenant_and_routing_fields(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="org-1",
        workspace_id="ws-1",
        workspace_type="slack",
        project_id="proj-1",
        external_channel_id="ch-1",
        external_thread_id="th-1",
        conversation_id="conv-1",
        user_id="user-1",
        agent_profile_id="ap-1",
        run_type="conversation",
        parent_session_id="parent-1",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery:slack:home",
    )
    backend.deliver(ctx, {})

    _, _, _, body = delivery_server.requests[0]
    cp = body["context"]
    assert cp["mode"] == "agentops"
    assert cp["org_id"] == "org-1"
    assert cp["workspace_id"] == "ws-1"
    assert cp["workspace_type"] == "slack"
    assert cp["project_id"] == "proj-1"
    assert cp["external_channel_id"] == "ch-1"
    assert cp["external_thread_id"] == "th-1"
    assert cp["conversation_id"] == "conv-1"
    assert cp["user_id"] == "user-1"
    assert cp["agent_profile_id"] == "ap-1"
    assert cp["run_type"] == "conversation"
    assert cp["parent_session_id"] == "parent-1"
    assert cp["backend_profile"] == "compose-self-hosted"
    assert cp["delivery_ref"] == "delivery:slack:home"


def test_scope_payload_excludes_permissions_ref(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="org-1",
        workspace_id="ws-1",
        permissions_ref="permissions-secret-sentinel",
    )
    backend.deliver(ctx, {})

    _, _, _, body = delivery_server.requests[0]
    cp = body["context"]
    assert "permissions_ref" not in cp
    assert "permissions-secret-sentinel" not in str(cp)


def test_scope_payload_excludes_run_id_and_job_id(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="org-1",
        workspace_id="ws-1",
        run_id="run-secret-sentinel",
        job_id="job-secret-sentinel",
    )
    backend.deliver(ctx, {})

    _, _, _, body = delivery_server.requests[0]
    cp = body["context"]
    assert "run_id" not in cp
    assert "job_id" not in cp
    assert "run-secret-sentinel" not in str(cp)
    assert "job-secret-sentinel" not in str(cp)


def test_none_context_produces_empty_scope(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}")
    backend.deliver(None, {})

    _, _, _, body = delivery_server.requests[0]
    assert body.get("context") == {}


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_rejects_empty_base_url():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("")


def test_rejects_relative_url():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("/api/v1")


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("ftp://example.test")


def test_rejects_url_with_credentials():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("https://user:pass@example.test")


def test_rejects_url_with_query():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("https://example.test?token=abc")


def test_rejects_url_with_fragment():
    with pytest.raises(ValueError):
        HttpDeliveryBackend("https://example.test#section")


@pytest.mark.parametrize("url", ["https://example.test?", "https://example.test#"])
def test_rejects_empty_query_or_fragment_delimiter(url):
    with pytest.raises(ValueError):
        HttpDeliveryBackend(url)


def test_rejects_invalid_port_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpDeliveryBackend("https://api.internal:secret-port-sentinel/control")

    assert "secret-port-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_whitespace_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpDeliveryBackend("https://api internal/control")

    assert "api internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_del_control_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpDeliveryBackend("https://api\x7finternal/control")

    assert "api\x7finternal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("url", [" https://example.test", "https://example.test ", "\thttps://example.test", "https://example.test\n"])
def test_rejects_leading_or_trailing_whitespace_without_normalizing(url):
    with pytest.raises(ValueError) as exc_info:
        HttpDeliveryBackend(url)

    assert "example.test" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_invalid_timeout_nan():
    with pytest.raises(ValueError, match="timeout"):
        HttpDeliveryBackend("https://example.test", timeout=float("nan"))


def test_rejects_invalid_timeout_inf():
    with pytest.raises(ValueError, match="timeout"):
        HttpDeliveryBackend("https://example.test", timeout=float("inf"))


def test_rejects_invalid_timeout_zero():
    with pytest.raises(ValueError, match="timeout"):
        HttpDeliveryBackend("https://example.test", timeout=0)


def test_rejects_invalid_timeout_negative():
    with pytest.raises(ValueError, match="timeout"):
        HttpDeliveryBackend("https://example.test", timeout=-1)


def test_rejects_non_numeric_timeout():
    with pytest.raises(ValueError, match="timeout") as exc_info:
        HttpDeliveryBackend("https://example.test", timeout="secret-timeout-sentinel")  # type: ignore[arg-type]

    assert "secret-timeout-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token transport
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(delivery_server):
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}", token="cp-secret-sentinel")
    backend.deliver(_ctx(), {"content": "hi"})

    _, path, auth, body = delivery_server.requests[0]
    assert auth == "Bearer cp-secret-sentinel"
    assert "cp-secret-sentinel" not in path
    assert "cp-secret-sentinel" not in repr(body)


def test_rejects_control_character_token_without_leaking_value():
    with pytest.raises(ValueError) as exc_info:
        HttpDeliveryBackend("https://api.internal", token="cp-secret-token\r\nX-Leak: yes")

    assert "cp-secret-token" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Error sanitization / logical endpoint labels
# ---------------------------------------------------------------------------


def test_transport_error_uses_logical_endpoint_label_and_suppresses_cause():
    backend = HttpDeliveryBackend("http://127.0.0.1:1", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(_ctx(), {"content": "hi"})
    msg = str(exc_info.value)
    assert "POST /delivery/deliver" in msg
    assert "127.0.0.1" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_transport_error_does_not_leak_delivery_ref():
    backend = HttpDeliveryBackend("http://127.0.0.1:1")
    ctx = _ctx(delivery_ref="delivery-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(ctx, {"content": "hi"})
    assert "delivery-secret-sentinel" not in str(exc_info.value)


def test_transport_error_does_not_leak_message_content():
    backend = HttpDeliveryBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(_ctx(), {"content": "message-content-sentinel"})
    assert "message-content-sentinel" not in str(exc_info.value)


def test_http_error_uses_logical_label_and_suppresses_cause(delivery_server):
    delivery_server.force_status = 500
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(_ctx(), {"content": "hi"})
    msg = str(exc_info.value)
    assert "POST /delivery/deliver" in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_uses_logical_label_and_suppresses_cause(delivery_server):
    delivery_server.raw_response = "non-json body sentinel"
    backend = HttpDeliveryBackend(f"http://127.0.0.1:{delivery_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(_ctx(), {"content": "hi"})
    msg = str(exc_info.value)
    assert "non-json body sentinel" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_utf8_response_uses_logical_label_and_suppresses_cause(monkeypatch):
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
    backend = HttpDeliveryBackend("https://api.internal", token="cp-secret-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        backend.deliver(_ctx(), {"content": "hi"})

    msg = str(exc_info.value)
    assert "raw-body-sentinel" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_delivery_backend_registers_for_compose_profile(delivery_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"delivery": "compose-self-hosted"},
                "options": {
                    "delivery": {
                        "base_url": f"http://127.0.0.1:{delivery_server.server_port}",
                        "token": "sentinel-token",
                    }
                },
            }
        }
    )
    register_http_delivery_backend(registry)

    backend = registry.get(BackendCapability.DELIVERY, _ctx())
    assert isinstance(backend, HttpDeliveryBackend)
    result = backend.deliver(_ctx(), {"content": "test"})
    assert result is None


def test_register_http_delivery_backend_preserves_timeout_validation(delivery_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"delivery": "compose-self-hosted"},
                "options": {
                    "delivery": {
                        "base_url": f"http://127.0.0.1:{delivery_server.server_port}",
                        "timeout": 0,
                    }
                },
            }
        }
    )
    register_http_delivery_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.DELIVERY, _ctx())
