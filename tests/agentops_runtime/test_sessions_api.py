"""Tests for the M12B sessions control-plane API handler.

Covers:
- auth (missing/wrong token, no-token-configured)
- happy paths for all eight endpoints
- malformed input (missing body, invalid JSON, missing required fields, bad types)
- sanitized backend errors (500 body contains no sentinel text, owner, query, session ID)
- tenant isolation (different contexts produce scoped contexts)
- turn-lock claim/renew/release behavior
- unknown path / non-POST method → 404
"""

from __future__ import annotations

import json

import pytest


_CONTEXT = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "conversation_id": "conv1",
    "agent_profile_id": "bot",
    "project_id": "proj1",
}

_CONTEXT_BOB = dict(_CONTEXT, user_id="bob", conversation_id="conv2")


class _FakeSessionBackend:
    def __init__(self):
        self.create_calls: list = []
        self.append_calls: list = []
        self.messages_calls: list = []
        self.search_calls: list = []
        self.resume_calls: list = []
        self.claim_calls: list = []
        self.renew_calls: list = []
        self.release_calls: list = []
        self.create_result = "test-session-id"
        self.append_result: int | None = 42
        self.messages_result: list = [{"role": "user", "content": "hello"}]
        self.search_result: list = [{"role": "user", "content": "match"}]
        self.resume_result = "resumed-session-id"
        self.claim_result = True
        self.renew_result = False

    def create_session(self, context, **kwargs):
        self.create_calls.append((context, kwargs))
        return self.create_result

    def append_message(self, context, message):
        self.append_calls.append((context, message))
        return self.append_result

    def read_messages(self, context, **kwargs):
        self.messages_calls.append((context, kwargs))
        return self.messages_result

    def search(self, context, query):
        self.search_calls.append((context, query))
        return self.search_result

    def resolve_resume_session_id(self, session_id):
        self.resume_calls.append(session_id)
        return self.resume_result

    def claim_turn_lock(self, context, **kwargs):
        self.claim_calls.append((context, kwargs))
        return self.claim_result

    def renew_turn_lock(self, context, **kwargs):
        self.renew_calls.append((context, kwargs))
        return self.renew_result

    def release_turn_lock(self, context, **kwargs):
        self.release_calls.append((context, kwargs))


class _BrokenSessionBackend:
    def create_session(self, context, **kwargs):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def append_message(self, context, message):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def read_messages(self, context, **kwargs):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def search(self, context, query):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def resolve_resume_session_id(self, session_id):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def claim_turn_lock(self, context, **kwargs):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def renew_turn_lock(self, context, **kwargs):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def release_turn_lock(self, context, **kwargs):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")


