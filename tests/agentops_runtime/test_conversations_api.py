"""Tests for /conversations/* server-side API and SQLiteConversationRouterBackend (M12B)."""

from __future__ import annotations

import json
import threading
import urllib.parse

import pytest


# ---------------------------------------------------------------------------
# Backend unit tests (SQLiteConversationRouterBackend)
# ---------------------------------------------------------------------------


def _make_backend(tmp_path, name="test.db"):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend

    return SQLiteConversationRouterBackend(str(tmp_path / name))


_CTX_A = {
    "mode": "agentops",
    "org_id": "org-alpha",
    "workspace_id": "ws-one",
    "workspace_type": None,
    "project_id": "proj-x",
    "external_channel_id": None,
    "external_thread_id": None,
    "conversation_id": None,
    "user_id": "alice",
    "agent_profile_id": "bot",
    "run_type": None,
    "parent_session_id": None,
    "backend_profile": None,
    "delivery_ref": None,
}

_CTX_B = dict(_CTX_A, org_id="org-beta")


def test_backend_route_turn_stable_for_same_scope(tmp_path):
    backend = _make_backend(tmp_path)
    run_id_1 = backend.route_turn(_CTX_A, "conv-abc", {})
    run_id_2 = backend.route_turn(_CTX_A, "conv-abc", {})
    assert run_id_1 == run_id_2
    assert isinstance(run_id_1, str) and run_id_1


def test_backend_durable_reopen_returns_same_run_id(tmp_path):
    backend_1 = _make_backend(tmp_path)
    run_id_1 = backend_1.route_turn(_CTX_A, "conv-durable", {})

    backend_2 = _make_backend(tmp_path)
    run_id_2 = backend_2.route_turn(_CTX_A, "conv-durable", {})
    assert run_id_1 == run_id_2


def test_backend_tenant_isolation(tmp_path):
    backend = _make_backend(tmp_path)
    run_a = backend.route_turn(_CTX_A, "conv-shared", {})
    run_b = backend.route_turn(_CTX_B, "conv-shared", {})
    assert run_a != run_b


def test_backend_run_id_excluded_from_scope(tmp_path):
    backend = _make_backend(tmp_path)
    ctx_with_run = dict(_CTX_A, run_id="run-ignored")
    ctx_without_run = dict(_CTX_A)
    ctx_without_run.pop("run_id", None)

    run_a = backend.route_turn(ctx_with_run, "conv-runid", {})
    run_b = backend.route_turn(ctx_without_run, "conv-runid", {})
    assert run_a == run_b


def test_backend_job_id_excluded_from_scope(tmp_path):
    backend = _make_backend(tmp_path)
    ctx_with_job = dict(_CTX_A, job_id="job-ignored")
    ctx_without_job = dict(_CTX_A)
    ctx_without_job.pop("job_id", None)

    run_a = backend.route_turn(ctx_with_job, "conv-jobid", {})
    run_b = backend.route_turn(ctx_without_job, "conv-jobid", {})
    assert run_a == run_b


def test_backend_get_active_run_returns_none_initially(tmp_path):
    backend = _make_backend(tmp_path)
    result = backend.get_active_run(_CTX_A, "conv-new")
    assert result is None


def test_backend_get_active_run_after_route_turn(tmp_path):
    backend = _make_backend(tmp_path)
    run_id = backend.route_turn(_CTX_A, "conv-after-route", {})
    result = backend.get_active_run(_CTX_A, "conv-after-route")
    assert result == run_id


def test_backend_resolve_conversation_prefers_context_conversation_id(tmp_path):
    backend = _make_backend(tmp_path)
    ctx = dict(_CTX_A, conversation_id="ctx-conv-id")
    result = backend.resolve_conversation(ctx, {"conversation_id": "event-conv-id"})
    assert result == "ctx-conv-id"


def test_backend_resolve_conversation_falls_back_to_event_conversation_id(tmp_path):
    backend = _make_backend(tmp_path)
    ctx = dict(_CTX_A, conversation_id=None)
    result = backend.resolve_conversation(ctx, {"conversation_id": "event-conv-abc"})
    assert result == "event-conv-abc"


def test_backend_resolve_conversation_falls_back_to_event_thread_id(tmp_path):
    backend = _make_backend(tmp_path)
    ctx = dict(_CTX_A, conversation_id=None)
    result = backend.resolve_conversation(ctx, {"thread_id": "thread-abc"})
    assert result == "thread-abc"


def test_backend_resolve_conversation_scope_derived_id_not_literal_local(tmp_path):
    backend = _make_backend(tmp_path)
    ctx = dict(_CTX_A, conversation_id=None)
    result = backend.resolve_conversation(ctx, {})
    assert result != "local"
    assert isinstance(result, str) and result


def test_backend_resolve_conversation_scope_derived_id_is_stable(tmp_path):
    backend = _make_backend(tmp_path)
    ctx = dict(_CTX_A, conversation_id=None)
    result_1 = backend.resolve_conversation(ctx, {})
    result_2 = backend.resolve_conversation(ctx, {})
    assert result_1 == result_2


