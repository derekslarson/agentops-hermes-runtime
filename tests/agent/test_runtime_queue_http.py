"""Tests for the M12B HTTP queue adapter.

Covers:
- enqueue/claim/ack/nack/extend_lease transport and return values
- malformed responses fail closed (missing field, wrong type)
- receipt values never appear in error message strings
- payload content preserved verbatim (including secret-looking keys); never in error strings
- context scope payload includes tenant fields AND run_id/job_id; permissions_ref excluded
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token leakage; logical endpoint labels used
- AGENTOPS_QUEUE_URL env var is irrelevant to the HTTP adapter
- registry integration
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_queue_http import (
    HttpQueueBackend,
    register_http_queue_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _QueueHandler(BaseHTTPRequestHandler):
    server: "_QueueServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        force_status = getattr(self.server, "force_status", None)
        if force_status is not None:
            self.send_error(force_status)
            return

        raw_response = getattr(self.server, "raw_response", None)
        if raw_response is not None:
            raw = str(raw_response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        path = self.path.strip("/")

        if path == "queue/enqueue":
            bad_type = getattr(self.server, "enqueue_bad_type", None)
            if bad_type is not None:
                self._json({"receipt": bad_type})
                return
            if getattr(self.server, "enqueue_missing_receipt", False):
                self._json({})
                return
            self._json({"receipt": getattr(self.server, "receipt_value", "receipt-abc")})
            return

        if path == "queue/claim":
            if getattr(self.server, "claim_missing_item", False):
                self._json({})
                return
            item = getattr(self.server, "claim_item", {"receipt": "r1", "payload": {"task": "run"}})
            self._json({"item": item})
            return

        if path == "queue/ack":
            self._json({})
            return

        if path == "queue/nack":
            self._json({})
            return

        if path == "queue/extend-lease":
            self._json({})
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


class _QueueServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    receipt_value: str
    enqueue_bad_type: Any
    enqueue_missing_receipt: bool
    claim_item: Any
    claim_missing_item: bool
    force_status: int | None
    raw_response: str | None


@pytest.fixture
def queue_server():
    server = _QueueServer(("127.0.0.1", 0), _QueueHandler)
    server.requests = []
    server.receipt_value = "receipt-abc"
    server.enqueue_bad_type = None
    server.enqueue_missing_receipt = False
    server.claim_item = {"receipt": "r1", "payload": {"task": "run"}}
    server.claim_missing_item = False
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
        run_id="run-abc",
        job_id="job-xyz",
    )
    base.update(overrides)
    return RuntimeContext(**base)


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def test_enqueue_posts_context_payload_idempotency_key_returns_receipt(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="sentinel-token")
    result = backend.enqueue(_ctx(), {"task": "run", "arg": 1}, idempotency_key="idem-1")

    assert result == "receipt-abc"
    assert len(queue_server.requests) == 1
    method, path, auth, body = queue_server.requests[0]
    assert method == "POST"
    assert path == "/queue/enqueue"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body["context"]["org_id"] == "acme"
    assert body["payload"] == {"task": "run", "arg": 1}
    assert body["idempotency_key"] == "idem-1"


def test_enqueue_without_idempotency_key_omits_field(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.enqueue(_ctx(), {"task": "run"})

    _, _, _, body = queue_server.requests[0]
    assert "idempotency_key" not in body


def test_enqueue_payload_preserved_verbatim_including_secret_looking_keys(queue_server):
    secret_payload = {"api_key": "sk-secret-value", "token": "bearer-xyz", "data": [1, 2, 3]}
    queue_server.receipt_value = "r-preserve"
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    result = backend.enqueue(_ctx(), secret_payload)

    assert result == "r-preserve"
    _, _, _, body = queue_server.requests[0]
    assert body["payload"] == secret_payload
    assert body["payload"]["api_key"] == "sk-secret-value"


def test_enqueue_raises_on_missing_receipt_field(queue_server):
    queue_server.enqueue_missing_receipt = True
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.enqueue(_ctx(), {"task": "run"})


def test_enqueue_raises_on_non_string_receipt_int(queue_server):
    queue_server.enqueue_bad_type = 123
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.enqueue(_ctx(), {"task": "run"})


def test_enqueue_raises_on_non_string_receipt_none(queue_server):
    queue_server.enqueue_bad_type = None
    # None receipt is stored in the server response; since receipt key will be present with None value...
    # Actually the server sends {"receipt": null}; we need to test this as a bad type.
    # Override raw_response to send null receipt explicitly
    queue_server.raw_response = '{"receipt": null}'
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.enqueue(_ctx(), {"task": "run"})


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_posts_context_and_capability_returns_item(queue_server):
    queue_server.claim_item = {"receipt": "r1", "payload": {"task": "run"}}
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="sentinel-token")
    result = backend.claim(_ctx(), capability="worker")

    assert result == {"receipt": "r1", "payload": {"task": "run"}}
    assert len(queue_server.requests) == 1
    method, path, auth, body = queue_server.requests[0]
    assert method == "POST"
    assert path == "/queue/claim"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("capability") == "worker"


def test_claim_returns_none_when_server_returns_null_item(queue_server):
    queue_server.claim_item = None
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    result = backend.claim(_ctx())

    assert result is None


def test_claim_without_capability_omits_field(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.claim(_ctx())

    _, _, _, body = queue_server.requests[0]
    assert "capability" not in body


def test_claim_raises_on_missing_item_key(queue_server):
    queue_server.claim_missing_item = True
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim(_ctx())


def test_claim_raises_on_non_dict_non_null_item(queue_server):
    queue_server.raw_response = '{"item": "not-a-dict"}'
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim(_ctx())


# ---------------------------------------------------------------------------
# ack
# ---------------------------------------------------------------------------


def test_ack_posts_context_and_receipt_returns_none(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="sentinel-token")
    result = backend.ack(_ctx(), "receipt-99")

    assert result is None
    assert len(queue_server.requests) == 1
    method, path, auth, body = queue_server.requests[0]
    assert method == "POST"
    assert path == "/queue/ack"
    assert auth == "Bearer sentinel-token"
    assert body.get("receipt") == "receipt-99"
    assert isinstance(body.get("context"), dict)


def test_ack_extracts_receipt_from_claimed_item_mapping(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    result = backend.ack(_ctx(), {"receipt": "receipt-from-item", "payload": {"secret": "payload-value"}})

    assert result is None
    _, _, _, body = queue_server.requests[0]
    assert body["receipt"] == "receipt-from-item"
    assert body["receipt"] != {"receipt": "receipt-from-item", "payload": {"secret": "payload-value"}}


def test_ack_rejects_missing_receipt_mapping_without_request_or_payload_leak(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(ValueError) as exc_info:
        backend.ack(_ctx(), {"payload": {"secret": "payload-value"}})

    assert queue_server.requests == []
    assert "payload-value" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_ack_rejects_empty_receipt_string_without_request(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(ValueError):
        backend.ack(_ctx(), "")

    assert queue_server.requests == []


# ---------------------------------------------------------------------------
# nack
# ---------------------------------------------------------------------------


def test_nack_posts_context_receipt_requeue_returns_none(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="sentinel-token")
    result = backend.nack(_ctx(), "receipt-88", requeue=True)

    assert result is None
    assert len(queue_server.requests) == 1
    method, path, auth, body = queue_server.requests[0]
    assert method == "POST"
    assert path == "/queue/nack"
    assert auth == "Bearer sentinel-token"
    assert body.get("receipt") == "receipt-88"
    assert body.get("requeue") is True


def test_nack_requeue_false_sent_in_body(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.nack(_ctx(), "receipt-77", requeue=False)

    _, _, _, body = queue_server.requests[0]
    assert body.get("requeue") is False


def test_nack_extracts_receipt_from_claimed_item_mapping(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.nack(_ctx(), {"receipt": "receipt-from-item", "payload": {"task": "run"}}, requeue=False)

    _, _, _, body = queue_server.requests[0]
    assert body["receipt"] == "receipt-from-item"


# ---------------------------------------------------------------------------
# extend_lease
# ---------------------------------------------------------------------------


def test_extend_lease_posts_context_receipt_seconds_returns_none(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="sentinel-token")
    result = backend.extend_lease(_ctx(), "receipt-55", seconds=30)

    assert result is None
    assert len(queue_server.requests) == 1
    method, path, auth, body = queue_server.requests[0]
    assert method == "POST"
    assert path == "/queue/extend-lease"
    assert auth == "Bearer sentinel-token"
    assert body.get("receipt") == "receipt-55"
    assert body.get("seconds") == 30


def test_extend_lease_extracts_receipt_from_claimed_item_mapping(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.extend_lease(_ctx(), {"receipt": "receipt-from-item", "payload": {"task": "run"}}, seconds=45)

    _, _, _, body = queue_server.requests[0]
    assert body["receipt"] == "receipt-from-item"


# ---------------------------------------------------------------------------
# Scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_run_id_and_job_id(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.enqueue(_ctx(run_id="run-123", job_id="job-456"), {"x": 1})

    _, _, _, body = queue_server.requests[0]
    ctx = body["context"]
    assert ctx["run_id"] == "run-123"
    assert ctx["job_id"] == "job-456"


def test_scope_payload_excludes_permissions_ref(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.enqueue(_ctx(), {"x": 1})

    _, _, _, body = queue_server.requests[0]
    ctx = body["context"]
    assert "permissions_ref" not in ctx


def test_scope_payload_includes_tenant_fields(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.enqueue(_ctx(org_id="org-1", workspace_id="ws-2", user_id="u-3"), {"x": 1})

    _, _, _, body = queue_server.requests[0]
    ctx = body["context"]
    assert ctx["org_id"] == "org-1"
    assert ctx["workspace_id"] == "ws-2"
    assert ctx["user_id"] == "u-3"


def test_scope_payload_is_empty_dict_for_none_context(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.enqueue(None, {"x": 1})

    _, _, _, body = queue_server.requests[0]
    assert body["context"] == {}


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_receipt_value_not_in_http_error_message(queue_server):
    queue_server.force_status = 500
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.ack(_ctx(), "secret-receipt-value")

    assert "secret-receipt-value" not in str(exc_info.value)


def test_payload_content_not_in_transport_error_message(queue_server):
    queue_server.force_status = 503
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.enqueue(_ctx(), {"secret_key": "secret-payload-value"})

    assert "secret-payload-value" not in str(exc_info.value)
    assert "secret_key" not in str(exc_info.value)


def test_token_not_in_transport_error_message(queue_server):
    queue_server.force_status = 500
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="super-secret-bearer")
    with pytest.raises(RuntimeError) as exc_info:
        backend.enqueue(_ctx(), {"x": 1})

    assert "super-secret-bearer" not in str(exc_info.value)


def test_base_url_not_in_error_message(queue_server):
    queue_server.force_status = 500
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim(_ctx())

    assert "127.0.0.1" not in str(exc_info.value)


def test_error_message_uses_logical_endpoint_label_for_enqueue(queue_server):
    queue_server.force_status = 500
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/queue/enqueue"):
        backend.enqueue(_ctx(), {"x": 1})


def test_error_message_uses_logical_endpoint_label_for_claim(queue_server):
    queue_server.force_status = 503
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/queue/claim"):
        backend.claim(_ctx())


def test_no_raw_exception_chain_retained(queue_server):
    queue_server.force_status = 500
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.ack(_ctx(), "some-receipt")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_empty_base_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpQueueBackend("")


def test_non_http_scheme_raises_value_error():
    with pytest.raises(ValueError):
        HttpQueueBackend("ftp://queue.internal")


def test_credentials_in_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpQueueBackend("https://user:pass@queue.internal")


@pytest.mark.parametrize("url", ["https://queue.internal?foo=bar", "https://queue.internal?"])
def test_query_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpQueueBackend(url)


@pytest.mark.parametrize("url", ["https://queue.internal#section", "https://queue.internal#"])
def test_fragment_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpQueueBackend(url)


def test_invalid_timeout_raises_value_error():
    with pytest.raises(ValueError):
        HttpQueueBackend("https://queue.internal", timeout=-1)


@pytest.mark.parametrize("url", [" https://queue.internal", "https://queue.internal ", "\thttps://queue.internal"])
def test_base_url_with_leading_or_trailing_whitespace_rejected_without_normalizing(url):
    with pytest.raises(ValueError) as exc_info:
        HttpQueueBackend(url)

    assert "queue.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_port_rejected_without_retaining_raw_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpQueueBackend("https://queue.internal:secret-port")

    assert "secret-port" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_token_with_control_character_rejected_without_retaining_secret():
    with pytest.raises(ValueError) as exc_info:
        HttpQueueBackend("https://queue.internal", token="token-secret\nX-Leak: yes")

    assert "token-secret" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token in header only
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}", token="tok-123")
    backend.enqueue(_ctx(), {"x": 1})

    _, _, auth, body = queue_server.requests[0]
    assert auth == "Bearer tok-123"
    body_str = json.dumps(body)
    assert "tok-123" not in body_str


def test_no_token_sends_no_authorization_header(queue_server):
    backend = HttpQueueBackend(f"http://127.0.0.1:{queue_server.server_port}")
    backend.claim(_ctx())

    _, _, auth, _ = queue_server.requests[0]
    assert auth is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_queue_backend_registers_queue_capability():
    registry = RuntimeBackendRegistry()
    register_http_queue_backend(registry)

    assert "compose-self-hosted" in registry.registered_profiles(BackendCapability.QUEUE)


def test_register_http_queue_backend_factory_creates_http_queue_backend(queue_server):
    registry = RuntimeBackendRegistry()
    register_http_queue_backend(registry)
    registry.set_capability_options(
        BackendCapability.QUEUE,
        {"base_url": f"http://127.0.0.1:{queue_server.server_port}", "token": "t"},
        merge=False,
    )
    context = _ctx()
    backend = registry.get(BackendCapability.QUEUE, context)
    assert isinstance(backend, HttpQueueBackend)
