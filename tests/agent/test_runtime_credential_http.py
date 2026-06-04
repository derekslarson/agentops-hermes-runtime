"""Tests for the M12B HTTP credential-resolver adapter.

Covers:
- resolve() transport and return values
- resolve() treats missing secret_ref key as malformed, null as None,
  non-string non-null as malformed with type-only error
- non-string input ref is rejected before any request
- returned secret_ref transported verbatim; ref/secret_ref never in error strings
- context scope payload includes credential/secret binding fields
  (mode/org/workspace/workspace_type/user/project/agent_profile/permissions_ref/backend_profile)
  and excludes run_id/job_id/conversation_id/delivery_ref
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token/ref/secret_ref leakage; logical endpoint labels used
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
from agent.runtime_credential_http import (
    HttpCredentialResolver,
    register_http_credential_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _CredentialHandler(BaseHTTPRequestHandler):
    server: "_CredentialServer"

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

        truncated_response = getattr(self.server, "truncated_response", None)
        if truncated_response is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(truncated_response) + 20))
            self.end_headers()
            self.wfile.write(truncated_response)
            return

        path = self.path.strip("/")

        if path == "credentials/resolve":
            secret_ref = getattr(self.server, "secret_ref", "secret:model/openrouter/default")
            self._json({"secret_ref": secret_ref})
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


class _CredentialServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    secret_ref: Any
    force_status: int | None
    raw_response: str | bytes | None
    truncated_response: bytes | None


@pytest.fixture
def credential_server():
    server = _CredentialServer(("127.0.0.1", 0), _CredentialHandler)
    server.requests = []
    server.secret_ref = "secret:model/openrouter/default"
    server.force_status = None
    server.raw_response = None
    server.truncated_response = None
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
# resolve
# ---------------------------------------------------------------------------


def test_resolve_posts_context_and_ref_returns_secret_ref(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}", token="sentinel-token")
    result = resolver.resolve(_ctx(), "model:openrouter/default")

    assert result == "secret:model/openrouter/default"
    assert len(credential_server.requests) == 1
    method, path, auth, body = credential_server.requests[0]
    assert method == "POST"
    assert path == "/credentials/resolve"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("ref") == "model:openrouter/default"


def test_resolve_returns_none_for_null_secret_ref(credential_server):
    credential_server.raw_response = '{"secret_ref": null}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    result = resolver.resolve(_ctx(), "absent-ref")

    assert result is None


def test_resolve_raises_on_missing_secret_ref_key(credential_server):
    credential_server.raw_response = '{}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError):
        resolver.resolve(_ctx(), "ref-missing-key")


def test_resolve_raises_on_non_string_non_null_secret_ref_int(credential_server):
    credential_server.raw_response = '{"secret_ref": 42}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-bad-type")
    assert "int" in str(exc_info.value)


def test_resolve_raises_on_non_string_non_null_secret_ref_list(credential_server):
    credential_server.raw_response = '{"secret_ref": ["a", "b"]}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-bad-type-list")
    assert "list" in str(exc_info.value)


def test_resolve_non_string_error_omits_raw_secret_ref(credential_server):
    credential_server.raw_response = '{"secret_ref": 99999}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-abc")
    assert "99999" not in str(exc_info.value)


def test_resolve_ref_not_in_error_on_malformed_response(credential_server):
    credential_server.raw_response = '{}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "cred-ref-sentinel")
    assert "cred-ref-sentinel" not in str(exc_info.value)


def test_resolve_rejects_non_string_ref_before_request(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises((TypeError, ValueError)) as exc_info:
        resolver.resolve(_ctx(), 123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert credential_server.requests == []


def test_resolve_returns_secret_ref_verbatim(credential_server):
    credential_server.secret_ref = "secret:github/derek/pat"
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    result = resolver.resolve(_ctx(), "tool:github/default")

    assert result == "secret:github/derek/pat"


# ---------------------------------------------------------------------------
# Scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_binding_fields(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    resolver.resolve(
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

    _, _, _, body = credential_server.requests[0]
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


def test_scope_payload_excludes_run_id_job_id_conversation_id_delivery_ref(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    resolver.resolve(_ctx(run_id="run-123", job_id="job-456", conversation_id="conv-789", delivery_ref="delivery:slack:home"), "ref-excl")

    _, _, _, body = credential_server.requests[0]
    ctx = body["context"]
    assert "run_id" not in ctx
    assert "job_id" not in ctx
    assert "conversation_id" not in ctx
    assert "delivery_ref" not in ctx


def test_scope_payload_is_empty_dict_for_none_context(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    resolver.resolve(None, "ref-none-ctx")

    _, _, _, body = credential_server.requests[0]
    assert body["context"] == {}


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_ref_not_in_http_error_message(credential_server):
    credential_server.force_status = 500
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "cred-ref-sentinel")

    assert "cred-ref-sentinel" not in str(exc_info.value)


def test_token_not_in_transport_error_message(credential_server):
    credential_server.force_status = 500
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}", token="super-secret-bearer")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-x")

    assert "super-secret-bearer" not in str(exc_info.value)


def test_base_url_not_in_error_message(credential_server):
    credential_server.force_status = 500
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-y")

    assert "127.0.0.1" not in str(exc_info.value)


def test_error_message_uses_logical_endpoint_label(credential_server):
    credential_server.force_status = 500
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/credentials/resolve"):
        resolver.resolve(_ctx(), "ref-z")


def test_no_raw_exception_chain_retained_on_http_error(credential_server):
    credential_server.force_status = 500
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-chain")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_no_raw_exception_chain_retained_on_transport_error():
    resolver = HttpCredentialResolver("http://127.0.0.1:19999")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-transport")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_timeout_error_is_sanitized_without_raw_exception_chain(monkeypatch):
    def fail_with_timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("timed out while reading secret-ref-sentinel")

    monkeypatch.setattr("urllib.request.urlopen", fail_with_timeout)
    resolver = HttpCredentialResolver("https://credentials.internal", token="token-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-timeout")

    text = str(exc_info.value)
    assert "timed out" not in text
    assert "secret-ref-sentinel" not in text
    assert "ref-timeout" not in text
    assert "token-sentinel" not in text
    assert "credentials.internal" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_utf8_response_body_is_sanitized(credential_server):
    credential_server.raw_response = b'{"secret_ref":"secret-ref-sentinel"}\xff'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-invalid-utf8")

    text = str(exc_info.value)
    assert "secret-ref-sentinel" not in text
    assert "ref-invalid-utf8" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_truncated_response_body_is_sanitized_without_retaining_partial_secret_ref(credential_server):
    credential_server.truncated_response = b'{"secret_ref":"secret-ref-sentinel'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-truncated-body")

    text = str(exc_info.value)
    assert "secret-ref-sentinel" not in text
    assert "ref-truncated-body" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_body_is_sanitized(credential_server):
    credential_server.raw_response = "not-json-secret-ref-sentinel"
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-non-json")

    text = str(exc_info.value)
    assert "not-json-secret-ref-sentinel" not in text
    assert "ref-non-json" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_object_json_response_fails_closed_with_type_only_error(credential_server):
    credential_server.raw_response = '["secret-ref-sentinel"]'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-array-json")

    text = str(exc_info.value)
    assert "list" in text
    assert "secret-ref-sentinel" not in text
    assert "ref-array-json" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_secret_ref_not_in_error_on_non_string_type(credential_server):
    credential_server.raw_response = '{"secret_ref": 42}'
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        resolver.resolve(_ctx(), "ref-check")
    assert "42" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_empty_base_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpCredentialResolver("")


def test_non_string_base_url_raises_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver(123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_http_scheme_raises_value_error():
    with pytest.raises(ValueError):
        HttpCredentialResolver("ftp://credentials.internal")


def test_credentials_in_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpCredentialResolver("https://user:***@credentials.internal")


def test_malformed_host_url_raises_sanitized_value_error_without_raw_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver("http://[credential-host-sentinel]")

    text = str(exc_info.value)
    assert "credential-host-sentinel" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("url", ["https://credentials.internal?foo=bar", "https://credentials.internal?"])
def test_query_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpCredentialResolver(url)


@pytest.mark.parametrize("url", ["https://credentials.internal#section", "https://credentials.internal#"])
def test_fragment_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpCredentialResolver(url)


def test_invalid_timeout_raises_value_error():
    with pytest.raises(ValueError):
        HttpCredentialResolver("https://credentials.internal", timeout=-1)


@pytest.mark.parametrize(
    "url",
    [" https://credentials.internal", "https://credentials.internal ", "\thttps://credentials.internal"],
)
def test_base_url_with_leading_or_trailing_whitespace_rejected_without_normalizing(url):
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver(url)

    assert "credentials.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_port_rejected_without_retaining_raw_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver("https://credentials.internal:secret-port")

    assert "secret-port" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_token_with_control_character_rejected_without_retaining_secret():
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver("https://credentials.internal", token="token-secret\nX-Leak: yes")

    assert "token-secret" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_string_token_rejected_with_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpCredentialResolver("https://credentials.internal", token=123)  # type: ignore[arg-type]

    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token in header only
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}", token="tok-123")
    resolver.resolve(_ctx(), "ref-tok")

    _, _, auth, body = credential_server.requests[0]
    assert auth == "Bearer tok-123"
    body_str = json.dumps(body)
    assert "tok-123" not in body_str


def test_no_token_sends_no_authorization_header(credential_server):
    resolver = HttpCredentialResolver(f"http://127.0.0.1:{credential_server.server_port}")
    resolver.resolve(_ctx(), "ref-no-tok")

    _, _, auth, _ = credential_server.requests[0]
    assert auth is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_credential_backend_registers_credential_capability():
    registry = RuntimeBackendRegistry()
    register_http_credential_backend(registry)

    assert "compose-self-hosted" in registry.registered_profiles(BackendCapability.CREDENTIAL)


def test_register_http_credential_backend_factory_creates_http_credential_resolver(credential_server):
    registry = RuntimeBackendRegistry()
    register_http_credential_backend(registry)
    registry.set_capability_options(
        BackendCapability.CREDENTIAL,
        {"base_url": f"http://127.0.0.1:{credential_server.server_port}", "token": "t"},
        merge=False,
    )
    context = _ctx()
    resolver = registry.get(BackendCapability.CREDENTIAL, context)
    assert isinstance(resolver, HttpCredentialResolver)