def test_backend_resolve_conversation_scope_derived_isolated_across_tenants(tmp_path):
    backend = _make_backend(tmp_path)
    ctx_a = dict(_CTX_A, conversation_id=None)
    ctx_b = dict(_CTX_B, conversation_id=None)
    result_a = backend.resolve_conversation(ctx_a, {})
    result_b = backend.resolve_conversation(ctx_b, {})
    assert result_a != result_b


def test_backend_route_turn_concurrent_returns_same_run_id(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend

    db_path = str(tmp_path / "concurrent.db")
    results: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            b = SQLiteConversationRouterBackend(db_path)
            results.append(b.route_turn(_CTX_A, "conv-concurrent", {}))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"
    assert len(results) == 8
    assert len(set(results)) == 1, f"concurrent calls returned different run_ids: {set(results)}"


# ---------------------------------------------------------------------------
# HTTP handler tests (handle_conversation_request)
# ---------------------------------------------------------------------------


def _make_handler_backend(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend

    return SQLiteConversationRouterBackend(str(tmp_path / "handler.db"))


_CONTEXT = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": None,
    "project_id": "proj1",
    "external_channel_id": None,
    "external_thread_id": None,
    "conversation_id": None,
    "user_id": "alice",
    "agent_profile_id": "bot",
    "run_type": None,
    "parent_session_id": None,
    "backend_profile": None,
    "delivery_ref": None,
}


def _call(path, body_dict=None, token=None, auth=None, method="POST", tmp_path=None):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "call.db"))
    body_bytes = json.dumps(body_dict).encode() if body_dict is not None else b""
    return handle_conversation_request(
        method=method,
        path=path,
        query_string="",
        body_bytes=body_bytes,
        auth_header=auth,
        token=token,
        backend=backend,
    )


def test_handle_resolve_returns_conversation_id(tmp_path):
    status, resp = _call(
        "/conversations/resolve",
        body_dict={"context": _CONTEXT, "event": {}},
        tmp_path=tmp_path,
    )
    assert status == 200
    assert "conversation_id" in resp
    assert isinstance(resp["conversation_id"], str) and resp["conversation_id"]


def test_handle_resolve_uses_event_conversation_id(tmp_path):
    status, resp = _call(
        "/conversations/resolve",
        body_dict={"context": _CONTEXT, "event": {"conversation_id": "ev-conv-123"}},
        tmp_path=tmp_path,
    )
    assert status == 200
    assert resp["conversation_id"] == "ev-conv-123"


def test_handle_active_run_returns_null_when_no_run(tmp_path):
    status, resp = _call(
        "/conversations/conv-abc/active-run",
        body_dict={"context": _CONTEXT},
        tmp_path=tmp_path,
    )
    assert status == 200
    assert resp.get("run_id") is None


def test_handle_route_turn_passes_turn_payload_to_backend():
    from agentops_runtime.conversations_api import handle_conversation_request

    class _RecordingBackend:
        def __init__(self):
            self.turn = None

        def route_turn(self, _context, _conversation_id, turn):
            self.turn = turn
            return "run-from-turn"

    backend = _RecordingBackend()
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/conv-abc/route",
        query_string="",
        body_bytes=json.dumps({"context": _CONTEXT, "turn": {"message": "hello", "seq": "1"}}).encode(),
        auth_header=None,
        token=None,
        backend=backend,
    )

    assert status == 200
    assert resp == {"run_id": "run-from-turn"}
    assert backend.turn == {"message": "hello", "seq": "1"}


def test_handle_route_turn_rejects_non_object_turn(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "bad-turn.db"))
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/conv-abc/route",
        query_string="",
        body_bytes=json.dumps({"context": _CONTEXT, "turn": "not-object"}).encode(),
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400
    assert resp == {"error": "invalid request"}


@pytest.mark.parametrize("turn", [None, [], "", 0, False])
def test_handle_route_turn_rejects_falsy_non_object_turn_values(tmp_path, turn):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "falsy-bad-turn.db"))
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/conv-abc/route",
        query_string="",
        body_bytes=json.dumps({"context": _CONTEXT, "turn": turn}).encode(),
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400
    assert resp == {"error": "invalid request"}


def test_handle_route_turn_returns_run_id(tmp_path):
    status, resp = _call(
        "/conversations/conv-abc/route",
        body_dict={"context": _CONTEXT, "turn": {}},
        tmp_path=tmp_path,
    )
    assert status == 200
    assert "run_id" in resp
    assert isinstance(resp["run_id"], str) and resp["run_id"]


def test_handle_route_turn_then_active_run(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "flow.db"))

    def call(path, body):
        return handle_conversation_request(
            method="POST",
            path=path,
            query_string="",
            body_bytes=json.dumps(body).encode(),
            auth_header=None,
            token=None,
            backend=backend,
        )

    _, route_resp = call("/conversations/conv-flow/route", {"context": _CONTEXT, "turn": {}})
    run_id = route_resp["run_id"]

    _, active_resp = call("/conversations/conv-flow/active-run", {"context": _CONTEXT})
    assert active_resp["run_id"] == run_id


