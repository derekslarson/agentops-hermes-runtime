"""Tests for curated memory API handler (M12B).

Contract mirrors agent/runtime_memory_http.py (HttpMemoryBackend):
  GET /memory?target=<target>&context=<json> -> {"content": str | null}
  POST /memory with body {context, target, content, action} -> {}

Scope key uses the 8 fixed client fields from HttpMemoryBackend._scope_payload:
  mode, org_id, workspace_id, project_id, external_channel_id,
  external_thread_id, conversation_id, user_id — preserving None.
Empty context {} is accepted for both GET and POST.
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from agentops_runtime.curated_memory_api import SQLiteCuratedMemoryStore, handle_curated_memory_request


_SCOPE = {
    "mode": "agentops",
    "org_id": "acme",
    "workspace_id": "slack-ws",
    "project_id": "runtime-mvp",
    "external_channel_id": "C123",
    "external_thread_id": "T456",
    "conversation_id": "conv-1",
    "user_id": "derek",
}


def _get(backend, context=None, target="memory", token=None, auth_header=None):
    ctx = context if context is not None else _SCOPE
    qs = urllib.parse.urlencode({"target": target, "context": json.dumps(ctx)})
    return handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string=qs,
        body_bytes=b"",
        auth_header=auth_header,
        token=token,
        backend=backend,
    )


def _post(backend, content, context=None, target="memory", action="add", token=None, auth_header=None):
    ctx = context if context is not None else _SCOPE
    body = json.dumps({"context": ctx, "target": target, "content": content, "action": action})
    return handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode("utf-8"),
        auth_header=auth_header,
        token=token,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# SQLiteCuratedMemoryStore — unit tests
# ---------------------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    store.write("scope1", "memory", "hello world")
    assert store.read("scope1", "memory") == "hello world"


def test_store_missing_returns_none(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    assert store.read("scope1", "memory") is None


def test_store_overwrite_replaces_content(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    store.write("scope1", "memory", "first")
    store.write("scope1", "memory", "second")
    assert store.read("scope1", "memory") == "second"


def test_store_target_isolation(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    store.write("scope1", "notes", "note content")
    assert store.read("scope1", "memory") is None
    assert store.read("scope1", "notes") == "note content"


def test_store_scope_isolation(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    store.write("scope-a", "memory", "data for a")
    store.write("scope-b", "memory", "data for b")
    assert store.read("scope-a", "memory") == "data for a"
    assert store.read("scope-b", "memory") == "data for b"


def test_store_durable_second_instance(tmp_path):
    db_path = str(tmp_path / "mem.db")
    SQLiteCuratedMemoryStore(db_path).write("scope1", "memory", "persisted content")
    assert SQLiteCuratedMemoryStore(db_path).read("scope1", "memory") == "persisted content"


# ---------------------------------------------------------------------------
# GET — basic contract
# ---------------------------------------------------------------------------


def test_get_returns_null_when_no_content(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = _get(store)
    assert status == 200
    assert response == {"content": None}


def test_get_returns_content_after_post(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "test content")
    status, response = _get(store)
    assert status == 200
    assert response == {"content": "test content"}


def test_get_empty_context_accepted(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    qs = urllib.parse.urlencode({"target": "memory", "context": "{}"})
    status, response = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string=qs,
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 200
    assert response == {"content": None}


def test_get_default_target_is_memory(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "default target content")
    qs = urllib.parse.urlencode({"context": json.dumps(_SCOPE)})
    status, response = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string=qs,
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 200
    assert response == {"content": "default target content"}


def test_get_default_context_is_empty_dict(tmp_path):
    """GET without context param defaults to {} scope."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": {}, "target": "memory", "content": "empty ctx data", "action": "add"})
    handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode("utf-8"),
        auth_header=None,
        token=None,
        backend=store,
    )
    status, response = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string="target=memory",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 200
    assert response == {"content": "empty ctx data"}


