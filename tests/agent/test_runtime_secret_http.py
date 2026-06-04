"""Tests for the M12B HTTP secret-store adapter.

Covers:
- get_secret/put_secret transport and return values
- get_secret treats missing value key as malformed, null value as None,
  non-string non-null as malformed with type-only error
- put_secret rejects non-string values before any request
- secret values transported verbatim; refs/values never in error strings
- context scope payload includes credential/secret binding fields
  (mode/org/workspace/workspace_type/user/project/agent_profile/permissions_ref/backend_profile)
  and excludes run_id/job_id/conversation_id/delivery_ref
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token/ref/value leakage; logical endpoint labels used
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
from agent.runtime_secret_http import (
    HttpSecretStoreBackend,
    register_http_secret_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _SecretHandler(BaseHTTPRequestHandler):
    server: "_SecretServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        force_status = getattr(self.server, "force_status", None)
        if force_status is not None:
            self.send_error(force_status)
            return

        raw_response = getattr(self.server, "raw_response", None)
        if raw_response is not None:
            raw = raw_response if isinstance(raw_response, bytes) else str(raw_response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        path = self.path.strip("/")

        if path == "secrets/get":
            value = getattr(self.server, "get_value", "secret-value-abc")
            self._json({"value": value})
            return

        if path == "secrets/put":
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


class _SecretServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    get_value: Any
    force_status: int | None
    raw_response: str | None


@pytest.fixture
def secret_server():
    server = _SecretServer(("127.0.0.1", 0), _SecretHandler)
    server.requests = []
    server.get_value = "secret-value-abc"
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
        workspace_type="slack",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="agentops-runtime",
        run_type="conversation",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery:slack:home",
        run_id="run-abc",
        job_id="job-xyz",
        permissions_ref="perms:acme:derek",
    )
    base.update(overrides)
    return RuntimeContext(**base)


# ---------------------------------------------------------------------------
# get_secret
# ---------------------------------------------------------------------------


def test_get_secret_posts_context_and_ref_returns_string(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}", token="sentinel-token")
    result = backend.get_secret(_ctx(), "my-secret-ref")

    assert result == "secret-value-abc"
    assert len(secret_server.requests) == 1
    method, path, auth, body = secret_server.requests[0]
    assert method == "POST"
    assert path == "/secrets/get"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("ref") == "my-secret-ref"


def test_get_secret_returns_none_for_null_value(secret_server):
    secret_server.raw_response = '{"value": null}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    result = backend.get_secret(_ctx(), "absent-ref")

    assert result is None


def test_get_secret_raises_on_missing_value_key(secret_server):
    secret_server.raw_response = '{}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.get_secret(_ctx(), "ref-missing-value")


def test_get_secret_raises_on_non_string_non_null_value_int(secret_server):
    secret_server.raw_response = '{"value": 42}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-bad-type")
    assert "int" in str(exc_info.value)


def test_get_secret_raises_on_non_string_non_null_value_list(secret_server):
    secret_server.raw_response = '{"value": ["a", "b"]}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-bad-type-list")
    assert "list" in str(exc_info.value)


def test_get_secret_non_string_error_omits_raw_value(secret_server):
    secret_server.raw_response = '{"value": 99999}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-abc")
    assert "99999" not in str(exc_info.value)


def test_get_secret_ref_not_in_error_on_malformed_response(secret_server):
    secret_server.raw_response = '{}'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "secret-ref-sentinel")
    assert "secret-ref-sentinel" not in str(exc_info.value)


def test_get_secret_rejects_non_string_ref_before_request(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises((TypeError, ValueError)) as exc_info:
        backend.get_secret(_ctx(), 123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert secret_server.requests == []


# ---------------------------------------------------------------------------
# put_secret
# ---------------------------------------------------------------------------


def test_put_secret_posts_context_ref_value_returns_none(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}", token="sentinel-token")
    result = backend.put_secret(_ctx(), "my-put-ref", "my-put-value")

    assert result is None
    assert len(secret_server.requests) == 1
    method, path, auth, body = secret_server.requests[0]
    assert method == "POST"
    assert path == "/secrets/put"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("ref") == "my-put-ref"
    assert body.get("value") == "my-put-value"


def test_put_secret_rejects_non_string_ref_before_request(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises((TypeError, ValueError)) as exc_info:
        backend.put_secret(_ctx(), 123, "value-abc")  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert "value-abc" not in str(exc_info.value)
    assert secret_server.requests == []


def test_put_secret_rejects_non_string_value_int_before_request(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises((TypeError, ValueError)):
        backend.put_secret(_ctx(), "ref-abc", 42)  # type: ignore[arg-type]

    assert secret_server.requests == []


def test_put_secret_rejects_non_string_value_none_before_request(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises((TypeError, ValueError)):
        backend.put_secret(_ctx(), "ref-abc", None)  # type: ignore[arg-type]

    assert secret_server.requests == []


def test_put_secret_rejects_non_string_value_bytes_before_request(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises((TypeError, ValueError)):
        backend.put_secret(_ctx(), "ref-abc", b"bytes-secret")  # type: ignore[arg-type]

    assert secret_server.requests == []


def test_put_secret_transports_value_verbatim(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    backend.put_secret(_ctx(), "ref-verbatim", "actual-secret-value-xyz")

    _, _, _, body = secret_server.requests[0]
    assert body["value"] == "actual-secret-value-xyz"


# ---------------------------------------------------------------------------
# Scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_binding_fields(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    backend.get_secret(
        _ctx(
            mode="agentops",
            org_id="org-1",
            workspace_id="ws-2",
            workspace_type="slack",
            user_id="u-3",
            project_id="proj-4",
            agent_profile_id="profile-5",
            permissions_ref="perms:org-1:u-3",
            backend_profile="compose-self-hosted",
        ),
        "ref-scope",
    )

    _, _, _, body = secret_server.requests[0]
    ctx = body["context"]
    assert ctx["mode"] == "agentops"
    assert ctx["org_id"] == "org-1"
    assert ctx["workspace_id"] == "ws-2"
    assert ctx["workspace_type"] == "slack"
    assert ctx["user_id"] == "u-3"
    assert ctx["project_id"] == "proj-4"
    assert ctx["agent_profile_id"] == "profile-5"
    assert ctx["permissions_ref"] == "perms:org-1:u-3"
    assert ctx["backend_profile"] == "compose-self-hosted"


def test_scope_payload_excludes_run_id_job_id_conversation_id_delivery_ref(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    backend.get_secret(_ctx(run_id="run-123", job_id="job-456", conversation_id="conv-789", delivery_ref="delivery:slack:home"), "ref-excl")

    _, _, _, body = secret_server.requests[0]
    ctx = body["context"]
    assert "run_id" not in ctx
    assert "job_id" not in ctx
    assert "conversation_id" not in ctx
    assert "delivery_ref" not in ctx


def test_scope_payload_is_empty_dict_for_none_context(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    backend.get_secret(None, "ref-none-ctx")

    _, _, _, body = secret_server.requests[0]
    assert body["context"] == {}


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_ref_not_in_http_error_message(secret_server):
    secret_server.force_status = 500
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "secret-ref-sentinel")

    assert "secret-ref-sentinel" not in str(exc_info.value)


def test_value_not_in_http_error_message(secret_server):
    secret_server.force_status = 503
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.put_secret(_ctx(), "ref-abc", "secret-value-sentinel")

    assert "secret-value-sentinel" not in str(exc_info.value)


def test_token_not_in_transport_error_message(secret_server):
    secret_server.force_status = 500
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}", token="super-secret-bearer")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-x")

    assert "super-secret-bearer" not in str(exc_info.value)


def test_base_url_not_in_error_message(secret_server):
    secret_server.force_status = 500
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-y")

    assert "127.0.0.1" not in str(exc_info.value)


def test_error_message_uses_logical_endpoint_label_for_get(secret_server):
    secret_server.force_status = 500
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/secrets/get"):
        backend.get_secret(_ctx(), "ref-z")


def test_error_message_uses_logical_endpoint_label_for_put(secret_server):
    secret_server.force_status = 503
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/secrets/put"):
        backend.put_secret(_ctx(), "ref-w", "val-w")


def test_no_raw_exception_chain_retained_on_http_error(secret_server):
    secret_server.force_status = 500
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-chain")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_no_raw_exception_chain_retained_on_transport_error():
    backend = HttpSecretStoreBackend("http://127.0.0.1:19999")
    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-transport")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_utf8_response_body_is_sanitized(secret_server):
    secret_server.raw_response = b'{"value":"secret-value-sentinel"}\xff'
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        backend.get_secret(_ctx(), "ref-invalid-utf8")

    text = str(exc_info.value)
    assert "secret-value-sentinel" not in text
    assert "ref-invalid-utf8" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_empty_base_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpSecretStoreBackend("")


def test_non_string_base_url_raises_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpSecretStoreBackend(123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_http_scheme_raises_value_error():
    with pytest.raises(ValueError):
        HttpSecretStoreBackend("ftp://secrets.internal")


def test_credentials_in_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpSecretStoreBackend("https://user:pass@secrets.internal")


@pytest.mark.parametrize("url", ["https://secrets.internal?foo=bar", "https://secrets.internal?"])
def test_query_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpSecretStoreBackend(url)


@pytest.mark.parametrize("url", ["https://secrets.internal#section", "https://secrets.internal#"])
def test_fragment_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpSecretStoreBackend(url)


def test_invalid_timeout_raises_value_error():
    with pytest.raises(ValueError):
        HttpSecretStoreBackend("https://secrets.internal", timeout=-1)


@pytest.mark.parametrize(
    "url",
    [" https://secrets.internal", "https://secrets.internal ", "\thttps://secrets.internal"],
)
def test_base_url_with_leading_or_trailing_whitespace_rejected_without_normalizing(url):
    with pytest.raises(ValueError) as exc_info:
        HttpSecretStoreBackend(url)

    assert "secrets.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_port_rejected_without_retaining_raw_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpSecretStoreBackend("https://secrets.internal:secret-port")

    assert "secret-port" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_token_with_control_character_rejected_without_retaining_secret():
    with pytest.raises(ValueError) as exc_info:
        HttpSecretStoreBackend("https://secrets.internal", token="token-secret\nX-Leak: yes")

    assert "token-secret" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_string_token_rejected_with_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpSecretStoreBackend("https://secrets.internal", token=123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token in header only
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}", token="tok-123")
    backend.get_secret(_ctx(), "ref-tok")

    _, _, auth, body = secret_server.requests[0]
    assert auth == "Bearer tok-123"
    body_str = json.dumps(body)
    assert "tok-123" not in body_str


def test_no_token_sends_no_authorization_header(secret_server):
    backend = HttpSecretStoreBackend(f"http://127.0.0.1:{secret_server.server_port}")
    backend.get_secret(_ctx(), "ref-no-tok")

    _, _, auth, _ = secret_server.requests[0]
    assert auth is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_secret_backend_registers_secret_capability():
    registry = RuntimeBackendRegistry()
    register_http_secret_backend(registry)

    assert "compose-self-hosted" in registry.registered_profiles(BackendCapability.SECRET)


def test_register_http_secret_backend_factory_creates_http_secret_backend(secret_server):
    registry = RuntimeBackendRegistry()
    register_http_secret_backend(registry)
    registry.set_capability_options(
        BackendCapability.SECRET,
        {"base_url": f"http://127.0.0.1:{secret_server.server_port}", "token": "t"},
        merge=False,
    )
    context = _ctx()
    backend = registry.get(BackendCapability.SECRET, context)
    assert isinstance(backend, HttpSecretStoreBackend)
