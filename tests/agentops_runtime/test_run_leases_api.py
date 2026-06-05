"""Tests for /run-leases/* control-plane API handler (M12B)."""

from __future__ import annotations

import json
import math
import time

import pytest


_LEASE_SCOPE_FIELDS = (
    "mode", "org_id", "workspace_id", "workspace_type", "project_id",
    "external_channel_id", "external_thread_id", "conversation_id",
    "user_id", "agent_profile_id", "run_type", "parent_session_id",
    "backend_profile", "delivery_ref",
)

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
}

_CTX_ORG2 = dict(_FULL_CTX, org_id="org2", workspace_id="ws2", user_id="bob")


@pytest.fixture
def backend(tmp_path):
    from agentops_runtime.run_leases_api import SQLiteRunLeaseBackend

    return SQLiteRunLeaseBackend(str(tmp_path / "leases.db"))


def _post(path, body, backend, *, token=None, auth_header=None):
    from agentops_runtime.run_leases_api import handle_run_lease_request

    return handle_run_lease_request(
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


def test_run_leases_api_can_be_imported():
    from agentops_runtime import run_leases_api

    assert hasattr(run_leases_api, "SQLiteRunLeaseBackend")
    assert hasattr(run_leases_api, "handle_run_lease_request")


# ---------------------------------------------------------------------------
# SQLiteRunLeaseBackend: claim
# ---------------------------------------------------------------------------


def test_claim_acquires_new_lease(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    claimed = backend.claim(scope, "run-001", owner="worker-a", now=time.time(), lease_seconds=60)
    assert claimed is True


def test_claim_returns_false_when_other_owner_holds_lease(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    claimed = backend.claim(scope, "run-001", owner="worker-b", now=now, lease_seconds=60)
    assert claimed is False


def test_claim_returns_true_for_same_owner_reclaim(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    claimed = backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    assert claimed is True


def test_claim_acquires_after_lease_expired(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    backend.claim(scope, "run-001", owner="worker-a", now=past, lease_seconds=10)
    claimed = backend.claim(scope, "run-001", owner="worker-b", now=time.time(), lease_seconds=60)
    assert claimed is True


def test_claim_isolation_across_scopes(backend):
    scope1 = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    scope2 = json.dumps([_CTX_ORG2.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope1, "run-001", owner="worker-a", now=now, lease_seconds=60)
    claimed = backend.claim(scope2, "run-001", owner="worker-b", now=now, lease_seconds=60)
    assert claimed is True


def test_claim_isolation_across_keys(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    claimed = backend.claim(scope, "run-002", owner="worker-b", now=now, lease_seconds=60)
    assert claimed is True


def test_concurrent_claim_allows_only_one_owner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agentops_runtime.run_leases_api import SQLiteRunLeaseBackend

    db_path = str(tmp_path / "concurrent-leases.db")
    SQLiteRunLeaseBackend(db_path)
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    barrier = Barrier(8)

    def claim_as(index: int) -> bool:
        candidate = SQLiteRunLeaseBackend(db_path)
        barrier.wait(timeout=5)
        return candidate.claim(scope, "run-001", owner=f"worker-{index}", now=100.0, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim_as, range(8)))

    assert results.count(True) == 1


# ---------------------------------------------------------------------------
# SQLiteRunLeaseBackend: renew
# ---------------------------------------------------------------------------


def test_renew_returns_true_for_current_owner(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    renewed = backend.renew(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    assert renewed is True


def test_renew_returns_false_when_no_lease(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    renewed = backend.renew(scope, "run-001", owner="worker-a", now=time.time(), lease_seconds=60)
    assert renewed is False


def test_renew_returns_false_for_different_owner(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    renewed = backend.renew(scope, "run-001", owner="worker-b", now=now, lease_seconds=60)
    assert renewed is False


def test_renew_returns_false_after_expired(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    backend.claim(scope, "run-001", owner="worker-a", now=past, lease_seconds=10)
    renewed = backend.renew(scope, "run-001", owner="worker-a", now=time.time(), lease_seconds=60)
    assert renewed is False


def test_claim_with_null_lease_seconds_is_indefinite(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    backend.claim(scope, "run-001", owner="worker-a", now=100.0, lease_seconds=None)

    assert backend.claim(scope, "run-001", owner="worker-b", now=10_000.0, lease_seconds=60) is False
    assert backend.expire_stale(scope, now=10_000.0) == 0


def test_renew_with_null_lease_seconds_preserves_current_expiry(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    backend.claim(scope, "run-001", owner="worker-a", now=100.0, lease_seconds=10)

    assert backend.renew(scope, "run-001", owner="worker-a", now=105.0, lease_seconds=None) is True
    assert backend.renew(scope, "run-001", owner="worker-a", now=111.0, lease_seconds=60) is False


# ---------------------------------------------------------------------------
# SQLiteRunLeaseBackend: release
# ---------------------------------------------------------------------------


def test_release_clears_lease(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    backend.release(scope, "run-001", owner="worker-a")
    renewed = backend.renew(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    assert renewed is False


def test_release_allows_new_owner_to_claim(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    backend.release(scope, "run-001", owner="worker-a")
    claimed = backend.claim(scope, "run-001", owner="worker-b", now=now, lease_seconds=60)
    assert claimed is True


# ---------------------------------------------------------------------------
# SQLiteRunLeaseBackend: cancel
# ---------------------------------------------------------------------------


def test_is_cancelled_returns_false_initially(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    assert backend.is_cancelled(scope, "run-001") is False


def test_request_cancel_and_is_cancelled(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    backend.request_cancel(scope, "run-001")
    assert backend.is_cancelled(scope, "run-001") is True


def test_clear_cancel_resets_flag(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    backend.request_cancel(scope, "run-001")
    backend.clear_cancel(scope, "run-001")
    assert backend.is_cancelled(scope, "run-001") is False


def test_cancel_flag_persists_after_release(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=60)
    backend.request_cancel(scope, "run-001")
    backend.release(scope, "run-001", owner="worker-a")
    assert backend.is_cancelled(scope, "run-001") is True


def test_cancel_flag_persists_after_expire_stale(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    backend.claim(scope, "run-001", owner="worker-a", now=past, lease_seconds=10)
    backend.request_cancel(scope, "run-001")
    backend.expire_stale(scope, now=time.time())
    assert backend.is_cancelled(scope, "run-001") is True


def test_cancel_isolation_across_scopes(backend):
    scope1 = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    scope2 = json.dumps([_CTX_ORG2.get(f) for f in _LEASE_SCOPE_FIELDS])
    backend.request_cancel(scope1, "run-001")
    assert backend.is_cancelled(scope2, "run-001") is False


# ---------------------------------------------------------------------------
# SQLiteRunLeaseBackend: expire_stale
# ---------------------------------------------------------------------------


def test_expire_stale_returns_zero_when_nothing_expired(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=now, lease_seconds=300)
    count = backend.expire_stale(scope, now=now)
    assert isinstance(count, int)
    assert not isinstance(count, bool)
    assert count == 0


def test_expire_stale_returns_count_of_expired_leases(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    backend.claim(scope, "run-001", owner="worker-a", now=past, lease_seconds=10)
    backend.claim(scope, "run-002", owner="worker-b", now=past, lease_seconds=10)
    count = backend.expire_stale(scope, now=time.time())
    assert count == 2


def test_expire_stale_is_scope_local(backend):
    scope1 = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    scope2 = json.dumps([_CTX_ORG2.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    backend.claim(scope1, "run-001", owner="worker-a", now=past, lease_seconds=10)
    backend.claim(scope2, "run-002", owner="worker-b", now=past, lease_seconds=10)
    count = backend.expire_stale(scope1, now=time.time())
    assert count == 1


def test_expire_stale_does_not_expire_active_leases(backend):
    scope = json.dumps([_FULL_CTX.get(f) for f in _LEASE_SCOPE_FIELDS])
    past = time.time() - 100
    now = time.time()
    backend.claim(scope, "run-001", owner="worker-a", now=past, lease_seconds=10)
    backend.claim(scope, "run-002", owner="worker-b", now=now, lease_seconds=300)
    count = backend.expire_stale(scope, now=now)
    assert count == 1


# ---------------------------------------------------------------------------
# handle_run_lease_request: routing happy paths
# ---------------------------------------------------------------------------


def test_claim_endpoint_returns_claimed_true(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "worker-a"},
        backend,
    )
    assert status == 200
    assert resp == {"claimed": True}


def test_claim_endpoint_returns_claimed_false_for_other_owner(backend):
    now = time.time()
    _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "worker-a", "now": now, "lease_seconds": 60},
        backend,
    )
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "worker-b", "now": now, "lease_seconds": 60},
        backend,
    )
    assert status == 200
    assert resp == {"claimed": False}


def test_renew_endpoint_returns_renewed_true(backend):
    now = time.time()
    _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": now, "lease_seconds": 60}, backend)
    status, resp = _post(
        "/run-leases/run-001/renew",
        {"context": _FULL_CTX, "owner": "w", "now": now, "lease_seconds": 60},
        backend,
    )
    assert status == 200
    assert resp == {"renewed": True}


def test_release_endpoint_returns_empty(backend):
    now = time.time()
    _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": now, "lease_seconds": 60}, backend)
    status, resp = _post(
        "/run-leases/run-001/release",
        {"context": _FULL_CTX, "owner": "w"},
        backend,
    )
    assert status == 200
    assert resp == {}


def test_request_cancel_endpoint_returns_empty(backend):
    status, resp = _post(
        "/run-leases/run-001/request-cancel",
        {"context": _FULL_CTX, "requester": "supervisor"},
        backend,
    )
    assert status == 200
    assert resp == {}


def test_is_cancelled_endpoint_returns_false_initially(backend):
    status, resp = _post(
        "/run-leases/run-001/is-cancelled",
        {"context": _FULL_CTX},
        backend,
    )
    assert status == 200
    assert resp == {"cancelled": False}


def test_is_cancelled_endpoint_returns_true_after_cancel(backend):
    _post("/run-leases/run-001/request-cancel", {"context": _FULL_CTX, "requester": "sup"}, backend)
    status, resp = _post(
        "/run-leases/run-001/is-cancelled",
        {"context": _FULL_CTX},
        backend,
    )
    assert status == 200
    assert resp == {"cancelled": True}


def test_clear_cancel_endpoint_returns_empty(backend):
    _post("/run-leases/run-001/request-cancel", {"context": _FULL_CTX, "requester": "sup"}, backend)
    status, resp = _post(
        "/run-leases/run-001/clear-cancel",
        {"context": _FULL_CTX},
        backend,
    )
    assert status == 200
    assert resp == {}


def test_expire_stale_endpoint_returns_expired_count(backend):
    status, resp = _post(
        "/run-leases/expire-stale",
        {"context": _FULL_CTX},
        backend,
    )
    assert status == 200
    assert isinstance(resp.get("expired"), int)
    assert not isinstance(resp.get("expired"), bool)


def test_expire_stale_endpoint_returns_real_count(backend):
    past = time.time() - 100
    _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": past, "lease_seconds": 10}, backend)
    status, resp = _post("/run-leases/expire-stale", {"context": _FULL_CTX, "now": time.time()}, backend)
    assert status == 200
    assert resp["expired"] == 1


# ---------------------------------------------------------------------------
# URL-encoded key handling
# ---------------------------------------------------------------------------


def test_url_encoded_key_is_decoded(backend):
    import urllib.parse
    key = "run/job-001"
    encoded = urllib.parse.quote(key, safe="")
    status, resp = _post(
        f"/run-leases/{encoded}/claim",
        {"context": _FULL_CTX, "owner": "w"},
        backend,
    )
    assert status == 200
    assert resp == {"claimed": True}


def test_expire_stale_key_used_as_run_key_not_literal(backend):
    status, resp = _post(
        "/run-leases/expire-stale/claim",
        {"context": _FULL_CTX, "owner": "w"},
        backend,
    )
    assert status == 200
    assert resp == {"claimed": True}


# ---------------------------------------------------------------------------
# Routing: 404 cases
# ---------------------------------------------------------------------------


def test_unknown_action_returns_404(backend):
    status, resp = _post("/run-leases/run-001/unknown", {"context": _FULL_CTX}, backend)
    assert status == 404


def test_missing_action_returns_404(backend):
    status, resp = _post("/run-leases/run-001", {"context": _FULL_CTX}, backend)
    assert status == 404


def test_encoded_slash_path_returns_404(backend):
    status, resp = _post("/run-leases%2Frun-001%2Fclaim", {"context": _FULL_CTX, "owner": "w"}, backend)
    assert status == 404


def test_prefix_lookalike_returns_404(backend):
    status, resp = _post("/run-leasesXYZ/run-001/claim", {"context": _FULL_CTX, "owner": "w"}, backend)
    assert status == 404


def test_semicolon_alias_returns_404(backend):
    status, resp = _post("/run-leases/run-001/claim;extra", {"context": _FULL_CTX, "owner": "w"}, backend)
    assert status == 404


def test_semicolon_query_alias_returns_404(backend):
    from agentops_runtime.run_leases_api import handle_run_lease_request

    status, resp = handle_run_lease_request(
        method="POST",
        path="/run-leases/run-001/claim;",
        query_string="x=1",
        body_bytes=json.dumps({"context": _FULL_CTX, "owner": "w"}).encode(),
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 404


def test_too_many_segments_returns_404(backend):
    status, resp = _post("/run-leases/run-001/claim/extra", {"context": _FULL_CTX, "owner": "w"}, backend)
    assert status == 404


# ---------------------------------------------------------------------------
# Auth checks (before body/backend)
# ---------------------------------------------------------------------------


def test_auth_required_before_body_for_claim(backend):
    body_bytes = json.dumps({"context": _FULL_CTX, "owner": "w"}).encode()
    from agentops_runtime.run_leases_api import handle_run_lease_request

    status, resp = handle_run_lease_request(
        method="POST",
        path="/run-leases/run-001/claim",
        query_string="",
        body_bytes=body_bytes,
        auth_header="Bearer wrong-token",
        token="correct-token",
        backend=backend,
    )
    assert status == 401
    assert resp.get("error") == "unauthorized"


def test_no_token_passes_auth(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w"},
        backend,
        token=None,
    )
    assert status == 200


def test_correct_token_passes_auth(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w"},
        backend,
        token="correct-token",
        auth_header="Bearer correct-token",
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Content-Length validation
# ---------------------------------------------------------------------------


def test_negative_content_length_returns_400():
    from agentops_runtime.run_leases_api import handle_run_lease_request, SQLiteRunLeaseBackend

    class _Sentinel:
        pass

    status, resp = handle_run_lease_request(
        method="POST",
        path="/run-leases/run-001/claim",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=_Sentinel(),
    )
    assert status == 400


def test_missing_body_returns_400(backend):
    from agentops_runtime.run_leases_api import handle_run_lease_request

    status, resp = handle_run_lease_request(
        method="POST",
        path="/run-leases/run-001/claim",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Numeric validation: now
# ---------------------------------------------------------------------------


def test_now_bool_rejected(backend):
    status, _ = _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": True}, backend)
    assert status == 400


def test_now_string_rejected(backend):
    status, _ = _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": "yesterday"}, backend)
    assert status == 400


def test_now_nan_rejected(backend):
    from agentops_runtime.run_leases_api import handle_run_lease_request

    body = b'{"context": {}, "owner": "w", "now": NaN}'
    status, _ = handle_run_lease_request(
        method="POST",
        path="/run-leases/run-001/claim",
        query_string="",
        body_bytes=body,
        auth_header=None,
        token=None,
        backend=backend,
    )
    assert status == 400


def test_now_none_accepted(backend):
    status, resp = _post("/run-leases/run-001/claim", {"context": _FULL_CTX, "owner": "w", "now": None}, backend)
    assert status == 200


def test_now_finite_float_accepted(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "now": time.time()},
        backend,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Numeric validation: lease_seconds
# ---------------------------------------------------------------------------


def test_lease_seconds_bool_rejected(backend):
    status, _ = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": True},
        backend,
    )
    assert status == 400


def test_lease_seconds_zero_rejected(backend):
    status, _ = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": 0},
        backend,
    )
    assert status == 400


def test_lease_seconds_negative_rejected(backend):
    status, _ = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": -1},
        backend,
    )
    assert status == 400


def test_lease_seconds_string_rejected(backend):
    status, _ = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": "sixty"},
        backend,
    )
    assert status == 400


def test_lease_seconds_none_accepted(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": None},
        backend,
    )
    assert status == 200


def test_lease_seconds_positive_int_accepted(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": 300},
        backend,
    )
    assert status == 200


def test_lease_seconds_positive_float_accepted(backend):
    status, resp = _post(
        "/run-leases/run-001/claim",
        {"context": _FULL_CTX, "owner": "w", "lease_seconds": 30.5},
        backend,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Context validation
# ---------------------------------------------------------------------------


def test_empty_context_accepted(backend):
    status, resp = _post("/run-leases/run-001/claim", {"context": {}, "owner": "w"}, backend)
    assert status == 200


def test_null_context_accepted(backend):
    status, resp = _post("/run-leases/run-001/claim", {"context": None, "owner": "w"}, backend)
    assert status == 200


def test_list_context_rejected(backend):
    status, _ = _post("/run-leases/run-001/claim", {"context": ["x"], "owner": "w"}, backend)
    assert status == 400


def test_context_with_wrong_type_field_rejected(backend):
    ctx = dict(_FULL_CTX, mode=123)
    status, _ = _post("/run-leases/run-001/claim", {"context": ctx, "owner": "w"}, backend)
    assert status == 400


def test_run_id_excluded_from_scope(backend):
    ctx_with_run_id = dict(_FULL_CTX, run_id="run-sentinel", job_id="job-sentinel")
    status, resp = _post("/run-leases/run-001/claim", {"context": ctx_with_run_id, "owner": "w"}, backend)
    assert status == 200
    status2, resp2 = _post("/run-leases/run-001/is-cancelled", {"context": ctx_with_run_id}, backend)
    assert resp2["cancelled"] is False


def test_null_context_and_empty_context_share_same_scope(backend):
    now = time.time()
    _post("/run-leases/run-001/claim", {"context": None, "owner": "w", "now": now, "lease_seconds": 60}, backend)
    status, resp = _post("/run-leases/run-001/claim", {"context": {}, "owner": "other", "now": now, "lease_seconds": 60}, backend)
    assert resp["claimed"] is False


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


def test_claim_missing_owner_returns_400(backend):
    status, _ = _post("/run-leases/run-001/claim", {"context": _FULL_CTX}, backend)
    assert status == 400


def test_request_cancel_missing_requester_returns_400(backend):
    status, _ = _post("/run-leases/run-001/request-cancel", {"context": _FULL_CTX}, backend)
    assert status == 400


def test_release_missing_owner_returns_400(backend):
    status, _ = _post("/run-leases/run-001/release", {"context": _FULL_CTX}, backend)
    assert status == 400


# ---------------------------------------------------------------------------
# expire-stale: context required
# ---------------------------------------------------------------------------


def test_expire_stale_with_default_context_works(backend):
    status, resp = _post("/run-leases/expire-stale", {"context": {}}, backend)
    assert status == 200
    assert isinstance(resp.get("expired"), int)


def test_expire_stale_with_invalid_context_rejected(backend):
    status, _ = _post("/run-leases/expire-stale", {"context": "bad"}, backend)
    assert status == 400
