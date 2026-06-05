"""Tests for the M12B worker-registry server-side handler (workers_api.py).

Covers:
- SQLiteWorkerRegistryBackend CRUD (register/heartbeat/drain/recover/list)
- Happy path: each endpoint round-trips through handle_worker_request
- Scope isolation: None/{} context -> default scope; distinct contexts isolated
- Route exactness: semicolons, prefix lookalikes, encoded slashes, unknown actions rejected
- Auth: 401 before body read when token set; auth check before backend construction
- Body validation: missing/malformed/non-object body -> 400 fail-closed
- Numeric validation: non-finite/bool/negative now or ttl_seconds -> 400
- Error sanitization: no raw worker IDs, tokens, DB paths, or backend exception text
- Worker/slots payload preserved in list output (no lossy server-side redaction)
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from agentops_runtime.workers_api import (
    SQLiteWorkerRegistryBackend,
    handle_worker_request,
    is_worker_route,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCOPE_FIELDS = (
    "mode", "org_id", "workspace_id", "workspace_type", "project_id",
    "external_channel_id", "external_thread_id", "conversation_id",
    "user_id", "agent_profile_id", "run_type", "parent_session_id",
    "backend_profile", "delivery_ref",
)
_DEFAULT_SCOPE = json.dumps([None] * len(_SCOPE_FIELDS), separators=(",", ":"))


def _ctx(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "agentops",
        "org_id": "acme",
        "workspace_id": "ws-main",
        "user_id": "alice",
        "conversation_id": "conv-1",
        "agent_profile_id": "default",
        "project_id": "proj-1",
        "run_type": "conversation",
        "backend_profile": "compose-self-hosted",
    }
    base.update(overrides)
    return base


def _body(**fields: Any) -> bytes:
    return json.dumps(fields).encode("utf-8")


def _call(
    path: str,
    body: bytes | None = None,
    *,
    backend: Any,
    token: str | None = None,
    auth: str | None = None,
    method: str = "POST",
) -> tuple[int, dict[str, Any]]:
    if body is None:
        body = _body(context=_ctx())
    return handle_worker_request(
        method=method,
        path=path,
        query_string="",
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


@pytest.fixture
def db(tmp_path: Path):
    return SQLiteWorkerRegistryBackend(str(tmp_path / "workers.db"))


# ---------------------------------------------------------------------------
# SQLiteWorkerRegistryBackend — CRUD
# ---------------------------------------------------------------------------


def test_backend_register_returns_nonempty_string(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "w-1", "capacity": 4})
    assert isinstance(worker_id, str)
    assert len(worker_id) > 0


def test_backend_register_preserves_supplied_worker_id_for_supervisor_parity(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "supervisor-worker", "capacity": 4})
    assert worker_id == "supervisor-worker"


def test_backend_register_generates_id_when_worker_id_missing(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"capacity": 4})
    assert isinstance(worker_id, str)
    assert len(worker_id) > 0


def test_backend_register_returns_distinct_ids(db):
    scope = _DEFAULT_SCOPE
    id1 = db.register(scope, worker={"id": "w-1"})
    id2 = db.register(scope, worker={"id": "w-2"})
    assert id1 != id2


def test_backend_heartbeat_does_not_raise_for_known_worker(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "w-1"})
    db.heartbeat(scope, worker_id, slots={"tasks": 2})


def test_backend_heartbeat_unknown_worker_raises_key_error(db):
    with pytest.raises(KeyError):
        db.heartbeat(_DEFAULT_SCOPE, "missing-worker", slots={})


def test_backend_mark_draining_marks_worker(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "w-1"})
    db.mark_draining(scope, worker_id)
    workers = db.list_workers(scope)
    assert any(w.get("draining") for w in workers)


def test_backend_mark_draining_unknown_worker_raises_key_error(db):
    with pytest.raises(KeyError):
        db.mark_draining(_DEFAULT_SCOPE, "missing-worker")


def test_backend_register_duplicate_supplied_id_refreshes_record(db):
    scope = _DEFAULT_SCOPE
    first = db.register(scope, worker={"id": "w-1", "capacity": 1}, now=100.0, ttl_seconds=10.0)
    second = db.register(scope, worker={"id": "w-1", "capacity": 5}, now=200.0, ttl_seconds=20.0)
    assert first == second == "w-1"

    workers = db.list_workers(scope)
    assert len(workers) == 1
    assert workers[0]["worker_id"] == "w-1"
    assert workers[0]["worker"]["capacity"] == 5
    assert workers[0]["registered_at"] == 200.0
    assert workers[0]["expires_at"] == 220.0


def test_backend_list_workers_returns_registered_worker(db):
    scope = _DEFAULT_SCOPE
    db.register(scope, worker={"id": "w-1", "capacity": 2})
    workers = db.list_workers(scope)
    assert len(workers) == 1


def test_backend_list_workers_includes_worker_id_field(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "w-1"})
    workers = db.list_workers(scope)
    assert any(w.get("worker_id") == worker_id for w in workers)


def test_backend_list_workers_preserves_worker_payload(db):
    scope = _DEFAULT_SCOPE
    db.register(scope, worker={"id": "w-1", "capacity": 8, "extra": "value"})
    workers = db.list_workers(scope)
    worker_data = workers[0].get("worker") or {}
    assert worker_data.get("capacity") == 8
    assert worker_data.get("extra") == "value"


def test_backend_list_workers_preserves_slots_payload(db):
    scope = _DEFAULT_SCOPE
    worker_id = db.register(scope, worker={"id": "w-1"})
    db.heartbeat(scope, worker_id, slots={"task_count": 3, "mode": "active"})
    workers = db.list_workers(scope)
    slots = workers[0].get("slots") or {}
    assert slots.get("task_count") == 3
    assert slots.get("mode") == "active"


def test_backend_recover_expired_removes_timed_out_workers(db):
    scope = _DEFAULT_SCOPE
    now_val = 1000.0
    worker_id = db.register(scope, worker={"id": "w-1"}, now=now_val, ttl_seconds=60.0)
    recovered = db.recover_expired(scope, now=now_val + 120.0)
    assert worker_id in recovered
    assert db.list_workers(scope) == []


def test_backend_recover_expired_returns_sorted_worker_ids(db):
    scope = _DEFAULT_SCOPE
    db.register(scope, worker={"id": "w-b"}, now=1000.0, ttl_seconds=60.0)
    db.register(scope, worker={"id": "w-a"}, now=1000.0, ttl_seconds=60.0)
    assert db.recover_expired(scope, now=1200.0) == ["w-a", "w-b"]


def test_backend_recover_expired_returns_empty_when_none_expired(db):
    scope = _DEFAULT_SCOPE
    db.register(scope, worker={"id": "w-1"})
    recovered = db.recover_expired(scope, now=1000.0)
    assert recovered == []


def test_backend_recover_expired_returns_list(db):
    result = db.recover_expired(_DEFAULT_SCOPE)
    assert isinstance(result, list)


def test_backend_scope_isolation(db):
    scope_a = json.dumps(["a"] + [None] * (len(_SCOPE_FIELDS) - 1), separators=(",", ":"))
    scope_b = json.dumps(["b"] + [None] * (len(_SCOPE_FIELDS) - 1), separators=(",", ":"))
    db.register(scope_a, worker={"id": "w-1"})
    assert db.list_workers(scope_b) == []


def test_backend_heartbeat_updates_ttl(db):
    scope = _DEFAULT_SCOPE
    now_val = 1000.0
    worker_id = db.register(scope, worker={"id": "w-1"}, now=now_val, ttl_seconds=10.0)
    db.heartbeat(scope, worker_id, slots={}, now=now_val + 5.0, ttl_seconds=60.0)
    recovered = db.recover_expired(scope, now=now_val + 30.0)
    assert worker_id not in recovered


# ---------------------------------------------------------------------------
# is_worker_route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/workers/register",
    "/workers/recover-expired",
    "/workers/list",
    "/workers/some-id/heartbeat",
    "/workers/some-id/drain",
    "/workers/id%20with%20spaces/heartbeat",
])
def test_is_worker_route_accepts_valid_paths(path):
    assert is_worker_route(path) is True


@pytest.mark.parametrize("path", [
    "/workers",
    "/workers/",
    "/workers/register/extra",
    "/workers/id/unknown-action",
    "/workers/id/heartbeat/extra",
    "/workers2/register",
    "/workerx/list",
    "/workers%2Fregister",
    "/workers/id%2Fheartbeat",
    "/workers;register",
    "/workers/id;heartbeat",
    "/workers/id/heartbeat;alias",
])
def test_is_worker_route_rejects_invalid_paths(path):
    assert is_worker_route(path) is False


# ---------------------------------------------------------------------------
# handle_worker_request — happy path
# ---------------------------------------------------------------------------


def test_register_returns_200_with_worker_id(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, response = _call("/workers/register", body, backend=db)
    assert status == 200
    assert isinstance(response.get("worker_id"), str)
    assert response["worker_id"]


def test_register_returns_supplied_worker_id_for_native_supervisor_parity(db):
    body = _body(context=_ctx(), worker={"id": "supervisor-worker"})
    status, response = _call("/workers/register", body, backend=db)
    assert status == 200
    assert response["worker_id"] == "supervisor-worker"


def test_drain_with_supplied_supervisor_worker_id_marks_registered_worker(db):
    reg_body = _body(context=_ctx(), worker={"id": "supervisor-worker"})
    _call("/workers/register", reg_body, backend=db)
    drain_body = _body(context=_ctx())
    status, _ = _call("/workers/supervisor-worker/drain", drain_body, backend=db)
    assert status == 200

    _, response = _call("/workers/list", _body(context=_ctx()), backend=db)
    assert response["workers"][0]["worker_id"] == "supervisor-worker"
    assert response["workers"][0]["draining"] is True


def test_register_with_none_context_returns_200(db):
    body = _body(context=None, worker={"id": "w-1"})
    status, response = _call("/workers/register", body, backend=db)
    assert status == 200
    assert isinstance(response.get("worker_id"), str)


def test_register_with_empty_context_returns_200(db):
    body = _body(context={}, worker={"id": "w-1"})
    status, response = _call("/workers/register", body, backend=db)
    assert status == 200


def test_heartbeat_returns_200(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    _, reg = _call("/workers/register", body, backend=db)
    worker_id = reg["worker_id"]
    hb_body = _body(context=_ctx(), slots={"tasks": 1})
    status, response = _call(f"/workers/{worker_id}/heartbeat", hb_body, backend=db)
    assert status == 200


def test_drain_returns_200(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    _, reg = _call("/workers/register", body, backend=db)
    worker_id = reg["worker_id"]
    drain_body = _body(context=_ctx())
    status, response = _call(f"/workers/{worker_id}/drain", drain_body, backend=db)
    assert status == 200


def test_heartbeat_unknown_worker_returns_404_without_raw_id(db):
    body = _body(context=_ctx(), slots={})
    status, response = _call("/workers/secret-worker-id/heartbeat", body, backend=db)
    assert status == 404
    assert response.get("error") == "not found"
    assert "secret-worker-id" not in json.dumps(response)


def test_drain_unknown_worker_returns_404_without_raw_id(db):
    body = _body(context=_ctx())
    status, response = _call("/workers/secret-worker-id/drain", body, backend=db)
    assert status == 404
    assert response.get("error") == "not found"
    assert "secret-worker-id" not in json.dumps(response)


def test_recover_expired_returns_200_with_recovered_list(db):
    body = _body(context=_ctx())
    status, response = _call("/workers/recover-expired", body, backend=db)
    assert status == 200
    assert isinstance(response.get("recovered"), list)


def test_list_returns_200_with_workers_list(db):
    body = _body(context=_ctx())
    status, response = _call("/workers/list", body, backend=db)
    assert status == 200
    assert isinstance(response.get("workers"), list)


def test_list_returns_registered_workers(db):
    reg_body = _body(context=_ctx(), worker={"id": "w-1", "capacity": 4})
    _call("/workers/register", reg_body, backend=db)
    list_body = _body(context=_ctx())
    _, response = _call("/workers/list", list_body, backend=db)
    assert len(response["workers"]) == 1


def test_heartbeat_with_url_encoded_worker_id(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    _, reg = _call("/workers/register", body, backend=db)
    worker_id = reg["worker_id"]
    encoded_id = worker_id.replace("-", "%2D")
    hb_body = _body(context=_ctx(), slots={})
    status, _ = _call(f"/workers/{encoded_id}/heartbeat", hb_body, backend=db)
    assert status == 200


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_auth_required_when_token_set_returns_401_without_header(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, response = _call("/workers/register", body, backend=db, token="secret-token")
    assert status == 401
    assert response.get("error") == "unauthorized"


def test_auth_required_when_token_set_returns_401_with_wrong_header(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, _ = _call("/workers/register", body, backend=db, token="secret-token", auth="Bearer wrong")
    assert status == 401


def test_auth_accepted_with_correct_bearer_token(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, response = _call(
        "/workers/register", body, backend=db,
        token="secret-token", auth="Bearer secret-token",
    )
    assert status == 200
    assert "worker_id" in response


def test_auth_not_required_when_no_token(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, _ = _call("/workers/register", body, backend=db, token=None, auth=None)
    assert status == 200


def test_auth_check_happens_before_backend_construction(db):
    """401 returned before backend is queried when token mismatch."""
    body = _body(context=_ctx(), worker={"id": "w-1"})

    class _ExplodingBackend:
        def register(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("backend should not be called")

    status, response = _call(
        "/workers/register", body, backend=_ExplodingBackend(),
        token="secret-token", auth=None,
    )
    assert status == 401


# ---------------------------------------------------------------------------
# Route validation
# ---------------------------------------------------------------------------


def test_unknown_action_returns_404(db):
    body = _body(context=_ctx())
    status, _ = _call("/workers/unknown-action", body, backend=db)
    assert status == 404


def test_extra_segment_returns_404(db):
    body = _body(context=_ctx())
    status, _ = _call("/workers/register/extra", body, backend=db)
    assert status == 404


def test_semicolon_alias_returns_404(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, _ = _call("/workers/register;action=x", body, backend=db)
    assert status == 404


def test_prefix_lookalike_returns_404(db):
    body = _body(context=_ctx())
    status, _ = _call("/workers2/register", body, backend=db)
    assert status == 404


def test_encoded_slash_in_worker_id_returns_404(db):
    body = _body(context=_ctx(), slots={})
    status, _ = _call("/workers/id%2Fextra/heartbeat", body, backend=db)
    assert status == 404


def test_encoded_prefix_ambiguity_returns_404(db):
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, _ = _call("/workers%2Fregister", body, backend=db)
    assert status == 404


def test_non_post_method_returns_405(db):
    body = _body(context=_ctx())
    status, _ = handle_worker_request(
        method="GET",
        path="/workers/list",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=db,
    )
    assert status == 405


# ---------------------------------------------------------------------------
# Scope isolation via context
# ---------------------------------------------------------------------------


def test_workers_registered_under_different_contexts_are_isolated(db):
    ctx_a = _ctx(org_id="org-a")
    ctx_b = _ctx(org_id="org-b")
    _call("/workers/register", _body(context=ctx_a, worker={"id": "w-1"}), backend=db)

    _, response_b = _call("/workers/list", _body(context=ctx_b), backend=db)
    assert response_b["workers"] == []


def test_none_context_and_empty_context_map_to_same_scope(db):
    _call("/workers/register", _body(context=None, worker={"id": "w-1"}), backend=db)
    _, response = _call("/workers/list", _body(context={}), backend=db)
    assert len(response["workers"]) == 1


def test_recover_expired_uses_context_scope(db):
    ctx_a = _ctx(org_id="org-a")
    ctx_b = _ctx(org_id="org-b")
    now_val = 1000.0
    # Register in org-a with short TTL
    reg_body = _body(context=ctx_a, worker={"id": "w-1"}, now=now_val, ttl_seconds=10.0)
    _, reg = _call("/workers/register", reg_body, backend=db)

    # Recover under org-b should not remove org-a's workers
    recover_body = _body(context=ctx_b, now=now_val + 120.0)
    _, response = _call("/workers/recover-expired", recover_body, backend=db)
    assert reg["worker_id"] not in response["recovered"]

    # Recover under org-a should remove it
    recover_body_a = _body(context=ctx_a, now=now_val + 120.0)
    _, response_a = _call("/workers/recover-expired", recover_body_a, backend=db)
    assert reg["worker_id"] in response_a["recovered"]


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_missing_body_returns_400(db):
    status, _ = _call("/workers/list", b"", backend=db)
    assert status == 400


def test_invalid_json_body_returns_400(db):
    status, _ = _call("/workers/list", b"not-json", backend=db)
    assert status == 400


def test_non_object_json_body_returns_400_list(db):
    status, _ = _call("/workers/list", b'["a", "b"]', backend=db)
    assert status == 400


def test_non_object_json_body_returns_400_string(db):
    status, _ = _call("/workers/list", b'"a string"', backend=db)
    assert status == 400


def test_non_object_context_returns_400(db):
    body = _body(context="not-a-dict")
    status, _ = _call("/workers/list", body, backend=db)
    assert status == 400


def test_context_with_non_string_field_returns_400(db):
    ctx = _ctx(org_id=42)
    body = _body(context=ctx)
    status, _ = _call("/workers/list", body, backend=db)
    assert status == 400


# ---------------------------------------------------------------------------
# Numeric validation: now
# ---------------------------------------------------------------------------


def test_nan_now_in_register_returns_400(db):
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "now": NaN}}'.encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_inf_now_in_register_returns_400(db):
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "now": Infinity}}'.encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_negative_now_in_recover_expired_is_accepted(db):
    body = _body(context=_ctx(), now=-1.0)
    status, _ = _call("/workers/recover-expired", body, backend=db)
    assert status == 200


def test_bool_now_returns_400(db):
    body = json.dumps({"context": _ctx(), "worker": {"id": "w-1"}, "now": True}).encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_oversized_integer_now_returns_sanitized_400(db):
    huge_now = "1" + ("0" * 400)
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "now": {huge_now}}}'.encode()
    status, response = _call("/workers/register", body, backend=db)
    assert status == 400
    assert huge_now not in str(response)


# ---------------------------------------------------------------------------
# Numeric validation: ttl_seconds
# ---------------------------------------------------------------------------


def test_negative_ttl_seconds_returns_400(db):
    body = _body(context=_ctx(), worker={"id": "w-1"}, ttl_seconds=-1.0)
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_zero_ttl_seconds_returns_400(db):
    body = _body(context=_ctx(), worker={"id": "w-1"}, ttl_seconds=0.0)
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_nan_ttl_seconds_returns_400(db):
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "ttl_seconds": NaN}}'.encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_inf_ttl_seconds_returns_400(db):
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "ttl_seconds": Infinity}}'.encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_bool_ttl_seconds_returns_400(db):
    body = json.dumps({"context": _ctx(), "worker": {"id": "w-1"}, "ttl_seconds": True}).encode()
    status, _ = _call("/workers/register", body, backend=db)
    assert status == 400


def test_valid_ttl_seconds_is_accepted(db):
    body = _body(context=_ctx(), worker={"id": "w-1"}, ttl_seconds=60.0)
    status, response = _call("/workers/register", body, backend=db)
    assert status == 200
    assert "worker_id" in response


def test_oversized_integer_ttl_seconds_returns_sanitized_400(db):
    huge_ttl = "1" + ("0" * 400)
    ctx_json = json.dumps(_ctx())
    body = f'{{"context": {ctx_json}, "worker": {{"id": "w-1"}}, "ttl_seconds": {huge_ttl}}}'.encode()
    status, response = _call("/workers/register", body, backend=db)
    assert status == 400
    assert huge_ttl not in str(response)


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_backend_error_does_not_leak_worker_id_in_503(db):
    """Backend exception text must not appear in response."""

    class _ExplodingBackend:
        def register(self, scope, worker, **kw):
            raise RuntimeError("db_path=/secret/path/workers.db worker_id=secret-worker-sentinel")

    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, response = handle_worker_request(
        method="POST",
        path="/workers/register",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=_ExplodingBackend(),
    )
    assert status == 503
    assert "secret-worker-sentinel" not in str(response)
    assert "secret/path" not in str(response)


def test_401_response_does_not_echo_token():
    body = _body(context=_ctx(), worker={"id": "w-1"})
    status, response = handle_worker_request(
        method="POST",
        path="/workers/register",
        query_string="",
        body_bytes=body,
        auth_header="Bearer wrong-secret-sentinel",
        token="real-secret-sentinel",
        backend=None,  # type: ignore[arg-type]
    )
    assert status == 401
    assert "real-secret-sentinel" not in str(response)
    assert "wrong-secret-sentinel" not in str(response)


def test_404_response_does_not_echo_path_segments():
    body = _body(context=_ctx())
    status, response = handle_worker_request(
        method="POST",
        path="/workers/secret-action-sentinel",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=None,  # type: ignore[arg-type]
    )
    assert status == 404
    assert "secret-action-sentinel" not in str(response)


# ---------------------------------------------------------------------------
# Worker/slots payload preservation in list output
# ---------------------------------------------------------------------------


def test_list_output_includes_worker_metadata(db):
    reg_body = _body(context=_ctx(), worker={"id": "w-1", "capacity": 4, "region": "us-east"})
    _call("/workers/register", reg_body, backend=db)
    _, response = _call("/workers/list", _body(context=_ctx()), backend=db)
    worker = response["workers"][0]
    worker_data = worker.get("worker") or {}
    assert worker_data.get("capacity") == 4
    assert worker_data.get("region") == "us-east"


def test_list_output_includes_slots_after_heartbeat(db):
    reg_body = _body(context=_ctx(), worker={"id": "w-1"})
    _, reg = _call("/workers/register", reg_body, backend=db)
    worker_id = reg["worker_id"]
    hb_body = _body(context=_ctx(), slots={"task_count": 5, "active": True})
    _call(f"/workers/{worker_id}/heartbeat", hb_body, backend=db)
    _, response = _call("/workers/list", _body(context=_ctx()), backend=db)
    worker = response["workers"][0]
    slots = worker.get("slots") or {}
    assert slots.get("task_count") == 5
    assert slots.get("active") is True


def test_list_output_includes_draining_flag_after_drain(db):
    reg_body = _body(context=_ctx(), worker={"id": "w-1"})
    _, reg = _call("/workers/register", reg_body, backend=db)
    worker_id = reg["worker_id"]
    _call(f"/workers/{worker_id}/drain", _body(context=_ctx()), backend=db)
    _, response = _call("/workers/list", _body(context=_ctx()), backend=db)
    worker = response["workers"][0]
    assert worker.get("draining") is True