def _call(method, path, qs="", body=b"", auth=None, token=None, backend=None):
    from agentops_runtime.sessions_api import handle_session_request
    if backend is None:
        backend = _FakeSessionBackend()
    return handle_session_request(
        method=method,
        path=path,
        query_string=qs,
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


def _create_body(context=None, **kwargs):
    body = {"context": context if context is not None else _CONTEXT}
    body.update(kwargs)
    return json.dumps(body).encode()


def _append_body(context=None, message=None):
    body = {
        "context": context if context is not None else _CONTEXT,
        "message": message if message is not None else {"role": "user", "content": "hello"},
    }
    return json.dumps(body).encode()


def _messages_body(context=None, **kwargs):
    body = {"context": context if context is not None else _CONTEXT}
    body.update(kwargs)
    return json.dumps(body).encode()


def _search_body(context=None, query="test query"):
    body = {"context": context if context is not None else _CONTEXT, "query": query}
    return json.dumps(body).encode()


def _lock_body(context=None, owner="agent-1", ttl_seconds=300.0):
    body = {
        "context": context if context is not None else _CONTEXT,
        "owner": owner,
        "ttl_seconds": ttl_seconds,
    }
    return json.dumps(body).encode()


def _release_body(context=None, owner="agent-1"):
    body = {
        "context": context if context is not None else _CONTEXT,
        "owner": owner,
    }
    return json.dumps(body).encode()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_succeeds():
    from agentops_runtime.sessions_api import handle_session_request
    assert callable(handle_session_request)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_rejects_missing_authorization_header():
    status, resp = _call("POST", "/sessions/create", body=_create_body(), auth=None, token="secret-token")
    assert status == 401
    assert resp == {"error": "unauthorized"}
    assert "secret-token" not in str(resp)


def test_auth_rejects_wrong_authorization_header():
    status, resp = _call("POST", "/sessions/create", body=_create_body(), auth="Bearer wrong", token="secret-token")
    assert status == 401
    assert resp == {"error": "unauthorized"}


def test_auth_no_token_configured_allows_request():
    fake = _FakeSessionBackend()
    status, _ = _call("POST", "/sessions/create", body=_create_body(), auth=None, token=None, backend=fake)
    assert status == 200


def test_auth_correct_token_allows_request():
    fake = _FakeSessionBackend()
    status, _ = _call(
        "POST", "/sessions/create",
        body=_create_body(), auth="Bearer secret-token", token="secret-token", backend=fake,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# POST /sessions/create
# ---------------------------------------------------------------------------


def test_create_happy_path():
    fake = _FakeSessionBackend()
    fake.create_result = "sid-abc123"
    status, resp = _call("POST", "/sessions/create", body=_create_body(), backend=fake)
    assert status == 200
    assert resp == {"session_id": "sid-abc123"}
    assert len(fake.create_calls) == 1
    ctx, kwargs = fake.create_calls[0]
    assert ctx.user_id == "alice"
    assert ctx.conversation_id == "conv1"


def test_create_forwards_optional_fields():
    fake = _FakeSessionBackend()
    body = _create_body(
        session_id="explicit-sid",
        source="test-source",
        model="claude-opus-4-8",
    )
    status, _ = _call("POST", "/sessions/create", body=body, backend=fake)
    assert status == 200
    _, kwargs = fake.create_calls[0]
    assert kwargs.get("session_id") == "explicit-sid"
    assert kwargs.get("source") == "test-source"
    assert kwargs.get("model") == "claude-opus-4-8"


def test_create_preserves_full_runtime_context_scope_fields():
    fake = _FakeSessionBackend()
    context = dict(
        _CONTEXT,
        run_id="run-1",
        job_id="job-1",
        permissions_ref="perm-1",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery-1",
        metadata={"tenant_region": "us-west"},
    )
    status, _ = _call("POST", "/sessions/create", body=_create_body(context=context), backend=fake)
    assert status == 200
    ctx, _kwargs = fake.create_calls[0]
    assert ctx.run_id == "run-1"
    assert ctx.job_id == "job-1"
    assert ctx.permissions_ref == "perm-1"
    assert ctx.backend_profile == "compose-self-hosted"
    assert ctx.delivery_ref == "delivery-1"
    assert ctx.metadata["tenant_region"] == "us-west"


def test_create_rejects_non_object_context_metadata():
    context = dict(_CONTEXT, metadata="LEAKSENTINEL-metadata")
    status, resp = _call("POST", "/sessions/create", body=_create_body(context=context))
    assert status == 400
    assert "LEAKSENTINEL" not in str(resp)


def test_create_missing_body_returns_400():
    status, resp = _call("POST", "/sessions/create", body=b"")
    assert status == 400
    assert "error" in resp


def test_create_invalid_json_returns_400():
    status, resp = _call("POST", "/sessions/create", body=b"\xff")
    assert status == 400
    assert "error" in resp


def test_create_non_object_body_returns_400():
    status, resp = _call("POST", "/sessions/create", body=json.dumps([1, 2]).encode())
    assert status == 400
    assert "error" in resp


def test_create_missing_context_returns_400():
    status, resp = _call("POST", "/sessions/create", body=json.dumps({}).encode())
    assert status == 400
    assert "error" in resp


def test_create_allows_native_none_context_payload():
    fake = _FakeSessionBackend()
    status, resp = _call("POST", "/sessions/create", body=json.dumps({"context": {}}).encode(), backend=fake)
    assert status == 200
    assert resp == {"session_id": "test-session-id"}
    ctx, kwargs = fake.create_calls[0]
    assert ctx is None
    assert kwargs == {}


def test_create_invalid_context_mode_returns_400():
    ctx = dict(_CONTEXT, mode="unknown-mode")
    status, resp = _call("POST", "/sessions/create", body=_create_body(context=ctx))
    assert status == 400
    assert "error" in resp


def test_create_non_string_context_field_returns_400():
    ctx = dict(_CONTEXT, user_id=42)
    status, resp = _call("POST", "/sessions/create", body=_create_body(context=ctx))
    assert status == 400
    assert "error" in resp


def test_create_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/create", body=_create_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)
    assert "password=secret" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/append
# ---------------------------------------------------------------------------


def test_append_happy_path_returns_message_id():
    fake = _FakeSessionBackend()
    fake.append_result = 7
    status, resp = _call("POST", "/sessions/append", body=_append_body(), backend=fake)
    assert status == 200
    assert resp == {"message_id": 7}
    assert len(fake.append_calls) == 1


def test_append_happy_path_none_message_id_returns_empty():
    fake = _FakeSessionBackend()
    fake.append_result = None
    status, resp = _call("POST", "/sessions/append", body=_append_body(), backend=fake)
    assert status == 200
    assert resp == {}


def test_append_missing_message_returns_400():
    body = json.dumps({"context": _CONTEXT}).encode()
    status, resp = _call("POST", "/sessions/append", body=body)
    assert status == 400
    assert "error" in resp


def test_append_non_object_message_returns_400():
    body = json.dumps({"context": _CONTEXT, "message": "LEAKSENTINEL_string"}).encode()
    status, resp = _call("POST", "/sessions/append", body=body)
    assert status == 400
    assert "error" in resp
    assert "LEAKSENTINEL_string" not in str(resp)


def test_append_preserves_transcript_payload_verbatim():
    fake = _FakeSessionBackend()
    msg = {"role": "assistant", "content": "SENTINEL_PAYLOAD value", "tool_calls": [{"id": "tc1"}]}
    body = json.dumps({"context": _CONTEXT, "message": msg}).encode()
    status, resp = _call("POST", "/sessions/append", body=body, backend=fake)
    assert status == 200
    _, received_msg = fake.append_calls[0]
    assert received_msg["content"] == "SENTINEL_PAYLOAD value"


def test_append_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/append", body=_append_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/messages
# ---------------------------------------------------------------------------


def test_messages_happy_path():
    fake = _FakeSessionBackend()
    fake.messages_result = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    status, resp = _call("POST", "/sessions/messages", body=_messages_body(), backend=fake)
    assert status == 200
    assert "messages" in resp
    assert len(resp["messages"]) == 2


def test_messages_missing_context_returns_400():
    status, resp = _call("POST", "/sessions/messages", body=json.dumps({}).encode())
    assert status == 400
    assert "error" in resp


def test_messages_with_limit():
    fake = _FakeSessionBackend()
    body = _messages_body(limit=5)
    status, _ = _call("POST", "/sessions/messages", body=body, backend=fake)
    assert status == 200
    _, kwargs = fake.messages_calls[0]
    assert kwargs.get("limit") == 5


def test_messages_without_limit_uses_bounded_default():
    fake = _FakeSessionBackend()
    status, _ = _call("POST", "/sessions/messages", body=_messages_body(), backend=fake)
    assert status == 200
    _, kwargs = fake.messages_calls[0]
    assert kwargs.get("limit") == 1000


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_messages_rejects_invalid_or_unbounded_limit(limit):
    status, resp = _call("POST", "/sessions/messages", body=_messages_body(limit=limit))
    assert status == 400
    assert "limit" in resp["error"]


def test_messages_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/messages", body=_messages_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/search
# ---------------------------------------------------------------------------


def test_search_happy_path():
    fake = _FakeSessionBackend()
    fake.search_result = [{"role": "user", "content": "match"}]
    status, resp = _call("POST", "/sessions/search", body=_search_body(query="hello"), backend=fake)
    assert status == 200
    assert "results" in resp
    assert len(resp["results"]) == 1
    assert len(fake.search_calls) == 1
    _, query_arg = fake.search_calls[0]
    assert query_arg == "hello"


def test_search_results_are_bounded():
    fake = _FakeSessionBackend()
    fake.search_result = [{"role": "user", "content": str(i)} for i in range(1005)]
    status, resp = _call("POST", "/sessions/search", body=_search_body(query="hello"), backend=fake)
    assert status == 200
    assert len(resp["results"]) == 1000


def test_search_missing_query_returns_400():
    body = json.dumps({"context": _CONTEXT}).encode()
    status, resp = _call("POST", "/sessions/search", body=body)
    assert status == 400
    assert "error" in resp


def test_search_non_string_query_returns_400():
    body = json.dumps({"context": _CONTEXT, "query": 123}).encode()
    status, resp = _call("POST", "/sessions/search", body=body)
    assert status == 400
    assert "error" in resp


def test_search_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/search", body=_search_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


def test_search_error_does_not_leak_query():
    broken = _BrokenSessionBackend()
    body = json.dumps({"context": _CONTEXT, "query": "LEAKSENTINEL-search-query"}).encode()
    status, resp = _call("POST", "/sessions/search", body=body, backend=broken)
    assert status == 500
    assert "LEAKSENTINEL-search-query" not in str(resp)
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/<session_id>/resume-target
# ---------------------------------------------------------------------------


def test_resume_target_happy_path():
    fake = _FakeSessionBackend()
    fake.resume_result = "resumed-sid"
    status, resp = _call("POST", "/sessions/abc123/resume-target", body=b"", backend=fake)
    assert status == 200
    assert resp == {"session_id": "resumed-sid"}
    assert len(fake.resume_calls) == 1


def test_resume_target_url_decodes_session_id():
    fake = _FakeSessionBackend()
    status, _ = _call("POST", "/sessions/abc%2F123/resume-target", body=b"", backend=fake)
    assert status == 200
    assert fake.resume_calls[0] == "abc/123"


def test_resume_target_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/some-session-id/resume-target", body=b"", backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


def test_resume_target_error_does_not_leak_session_id():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/LEAKSENTINEL-session-id/resume-target", body=b"", backend=broken)
    assert status == 500
    assert "LEAKSENTINEL-session-id" not in str(resp)
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/turn-lock/claim
# ---------------------------------------------------------------------------


def test_turn_lock_claim_returns_true():
    fake = _FakeSessionBackend()
    fake.claim_result = True
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=_lock_body(), backend=fake)
    assert status == 200
    assert resp == {"claimed": True}


def test_turn_lock_claim_returns_false_when_contended():
    fake = _FakeSessionBackend()
    fake.claim_result = False
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=_lock_body(), backend=fake)
    assert status == 200
    assert resp == {"claimed": False}


