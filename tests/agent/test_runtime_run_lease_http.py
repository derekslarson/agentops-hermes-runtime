"""Tests for the M12B HTTP run-lease adapter.

Covers:
- claim/renew return bool; release/request_cancel/clear_cancelled return None
- is_cancelled returns bool; expire_stale returns int (bool rejected even as int subclass)
- malformed responses fail closed (missing field, wrong type)
- lease key is URL-encoded in path; raw key never appears in error messages
- context scope payload includes tenant fields; run_id/job_id intentionally excluded
- distinct RuntimeContexts produce distinct scope payloads (tenant isolation)
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token leakage; logical endpoint labels used
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
from agent.runtime_run_lease_http import (
    HttpRunLeaseBackend,
    register_http_run_lease_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _LeaseHandler(BaseHTTPRequestHandler):
    server: "_LeaseServer"

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

        parts = self.path.strip("/").split("/")

        if len(parts) == 2 and parts[0] == "run-leases" and parts[1] == "expire-stale":
            bad_type = getattr(self.server, "expire_stale_bad_type", None)
            if bad_type is not None:
                self._json({"expired": bad_type})
                return
            self._json({"expired": getattr(self.server, "expire_stale_count", 3)})
            return

        if len(parts) == 3 and parts[0] == "run-leases":
            action = parts[2]

            if action == "claim":
                bad_type = getattr(self.server, "claim_bad_type", None)
                if bad_type is not None:
                    self._json({"claimed": bad_type})
                    return
                if getattr(self.server, "claim_missing", False):
                    self._json({})
                    return
                self._json({"claimed": getattr(self.server, "claim_result", True)})
                return

            if action == "renew":
                bad_type = getattr(self.server, "renew_bad_type", None)
                if bad_type is not None:
                    self._json({"renewed": bad_type})
                    return
                self._json({"renewed": getattr(self.server, "renew_result", True)})
                return

            if action == "release":
                self._json({})
                return

            if action == "request-cancel":
                self._json({})
                return

            if action == "is-cancelled":
                bad_type = getattr(self.server, "is_cancelled_bad_type", None)
                if bad_type is not None:
                    self._json({"cancelled": bad_type})
                    return
                self._json({"cancelled": getattr(self.server, "is_cancelled_result", False)})
                return

            if action == "clear-cancel":
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


class _LeaseServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    claim_result: bool
    claim_bad_type: Any
    claim_missing: bool
    renew_result: bool
    renew_bad_type: Any
    is_cancelled_result: bool
    is_cancelled_bad_type: Any
    expire_stale_count: int
    expire_stale_bad_type: Any
    force_status: int | None
    raw_response: str | None


@pytest.fixture
def lease_server():
    server = _LeaseServer(("127.0.0.1", 0), _LeaseHandler)
    server.requests = []
    server.claim_result = True
    server.claim_bad_type = None
    server.claim_missing = False
    server.renew_result = True
    server.renew_bad_type = None
    server.is_cancelled_result = False
    server.is_cancelled_bad_type = None
    server.expire_stale_count = 3
    server.expire_stale_bad_type = None
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
# claim
# ---------------------------------------------------------------------------


def test_claim_posts_context_and_owner_returns_true(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="sentinel-token")
    result = backend.claim(_ctx(), "run-123", owner="worker-1")

    assert result is True
    assert len(lease_server.requests) == 1
    method, path, auth, body = lease_server.requests[0]
    assert method == "POST"
    assert path == "/run-leases/run-123/claim"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body["context"]["org_id"] == "acme"
    assert body["owner"] == "worker-1"


def test_claim_returns_false_when_server_says_false(lease_server):
    lease_server.claim_result = False
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    assert backend.claim(_ctx(), "run-456", owner="worker-1") is False


def test_claim_raises_on_non_bool_response_string(lease_server):
    lease_server.claim_bad_type = "yes"
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim(_ctx(), "run-456", owner="worker-1")


def test_claim_raises_on_non_bool_response_int(lease_server):
    lease_server.claim_bad_type = 1
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim(_ctx(), "run-456", owner="worker-1")


def test_claim_raises_on_missing_claimed_field(lease_server):
    lease_server.claim_missing = True
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim(_ctx(), "run-456", owner="worker-1")


# ---------------------------------------------------------------------------
# renew
# ---------------------------------------------------------------------------


def test_renew_posts_context_and_owner_returns_true(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="sentinel-token")
    result = backend.renew(_ctx(), "run-123", owner="worker-1")

    assert result is True
    method, path, auth, body = lease_server.requests[0]
    assert method == "POST"
    assert path == "/run-leases/run-123/renew"
    assert auth == "Bearer sentinel-token"
    assert body["owner"] == "worker-1"


def test_renew_returns_false_when_server_says_false(lease_server):
    lease_server.renew_result = False
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    assert backend.renew(_ctx(), "run-123", owner="worker-1") is False


def test_renew_raises_on_non_bool_int_response(lease_server):
    lease_server.renew_bad_type = 1
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.renew(_ctx(), "run-123", owner="worker-1")


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_posts_context_and_owner_returns_none(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="sentinel-token")
    result = backend.release(_ctx(), "run-123", owner="worker-1")

    assert result is None
    method, path, _, body = lease_server.requests[0]
    assert method == "POST"
    assert path == "/run-leases/run-123/release"
    assert body["owner"] == "worker-1"


# ---------------------------------------------------------------------------
# request_cancel
# ---------------------------------------------------------------------------


def test_request_cancel_posts_context_and_requester_returns_none(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="sentinel-token")
    result = backend.request_cancel(_ctx(), "run-123", requester="ops-bot")

    assert result is None
    method, path, _, body = lease_server.requests[0]
    assert method == "POST"
    assert path == "/run-leases/run-123/request-cancel"
    assert body["requester"] == "ops-bot"


# ---------------------------------------------------------------------------
# is_cancelled
# ---------------------------------------------------------------------------


def test_is_cancelled_returns_false_by_default(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    result = backend.is_cancelled(_ctx(), "run-123")

    assert result is False
    _, path, _, _ = lease_server.requests[0]
    assert path == "/run-leases/run-123/is-cancelled"


def test_is_cancelled_returns_true_when_server_says_true(lease_server):
    lease_server.is_cancelled_result = True
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    assert backend.is_cancelled(_ctx(), "run-123") is True


def test_is_cancelled_raises_on_non_bool_string_response(lease_server):
    lease_server.is_cancelled_bad_type = "true"
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.is_cancelled(_ctx(), "run-123")


# ---------------------------------------------------------------------------
# clear_cancelled
# ---------------------------------------------------------------------------


def test_clear_cancelled_posts_and_returns_none(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    result = backend.clear_cancelled(_ctx(), "run-123")

    assert result is None
    _, path, _, _ = lease_server.requests[0]
    assert path == "/run-leases/run-123/clear-cancel"


# ---------------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------------


def test_expire_stale_posts_and_returns_int_count(lease_server):
    lease_server.expire_stale_count = 5
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    result = backend.expire_stale(_ctx())

    assert result == 5
    _, path, _, _ = lease_server.requests[0]
    assert path == "/run-leases/expire-stale"


def test_expire_stale_returns_zero(lease_server):
    lease_server.expire_stale_count = 0
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    assert backend.expire_stale(_ctx()) == 0


def test_expire_stale_rejects_bool_even_though_bool_is_int_subclass(lease_server):
    lease_server.expire_stale_bad_type = True
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.expire_stale(_ctx())


def test_expire_stale_rejects_string_response(lease_server):
    lease_server.expire_stale_bad_type = "5"
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.expire_stale(_ctx())


# ---------------------------------------------------------------------------
# Key URL encoding
# ---------------------------------------------------------------------------


def test_key_with_special_chars_is_url_encoded(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    backend.claim(_ctx(), "run/with spaces&chars", owner="worker-1")

    _, path, _, _ = lease_server.requests[0]
    assert "run/with spaces&chars" not in path
    assert "/" not in path.split("/run-leases/")[1].split("/")[0]


# ---------------------------------------------------------------------------
# Context scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_tenant_fields(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    ctx = _ctx(org_id="org-xyz", workspace_id="ws-abc", user_id="u-123")
    backend.claim(ctx, "run-123", owner="w-1")

    _, _, _, body = lease_server.requests[0]
    ctx_payload = body["context"]
    assert ctx_payload["org_id"] == "org-xyz"
    assert ctx_payload["workspace_id"] == "ws-abc"
    assert ctx_payload["user_id"] == "u-123"


def test_scope_payload_excludes_run_id_and_job_id(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="org-a",
        run_id="run-should-not-appear",
        job_id="job-should-not-appear",
        backend_profile="compose-self-hosted",
    )
    backend.claim(ctx, "run-should-not-appear", owner="w-1")

    _, _, _, body = lease_server.requests[0]
    ctx_payload = body["context"]
    assert "run_id" not in ctx_payload
    assert "job_id" not in ctx_payload


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_distinct_contexts_produce_distinct_scope_payloads(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}")
    ctx_a = _ctx(org_id="org-a", user_id="user-a")
    ctx_b = _ctx(org_id="org-b", user_id="user-b")

    backend.claim(ctx_a, "run-123", owner="w-1")
    backend.claim(ctx_b, "run-123", owner="w-2")

    scope_a = lease_server.requests[0][3]["context"]
    scope_b = lease_server.requests[1][3]["context"]
    assert scope_a["org_id"] == "org-a"
    assert scope_b["org_id"] == "org-b"
    assert scope_a["user_id"] == "user-a"
    assert scope_b["user_id"] == "user-b"


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_rejects_empty_base_url():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("")


def test_rejects_relative_url():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("/api/v1")


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("ftp://example.test")


def test_rejects_url_with_credentials():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("https://user:pass@example.test")


def test_rejects_url_with_query():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("https://example.test?token=abc")


def test_rejects_url_with_fragment():
    with pytest.raises(ValueError):
        HttpRunLeaseBackend("https://example.test#section")


def test_rejects_invalid_timeout_nan():
    with pytest.raises(ValueError, match="timeout"):
        HttpRunLeaseBackend("https://example.test", timeout=float("nan"))


def test_rejects_invalid_timeout_zero():
    with pytest.raises(ValueError, match="timeout"):
        HttpRunLeaseBackend("https://example.test", timeout=0)


def test_rejects_invalid_timeout_negative():
    with pytest.raises(ValueError, match="timeout"):
        HttpRunLeaseBackend("https://example.test", timeout=-1)


# ---------------------------------------------------------------------------
# Token transport
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header_not_in_url_or_body(lease_server):
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="cp-secret-sentinel")
    backend.claim(_ctx(), "run-123", owner="w-1")

    _, path, auth, body = lease_server.requests[0]
    assert auth == "Bearer cp-secret-sentinel"
    assert "cp-secret-sentinel" not in path
    assert "cp-secret-sentinel" not in repr(body)


# ---------------------------------------------------------------------------
# Error sanitization / logical endpoint labels
# ---------------------------------------------------------------------------


def test_request_errors_do_not_leak_token():
    backend = HttpRunLeaseBackend("http://127.0.0.1:1", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim(_ctx(), "run-123", owner="w-1")
    assert "cp-secret-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_request_errors_use_logical_claim_endpoint_label_without_raw_key():
    backend = HttpRunLeaseBackend("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim(_ctx(), "secret-lease-key-sentinel", owner="w-1")
    msg = str(exc_info.value)
    assert "secret-lease-key-sentinel" not in msg
    assert "POST /run-leases/<key>/claim" in msg


def test_request_errors_use_logical_renew_endpoint_label_without_raw_key():
    backend = HttpRunLeaseBackend("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.renew(_ctx(), "secret-lease-key-sentinel", owner="w-1")
    msg = str(exc_info.value)
    assert "secret-lease-key-sentinel" not in msg
    assert "POST /run-leases/<key>/renew" in msg


def test_expire_stale_errors_use_logical_endpoint_label():
    backend = HttpRunLeaseBackend("http://127.0.0.1:1", token="tok")
    with pytest.raises(RuntimeError) as exc_info:
        backend.expire_stale(_ctx())
    msg = str(exc_info.value)
    assert "tok" not in msg
    assert "POST /run-leases/expire-stale" in msg


def test_http_error_response_uses_logical_label(lease_server):
    lease_server.force_status = 500
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim(_ctx(), "sensitive-key", owner="w-1")
    msg = str(exc_info.value)
    assert "cp-secret-sentinel" not in msg
    assert "sensitive-key" not in msg
    assert "POST /run-leases/<key>/claim" in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_body_is_not_retained_as_exception_cause(lease_server):
    lease_server.raw_response = "non-json response body sentinel"
    backend = HttpRunLeaseBackend(f"http://127.0.0.1:{lease_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim(_ctx(), "sensitive-key", owner="w-1")

    msg = str(exc_info.value)
    assert "non-json response body sentinel" not in msg
    assert "sensitive-key" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_run_lease_backend_registers_for_profile(lease_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"run_lease": "compose-self-hosted"},
                "options": {
                    "run_lease": {
                        "base_url": f"http://127.0.0.1:{lease_server.server_port}",
                        "token": "sentinel-token",
                    }
                },
            }
        }
    )
    register_http_run_lease_backend(registry)

    backend = registry.get(BackendCapability.RUN_LEASE, _ctx())
    assert isinstance(backend, HttpRunLeaseBackend)
    assert backend.claim(_ctx(), "run-123", owner="w-1") is True


def test_register_http_run_lease_backend_preserves_timeout_validation(lease_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"run_lease": "compose-self-hosted"},
                "options": {
                    "run_lease": {
                        "base_url": f"http://127.0.0.1:{lease_server.server_port}",
                        "timeout": 0,
                    }
                },
            }
        }
    )
    register_http_run_lease_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.RUN_LEASE, _ctx())
