from __future__ import annotations

import json

import pytest


_SCOPE = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "user_id": "alice",
    "conversation_id": "conv1",
    "external_channel_id": None,
    "external_thread_id": None,
    "agent_profile_id": "bot",
    "project_id": "proj1",
    "run_id": "run-1",
    "run_type": "conversation",
    "job_id": None,
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
}

_SCOPE_BOB = dict(_SCOPE, user_id="bob", conversation_id="conv2")


class _FakeAuditBackend:
    def __init__(self):
        self._store: dict = {}
        self.record_calls: list = []

    def _ctx_key(self, context):
        if context is None:
            return ()
        d = context.to_dict()
        return tuple(sorted((k, d.get(k)) for k in d if k != "metadata"))

    def record(self, context, event):
        self.record_calls.append((context, event))
        key = self._ctx_key(context)
        self._store.setdefault(key, []).append(dict(event))

    def list_events(self, context, *, limit=None):
        events = list(self._store.get(self._ctx_key(context), []))
        return events if limit is None else events[:limit]


class _BrokenAuditBackend:
    def record(self, context, event):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def list_events(self, context, *, limit=None):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")


def _call(method, path, qs="", body=b"", auth=None, token=None, backend=None):
    from agentops_runtime.audit_api import handle_audit_request
    if backend is None:
        backend = _FakeAuditBackend()
    return handle_audit_request(
        method=method,
        path=path,
        query_string=qs,
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


def _post_body(scope=None, event=None):
    return json.dumps({
        "scope": scope if scope is not None else _SCOPE,
        "event": event if event is not None else {"event_type": "test"},
    }).encode()


def test_import_succeeds():
    from agentops_runtime.audit_api import handle_audit_request
    assert callable(handle_audit_request)


def test_post_audit_happy_path():
    fake = _FakeAuditBackend()
    sentinel_event = {"event_type": "SENTINEL_EVENT_VALUE", "action": "login"}
    status, resp = _call("POST", "/audit", body=_post_body(event=sentinel_event), backend=fake)
    assert status == 200
    assert len(fake.record_calls) == 1
    ctx, event_arg = fake.record_calls[0]
    assert ctx.run_id == "run-1"
    assert event_arg == sentinel_event
    assert "SENTINEL_EVENT_VALUE" not in str(resp)


def test_get_audit_returns_redacted_event_summaries():
    class _ListBackend:
        def list_events(self, context, *, limit=None):
            return [
                {
                    "event_type": "LEAKSENTINEL_event_type",
                    "scope": {"user_id": "LEAKSENTINEL_alice"},
                    "event": {"payload": "LEAKSENTINEL_payload"},
                }
            ]

    qs = "scope=" + json.dumps(_SCOPE)
    status, resp = _call("GET", "/audit", qs=qs, backend=_ListBackend())
    assert status == 200
    assert resp == {"events": [{"redacted": True}], "count": 1, "returned": 1, "truncated": False}
    rendered = str(resp)
    assert "LEAKSENTINEL_event_type" not in rendered
    assert "LEAKSENTINEL_alice" not in rendered
    assert "LEAKSENTINEL_payload" not in rendered


def test_get_audit_readback_is_bounded_and_reports_truncation():
    from agentops_runtime.audit_api import AUDIT_READBACK_LIMIT

    class _ManyEventsBackend:
        def __init__(self):
            self.limit_seen = None

        def list_events(self, context, *, limit=None):
            self.limit_seen = limit
            return [
                {"event_type": "LEAKSENTINEL", "idx": i}
                for i in range(AUDIT_READBACK_LIMIT + 5)
            ]

    backend = _ManyEventsBackend()
    qs = "scope=" + json.dumps(_SCOPE)
    status, resp = _call("GET", "/audit", qs=qs, backend=backend)

    assert status == 200
    assert backend.limit_seen == AUDIT_READBACK_LIMIT + 1
    assert len(resp["events"]) == AUDIT_READBACK_LIMIT
    assert resp["returned"] == AUDIT_READBACK_LIMIT
    assert resp["count"] == AUDIT_READBACK_LIMIT
    assert resp["truncated"] is True
    assert "LEAKSENTINEL" not in str(resp)


def test_auth_rejects_missing_authorization_header():
    status, resp = _call("POST", "/audit", body=_post_body(), auth=None, token="secret-token")
    assert status == 401
    assert resp == {"error": "unauthorized"}
    assert "secret-token" not in str(resp)


def test_auth_rejects_wrong_authorization_header():
    status, resp = _call("POST", "/audit", body=_post_body(), auth="Bearer wrong", token="secret-token")
    assert status == 401
    assert resp == {"error": "unauthorized"}


def test_auth_no_token_configured_allows_request():
    fake = _FakeAuditBackend()
    status, _ = _call("POST", "/audit", body=_post_body(), auth=None, token=None, backend=fake)
    assert status == 200


def test_post_missing_body_returns_400():
    status, resp = _call("POST", "/audit", body=b"")
    assert status == 400
    assert "error" in resp


def test_post_invalid_json_body_returns_400():
    status, resp = _call("POST", "/audit", body=b"\xff")
    assert status == 400
    assert "error" in resp


def test_post_non_object_body_returns_400():
    status, resp = _call("POST", "/audit", body=json.dumps([1, 2]).encode())
    assert status == 400
    assert "error" in resp


def test_post_missing_scope_returns_400():
    body = json.dumps({"event": {"event_type": "test"}}).encode()
    status, resp = _call("POST", "/audit", body=body)
    assert status == 400
    assert "error" in resp


def test_post_non_object_scope_returns_400():
    body = json.dumps({"scope": "LEAKSENTINEL_scope_string", "event": {"event_type": "test"}}).encode()
    status, resp = _call("POST", "/audit", body=body)
    assert status == 400
    assert "error" in resp
    assert "LEAKSENTINEL_scope_string" not in str(resp)


def test_post_incomplete_scope_returns_400():
    incomplete = dict(_SCOPE)
    incomplete.pop("conversation_id")
    status, resp = _call("POST", "/audit", body=_post_body(scope=incomplete))
    assert status == 400
    assert resp == {"error": "scope.conversation_id is required"}


def test_post_missing_event_returns_400():
    body = json.dumps({"scope": _SCOPE}).encode()
    status, resp = _call("POST", "/audit", body=body)
    assert status == 400
    assert "error" in resp


def test_post_non_object_event_returns_400():
    body = json.dumps({"scope": _SCOPE, "event": "LEAKSENTINEL_event_string"}).encode()
    status, resp = _call("POST", "/audit", body=body)
    assert status == 400
    assert "error" in resp
    assert "LEAKSENTINEL_event_string" not in str(resp)


def test_backend_record_failure_returns_sanitized_500():
    broken = _BrokenAuditBackend()
    status, resp = _call("POST", "/audit", body=_post_body(), backend=broken)
    assert status == 500
    assert "LEAKSENTINEL" not in str(resp)
    assert "password" not in str(resp)
    assert resp == {"error": "internal error"}


def test_get_backend_list_failure_returns_sanitized_500():
    broken = _BrokenAuditBackend()
    qs = "scope=" + json.dumps(_SCOPE)
    status, resp = _call("GET", "/audit", qs=qs, backend=broken)
    assert status == 500
    assert "LEAKSENTINEL" not in str(resp)
    assert resp == {"error": "internal error"}


def test_scope_isolation():
    fake = _FakeAuditBackend()
    _call("POST", "/audit", body=_post_body(scope=_SCOPE, event={"event_type": "alice_event"}), backend=fake)
    _call("POST", "/audit", body=_post_body(scope=_SCOPE_BOB, event={"event_type": "bob_event"}), backend=fake)

    qs_alice = "scope=" + json.dumps(_SCOPE)
    status_a, resp_a = _call("GET", "/audit", qs=qs_alice, backend=fake)
    assert status_a == 200
    assert resp_a == {"events": [{"redacted": True}], "count": 1, "returned": 1, "truncated": False}

    qs_bob = "scope=" + json.dumps(_SCOPE_BOB)
    status_b, resp_b = _call("GET", "/audit", qs=qs_bob, backend=fake)
    assert status_b == 200
    assert resp_b == {"events": [{"redacted": True}], "count": 1, "returned": 1, "truncated": False}


def test_unknown_method_returns_404():
    status, resp = _call("DELETE", "/audit", body=_post_body())
    assert status == 404
    assert "error" in resp


def test_unknown_path_returns_404():
    status, resp = _call("GET", "/audit/extra")
    assert status == 404
    assert "error" in resp