# ---------------------------------------------------------------------------
# POST — basic contract
# ---------------------------------------------------------------------------


def test_post_stores_content_returns_empty_dict(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = _post(store, "hello")
    assert status == 200
    assert response == {}


def test_post_overwrite_replaces_content(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "first write")
    _post(store, "second write")
    _, response = _get(store)
    assert response == {"content": "second write"}


def test_post_empty_context_accepted(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": {}, "target": "memory", "content": "empty ctx data", "action": "add"})
    status, response = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode("utf-8"),
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 200
    assert response == {}


def test_post_default_target_is_memory(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": _SCOPE, "content": "no target field"})
    handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode("utf-8"),
        auth_header=None,
        token=None,
        backend=store,
    )
    _, response = _get(store, target="memory")
    assert response == {"content": "no target field"}


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


def test_cross_scope_isolation(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    ctx_derek = dict(_SCOPE, user_id="derek")
    ctx_alex = dict(_SCOPE, user_id="alex")
    _post(store, "derek content", context=ctx_derek)
    _post(store, "alex content", context=ctx_alex)
    _, resp_derek = _get(store, context=ctx_derek)
    _, resp_alex = _get(store, context=ctx_alex)
    assert resp_derek == {"content": "derek content"}
    assert resp_alex == {"content": "alex content"}


def test_target_isolation_via_handler(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "memory content", target="memory")
    _post(store, "notes content", target="notes")
    _, resp_memory = _get(store, target="memory")
    _, resp_notes = _get(store, target="notes")
    assert resp_memory == {"content": "memory content"}
    assert resp_notes == {"content": "notes content"}


def test_nullable_field_separation(tmp_path):
    """Two contexts that differ only in a nullable field are distinct scopes."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    ctx_with_channel = dict(_SCOPE, external_channel_id="C123")
    ctx_without_channel = dict(_SCOPE, external_channel_id=None)
    _post(store, "with channel", context=ctx_with_channel)
    _post(store, "without channel", context=ctx_without_channel)
    _, resp_with = _get(store, context=ctx_with_channel)
    _, resp_without = _get(store, context=ctx_without_channel)
    assert resp_with == {"content": "with channel"}
    assert resp_without == {"content": "without channel"}


def test_empty_context_is_isolated_from_full_context(tmp_path):
    """Empty {} context and a full-scope context are distinct scopes."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "full scope content", context=_SCOPE)
    body = json.dumps({"context": {}, "target": "memory", "content": "empty scope content", "action": "add"})
    handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode("utf-8"),
        auth_header=None,
        token=None,
        backend=store,
    )
    _, resp_full = _get(store, context=_SCOPE)
    qs = urllib.parse.urlencode({"target": "memory", "context": "{}"})
    _, resp_empty = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string=qs,
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert resp_full == {"content": "full scope content"}
    assert resp_empty == {"content": "empty scope content"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_missing_returns_401(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = _get(store, token="secret", auth_header=None)
    assert status == 401
    assert "error" in response


def test_auth_wrong_token_returns_401(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = _get(store, token="secret", auth_header="Bearer wrong")
    assert status == 401
    assert "error" in response


def test_auth_correct_token_allows_get(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = _get(store, token="secret", auth_header="Bearer secret")
    assert status == 200


def test_auth_correct_token_allows_post(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = _post(store, "content", token="secret", auth_header="Bearer secret")
    assert status == 200


def test_auth_no_token_allows_all(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = _get(store, token=None)
    assert status == 200
    status, _ = _post(store, "content", token=None)
    assert status == 200


def test_auth_checked_before_post_body_parse(tmp_path):
    """Auth must return 401 even when the body is invalid JSON."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=b"not json {",
        auth_header="Bearer wrong",
        token="secret",
        backend=store,
    )
    assert status == 401


def test_auth_token_value_not_in_error_response(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _, response = _get(store, token="SENTINEL_TOKEN_VALUE", auth_header=None)
    assert "SENTINEL_TOKEN_VALUE" not in json.dumps(response)


# ---------------------------------------------------------------------------
# Malformed requests
# ---------------------------------------------------------------------------


def test_post_malformed_json_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=b"not-json{",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400
    assert "error" in response


def test_post_non_object_body_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=json.dumps(["list"]).encode(),
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400


def test_post_missing_content_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": _SCOPE, "target": "memory"})
    status, response = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode(),
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400
    assert "error" in response


def test_post_non_string_content_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": _SCOPE, "target": "memory", "content": 42})
    status, _ = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode(),
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400


def test_post_non_object_context_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    body = json.dumps({"context": "string-context", "content": "data"})
    status, _ = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=body.encode(),
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400


def test_get_malformed_context_json_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, response = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string="target=memory&context=not-valid-json",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400
    assert "error" in response


def test_get_non_object_context_returns_400(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    qs = urllib.parse.urlencode({"target": "memory", "context": json.dumps(["list"])})
    status, _ = handle_curated_memory_request(
        method="GET",
        path="/memory",
        query_string=qs,
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Wrong path / method
# ---------------------------------------------------------------------------


def test_wrong_path_returns_404(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = handle_curated_memory_request(
        method="GET",
        path="/memory/other",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 404


def test_memory_records_path_returns_404(tmp_path):
    """Handler must not intercept /memory/records paths."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = handle_curated_memory_request(
        method="GET",
        path="/memory/records",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 404


def test_wrong_method_returns_405(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    status, _ = handle_curated_memory_request(
        method="DELETE",
        path="/memory",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 405


# ---------------------------------------------------------------------------
# Backend exception sanitized
# ---------------------------------------------------------------------------


class _FailingStore:
    def read(self, scope: str, target: str) -> str | None:
        raise RuntimeError("db path=/var/lib/secret.db SENTINEL_BACKEND_DETAIL")

    def write(self, scope: str, target: str, content: str) -> None:
        raise RuntimeError("db path=/var/lib/secret.db SENTINEL_BACKEND_DETAIL")


def test_backend_exception_returns_503_on_get():
    status, response = _get(_FailingStore())
    assert status == 503
    assert "error" in response


def test_backend_exception_returns_503_on_post():
    status, response = _post(_FailingStore(), "content")
    assert status == 503
    assert "error" in response


def test_backend_exception_does_not_leak_details_in_get():
    _, response = _get(_FailingStore())
    serialized = json.dumps(response)
    assert "SENTINEL_BACKEND_DETAIL" not in serialized
    assert "/var/lib/secret.db" not in serialized


def test_backend_exception_does_not_leak_details_in_post():
    _, response = _post(_FailingStore(), "content")
    serialized = json.dumps(response)
    assert "SENTINEL_BACKEND_DETAIL" not in serialized
    assert "/var/lib/secret.db" not in serialized


# ---------------------------------------------------------------------------
# Content non-leakage
# ---------------------------------------------------------------------------


def test_error_response_does_not_contain_stored_content(tmp_path):
    """Error responses must never include memory content."""
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "SECRET_CONTENT_SENTINEL")
    status, response = handle_curated_memory_request(
        method="POST",
        path="/memory",
        query_string="",
        body_bytes=b"bad json",
        auth_header=None,
        token=None,
        backend=store,
    )
    assert status == 400
    assert "SECRET_CONTENT_SENTINEL" not in json.dumps(response)


def test_401_response_does_not_contain_content(tmp_path):
    store = SQLiteCuratedMemoryStore(str(tmp_path / "mem.db"))
    _post(store, "SECRET_CONTENT_SENTINEL", token=None)
    status, response = _get(store, token="secret", auth_header=None)
    assert status == 401
    assert "SECRET_CONTENT_SENTINEL" not in json.dumps(response)