def test_turn_lock_claim_missing_owner_returns_400():
    body = json.dumps({"context": _CONTEXT, "ttl_seconds": 300}).encode()
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=body)
    assert status == 400
    assert "error" in resp


@pytest.mark.parametrize("ttl_seconds", [0, -1, float("nan"), float("inf"), 86_401])
def test_turn_lock_claim_rejects_invalid_or_unbounded_ttl(ttl_seconds):
    body = _lock_body(ttl_seconds=ttl_seconds)
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=body)
    assert status == 400
    assert "ttl_seconds" in resp["error"]


def test_turn_lock_claim_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=_lock_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


def test_turn_lock_claim_error_does_not_leak_owner():
    broken = _BrokenSessionBackend()
    body = json.dumps({"context": _CONTEXT, "owner": "LEAKSENTINEL-owner", "ttl_seconds": 300}).encode()
    status, resp = _call("POST", "/sessions/turn-lock/claim", body=body, backend=broken)
    assert status == 500
    assert "LEAKSENTINEL-owner" not in str(resp)
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/turn-lock/renew
# ---------------------------------------------------------------------------


def test_turn_lock_renew_returns_bool():
    fake = _FakeSessionBackend()
    fake.renew_result = True
    status, resp = _call("POST", "/sessions/turn-lock/renew", body=_lock_body(), backend=fake)
    assert status == 200
    assert resp == {"renewed": True}


