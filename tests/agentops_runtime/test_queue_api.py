"""Tests for /queue/* control-plane API handler (M12B)."""

from __future__ import annotations

import json

import pytest


_FULL_CTX = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "workspace_type": "team",
    "project_id": "proj1",
    "external_channel_id": "ch1",
    "external_thread_id": "th1",
    "conversation_id": "conv1",
    "user_id": "alice",
    "agent_profile_id": "bot",
    "run_type": "conversation",
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
    "delivery_ref": None,
    "run_id": "run1",
    "job_id": None,
}

_CTX_ORG2 = {
    "mode": "agentops",
    "org_id": "org2",
    "workspace_id": "ws2",
    "workspace_type": "team",
    "project_id": "proj2",
    "external_channel_id": None,
    "external_thread_id": None,
    "conversation_id": "conv2",
    "user_id": "bob",
    "agent_profile_id": "bot2",
    "run_type": "conversation",
    "parent_session_id": None,
    "backend_profile": "compose-self-hosted",
    "delivery_ref": None,
    "run_id": "run2",
    "job_id": None,
}


@pytest.fixture
def queue_backend(tmp_path):
    from agentops_runtime.queue_api import SQLiteQueueBackend

    return SQLiteQueueBackend(str(tmp_path / "queue.db"))


def _post(path, body, backend, *, token=None, auth_header=None):
    from agentops_runtime.queue_api import handle_queue_request

    return handle_queue_request(
        method="POST",
        path=path,
        query_string="",
        body_bytes=json.dumps(body).encode(),
        auth_header=auth_header,
        token=token,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_queue_api_can_be_imported():
    from agentops_runtime import queue_api

    assert hasattr(queue_api, "SQLiteQueueBackend")
    assert hasattr(queue_api, "handle_queue_request")


# ---------------------------------------------------------------------------
# Enqueue happy path
# ---------------------------------------------------------------------------


def test_enqueue_returns_receipt(queue_backend):
    status, resp = _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"task": "run"}}, queue_backend)
    assert status == 200
    assert isinstance(resp.get("receipt"), str)
    assert resp["receipt"]


def test_enqueue_receipt_is_unique(queue_backend):
    _, r1 = _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"n": 1}}, queue_backend)
    _, r2 = _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"n": 2}}, queue_backend)
    assert r1["receipt"] != r2["receipt"]


# ---------------------------------------------------------------------------
# Claim happy path
# ---------------------------------------------------------------------------


def test_claim_returns_item_after_enqueue(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"task": "x"}}, queue_backend)
    status, resp = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert status == 200
    item = resp.get("item")
    assert isinstance(item, dict)
    assert "receipt" in item
    assert item.get("payload") == {"task": "x"}


def test_claim_returns_null_when_empty(queue_backend):
    status, resp = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert status == 200
    assert resp.get("item") is None


# ---------------------------------------------------------------------------
# Payload preservation
# ---------------------------------------------------------------------------


def test_payload_preserved_verbatim_including_secret_keys(queue_backend):
    secret_payload = {
        "password": "hunter2",
        "token": "sk-abc123",
        "secret_key": "MY_SECRET",
        "nested": {"api_key": "deep-secret"},
        "data": [1, 2, 3],
    }
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": secret_payload}, queue_backend)
    _, resp = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert resp["item"]["payload"] == secret_payload


# ---------------------------------------------------------------------------
# Context parsing
# ---------------------------------------------------------------------------


def test_null_context_enqueue_returns_400(queue_backend):
    status, resp = _post("/queue/enqueue", {"context": {}, "payload": {"x": 1}}, queue_backend)
    assert status == 400
    assert "error" in resp


def test_null_context_claim_returns_item_null(queue_backend):
    status, resp = _post("/queue/claim", {"context": {}}, queue_backend)
    assert status == 200
    assert resp.get("item") is None


def test_full_context_roundtrip(queue_backend):
    payload = {"job": "full-context-test"}
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": payload}, queue_backend)
    status, resp = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert status == 200
    assert resp["item"]["payload"] == payload


