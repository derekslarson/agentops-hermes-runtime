"""Tests for the M12B HTTP conversation-router adapter.

Covers:
- resolve_conversation posts scope + safe event and returns a non-empty conversation_id
- find_active_run returns None for null/empty/missing run_id responses
- route_turn posts scope + safe turn and returns a non-empty run_id; missing/empty raises
- secret-ish event/turn fields are redacted before transport
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token/query/response-body leakage
- distinct RuntimeContexts produce distinct scope payloads (tenant isolation)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_conversation_router_http import (
    HttpConversationRouter,
    register_http_conversation_router_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _RouterHandler(BaseHTTPRequestHandler):
    server: "_RouterServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        parsed = urlparse(self.path)
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        if parsed.path == "/conversations/resolve":
            run_id = getattr(self.server, "resolve_run_id", None)
            if run_id is not None:
                self._json({"run_id": run_id})
                return
            conv_id = getattr(self.server, "resolve_conv_id", "conv-abc")
            if getattr(self.server, "resolve_empty", False):
                self._json({"conversation_id": ""})
                return
            if getattr(self.server, "resolve_missing", False):
                self._json({})
                return
            self._json({"conversation_id": conv_id})
            return

        parts = parsed.path.strip("/").split("/")
        # POST /conversations/<id>/active-run or /conversations/<id>/route
        # only handle known conversation IDs; anything else 404s
        if len(parts) == 3 and parts[0] == "conversations" and parts[1] in {"conv-abc", "conv%2Fwith%20spaces%26chars"}:
            segment = parts[2]
            if segment == "active-run":
                if getattr(self.server, "active_run_null", False):
                    self._json({"run_id": None})
                    return
                if getattr(self.server, "active_run_empty", False):
                    self._json({"run_id": ""})
                    return
                if getattr(self.server, "active_run_missing", False):
                    self._json({})
                    return
                if getattr(self.server, "active_run_non_string", False):
                    self._json({"run_id": 123})
                    return
                self._json({"run_id": "run-xyz"})
                return
            if segment == "route":
                if getattr(self.server, "route_empty", False):
                    self._json({"run_id": ""})
                    return
                if getattr(self.server, "route_missing", False):
                    self._json({})
                    return
                self._json({"run_id": "run-xyz"})
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


class _RouterServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    resolve_conv_id: str
    resolve_run_id: str | None
    resolve_empty: bool
    resolve_missing: bool
    active_run_null: bool
    active_run_empty: bool
    active_run_missing: bool
    active_run_non_string: bool
    route_empty: bool
    route_missing: bool


@pytest.fixture
def router_server():
    server = _RouterServer(("127.0.0.1", 0), _RouterHandler)
    server.requests = []
    server.resolve_conv_id = "conv-abc"
    server.resolve_run_id = None
    server.resolve_empty = False
    server.resolve_missing = False
    server.active_run_null = False
    server.active_run_empty = False
    server.active_run_missing = False
    server.active_run_non_string = False
    server.route_empty = False
    server.route_missing = False
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
# Core contract tests
# ---------------------------------------------------------------------------


def test_resolve_conversation_posts_scope_and_event_returns_conversation_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}", token="sentinel-token")
    context = _ctx()

    conv_id = backend.resolve_conversation(context, {"channel": "C123", "text": "hi"})

    assert isinstance(conv_id, str) and conv_id
    assert len(router_server.requests) == 1
    method, path, auth, body = router_server.requests[0]
    assert method == "POST"
    assert path == "/conversations/resolve"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body["context"]["org_id"] == "acme"
    assert isinstance(body.get("event"), dict)


def test_find_active_run_returns_none_for_null_run_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.active_run_null = True

    result = backend.find_active_run(_ctx(), "conv-abc")
    assert result is None


def test_find_active_run_returns_none_for_empty_run_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.active_run_empty = True

    result = backend.find_active_run(_ctx(), "conv-abc")
    assert result is None


def test_find_active_run_returns_none_for_missing_run_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.active_run_missing = True

    result = backend.find_active_run(_ctx(), "conv-abc")
    assert result is None


def test_find_active_run_returns_run_id_when_present(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")

    result = backend.find_active_run(_ctx(), "conv-abc")
    assert result == "run-xyz"


def test_find_active_run_raises_for_malformed_non_string_run_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.active_run_non_string = True

    with pytest.raises(RuntimeError, match="run_id"):
        backend.find_active_run(_ctx(), "conv-abc")


def test_route_turn_posts_scope_and_turn_returns_run_id(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}", token="sentinel-token")
    context = _ctx()

    run_id = backend.route_turn(context, "conv-abc", {"text": "hello", "user": "derek"})

    assert isinstance(run_id, str) and run_id
    method, path, auth, body = router_server.requests[0]
    assert method == "POST"
    assert path == "/conversations/conv-abc/route"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert isinstance(body.get("turn"), dict)


def test_route_turn_raises_when_run_id_is_empty(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.route_empty = True

    with pytest.raises(RuntimeError, match="run_id"):
        backend.route_turn(_ctx(), "conv-abc", {"text": "hi"})


def test_route_turn_raises_when_run_id_is_missing(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    router_server.route_missing = True

    with pytest.raises(RuntimeError, match="run_id"):
        backend.route_turn(_ctx(), "conv-abc", {"text": "hi"})


# ---------------------------------------------------------------------------
# Secret field redaction
# ---------------------------------------------------------------------------


def test_secret_ish_event_fields_are_redacted_before_transport(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    secret_event = {
        "text": "hello",
        "token": "secret-token-sentinel",
        "authorization": "Bearer secret-auth-sentinel",
        "password": "secret-pass-sentinel",
        "credential": "secret-cred-sentinel",
        "api_key": "secret-apikey-sentinel",
        "nested": {"apikey": "secret-nested-apikey"},
    }

    backend.resolve_conversation(_ctx(), secret_event)

    _, _, _, body = router_server.requests[0]
    event_repr = repr(body.get("event", {}))
    assert "secret-token-sentinel" not in event_repr
    assert "secret-auth-sentinel" not in event_repr
    assert "secret-pass-sentinel" not in event_repr
    assert "secret-cred-sentinel" not in event_repr
    assert "secret-apikey-sentinel" not in event_repr
    assert "secret-nested-apikey" not in event_repr
    assert "text" in event_repr


def test_secret_ish_turn_fields_are_redacted_before_transport(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    secret_turn = {
        "text": "hello",
        "token": "secret-turn-token-sentinel",
        "password": "secret-turn-pass-sentinel",
        "api_key": "secret-turn-key-sentinel",
    }

    backend.route_turn(_ctx(), "conv-abc", secret_turn)

    _, _, _, body = router_server.requests[0]
    turn_repr = repr(body.get("turn", {}))
    assert "secret-turn-token-sentinel" not in turn_repr
    assert "secret-turn-pass-sentinel" not in turn_repr
    assert "secret-turn-key-sentinel" not in turn_repr
    assert "text" in turn_repr


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_http_conversation_router_rejects_empty_base_url():
    with pytest.raises(ValueError):
        HttpConversationRouter("")


def test_http_conversation_router_rejects_relative_url():
    with pytest.raises(ValueError):
        HttpConversationRouter("/api/v1")


def test_http_conversation_router_rejects_non_http_url():
    with pytest.raises(ValueError):
        HttpConversationRouter("ftp://example.test")


def test_http_conversation_router_rejects_url_with_credentials():
    with pytest.raises(ValueError):
        HttpConversationRouter("https://user:pass@example.test")


def test_http_conversation_router_rejects_url_with_query():
    with pytest.raises(ValueError):
        HttpConversationRouter("https://example.test/api?token=abc")


def test_http_conversation_router_rejects_url_with_fragment():
    with pytest.raises(ValueError):
        HttpConversationRouter("https://example.test/#section")


def test_http_conversation_router_rejects_invalid_timeout():
    with pytest.raises(ValueError, match="timeout"):
        HttpConversationRouter("https://example.test", timeout=float("nan"))

    with pytest.raises(ValueError, match="timeout"):
        HttpConversationRouter("https://example.test", timeout=0)

    with pytest.raises(ValueError, match="timeout"):
        HttpConversationRouter("https://example.test", timeout=-1)


# ---------------------------------------------------------------------------
# Token transport
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header_not_in_url_or_body(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}", token="cp-secret-sentinel")

    backend.resolve_conversation(_ctx(), {"text": "hi"})

    _, path, auth, body = router_server.requests[0]
    assert auth == "Bearer cp-secret-sentinel"
    assert "cp-secret-sentinel" not in path
    assert "cp-secret-sentinel" not in repr(body)


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_request_errors_do_not_leak_token_or_query(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}", token="cp-secret-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        backend.find_active_run(_ctx(), "does-not-exist-404")

    msg = str(exc_info.value)
    assert "cp-secret-sentinel" not in msg
    assert "?" not in msg


def test_request_error_does_not_contain_response_body():
    backend = HttpConversationRouter("http://127.0.0.1:1", token="tok")

    with pytest.raises(RuntimeError) as exc_info:
        backend.resolve_conversation(_ctx(), {"text": "hi"})

    msg = str(exc_info.value)
    assert "Error response" not in msg
    assert "tok" not in msg


def test_request_errors_use_logical_endpoint_labels_without_env_paths_or_profile_names():
    backend = HttpConversationRouter("http://127.0.0.1:1/private/compose-self-hosted", token="tok")

    with pytest.raises(RuntimeError) as exc_info:
        backend.find_active_run(_ctx(), "conv-/local/path/sentinel-compose-profile")

    msg = str(exc_info.value)
    assert "127.0.0.1" not in msg
    assert "private" not in msg
    assert "compose-self-hosted" not in msg
    assert "local/path" not in msg
    assert "conv-" not in msg
    assert "POST /conversations/<conversation_id>/active-run" in msg


# ---------------------------------------------------------------------------
# Tenant/scope isolation
# ---------------------------------------------------------------------------


def test_distinct_contexts_produce_distinct_scope_payloads(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")
    ctx_a = _ctx(
        org_id="org-a",
        workspace_id="ws-a",
        user_id="user-a",
        conversation_id="thread-a",
        external_channel_id="channel-a",
        external_thread_id="external-thread-a",
    )
    ctx_b = _ctx(
        org_id="org-b",
        workspace_id="ws-b",
        user_id="user-b",
        conversation_id="thread-b",
        external_channel_id="channel-b",
        external_thread_id="external-thread-b",
    )

    backend.resolve_conversation(ctx_a, {"text": "hi"})
    backend.resolve_conversation(ctx_b, {"text": "hi"})

    scope_a = router_server.requests[0][3]["context"]
    scope_b = router_server.requests[1][3]["context"]
    assert scope_a["org_id"] == "org-a"
    assert scope_b["org_id"] == "org-b"
    assert scope_a["workspace_id"] == "ws-a"
    assert scope_b["workspace_id"] == "ws-b"
    assert scope_a["user_id"] == "user-a"
    assert scope_b["user_id"] == "user-b"
    assert scope_a["conversation_id"] == "thread-a"
    assert scope_b["conversation_id"] == "thread-b"
    assert scope_a["external_channel_id"] == "channel-a"
    assert scope_b["external_channel_id"] == "channel-b"
    assert scope_a["external_thread_id"] == "external-thread-a"
    assert scope_b["external_thread_id"] == "external-thread-b"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_conversation_router_backend_registers_for_profile(router_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"conversation_router": "compose-self-hosted"},
                "options": {
                    "conversation_router": {
                        "base_url": f"http://127.0.0.1:{router_server.server_port}",
                        "token": "sentinel-token",
                    }
                },
            }
        }
    )
    register_http_conversation_router_backend(registry)

    backend = registry.get(BackendCapability.CONVERSATION_ROUTER, _ctx())
    assert isinstance(backend, HttpConversationRouter)

    conv_id = backend.resolve_conversation(_ctx(), {"text": "hi"})
    assert conv_id


def test_register_http_conversation_router_backend_preserves_timeout_validation(router_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"conversation_router": "compose-self-hosted"},
                "options": {
                    "conversation_router": {
                        "base_url": f"http://127.0.0.1:{router_server.server_port}",
                        "timeout": 0,
                    }
                },
            }
        }
    )
    register_http_conversation_router_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.CONVERSATION_ROUTER, _ctx())


# ---------------------------------------------------------------------------
# Conversation-ID path safety
# ---------------------------------------------------------------------------


def test_conversation_id_is_url_encoded_in_path(router_server):
    backend = HttpConversationRouter(f"http://127.0.0.1:{router_server.server_port}")

    backend.find_active_run(_ctx(), "conv/with spaces&chars")

    _, path, _, _ = router_server.requests[0]
    assert "conv/with spaces&chars" not in path
    assert "/" not in path.split("/conversations/")[1].split("/")[0]