def test_turn_lock_renew_returns_false_when_expired():
    fake = _FakeSessionBackend()
    fake.renew_result = False
    status, resp = _call("POST", "/sessions/turn-lock/renew", body=_lock_body(), backend=fake)
    assert status == 200
    assert resp == {"renewed": False}


@pytest.mark.parametrize("ttl_seconds", [0, -1, float("nan"), float("inf"), 86_401])
def test_turn_lock_renew_rejects_invalid_or_unbounded_ttl(ttl_seconds):
    body = _lock_body(ttl_seconds=ttl_seconds)
    status, resp = _call("POST", "/sessions/turn-lock/renew", body=body)
    assert status == 400
    assert "ttl_seconds" in resp["error"]


def test_turn_lock_renew_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/turn-lock/renew", body=_lock_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# POST /sessions/turn-lock/release
# ---------------------------------------------------------------------------


def test_turn_lock_release_returns_empty():
    fake = _FakeSessionBackend()
    status, resp = _call("POST", "/sessions/turn-lock/release", body=_release_body(), backend=fake)
    assert status == 200
    assert resp == {}
    assert len(fake.release_calls) == 1


def test_turn_lock_release_missing_owner_returns_400():
    body = json.dumps({"context": _CONTEXT}).encode()
    status, resp = _call("POST", "/sessions/turn-lock/release", body=body)
    assert status == 400
    assert "error" in resp