def test_non_object_context_rejected(queue_backend):
    status, resp = _post("/queue/enqueue", {"context": "bad-string", "payload": {"x": 1}}, queue_backend)
    assert status == 400
    assert "error" in resp


def test_list_context_rejected(queue_backend):
    status, resp = _post("/queue/enqueue", {"context": ["a", "b"], "payload": {"x": 1}}, queue_backend)
    assert status == 400


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


def test_backend_error_sanitized(queue_backend):
    class _BoomBackend:
        def enqueue(self, scope, payload, *, idempotency_key=None):
            raise RuntimeError("LEAKSENTINEL receipt=abc123 payload=secret token=xyz db=/var/secret/db")

    status, resp = _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, _BoomBackend())
    assert status == 503
    resp_text = json.dumps(resp)
    assert "LEAKSENTINEL" not in resp_text
    assert "receipt=abc123" not in resp_text
    assert "payload=secret" not in resp_text
    assert "token=xyz" not in resp_text
    assert "/var/secret/db" not in resp_text


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_fails_before_backend_access():
    class _SpyBackend:
        called = False

        def enqueue(self, scope, payload, *, idempotency_key=None):
            _SpyBackend.called = True
            return "receipt-x"

    _SpyBackend.called = False
    status, resp = _post(
        "/queue/enqueue",
        {"context": _FULL_CTX, "payload": {"x": 1}},
        _SpyBackend(),
        token="correct-token",
        auth_header="Bearer wrong-token",
    )
    assert status == 401
    assert _SpyBackend.called is False


def test_auth_passes_with_correct_token(queue_backend):
    status, resp = _post(
        "/queue/enqueue",
        {"context": _FULL_CTX, "payload": {"x": 1}},
        queue_backend,
        token="mytoken",
        auth_header="Bearer mytoken",
    )
    assert status == 200


def test_no_token_configured_allows_any_header(queue_backend):
    status, resp = _post(
        "/queue/enqueue",
        {"context": _FULL_CTX, "payload": {"x": 1}},
        queue_backend,
        token=None,
        auth_header="Bearer anything",
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Query-string receipt safety
# ---------------------------------------------------------------------------


def test_query_string_ignored_and_not_leaked():
    from agentops_runtime.queue_api import handle_queue_request
    from agentops_runtime.queue_api import SQLiteQueueBackend
    import tempfile, os

    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteQueueBackend(os.path.join(d, "q.db"))
        status, resp = handle_queue_request(
            method="POST",
            path="/queue/enqueue",
            query_string="receipt=LEAKSENTINEL&token=LEAKTOKEN",
            body_bytes=json.dumps({"context": _FULL_CTX, "payload": {"x": 1}}).encode(),
            auth_header=None,
            token=None,
            backend=backend,
        )
    assert status == 200
    resp_text = json.dumps(resp)
    assert "LEAKSENTINEL" not in resp_text
    assert "LEAKTOKEN" not in resp_text


# ---------------------------------------------------------------------------
# SQLite persistence across instances
# ---------------------------------------------------------------------------


def test_sqlite_persistence_across_instances(tmp_path):
    from agentops_runtime.queue_api import SQLiteQueueBackend

    db_path = str(tmp_path / "persist.db")
    backend_a = SQLiteQueueBackend(db_path)
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"key": "persisted"}}, backend_a)

    backend_b = SQLiteQueueBackend(db_path)
    status, resp = _post("/queue/claim", {"context": _FULL_CTX}, backend_b)
    assert status == 200
    assert resp["item"]["payload"] == {"key": "persisted"}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_duplicate_returns_same_receipt(queue_backend):
    body = {"context": _FULL_CTX, "payload": {"x": 1}, "idempotency_key": "idem-key-1"}
    _, r1 = _post("/queue/enqueue", body, queue_backend)
    _, r2 = _post("/queue/enqueue", body, queue_backend)
    assert r1["receipt"] == r2["receipt"]


