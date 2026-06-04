"""Tests for the M12B HTTP session backend adapter.

Covers:
- create_session, append_message, read_messages, search, resolve_resume_session_id
- claim/renew/release turn lock
- append/read legacy compatibility methods
- registry integration via register_http_session_backend
- local parity short-circuits (limit <= 0, empty search query)
- context scope payload includes tenant fields; no run_id/job_id
- malformed responses fail closed
- sanitization: no token/base URL/body/session_id/owner/query leakage in errors
- endpoint labels are logical
- transcript message payload is NOT redacted before transport
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
from agent.runtime_session_http import (
    HttpSessionBackend,
    register_http_session_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _SessionHandler(BaseHTTPRequestHandler):
    server: "_SessionServer"

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

        parts = self.path.strip("/").split("/")

        if parts == ["sessions", "create"]:
            session_id = self.server.create_result
            if session_id is _UNSET:
                self._json({})
            elif session_id is None:
                self._json({"session_id": None})
            elif session_id == "":
                self._json({"session_id": ""})
            else:
                self._json({"session_id": session_id})
            return

        if parts == ["sessions", "append"]:
            resp: dict[str, Any] = {}
            if self.server.append_message_id is not _UNSET:
                resp["message_id"] = self.server.append_message_id
            self._json(resp)
            return

        if parts == ["sessions", "messages"]:
            if self.server.messages_bad_response is not _UNSET:
                self._json({"messages": self.server.messages_bad_response})
                return
            if self.server.messages_missing:
                self._json({})
                return
            self._json({"messages": self.server.messages_result})
            return

        if parts == ["sessions", "search"]:
            if self.server.search_bad_response is not _UNSET:
                self._json({"results": self.server.search_bad_response})
                return
            if self.server.search_missing:
                self._json({})
                return
            self._json({"results": self.server.search_result})
            return

        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "resume-target":
            resume_id = self.server.resume_result
            if resume_id is _UNSET:
                self._json({})
            elif resume_id is None:
                self._json({"session_id": None})
            else:
                self._json({"session_id": resume_id})
            return

        if parts == ["sessions", "turn-lock", "claim"]:
            claimed = self.server.claim_result
            if claimed is _UNSET:
                self._json({})
            else:
                self._json({"claimed": claimed})
            return

        if parts == ["sessions", "turn-lock", "renew"]:
            renewed = self.server.renew_result
            if renewed is _UNSET:
                self._json({})
            else:
                self._json({"renewed": renewed})
            return

        if parts == ["sessions", "turn-lock", "release"]:
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


_UNSET = object()


class _SessionServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    create_result: Any
    append_message_id: Any
    messages_result: list[dict[str, Any]]
    messages_bad_response: Any
    messages_missing: bool
    search_result: list[dict[str, Any]]
    search_bad_response: Any
    search_missing: bool
    resume_result: Any
    claim_result: Any
    renew_result: Any
    force_status: int | None
    raw_response: Any


@pytest.fixture
def session_server():
    server = _SessionServer(("127.0.0.1", 0), _SessionHandler)
    server.requests = []
    server.create_result = "session-abc"
    server.append_message_id = _UNSET
    server.messages_result = []
    server.messages_bad_response = _UNSET
    server.messages_missing = False
    server.search_result = []
    server.search_bad_response = _UNSET
    server.search_missing = False
    server.resume_result = "session-abc"
    server.claim_result = True
    server.renew_result = True
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
# create_session
# ---------------------------------------------------------------------------


def test_create_session_posts_and_returns_session_id(session_server):
    session_server.create_result = "sess-42"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    session_id = backend.create_session(_ctx())

    assert session_id == "sess-42"
    assert len(session_server.requests) == 1
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/create"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body["context"]["org_id"] == "acme"


def test_create_session_with_none_context_sends_empty_scope(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    session_id = backend.create_session(None)

    assert isinstance(session_id, str)
    _, _, _, body = session_server.requests[0]
    assert body.get("context") == {} or "context" not in body


def test_create_session_optional_fields_sent_when_provided(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.create_session(
        _ctx(),
        session_id="custom-sid",
        source="test",
        parent_session_id="parent-1",
        model="test-model",
        model_config={"temperature": 0.5},
        system_prompt="be helpful",
        user_id="u-123",
    )

    _, _, _, body = session_server.requests[0]
    assert body.get("session_id") == "custom-sid"
    assert body.get("source") == "test"
    assert body.get("parent_session_id") == "parent-1"
    assert body.get("model") == "test-model"
    assert body.get("user_id") == "u-123"


def test_create_session_raises_on_missing_session_id_field(session_server):
    session_server.create_result = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.create_session(_ctx())


def test_create_session_raises_on_empty_session_id(session_server):
    session_server.create_result = ""
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.create_session(_ctx())


def test_create_session_raises_on_non_string_session_id(session_server):
    session_server.create_result = None
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.create_session(_ctx())


# ---------------------------------------------------------------------------
# append_message
# ---------------------------------------------------------------------------


def test_append_message_posts_message_and_returns_optional_id(session_server):
    session_server.append_message_id = 7
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.append_message(_ctx(), {"role": "user", "content": "hello"})

    assert result == 7
    assert len(session_server.requests) == 1
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/append"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)
    assert body.get("message") == {"role": "user", "content": "hello"}


def test_append_message_returns_none_when_no_message_id_in_response(session_server):
    session_server.append_message_id = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.append_message(_ctx(), {"role": "assistant", "content": "hi"})

    assert result is None


@pytest.mark.parametrize("bad_message_id", ["7", True, False, 7.5, {}])
def test_append_message_raises_on_non_integer_message_id(session_server, bad_message_id):
    session_server.append_message_id = bad_message_id
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")

    with pytest.raises(RuntimeError):
        backend.append_message(_ctx(), {"role": "assistant", "content": "hi"})


def test_append_message_with_none_context_does_not_crash(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.append_message(None, {"role": "user", "content": "hi"})


def test_append_message_does_not_redact_role_or_content(session_server):
    """Transcript payloads must not be redacted; session control plane is the durable store."""
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    sensitive_content = "user's secret-bearing message with api_key reference"
    backend.append_message(_ctx(), {"role": "user", "content": sensitive_content, "token": "should-not-be-hidden"})

    _, _, _, body = session_server.requests[0]
    msg = body.get("message", {})
    assert msg.get("role") == "user"
    assert msg.get("content") == sensitive_content
    assert msg.get("token") == "should-not-be-hidden"


def test_append_message_does_not_redact_tool_calls(session_server):
    """Tool calls in transcript messages are durable records and must be preserved intact."""
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    tool_calls = [{"id": "tc-1", "function": {"name": "search", "arguments": '{"api_key": "sk-test"}'}}]
    backend.append_message(_ctx(), {"role": "assistant", "content": None, "tool_calls": tool_calls})

    _, _, _, body = session_server.requests[0]
    msg = body.get("message", {})
    assert msg.get("tool_calls") == tool_calls


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_posts_and_returns_list(session_server):
    session_server.messages_result = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.read_messages(_ctx())

    assert result == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/messages"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_read_messages_returns_empty_list_when_none(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    assert backend.read_messages(_ctx()) == []


def test_read_messages_with_none_context_does_not_crash(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.read_messages(None)
    assert isinstance(result, list)


def test_read_messages_limit_zero_returns_empty_without_http(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.read_messages(_ctx(), limit=0)

    assert result == []
    assert len(session_server.requests) == 0


def test_read_messages_limit_negative_returns_empty_without_http(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.read_messages(_ctx(), limit=-5)

    assert result == []
    assert len(session_server.requests) == 0


def test_read_messages_positive_limit_sends_request(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.read_messages(_ctx(), limit=10)

    assert len(session_server.requests) == 1


def test_read_messages_raises_when_messages_is_not_a_list(session_server):
    session_server.messages_bad_response = "not-a-list"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.read_messages(_ctx())


def test_read_messages_raises_when_messages_missing(session_server):
    session_server.messages_missing = True
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.read_messages(_ctx())


def test_read_messages_raises_when_items_are_not_dicts(session_server):
    session_server.messages_bad_response = [{"role": "user"}, "not-a-dict"]
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.read_messages(_ctx())


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_posts_query_and_returns_results(session_server):
    session_server.search_result = [{"role": "user", "content": "match here"}]
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.search(_ctx(), "match")

    assert result == [{"role": "user", "content": "match here"}]
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/search"
    assert auth == "Bearer sentinel-token"
    assert body.get("query") == "match"
    assert isinstance(body.get("context"), dict)


def test_search_empty_query_returns_empty_without_http(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.search(_ctx(), "")

    assert result == []
    assert len(session_server.requests) == 0


def test_search_whitespace_query_returns_empty_without_http(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.search(_ctx(), "   ")

    assert result == []
    assert len(session_server.requests) == 0


def test_search_with_none_context_does_not_crash(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.search(None, "term")
    assert isinstance(result, list)


def test_search_raises_when_results_is_not_a_list(session_server):
    session_server.search_bad_response = "not-a-list"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.search(_ctx(), "query")


def test_search_raises_when_results_missing(session_server):
    session_server.search_missing = True
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.search(_ctx(), "query")


def test_search_raises_when_items_are_not_dicts(session_server):
    session_server.search_bad_response = [{"role": "user"}, "not-a-dict"]
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.search(_ctx(), "query")


# ---------------------------------------------------------------------------
# resolve_resume_session_id
# ---------------------------------------------------------------------------


def test_resolve_resume_session_id_posts_and_returns_id(session_server):
    session_server.resume_result = "resumed-session-abc"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.resolve_resume_session_id("source-session-xyz")

    assert result == "resumed-session-abc"
    method, path, auth, _ = session_server.requests[0]
    assert method == "POST"
    assert auth == "Bearer sentinel-token"
    assert "/sessions/" in path
    assert "/resume-target" in path


def test_resolve_resume_session_id_url_encodes_session_id(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.resolve_resume_session_id("session/with spaces&chars")

    _, path, _, _ = session_server.requests[0]
    assert "session/with spaces&chars" not in path
    assert "resume-target" in path


def test_resolve_resume_session_id_raises_on_missing_field(session_server):
    session_server.resume_result = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.resolve_resume_session_id("session-abc")


def test_resolve_resume_session_id_raises_on_none_session_id(session_server):
    session_server.resume_result = None
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.resolve_resume_session_id("session-abc")


def test_resolve_resume_session_id_error_label_uses_logical_placeholder(session_server):
    session_server.resume_result = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.resolve_resume_session_id("secret-session-id-sentinel")
    assert "secret-session-id-sentinel" not in str(exc_info.value)
    assert "/sessions/<session_id>/resume-target" in str(exc_info.value)


# ---------------------------------------------------------------------------
# claim_turn_lock
# ---------------------------------------------------------------------------


def test_claim_turn_lock_posts_and_returns_true(session_server):
    session_server.claim_result = True
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.claim_turn_lock(_ctx(), owner="worker-1", ttl_seconds=60.0)

    assert result is True
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/turn-lock/claim"
    assert auth == "Bearer sentinel-token"
    assert body.get("owner") == "worker-1"
    assert body.get("ttl_seconds") == 60.0
    assert isinstance(body.get("context"), dict)


def test_claim_turn_lock_returns_false_when_not_claimed(session_server):
    session_server.claim_result = False
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.claim_turn_lock(_ctx(), owner="worker-2", ttl_seconds=30.0)

    assert result is False


def test_claim_turn_lock_with_none_context_does_not_crash(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.claim_turn_lock(None, owner="w-1", ttl_seconds=60.0)
    assert isinstance(result, bool)


def test_claim_turn_lock_raises_when_claimed_is_missing(session_server):
    session_server.claim_result = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


def test_claim_turn_lock_raises_when_claimed_is_int(session_server):
    session_server.claim_result = 1
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


def test_claim_turn_lock_raises_when_claimed_is_string(session_server):
    session_server.claim_result = "true"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.claim_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


def test_claim_turn_lock_owner_not_leaked_in_error(session_server):
    session_server.force_status = 500
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim_turn_lock(_ctx(), owner="secret-owner-sentinel", ttl_seconds=60.0)
    assert "secret-owner-sentinel" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# renew_turn_lock
# ---------------------------------------------------------------------------


def test_renew_turn_lock_posts_and_returns_true(session_server):
    session_server.renew_result = True
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.renew_turn_lock(_ctx(), owner="worker-1", ttl_seconds=120.0)

    assert result is True
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/turn-lock/renew"
    assert auth == "Bearer sentinel-token"
    assert body.get("owner") == "worker-1"
    assert body.get("ttl_seconds") == 120.0
    assert isinstance(body.get("context"), dict)


def test_renew_turn_lock_returns_false_when_not_renewed(session_server):
    session_server.renew_result = False
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    assert backend.renew_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0) is False


def test_renew_turn_lock_raises_when_renewed_is_missing(session_server):
    session_server.renew_result = _UNSET
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.renew_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


def test_renew_turn_lock_raises_when_renewed_is_int(session_server):
    session_server.renew_result = 1
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.renew_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


def test_renew_turn_lock_raises_when_renewed_is_string(session_server):
    session_server.renew_result = "true"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.renew_turn_lock(_ctx(), owner="w-1", ttl_seconds=60.0)


# ---------------------------------------------------------------------------
# release_turn_lock
# ---------------------------------------------------------------------------


def test_release_turn_lock_posts_and_returns_none(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="sentinel-token")
    result = backend.release_turn_lock(_ctx(), owner="worker-1")

    assert result is None
    method, path, auth, body = session_server.requests[0]
    assert method == "POST"
    assert path == "/sessions/turn-lock/release"
    assert auth == "Bearer sentinel-token"
    assert body.get("owner") == "worker-1"
    assert isinstance(body.get("context"), dict)


def test_release_turn_lock_with_none_context_does_not_crash(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.release_turn_lock(None, owner="w-1")
    assert result is None


# ---------------------------------------------------------------------------
# append / read (legacy compatibility)
# ---------------------------------------------------------------------------


def test_append_sends_message_event_via_append_message(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.append(_ctx(), {"role": "user", "content": "legacy content"})

    assert len(session_server.requests) == 1
    _, path, _, body = session_server.requests[0]
    assert path == "/sessions/append"
    msg = body.get("message", {})
    assert msg.get("role") == "user"
    assert msg.get("content") == "legacy content"


def test_append_non_message_event_becomes_event_role_with_json_content(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.append(_ctx(), {"type": "tool_start", "tool_name": "search"})

    _, _, _, body = session_server.requests[0]
    msg = body.get("message", {})
    assert msg.get("role") == "event"
    content = msg.get("content")
    assert isinstance(content, str)
    parsed = json.loads(content)
    assert parsed.get("type") == "tool_start"
    assert parsed.get("tool_name") == "search"


def test_append_event_content_is_deterministic_json(session_server):
    """Non-message events must produce deterministic JSON (sort_keys=True)."""
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.append(_ctx(), {"z_key": "last", "a_key": "first"})

    _, _, _, body = session_server.requests[0]
    msg = body.get("message", {})
    content = msg.get("content", "")
    assert content == '{"a_key": "first", "z_key": "last"}'


def test_read_delegates_to_read_messages(session_server):
    session_server.messages_result = [{"role": "user", "content": "read-delegate"}]
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.read(_ctx())

    assert result == [{"role": "user", "content": "read-delegate"}]
    _, path, _, _ = session_server.requests[0]
    assert path == "/sessions/messages"


def test_read_with_limit_delegates_to_read_messages(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.read(_ctx(), limit=5)

    _, _, _, body = session_server.requests[0]
    assert body.get("limit") == 5


def test_read_with_zero_limit_returns_empty_without_http(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    result = backend.read(_ctx(), limit=0)

    assert result == []
    assert len(session_server.requests) == 0


# ---------------------------------------------------------------------------
# Context scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_tenant_fields(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    ctx = _ctx(org_id="org-xyz", workspace_id="ws-abc", user_id="u-123", workspace_type="slack")
    backend.read_messages(ctx)

    _, _, _, body = session_server.requests[0]
    ctx_payload = body["context"]
    assert ctx_payload["org_id"] == "org-xyz"
    assert ctx_payload["workspace_id"] == "ws-abc"
    assert ctx_payload["user_id"] == "u-123"
    assert ctx_payload["workspace_type"] == "slack"


def test_scope_payload_includes_all_required_fields(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    ctx = _ctx(
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
    backend.read_messages(ctx)

    _, _, _, body = session_server.requests[0]
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


def test_scope_payload_excludes_run_id_and_job_id(session_server):
    """run_id and job_id must never appear in the context scope payload."""
    from agent.runtime_context import RuntimeContext

    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="org-1",
        workspace_id="ws-1",
        run_id="run-secret-sentinel",
        job_id="job-secret-sentinel",
    )
    backend.read_messages(ctx)

    _, _, _, body = session_server.requests[0]
    cp = body["context"]
    assert "run_id" not in cp
    assert "job_id" not in cp
    assert "run-secret-sentinel" not in str(cp)
    assert "job-secret-sentinel" not in str(cp)


def test_none_context_produces_empty_scope(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.read_messages(None)

    _, _, _, body = session_server.requests[0]
    ctx_payload = body.get("context", {})
    assert ctx_payload == {}


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_rejects_empty_base_url():
    with pytest.raises(ValueError):
        HttpSessionBackend("")


def test_rejects_relative_url():
    with pytest.raises(ValueError):
        HttpSessionBackend("/api/v1")


def test_rejects_non_http_url():
    with pytest.raises(ValueError):
        HttpSessionBackend("ftp://example.test")


def test_rejects_url_with_credentials():
    with pytest.raises(ValueError):
        HttpSessionBackend("https://user:pass@example.test")


def test_rejects_url_with_query():
    with pytest.raises(ValueError):
        HttpSessionBackend("https://example.test?token=abc")


def test_rejects_url_with_fragment():
    with pytest.raises(ValueError):
        HttpSessionBackend("https://example.test#section")


@pytest.mark.parametrize("url", ["https://example.test?", "https://example.test#"])
def test_rejects_empty_query_or_fragment_delimiter(url):
    with pytest.raises(ValueError):
        HttpSessionBackend(url)


def test_rejects_invalid_port_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpSessionBackend("https://api.internal:secret-port-sentinel/control")

    assert "secret-port-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_whitespace_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpSessionBackend("https://api internal/control")

    assert "api internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_del_control_url_without_retaining_raw_url_in_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpSessionBackend("https://api\x7finternal/control")

    assert "api\x7finternal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("url", [" https://example.test", "https://example.test ", "\thttps://example.test", "https://example.test\n"])
def test_rejects_leading_or_trailing_whitespace_without_normalizing(url):
    with pytest.raises(ValueError) as exc_info:
        HttpSessionBackend(url)

    assert "example.test" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_rejects_invalid_timeout_nan():
    with pytest.raises(ValueError, match="timeout"):
        HttpSessionBackend("https://example.test", timeout=float("nan"))


def test_rejects_invalid_timeout_inf():
    with pytest.raises(ValueError, match="timeout"):
        HttpSessionBackend("https://example.test", timeout=float("inf"))


def test_rejects_invalid_timeout_zero():
    with pytest.raises(ValueError, match="timeout"):
        HttpSessionBackend("https://example.test", timeout=0)


def test_rejects_invalid_timeout_negative():
    with pytest.raises(ValueError, match="timeout"):
        HttpSessionBackend("https://example.test", timeout=-1)


def test_rejects_non_numeric_timeout():
    with pytest.raises(ValueError, match="timeout") as exc_info:
        HttpSessionBackend("https://example.test", timeout="secret-timeout-sentinel")  # type: ignore[arg-type]

    assert "secret-timeout-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token transport
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="cp-secret-sentinel")
    backend.read_messages(_ctx())

    _, path, auth, body = session_server.requests[0]
    assert auth == "Bearer cp-secret-sentinel"
    assert "cp-secret-sentinel" not in path
    assert "cp-secret-sentinel" not in repr(body)


def test_no_authorization_header_when_no_token(session_server):
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}")
    backend.read_messages(_ctx())

    _, _, auth, _ = session_server.requests[0]
    assert auth is None


def test_rejects_control_character_token_without_leaking_value():
    with pytest.raises(ValueError) as exc_info:
        HttpSessionBackend("https://api.internal", token="cp-secret-token\r\nX-Leak: yes")

    assert "cp-secret-token" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Error sanitization / logical endpoint labels
# ---------------------------------------------------------------------------


def test_transport_errors_do_not_leak_token():
    backend = HttpSessionBackend("http://127.0.0.1:1", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.read_messages(_ctx())
    assert "cp-secret-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_create_session_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.create_session(_ctx())
    msg = str(exc_info.value)
    assert "POST /sessions/create" in msg
    assert "127.0.0.1" not in msg


def test_read_messages_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.read_messages(_ctx())
    msg = str(exc_info.value)
    assert "POST /sessions/messages" in msg
    assert "127.0.0.1" not in msg


def test_search_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.search(_ctx(), "query")
    msg = str(exc_info.value)
    assert "POST /sessions/search" in msg
    assert "127.0.0.1" not in msg


def test_search_query_not_leaked_in_transport_error():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.search(_ctx(), "secret-query-sentinel")
    assert "secret-query-sentinel" not in str(exc_info.value)


def test_append_message_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append_message(_ctx(), {"role": "user", "content": "hi"})
    msg = str(exc_info.value)
    assert "POST /sessions/append" in msg
    assert "127.0.0.1" not in msg


def test_resolve_resume_session_id_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.resolve_resume_session_id("secret-session-id-sentinel")
    msg = str(exc_info.value)
    assert "secret-session-id-sentinel" not in msg
    assert "/sessions/<session_id>/resume-target" in msg
    assert "127.0.0.1" not in msg


def test_turn_lock_claim_transport_error_uses_logical_label():
    backend = HttpSessionBackend("http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as exc_info:
        backend.claim_turn_lock(_ctx(), owner="secret-owner-sentinel", ttl_seconds=60.0)
    msg = str(exc_info.value)
    assert "POST /sessions/turn-lock/claim" in msg
    assert "secret-owner-sentinel" not in msg
    assert "127.0.0.1" not in msg


def test_http_error_uses_logical_label_and_does_not_retain_cause(session_server):
    session_server.force_status = 500
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.read_messages(_ctx())
    msg = str(exc_info.value)
    assert "cp-secret-sentinel" not in msg
    assert "POST /sessions/messages" in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_is_not_retained_as_exception_cause(session_server):
    session_server.raw_response = "non-json body sentinel"
    backend = HttpSessionBackend(f"http://127.0.0.1:{session_server.server_port}", token="cp-secret-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.read_messages(_ctx())
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
    backend = HttpSessionBackend("https://api.internal", token="cp-secret-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        backend.read_messages(_ctx())

    msg = str(exc_info.value)
    assert "raw-body-sentinel" not in msg
    assert "cp-secret-sentinel" not in msg
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_session_backend_registers_for_profile(session_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"session": "compose-self-hosted"},
                "options": {
                    "session": {
                        "base_url": f"http://127.0.0.1:{session_server.server_port}",
                        "token": "sentinel-token",
                    }
                },
            }
        }
    )
    register_http_session_backend(registry)

    backend = registry.get(BackendCapability.SESSION, _ctx())
    assert isinstance(backend, HttpSessionBackend)
    result = backend.create_session(_ctx())
    assert isinstance(result, str)


def test_register_http_session_backend_preserves_timeout_validation(session_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"session": "compose-self-hosted"},
                "options": {
                    "session": {
                        "base_url": f"http://127.0.0.1:{session_server.server_port}",
                        "timeout": 0,
                    }
                },
            }
        }
    )
    register_http_session_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.SESSION, _ctx())