def test_turn_lock_release_backend_failure_returns_sanitized_500():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/turn-lock/release", body=_release_body(), backend=broken)
    assert status == 500
    assert resp == {"error": "internal error"}
    assert "LEAKSENTINEL" not in str(resp)


# ---------------------------------------------------------------------------
# Turn-lock sequence: claim → renew → release
# ---------------------------------------------------------------------------


def test_turn_lock_full_sequence():
    fake = _FakeSessionBackend()
    fake.claim_result = True
    fake.renew_result = True

    s1, r1 = _call("POST", "/sessions/turn-lock/claim", body=_lock_body(owner="worker-1"), backend=fake)
    assert s1 == 200
    assert r1["claimed"] is True

    s2, r2 = _call("POST", "/sessions/turn-lock/renew", body=_lock_body(owner="worker-1"), backend=fake)
    assert s2 == 200
    assert r2["renewed"] is True

    s3, r3 = _call("POST", "/sessions/turn-lock/release", body=_release_body(owner="worker-1"), backend=fake)
    assert s3 == 200
    assert r3 == {}

    assert len(fake.claim_calls) == 1
    assert len(fake.renew_calls) == 1
    assert len(fake.release_calls) == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_isolation_contexts_are_scoped_separately():
    class _TrackingBackend:
        def __init__(self):
            self.contexts_seen: list = []
            self._counter = 0

        def create_session(self, context, **kwargs):
            self.contexts_seen.append(context)
            self._counter += 1
            return f"sid-{self._counter}"

    backend = _TrackingBackend()

    body_alice = json.dumps({"context": _CONTEXT}).encode()
    body_bob = json.dumps({"context": _CONTEXT_BOB}).encode()

    s_a, r_a = _call("POST", "/sessions/create", body=body_alice, backend=backend)
    s_b, r_b = _call("POST", "/sessions/create", body=body_bob, backend=backend)

    assert s_a == 200
    assert s_b == 200

    ctx_a = backend.contexts_seen[0]
    ctx_b = backend.contexts_seen[1]

    assert ctx_a.user_id == "alice"
    assert ctx_b.user_id == "bob"
    assert ctx_a != ctx_b
    assert r_a["session_id"] != r_b["session_id"]


# ---------------------------------------------------------------------------
# Unknown path / method
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404():
    status, resp = _call("POST", "/sessions/unknown-endpoint", body=b"{}")
    assert status == 404
    assert "error" in resp


def test_get_method_returns_404():
    status, resp = _call("GET", "/sessions/create")
    assert status == 404
    assert "error" in resp


def test_non_post_method_returns_404():
    status, resp = _call("DELETE", "/sessions/create", body=_create_body())
    assert status == 404
    assert "error" in resp


# ---------------------------------------------------------------------------
# Error sanitization - general
# ---------------------------------------------------------------------------


def test_create_backend_error_text_not_in_response():
    broken = _BrokenSessionBackend()
    status, resp = _call("POST", "/sessions/create", body=_create_body(), backend=broken)
    assert status == 500
    assert "LEAKSENTINEL" not in str(resp)
    assert "password" not in str(resp)
    assert resp == {"error": "internal error"}