def test_handle_resolve_malformed_json_returns_400(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "mj.db"))
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=b"not json{{{",
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400
    assert "error" in resp


def test_handle_resolve_oversized_json_integer_returns_sanitized_400(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "huge-json-int.db"))
    oversized_int = "9" * 5_000
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=(b'{"context":{"org_id":' + oversized_int.encode() + b'}}'),
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400
    assert resp == {"error": "invalid request"}
    assert oversized_int not in json.dumps(resp)


def test_handle_resolve_deeply_nested_json_returns_sanitized_400(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "deep-json.db"))
    body = ("[" * 2_000 + "]" * 2_000).encode()
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400
    assert resp == {"error": "invalid request"}


def test_handle_resolve_numeric_body_returns_400(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "nb.db"))
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=b"42",
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400


def test_handle_auth_before_body_resolve(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "auth.db"))
    token = "secret-token"
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=b"not-even-json",
        auth_header="Bearer wrong-token",
        token=token,
        backend=backend,
    )
    assert status == 401
    assert resp.get("error") == "unauthorized"


def test_handle_auth_before_body_route(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "auth2.db"))
    token = "secret-token"
    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/conv-x/route",
        query_string="",
        body_bytes=b"not-even-json",
        auth_header=None,
        token=token,
        backend=backend,
    )
    assert status == 401


def test_handle_url_encoded_id_active_run(tmp_path):
    conv_id = "org/channel/thread"
    encoded = urllib.parse.quote(conv_id, safe="")
    path = f"/conversations/{encoded}/active-run"

    status, resp = _call(path, body_dict={"context": _CONTEXT}, tmp_path=tmp_path)
    assert status == 200
    assert "run_id" in resp


def test_handle_url_encoded_id_route(tmp_path):
    conv_id = "org/channel/thread"
    encoded = urllib.parse.quote(conv_id, safe="")
    path = f"/conversations/{encoded}/route"

    status, resp = _call(path, body_dict={"context": _CONTEXT, "turn": {}}, tmp_path=tmp_path)
    assert status == 200
    assert "run_id" in resp


def test_handle_extra_path_segments_returns_404(tmp_path):
    status, resp = _call(
        "/conversations/foo/bar/baz",
        body_dict={"context": _CONTEXT},
        tmp_path=tmp_path,
    )
    assert status == 404


def test_handle_semicolon_in_path_returns_404(tmp_path):
    status, resp = _call(
        "/conversations/resolve;injection",
        body_dict={"context": _CONTEXT, "event": {}},
        tmp_path=tmp_path,
    )
    assert status == 404


def test_handle_prefix_lookalike_returns_404(tmp_path):
    status, resp = _call(
        "/conversations2/resolve",
        body_dict={"context": _CONTEXT, "event": {}},
        tmp_path=tmp_path,
    )
    assert status == 404


def test_handle_no_raw_context_or_event_in_resolve_response(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "leak.db"))
    body = json.dumps({"context": _CONTEXT, "event": {"secret_val": "LEAKME-event-sentinel"}}).encode()
    _status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=backend,
    )
    resp_text = json.dumps(resp)
    assert "LEAKME-event-sentinel" not in resp_text


def test_handle_backend_error_sanitized(tmp_path):
    from agentops_runtime.conversations_api import handle_conversation_request

    class _BrokenBackend:
        def resolve_conversation(self, *a, **kw):
            raise RuntimeError("LEAKME-backend-secret path=/etc/passwd token=abc")

    status, resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=json.dumps({"context": _CONTEXT, "event": {}}).encode(),
        auth_header=None,
        token=None,
        backend=_BrokenBackend(),
    )
    assert status == 503
    resp_text = json.dumps(resp)
    assert "LEAKME-backend-secret" not in resp_text
    assert "path=" not in resp_text
    assert "token=" not in resp_text


def test_handle_unknown_method_returns_405(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "method.db"))
    status, _resp = handle_conversation_request(
        method="GET",
        path="/conversations/resolve",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 405


def test_handle_empty_body_returns_400(tmp_path):
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend, handle_conversation_request

    backend = SQLiteConversationRouterBackend(str(tmp_path / "empty.db"))
    status, _resp = handle_conversation_request(
        method="POST",
        path="/conversations/resolve",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# is_conversation_route
# ---------------------------------------------------------------------------


def test_is_conversation_route_resolve():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/resolve") is True


def test_is_conversation_route_active_run():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/conv-abc/active-run") is True


def test_is_conversation_route_route():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/conv-abc/route") is True


def test_is_conversation_route_encoded_id():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/org%2Fchannel%2Fthread/route") is True


def test_is_conversation_route_rejects_extra_segments():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/foo/bar/baz") is False


def test_is_conversation_route_rejects_semicolon():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/resolve;injection") is False


def test_is_conversation_route_rejects_prefix_lookalike():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations2/resolve") is False


def test_is_conversation_route_rejects_encoded_slash_in_resolve():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations%2Fresolve") is False


def test_is_conversation_route_rejects_unknown_action():
    from agentops_runtime.conversations_api import is_conversation_route

    assert is_conversation_route("/conversations/abc/unknown-action") is False