def test_different_idempotency_keys_create_separate_items(queue_backend):
    _, r1 = _post(
        "/queue/enqueue",
        {"context": _FULL_CTX, "payload": {"x": 1}, "idempotency_key": "key-a"},
        queue_backend,
    )
    _, r2 = _post(
        "/queue/enqueue",
        {"context": _FULL_CTX, "payload": {"x": 2}, "idempotency_key": "key-b"},
        queue_backend,
    )
    assert r1["receipt"] != r2["receipt"]


def test_concurrent_duplicate_idempotency_key_returns_original_receipt(tmp_path, monkeypatch):
    import threading

    from agentops_runtime import queue_api

    backend = queue_api.SQLiteQueueBackend(str(tmp_path / "idem-race.db"))
    real_connect = queue_api.sqlite3.connect
    barrier = threading.Barrier(2)

    class _Cursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            if row is None:
                barrier.wait(timeout=5)
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _Connection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            cursor = self._conn.execute(sql, *args, **kwargs)
            if sql.startswith("SELECT receipt FROM queue_items WHERE context_scope=? AND idempotency_key=?"):
                return _Cursor(cursor)
            return cursor

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(queue_api.sqlite3, "connect", lambda *args, **kwargs: _Connection(real_connect(*args, **kwargs)))

    results = []

    def _enqueue():
        status, resp = _post(
            "/queue/enqueue",
            {"context": _FULL_CTX, "payload": {"x": 1}, "idempotency_key": "race-key"},
            backend,
        )
        results.append((status, resp))

    threads = [threading.Thread(target=_enqueue), threading.Thread(target=_enqueue)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert [status for status, _ in results] == [200, 200]
    assert results[0][1]["receipt"] == results[1][1]["receipt"]


# ---------------------------------------------------------------------------
# Cross-context isolation
# ---------------------------------------------------------------------------


def test_cross_context_isolation(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"owner": "org1"}}, queue_backend)
    status, resp = _post("/queue/claim", {"context": _CTX_ORG2}, queue_backend)
    assert status == 200
    assert resp.get("item") is None


def test_same_workspace_different_user_isolated(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"owner": "alice"}}, queue_backend)
    same_workspace_other_user = dict(_FULL_CTX)
    same_workspace_other_user.update({"user_id": "bob", "conversation_id": "conv-bob", "run_id": "run-bob"})
    status, resp = _post("/queue/claim", {"context": same_workspace_other_user}, queue_backend)
    assert status == 200
    assert resp.get("item") is None


def test_context_rejects_non_string_scalar_field(queue_backend):
    invalid_context = dict(_FULL_CTX)
    invalid_context["user_id"] = {"nested": "not-allowed"}
    status, resp = _post("/queue/enqueue", {"context": invalid_context, "payload": {"x": 1}}, queue_backend)
    assert status == 400
    assert resp == {"error": "invalid context"}


# ---------------------------------------------------------------------------
# Ack/nack/extend
# ---------------------------------------------------------------------------


