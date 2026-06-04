"""Tests for the M12B compose credential-resolver server-side endpoint."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from agentops_runtime import compose_services

_CONTEXT = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "project_id": "proj1",
    "agent_profile_id": "bot",
    "permissions_ref": None,
    "backend_profile": "compose-self-hosted",
}


class _FakeCredentialBackend:
    def __init__(self, result: str | None = "cred-abc123"):
        self._result = result
        self.calls: list = []

    def resolve(self, context, ref):
        self.calls.append((context, ref))
        return self._result


class _BrokenCredentialBackend:
    def resolve(self, context, ref):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret cred-LEAKSECRETREF")


class _NonStringRefBackend:
    def resolve(self, context, ref):
        return 12345


def _call(method="POST", path="/credentials/resolve", qs="", body=b"", auth=None, token=None, backend=None):
    from agentops_runtime.credentials_api import handle_credential_request
    if backend is None:
        backend = _FakeCredentialBackend()
    return handle_credential_request(
        method=method,
        path=path,
        query_string=qs,
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Import and happy path
# ---------------------------------------------------------------------------


def test_import_succeeds():
    from agentops_runtime.credentials_api import handle_credential_request
    assert callable(handle_credential_request)


def test_happy_path_returns_secret_ref():
    body = json.dumps({"context": _CONTEXT, "ref": "MY_API_KEY"}).encode()
    backend = _FakeCredentialBackend(result="cred-abc123")
    status, response = _call(body=body, backend=backend)
    assert status == 200
    assert response.get("secret_ref") == "cred-abc123"


def test_backend_null_result_returns_null_secret_ref():
    body = json.dumps({"context": _CONTEXT, "ref": "MISSING_KEY"}).encode()
    backend = _FakeCredentialBackend(result=None)
    status, response = _call(body=body, backend=backend)
    assert status == 200
    assert response.get("secret_ref") is None


def test_empty_context_passes_none_to_backend():
    body = json.dumps({"context": {}, "ref": "MY_KEY"}).encode()
    backend = _FakeCredentialBackend(result=None)
    status, response = _call(body=body, backend=backend)
    assert status == 200
    assert "secret_ref" in response
    ctx_passed, _ = backend.calls[0]
    assert ctx_passed is None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_ref_returns_400():
    body = json.dumps({"context": _CONTEXT}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response


def test_non_string_ref_returns_400():
    body = json.dumps({"context": _CONTEXT, "ref": 123}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response


def test_empty_body_returns_400():
    status, response = _call(body=b"")
    assert status == 400
    assert "error" in response


def test_invalid_json_body_returns_400():
    status, response = _call(body=b"not json {")
    assert status == 400
    assert "error" in response


def test_non_object_json_body_returns_400():
    status, response = _call(body=json.dumps(["ref", "key"]).encode())
    assert status == 400
    assert "error" in response


# ---------------------------------------------------------------------------
# Context validation — fail-closed for non-agentops mode
# ---------------------------------------------------------------------------


def test_local_mode_context_rejected():
    ctx = dict(_CONTEXT, mode="local")
    body = json.dumps({"context": ctx, "ref": "KEY"}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response
    assert "invalid context" in response["error"]


def test_unknown_mode_context_rejected():
    ctx = dict(_CONTEXT, mode="cloud-custom")
    body = json.dumps({"context": ctx, "ref": "KEY"}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response


def test_non_empty_context_missing_mode_rejected():
    ctx = {k: v for k, v in _CONTEXT.items() if k != "mode"}
    body = json.dumps({"context": ctx, "ref": "KEY"}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response


def test_non_dict_context_rejected():
    body = json.dumps({"context": ["mode", "agentops"], "ref": "KEY"}).encode()
    status, response = _call(body=body)
    assert status == 400
    assert "error" in response


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_missing_returns_401_when_token_configured():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body, auth=None, token="expected-token")
    assert status == 401
    assert response.get("error") == "unauthorized"


def test_auth_wrong_returns_401():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body, auth="Bearer wrong-token", token="expected-token")
    assert status == 401
    assert response.get("error") == "unauthorized"


def test_auth_correct_returns_200():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body, auth="Bearer correct-token", token="correct-token")
    assert status == 200
    assert "secret_ref" in response


def test_no_token_configured_allows_unauthenticated():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body, auth=None, token=None)
    assert status == 200


# ---------------------------------------------------------------------------
# Backend error sanitization
# ---------------------------------------------------------------------------


def test_backend_exception_returns_500_with_generic_error():
    body = json.dumps({"context": _CONTEXT, "ref": "MY_KEY"}).encode()
    backend = _BrokenCredentialBackend()
    status, response = _call(body=body, backend=backend)
    assert status == 500
    assert response.get("error") == "internal error"


def test_backend_exception_text_not_leaked():
    body = json.dumps({"context": _CONTEXT, "ref": "MY_KEY"}).encode()
    backend = _BrokenCredentialBackend()
    status, response = _call(body=body, backend=backend)
    error_text = json.dumps(response)
    assert "LEAKSENTINEL" not in error_text
    assert "password=secret" not in error_text


def test_non_string_backend_result_returns_500():
    body = json.dumps({"context": _CONTEXT, "ref": "MY_KEY"}).encode()
    backend = _NonStringRefBackend()
    status, response = _call(body=body, backend=backend)
    assert status == 500
    assert response.get("error") == "internal error"


# ---------------------------------------------------------------------------
# Scope isolation — deterministic resolver produces distinct refs per field
# ---------------------------------------------------------------------------

_SCOPE_BASE = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "agent_profile_id": "bot",
    "workspace_type": "team",
    "user_id": "alice",
    "project_id": "proj1",
    "permissions_ref": "perm1",
    "backend_profile": "compose-self-hosted",
}


def _make_resolver():
    return compose_services._make_credential_resolver({})


def _resolve_with(overrides: dict, ref: str = "API_KEY") -> str:
    from agent.runtime_context import RuntimeContext
    resolver = _make_resolver()
    ctx = RuntimeContext.from_mapping(dict(_SCOPE_BASE, **overrides))
    result = resolver.resolve(ctx, ref)
    assert result is not None
    return result


def test_scope_isolation_user_id_produces_distinct_refs():
    ref_alice = _resolve_with({"user_id": "alice"})
    ref_bob = _resolve_with({"user_id": "bob"})
    assert ref_alice != ref_bob


def test_scope_isolation_project_id_produces_distinct_refs():
    ref_a = _resolve_with({"project_id": "proj-a"})
    ref_b = _resolve_with({"project_id": "proj-b"})
    assert ref_a != ref_b


def test_scope_isolation_workspace_type_produces_distinct_refs():
    ref_team = _resolve_with({"workspace_type": "team"})
    ref_personal = _resolve_with({"workspace_type": "personal"})
    assert ref_team != ref_personal


def test_scope_isolation_permissions_ref_produces_distinct_refs():
    ref_a = _resolve_with({"permissions_ref": "perm-a"})
    ref_b = _resolve_with({"permissions_ref": "perm-b"})
    assert ref_a != ref_b


def test_scope_isolation_backend_profile_produces_distinct_refs():
    ref_a = _resolve_with({"backend_profile": "compose-self-hosted"})
    ref_b = _resolve_with({"backend_profile": "compose-cloud"})
    assert ref_a != ref_b


def test_scope_isolation_delimiter_in_adjacent_fields_cannot_collide():
    # user_id="alice|project" + project_id="x" must NOT collide with
    # user_id="alice" + project_id="project|x" — raw delimiter join is ambiguous.
    ref_a = _resolve_with({"user_id": "alice|project", "project_id": "x"})
    ref_b = _resolve_with({"user_id": "alice", "project_id": "project|x"})
    assert ref_a != ref_b, (
        "scope_key encoding is ambiguous: delimiter-bearing adjacent fields collide"
    )


def test_incomplete_agentops_context_missing_required_fields_returns_400():
    body = json.dumps({"context": {"mode": "agentops"}, "ref": "API_KEY"}).encode()
    backend = _FakeCredentialBackend()
    status, response = _call(body=body, backend=backend)
    assert status == 400
    assert response.get("error") == "invalid context"
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Unknown method / path
# ---------------------------------------------------------------------------


def test_get_method_returns_404():
    status, response = _call(method="GET")
    assert status == 404


def test_unknown_path_returns_404():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(path="/credentials/unknown", body=body)
    assert status == 404


def test_post_to_wrong_path_returns_404():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(path="/credentials", body=body)
    assert status == 404


# ---------------------------------------------------------------------------
# Sentinel non-leak checks
# ---------------------------------------------------------------------------


def test_token_sentinel_not_in_401_error():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    sentinel_token = "cp-SENTINEL-SECRET-TOKEN"
    status, response = _call(body=body, auth=None, token=sentinel_token)
    assert status == 401
    error_text = json.dumps(response)
    assert sentinel_token not in error_text


def test_ref_sentinel_not_in_400_error():
    sentinel_ref = "SENTINEL-SECRET-REF-VALUE"
    body = json.dumps({"context": _CONTEXT, "ref": 9999, "sentinel_marker": sentinel_ref}).encode()
    status, response = _call(body=body)
    assert status == 400
    error_text = json.dumps(response)
    assert sentinel_ref not in error_text


def test_backend_exception_secret_ref_not_leaked():
    class _LeakingBackend:
        def resolve(self, context, ref):
            raise RuntimeError("cred-SENTINEL-SECRET-REF-IN-EXCEPTION")

    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body, backend=_LeakingBackend())
    assert status == 500
    error_text = json.dumps(response)
    assert "SENTINEL-SECRET-REF-IN-EXCEPTION" not in error_text


def test_response_always_has_secret_ref_key_on_success():
    body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
    status, response = _call(body=body)
    assert status == 200
    assert "secret_ref" in response


# ---------------------------------------------------------------------------
# compose_services integration: server-side dispatcher
# ---------------------------------------------------------------------------


class _FakeCredentialResolverBackend:
    def __init__(self):
        self.calls: list = []

    def resolve(self, context, ref):
        self.calls.append((context, ref))
        return f"cred-{ref}"


@pytest.fixture
def _api_server_with_credentials():
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    server.credential_backend = _FakeCredentialResolverBackend()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _http_post_json(url: str, body: bytes, token: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_api_service_routes_credentials_resolve(_api_server_with_credentials):
    body = json.dumps({"context": _CONTEXT, "ref": "MY_KEY"}).encode()
    status, _ = _http_post_json(f"{_api_server_with_credentials}/credentials/resolve", body)
    assert status == 200


def test_non_api_services_return_404_for_credentials_resolve():
    for service_name in ("worker", "scheduler", "local-secrets"):
        server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
        server.service_name = service_name
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
            status, _ = _http_post_json(f"{base}/credentials/resolve", body)
            assert status == 404, f"{service_name} should return 404 for POST /credentials/resolve"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def test_credential_dispatch_returns_401_before_backend_setup(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL credential backend failure password=x")

    monkeypatch.setenv("AGENTOPS_RUNTIME_TOKEN", "expected-token")
    monkeypatch.setattr(compose_services, "_get_or_create_credential_resolver", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
        status, response_body = _http_post_json(f"{base}/credentials/resolve", body, token=None)
        assert status == 401
        assert calls == []
        assert "LEAKSENTINEL" not in response_body.decode()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_credential_dispatch_returns_503_on_backend_failure(monkeypatch):
    def _raise():
        raise ValueError("LEAKSENTINEL credential backend failure password=x")

    monkeypatch.setattr(compose_services, "_get_or_create_credential_resolver", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"context": _CONTEXT, "ref": "KEY"}).encode()
        status, response_body = _http_post_json(f"{base}/credentials/resolve", body)
        assert status == 503
        assert "LEAKSENTINEL" not in response_body.decode()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_credential_dispatch_rejects_negative_content_length_before_backend(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL")

    monkeypatch.setattr(compose_services, "_get_or_create_credential_resolver", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /credentials/resolve HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: -1\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            raw = sock.recv(4096).decode("utf-8", errors="replace")
        assert "400" in raw
        assert calls == []
        assert "LEAKSENTINEL" not in raw
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_credential_dispatch_rejects_non_integer_content_length_before_backend(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL")

    monkeypatch.setattr(compose_services, "_get_or_create_credential_resolver", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            request = (
                "POST /credentials/resolve HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: abc\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            raw = sock.recv(4096).decode("utf-8", errors="replace")
        assert "400" in raw
        assert calls == []
        assert "LEAKSENTINEL" not in raw
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_credential_dispatch_rejects_oversized_body_before_backend(monkeypatch):
    calls = []

    def _raise():
        calls.append("called")
        raise ValueError("LEAKSENTINEL")

    monkeypatch.setattr(compose_services, "_get_or_create_credential_resolver", _raise)

    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = "api"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            oversized = compose_services._MAX_CREDENTIAL_REQUEST_BODY_BYTES + 1
            request = (
                "POST /credentials/resolve HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {oversized}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            raw = sock.recv(4096).decode("utf-8", errors="replace")
        assert "413" in raw
        assert calls == []
        assert "LEAKSENTINEL" not in raw
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_credential_resolver_returns_resolver():
    resolver = compose_services._make_credential_resolver({})
    assert hasattr(resolver, "resolve")
    assert callable(resolver.resolve)


def test_credential_resolver_agentops_context_returns_stable_ref():
    resolver = compose_services._make_credential_resolver({})
    from agent.runtime_context import RuntimeContext
    ctx = RuntimeContext.from_mapping({"mode": "agentops", "org_id": "org1", "workspace_id": "ws1"})
    ref1 = resolver.resolve(ctx, "MY_KEY")
    ref2 = resolver.resolve(ctx, "MY_KEY")
    assert ref1 == ref2
    assert isinstance(ref1, str)


def test_credential_resolver_none_context_returns_null():
    resolver = compose_services._make_credential_resolver({})
    result = resolver.resolve(None, "MY_KEY")
    assert result is None


def test_credential_resolver_different_refs_produce_different_values():
    resolver = compose_services._make_credential_resolver({})
    from agent.runtime_context import RuntimeContext
    ctx = RuntimeContext.from_mapping({"mode": "agentops", "org_id": "org1", "workspace_id": "ws1"})
    ref_a = resolver.resolve(ctx, "API_KEY_A")
    ref_b = resolver.resolve(ctx, "API_KEY_B")
    assert ref_a != ref_b


def test_get_or_create_credential_resolver_fails_closed_when_make_raises(monkeypatch):
    monkeypatch.setattr(compose_services, "_credential_resolver_instance", None)
    monkeypatch.setattr(compose_services, "_make_credential_resolver", lambda env: (_ for _ in ()).throw(ValueError("LEAKSENTINEL resolver fail")))
    monkeypatch.setattr(compose_services.os, "environ", {})
    with pytest.raises(ValueError):
        compose_services._get_or_create_credential_resolver()


# ---------------------------------------------------------------------------
# _sanitize_log_message: /credentials query redaction
# ---------------------------------------------------------------------------


def test_credentials_log_message_redacts_query_values():
    rendered = compose_services._sanitize_log_message(
        '"POST /credentials/resolve?debug=SENTINEL-SECRET-QUERY HTTP/1.1" 200 -'
    )
    assert "SENTINEL-SECRET-QUERY" not in rendered
    assert "/credentials/resolve" in rendered
    assert "<redacted>" in rendered


def test_credentials_log_message_no_query_unchanged():
    rendered = compose_services._sanitize_log_message(
        '"POST /credentials/resolve HTTP/1.1" 200 -'
    )
    assert "/credentials/resolve" in rendered
    assert "<redacted>" not in rendered
