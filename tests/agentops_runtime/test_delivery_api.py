"""Tests for the M12B delivery server-side handler (delivery_api.py).

Covers:
- SQLiteDeliveryBackend: deliver stores verbatim, appends without dedup, persists across instances
- handle_delivery_request: happy path, context variants, auth, validation, backend error
- is_delivery_route: exact match, semicolons, prefix lookalikes, encoded slash, extra segments
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentops_runtime.delivery_api import (
    SQLiteDeliveryBackend,
    handle_delivery_request,
    is_delivery_route,
)


def _backend(tmp_path: Path) -> SQLiteDeliveryBackend:
    return SQLiteDeliveryBackend(str(tmp_path / "delivery.db"))


def _call(
    body: bytes | None = None,
    *,
    backend: Any,
    token: str | None = None,
    auth: str | None = None,
    path: str = "/delivery/deliver",
    method: str = "POST",
) -> tuple[int, dict[str, Any]]:
    if body is None:
        body = json.dumps({"message": {"type": "text", "text": "hello"}}).encode()
    return handle_delivery_request(
        method=method,
        path=path,
        query_string="",
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Happy path and persistence
# ---------------------------------------------------------------------------


def test_deliver_happy_path_returns_empty_dict(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        json.dumps({"context": {"mode": "agentops"}, "message": {"text": "hi"}}).encode(),
        backend=backend,
    )
    assert status == 200
    assert resp == {}


def test_deliver_stores_message_verbatim_including_secret_keys(tmp_path):
    backend = _backend(tmp_path)
    msg = {"type": "text", "text": "hello", "api_key": "sk-secret", "password": "hunter2"}
    _call(json.dumps({"message": msg}).encode(), backend=backend)
    rows = backend.all_rows()
    assert len(rows) == 1
    stored_msg = json.loads(rows[0][1])
    assert stored_msg == msg


def test_deliver_persists_context_scope_including_delivery_ref(tmp_path):
    backend = _backend(tmp_path)
    ctx = {
        "mode": "agentops",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "project_id": "project-a",
        "external_thread_id": "thread-a",
        "conversation_id": "conversation-a",
        "user_id": "user-a",
        "delivery_ref": "slack:channel/thread",
    }
    _call(json.dumps({"context": ctx, "message": {"text": "scoped"}}).encode(), backend=backend)
    stored_ctx = json.loads(backend.all_rows()[0][2])
    assert stored_ctx == ctx


def test_same_delivery_ref_under_different_scopes_appends_distinct_rows(tmp_path):
    backend = _backend(tmp_path)
    msg = {"text": "same destination"}
    for org in ("org-a", "org-b"):
        ctx = {"org_id": org, "workspace_id": "workspace", "delivery_ref": "same-ref"}
        _call(json.dumps({"context": ctx, "message": msg}).encode(), backend=backend)
    rows = backend.all_rows()
    assert len(rows) == 2
    assert [json.loads(row[2])["org_id"] for row in rows] == ["org-a", "org-b"]


def test_deliver_appends_without_dedup(tmp_path):
    backend = _backend(tmp_path)
    msg = {"text": "same"}
    for _ in range(3):
        _call(json.dumps({"message": msg}).encode(), backend=backend)
    assert backend.count() == 3


def test_deliver_persists_across_instances(tmp_path):
    db_path = str(tmp_path / "delivery.db")
    b1 = SQLiteDeliveryBackend(db_path)
    _call(json.dumps({"message": {"x": 1}}).encode(), backend=b1)
    b2 = SQLiteDeliveryBackend(db_path)
    assert b2.count() == 1


# ---------------------------------------------------------------------------
# Context variants
# ---------------------------------------------------------------------------


def test_deliver_accepts_missing_context(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        json.dumps({"message": {"text": "no context"}}).encode(),
        backend=backend,
    )
    assert status == 200
    assert resp == {}


def test_deliver_accepts_empty_context(tmp_path):
    backend = _backend(tmp_path)
    body = json.dumps({"context": {}, "message": {"text": "empty ctx"}}).encode()
    status, resp = _call(body, backend=backend)
    assert status == 200
    assert resp == {}


def test_deliver_accepts_all_none_context_values(tmp_path):
    backend = _backend(tmp_path)
    ctx = {k: None for k in ("mode", "org_id", "workspace_id", "user_id")}
    body = json.dumps({"context": ctx, "message": {"text": "all none"}}).encode()
    status, resp = _call(body, backend=backend)
    assert status == 200
    assert resp == {}


def test_deliver_non_dict_context_returns_400(tmp_path):
    backend = _backend(tmp_path)
    body = json.dumps({"context": ["list"], "message": {"text": "x"}}).encode()
    status, resp = _call(body, backend=backend)
    assert status == 400
    assert "error" in resp


def test_deliver_rejects_non_string_context_scope_values(tmp_path):
    backend = _backend(tmp_path)
    body = json.dumps({"context": {"org_id": 123}, "message": {"text": "x"}}).encode()
    status, resp = _call(body, backend=backend)
    assert status == 400
    assert resp == {"error": "invalid context"}


def test_deliver_filters_non_scope_context_fields_before_persistence(tmp_path):
    backend = _backend(tmp_path)
    ctx = {
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "delivery_ref": "delivery-a",
        "run_id": "LEAKSENTINEL-run",
        "job_id": "LEAKSENTINEL-job",
        "token": "LEAKSENTINEL-token",
        "nested": {"secret": "LEAKSENTINEL-nested"},
    }
    status, resp = _call(json.dumps({"context": ctx, "message": {"text": "x"}}).encode(), backend=backend)
    assert status == 200
    assert resp == {}
    stored_ctx = json.loads(backend.all_rows()[0][2])
    assert stored_ctx == {
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "delivery_ref": "delivery-a",
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_rejected_returns_401_before_backend(tmp_path):
    class _FailBackend:
        def deliver(self, *args: Any) -> None:
            raise AssertionError("backend must not be called on auth failure")

    status, resp = _call(
        json.dumps({"message": {"text": "x"}}).encode(),
        backend=_FailBackend(),
        token="tok",
        auth="Bearer wrong",
    )
    assert status == 401
    assert resp == {"error": "unauthorized"}


def test_auth_passes_with_correct_token(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        json.dumps({"message": {"text": "x"}}).encode(),
        backend=backend,
        token="correct",
        auth="Bearer correct",
    )
    assert status == 200


def test_no_token_means_no_auth_required(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        json.dumps({"message": {"text": "x"}}).encode(),
        backend=backend,
        token=None,
        auth=None,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Body / message validation
# ---------------------------------------------------------------------------


def test_empty_body_returns_400(tmp_path):
    status, _ = _call(b"", backend=_backend(tmp_path))
    assert status == 400


def test_invalid_json_body_returns_400(tmp_path):
    status, _ = _call(b"not json", backend=_backend(tmp_path))
    assert status == 400


def test_deeply_nested_json_body_returns_sanitized_400(tmp_path):
    body = ("[" * 1200 + "0" + "]" * 1200).encode()
    status, resp = _call(body, backend=_backend(tmp_path))
    assert status == 400
    assert resp == {"error": "invalid JSON"}


def test_non_object_body_returns_400(tmp_path):
    status, _ = _call(json.dumps([1, 2]).encode(), backend=_backend(tmp_path))
    assert status == 400


def test_missing_message_returns_400(tmp_path):
    body = json.dumps({"context": {}}).encode()
    status, _ = _call(body, backend=_backend(tmp_path))
    assert status == 400


def test_non_dict_message_returns_400(tmp_path):
    body = json.dumps({"message": "not a dict"}).encode()
    status, _ = _call(body, backend=_backend(tmp_path))
    assert status == 400


# ---------------------------------------------------------------------------
# Backend exception -> sanitized 503
# ---------------------------------------------------------------------------


def test_backend_exception_returns_sanitized_503():
    class _BrokenBackend:
        def deliver(self, *args: Any) -> None:
            raise RuntimeError("db=/secret/db.sqlite token=sk-abc")

    status, resp = _call(
        json.dumps({"message": {"text": "x"}}).encode(),
        backend=_BrokenBackend(),
    )
    assert status == 503
    assert resp == {"error": "delivery backend unavailable"}
    assert "secret" not in str(resp)
    assert "sk-abc" not in str(resp)


# ---------------------------------------------------------------------------
# Method / path exactness
# ---------------------------------------------------------------------------


def test_handle_delivery_request_rejects_wrong_method(tmp_path):
    status, resp = _call(method="GET", backend=_backend(tmp_path))
    assert status == 405
    assert resp == {"error": "method not allowed"}


def test_handle_delivery_request_rejects_wrong_path(tmp_path):
    status, resp = _call(path="/delivery/deliver;alias", backend=_backend(tmp_path))
    assert status == 404
    assert resp == {"error": "not found"}


# ---------------------------------------------------------------------------
# is_delivery_route
# ---------------------------------------------------------------------------


def test_is_delivery_route_accepts_exact_path():
    assert is_delivery_route("/delivery/deliver") is True


def test_is_delivery_route_rejects_semicolon_alias():
    assert is_delivery_route("/delivery/deliver;x=y") is False


def test_is_delivery_route_rejects_prefix_lookalike():
    assert is_delivery_route("/delivery/deliverXYZ") is False


def test_is_delivery_route_rejects_encoded_slash():
    assert is_delivery_route("/delivery%2Fdeliver") is False


def test_is_delivery_route_rejects_extra_segments():
    assert is_delivery_route("/delivery/deliver/extra") is False


def test_is_delivery_route_rejects_delivery_root():
    assert is_delivery_route("/delivery") is False