def test_ack_after_claim_prevents_reclaim(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    _post("/queue/ack", {"context": _FULL_CTX, "receipt": receipt}, queue_backend)
    _, after = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert after.get("item") is None


def test_concurrent_claim_does_not_return_same_receipt_twice(tmp_path, monkeypatch):
    import threading

    from agentops_runtime import queue_api

    backend = queue_api.SQLiteQueueBackend(str(tmp_path / "claim-race.db"))
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, backend)
    real_connect = queue_api.sqlite3.connect
    barrier = threading.Barrier(2)

    class _Cursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            if row is not None:
                barrier.wait(timeout=5)
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _Connection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            cursor = self._conn.execute(sql, *args, **kwargs)
            if sql.startswith("SELECT receipt, payload FROM queue_items"):
                return _Cursor(cursor)
            return cursor

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(queue_api.sqlite3, "connect", lambda *args, **kwargs: _Connection(real_connect(*args, **kwargs)))

    claims = []
    scope, err = queue_api._parse_context_scope(_FULL_CTX)
    assert err is None
    assert scope is not None

    def _claim():
        claims.append(backend.claim(scope))

    threads = [threading.Thread(target=_claim), threading.Thread(target=_claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(claims) == 2
    assert sum(claim is not None for claim in claims) == 1


def test_nack_requeue_true_makes_item_reclaimable(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    _post("/queue/nack", {"context": _FULL_CTX, "receipt": receipt, "requeue": True}, queue_backend)
    _, after = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert after.get("item") is not None


def test_nack_requeue_false_discards_item(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    _post("/queue/nack", {"context": _FULL_CTX, "receipt": receipt, "requeue": False}, queue_backend)
    _, after = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    assert after.get("item") is None


def test_extend_lease_returns_200(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {"x": 1}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post(
        "/queue/extend-lease", {"context": _FULL_CTX, "receipt": receipt, "seconds": 60}, queue_backend
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Invalid lease seconds
# ---------------------------------------------------------------------------


def test_extend_lease_rejects_zero_seconds(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post(
        "/queue/extend-lease", {"context": _FULL_CTX, "receipt": receipt, "seconds": 0}, queue_backend
    )
    assert status == 400


def test_extend_lease_rejects_negative_seconds(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post(
        "/queue/extend-lease", {"context": _FULL_CTX, "receipt": receipt, "seconds": -5}, queue_backend
    )
    assert status == 400


def test_extend_lease_rejects_string_seconds(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post(
        "/queue/extend-lease", {"context": _FULL_CTX, "receipt": receipt, "seconds": "abc"}, queue_backend
    )
    assert status == 400


def test_extend_lease_rejects_float_seconds(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post(
        "/queue/extend-lease", {"context": _FULL_CTX, "receipt": receipt, "seconds": 3.5}, queue_backend
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Validation: missing/wrong fields
# ---------------------------------------------------------------------------


def test_missing_payload_on_enqueue_returns_400(queue_backend):
    status, _ = _post("/queue/enqueue", {"context": _FULL_CTX}, queue_backend)
    assert status == 400


def test_payload_must_be_dict(queue_backend):
    status, _ = _post("/queue/enqueue", {"context": _FULL_CTX, "payload": "not-a-dict"}, queue_backend)
    assert status == 400


def test_missing_receipt_on_ack_returns_400(queue_backend):
    status, _ = _post("/queue/ack", {"context": _FULL_CTX}, queue_backend)
    assert status == 400


def test_missing_requeue_on_nack_returns_400(queue_backend):
    _post("/queue/enqueue", {"context": _FULL_CTX, "payload": {}}, queue_backend)
    _, claimed = _post("/queue/claim", {"context": _FULL_CTX}, queue_backend)
    receipt = claimed["item"]["receipt"]
    status, _ = _post("/queue/nack", {"context": _FULL_CTX, "receipt": receipt}, queue_backend)
    assert status == 400


def test_empty_body_returns_400(queue_backend):
    from agentops_runtime.queue_api import handle_queue_request

    status, _ = handle_queue_request(
        method="POST",
        path="/queue/enqueue",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=queue_backend,
    )
    assert status == 400


def test_non_json_body_returns_400(queue_backend):
    from agentops_runtime.queue_api import handle_queue_request

    status, _ = handle_queue_request(
        method="POST",
        path="/queue/enqueue",
        query_string="",
        body_bytes=b"not-json",
        auth_header=None,
        token=None,
        backend=queue_backend,
    )
    assert status == 400


def test_json_array_body_returns_400(queue_backend):
    from agentops_runtime.queue_api import handle_queue_request

    status, _ = handle_queue_request(
        method="POST",
        path="/queue/enqueue",
        query_string="",
        body_bytes=b'["a","b"]',
        auth_header=None,
        token=None,
        backend=queue_backend,
    )
    assert status == 400
