"""Tests for /cron/jobs control-plane API handler (M12B).

Covers: create/list/get/update/delete, pause/resume/history, claim-due,
renew/release lease, complete/fail, scope isolation, and sanitized errors.
"""

from __future__ import annotations

import json

import pytest

from agent.runtime_cron_sqlite import SQLiteCronBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _backend(tmp_path, name="cron.db"):
    return SQLiteCronBackend(str(tmp_path / name))


def _ctx_dict(**overrides):
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="ws1",
        user_id="alice",
        conversation_id="conv1",
        agent_profile_id="bot",
        project_id="proj1",
        run_type="cron",
        backend_profile="compose-self-hosted",
    )
    base.update(overrides)
    return base


def _call(method, path, body=None, *, backend, context_header=None, token=None, auth_header=None):
    from agentops_runtime.cron_api import handle_cron_request

    body_bytes = json.dumps(body).encode() if body is not None else b""
    return handle_cron_request(
        method=method,
        path=path,
        query_string="",
        body_bytes=body_bytes,
        auth_header=auth_header,
        token=token,
        backend=backend,
        context_header=context_header,
    )


# ---------------------------------------------------------------------------
# Route parsing: exact matching, semicolons, prefix lookalikes
# ---------------------------------------------------------------------------


def test_cron_route_parser_accepts_valid_routes():
    from agentops_runtime.cron_api import _parse_cron_route

    assert _parse_cron_route("/cron/jobs")[2] is None
    assert _parse_cron_route("/cron/jobs/abc")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/pause")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/resume")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/history")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/renew-lease")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/release-lease")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/complete")[2] is None
    assert _parse_cron_route("/cron/jobs/abc/fail")[2] is None
    assert _parse_cron_route("/cron/jobs/claim-due")[2] is None


def test_cron_route_parser_rejects_semicolon_aliases():
    from agentops_runtime.cron_api import _parse_cron_route

    assert _parse_cron_route("/cron/jobs;scope=leak")[2] is not None
    assert _parse_cron_route("/cron/jobs/abc;jsessionid=x")[2] is not None
    assert _parse_cron_route("/cron/jobs/abc/pause;leak")[2] is not None


def test_cron_route_parser_rejects_unknown_actions():
    from agentops_runtime.cron_api import _parse_cron_route

    assert _parse_cron_route("/cron/jobs/abc/bogus")[2] is not None
    assert _parse_cron_route("/cron/jobs/abc/pause/extra")[2] is not None


def test_cron_route_parser_rejects_encoded_path_separators():
    from agentops_runtime.cron_api import _parse_cron_route, is_cron_route

    for raw_path in (
        "/cron/jobs/foo%2Fbar",
        "/cron/jobs/foo%2Fbar/pause",
        "/cron/jobs/claim-due%2Ftail",
        "/cron/jobs/foo%5Cbar",
    ):
        assert _parse_cron_route(raw_path)[2] is not None
        assert is_cron_route(raw_path) is False


def test_is_cron_route_returns_true_for_valid_paths():
    from agentops_runtime.cron_api import is_cron_route

    assert is_cron_route("/cron/jobs") is True
    assert is_cron_route("/cron/jobs/abc123") is True
    assert is_cron_route("/cron/jobs/claim-due") is True
    assert is_cron_route("/cron/jobs/abc/renew-lease") is True


def test_is_cron_route_returns_false_for_invalid_paths():
    from agentops_runtime.cron_api import is_cron_route

    assert is_cron_route("/cron/jobsXYZ") is False
    assert is_cron_route("/cron/jobs;anything") is False
    assert is_cron_route("/cronXYZ") is False
    assert is_cron_route("/run-leases/x") is False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_handle_cron_request_returns_401_for_bad_token(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        "GET", "/cron/jobs",
        backend=backend,
        token="good-token",
        auth_header="Bearer bad-token",
    )
    assert status == 401
    assert resp == {"error": "unauthorized"}


def test_handle_cron_request_allows_no_token(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        "GET", "/cron/jobs",
        backend=backend,
        context_header=json.dumps(_ctx_dict()),
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_job_returns_id(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        "POST", "/cron/jobs",
        {"context": _ctx_dict(), "job": {"prompt": "hello", "schedule": "5m"}},
        backend=backend,
    )
    assert status == 200
    assert "id" in resp
    assert isinstance(resp["id"], str) and resp["id"]


def test_create_job_missing_job_field_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call("POST", "/cron/jobs", {"context": _ctx_dict()}, backend=backend)
    assert status == 400


def test_create_job_rejects_invalid_next_run_at_without_leaking(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call(
        "POST", "/cron/jobs",
        {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": "LEAKSENTINEL-not-a-number"}},
        backend=backend,
    )
    assert status == 400
    assert "LEAKSENTINEL" not in json.dumps(resp)


def test_create_job_rejects_bool_next_run_at(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs",
        {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": True}},
        backend=backend,
    )
    assert status == 400


def test_create_job_empty_body_returns_400(tmp_path):
    backend = _backend(tmp_path)
    from agentops_runtime.cron_api import handle_cron_request
    status, resp = handle_cron_request(
        method="POST", path="/cron/jobs", query_string="",
        body_bytes=b"", auth_header=None, token=None, backend=backend,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_jobs_returns_jobs_list(tmp_path):
    backend = _backend(tmp_path)
    # create a job first
    _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    status, resp = _call(
        "GET", "/cron/jobs",
        backend=backend,
        context_header=json.dumps(_ctx_dict()),
    )
    assert status == 200
    assert "jobs" in resp
    assert len(resp["jobs"]) == 1


def test_list_jobs_scope_isolation(tmp_path):
    backend = _backend(tmp_path)
    ctx_alice = _ctx_dict(user_id="alice")
    ctx_bob = _ctx_dict(user_id="bob")
    _call("POST", "/cron/jobs", {"context": ctx_alice, "job": {"prompt": "alice-job", "schedule": "5m"}}, backend=backend)
    status, resp = _call("GET", "/cron/jobs", backend=backend, context_header=json.dumps(ctx_bob))
    assert status == 200
    assert resp["jobs"] == []


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


def test_get_job_returns_job(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, resp = _call("GET", f"/cron/jobs/{job_id}", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert status == 200
    assert resp["job"] is not None
    assert resp["job"]["id"] == job_id


def test_get_job_returns_null_for_missing(tmp_path):
    backend = _backend(tmp_path)
    status, resp = _call("GET", "/cron/jobs/nonexistent", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert status == 200
    assert resp["job"] is None


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_job_succeeds(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, resp = _call("PATCH", f"/cron/jobs/{job_id}", {"context": _ctx_dict(), "job": {"prompt": "updated"}}, backend=backend)
    assert status == 200
    assert resp == {}


def test_update_job_missing_job_field_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("PATCH", "/cron/jobs/any-id", {"context": _ctx_dict()}, backend=backend)
    assert status == 400


def test_update_job_rejects_invalid_next_run_at_without_leaking(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, resp = _call(
        "PATCH", f"/cron/jobs/{job_id}",
        {"context": _ctx_dict(), "job": {"next_run_at": "LEAKSENTINEL-not-a-number"}},
        backend=backend,
    )
    assert status == 400
    assert "LEAKSENTINEL" not in json.dumps(resp)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_job_succeeds(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, resp = _call("DELETE", f"/cron/jobs/{job_id}", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert status == 200
    assert resp == {}
    _, get_resp = _call("GET", f"/cron/jobs/{job_id}", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert get_resp["job"] is None


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------


def test_pause_and_resume_job(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, _ = _call("POST", f"/cron/jobs/{job_id}/pause", {"context": _ctx_dict()}, backend=backend)
    assert status == 200
    _, get_resp = _call("GET", f"/cron/jobs/{job_id}", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert get_resp["job"]["paused"] is True
    status, _ = _call("POST", f"/cron/jobs/{job_id}/resume", {"context": _ctx_dict()}, backend=backend)
    assert status == 200


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_empty_list_for_new_job(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m"}}, backend=backend)
    job_id = create_resp["id"]
    status, resp = _call("GET", f"/cron/jobs/{job_id}/history", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert status == 200
    assert resp == {"history": []}


# ---------------------------------------------------------------------------
# Claim-due
# ---------------------------------------------------------------------------


def test_claim_due_returns_due_jobs(tmp_path):
    backend = _backend(tmp_path)
    _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m", "next_run_at": 0.0}}, backend=backend)
    status, resp = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "worker-1", "now": 9999.0, "lease_seconds": 60.0},
        backend=backend,
    )
    assert status == 200
    assert "jobs" in resp
    assert len(resp["jobs"]) >= 1


def test_claim_due_with_draining_true_returns_no_jobs(tmp_path):
    backend = _backend(tmp_path)
    _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "schedule": "5m", "next_run_at": 0.0}}, backend=backend)
    status, resp = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "worker-1", "now": 9999.0, "lease_seconds": 60.0, "draining": True},
        backend=backend,
    )
    assert status == 200
    assert resp == {"jobs": []}


def test_claim_due_rejects_non_bool_draining(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "worker-1", "draining": "yes"},
        backend=backend,
    )
    assert status == 400


def test_claim_due_missing_owner_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict()}, backend=backend)
    assert status == 400


def test_claim_due_rejects_bool_lease_seconds(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "lease_seconds": True},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_zero_lease_seconds(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "lease_seconds": 0},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_negative_lease_seconds(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "lease_seconds": -1},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_non_positive_limit(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "limit": 0},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_fractional_limit(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "limit": 1.5},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_nan_now(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "now": float("nan")},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_oversized_now_without_crashing(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "now": 10**400},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_oversized_limit_without_crashing(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/claim-due",
        {"context": _ctx_dict(), "owner": "w", "limit": 10**400},
        backend=backend,
    )
    assert status == 400


def test_claim_due_rejects_json_integer_with_too_many_digits(tmp_path):
    from agentops_runtime.cron_api import handle_cron_request

    backend = _backend(tmp_path)
    body = b'{"context":{},"owner":"w","now":' + (b"9" * 5000) + b"}"
    status, resp = handle_cron_request("POST", "/cron/jobs/claim-due", "", body, None, None, backend)
    assert status == 400
    assert resp == {"error": "invalid request"}


def test_context_header_rejects_json_integer_with_too_many_digits(tmp_path):
    backend = _backend(tmp_path)
    huge_context = '{"org_id":' + ("9" * 5000) + "}"
    status, resp = _call("GET", "/cron/jobs", backend=backend, context_header=huge_context)
    assert status == 400
    assert resp == {"error": "invalid context"}


# ---------------------------------------------------------------------------
# Renew-lease
# ---------------------------------------------------------------------------


def test_renew_lease_returns_renewed_bool(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/renew-lease",
        {"context": _ctx_dict(), "owner": "w", "now": 2.0, "lease_seconds": 30.0},
        backend=backend,
    )
    assert status == 200
    assert "renewed" in resp
    assert isinstance(resp["renewed"], bool)


def test_renew_lease_missing_owner_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("POST", "/cron/jobs/any/renew-lease", {"context": _ctx_dict()}, backend=backend)
    assert status == 400


def test_renew_lease_rejects_oversized_lease_seconds_without_crashing(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/job-1/renew-lease",
        {"context": _ctx_dict(), "owner": "w", "lease_seconds": 10**400},
        backend=backend,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Release-lease
# ---------------------------------------------------------------------------


def test_release_lease_returns_empty(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/release-lease",
        {"context": _ctx_dict(), "owner": "w"},
        backend=backend,
    )
    assert status == 200
    assert resp == {}


# ---------------------------------------------------------------------------
# Complete / Fail
# ---------------------------------------------------------------------------


def test_complete_run_returns_history_entry(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/complete",
        {"context": _ctx_dict(), "owner": "w", "now": 2.0},
        backend=backend,
    )
    assert status == 200
    assert "status" in resp


def test_fail_run_returns_history_entry(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/fail",
        {"context": _ctx_dict(), "owner": "w", "error": "boom", "now": 2.0},
        backend=backend,
    )
    assert status == 200
    assert "status" in resp


def test_fail_run_missing_error_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("POST", "/cron/jobs/any/fail", {"context": _ctx_dict(), "owner": "w"}, backend=backend)
    assert status == 400


def test_complete_run_missing_owner_returns_400(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("POST", "/cron/jobs/any/complete", {"context": _ctx_dict()}, backend=backend)
    assert status == 400


def test_complete_run_rejects_invalid_next_run_at_without_leaking(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/complete",
        {"context": _ctx_dict(), "owner": "w", "next_run_at": "LEAKSENTINEL-not-a-number"},
        backend=backend,
    )

    assert status == 400
    assert "LEAKSENTINEL" not in json.dumps(resp)


def test_fail_run_rejects_invalid_next_run_at_without_leaking(tmp_path):
    backend = _backend(tmp_path)
    _, create_resp = _call("POST", "/cron/jobs", {"context": _ctx_dict(), "job": {"prompt": "p", "next_run_at": 0.0}}, backend=backend)
    job_id = create_resp["id"]
    _call("POST", "/cron/jobs/claim-due", {"context": _ctx_dict(), "owner": "w", "now": 1.0, "lease_seconds": 60.0}, backend=backend)

    status, resp = _call(
        "POST", f"/cron/jobs/{job_id}/fail",
        {"context": _ctx_dict(), "owner": "w", "error": "boom", "next_run_at": "LEAKSENTINEL-not-a-number"},
        backend=backend,
    )

    assert status == 400
    assert "LEAKSENTINEL" not in json.dumps(resp)


def test_complete_run_rejects_oversized_next_run_at_without_crashing(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call(
        "POST", "/cron/jobs/job-1/complete",
        {"context": _ctx_dict(), "owner": "w", "next_run_at": 10**400},
        backend=backend,
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Sanitized backend error
# ---------------------------------------------------------------------------


def test_backend_exception_returns_503_without_leaking(tmp_path):
    class _BrokenBackend:
        def list_jobs(self, ctx):
            raise RuntimeError("LEAKSENTINEL db=/secret/path token=secret123")

    backend = _BrokenBackend()
    status, resp = _call("GET", "/cron/jobs", backend=backend, context_header=json.dumps(_ctx_dict()))
    assert status == 503
    resp_text = json.dumps(resp)
    assert "LEAKSENTINEL" not in resp_text
    assert "db=" not in resp_text
    assert "token=" not in resp_text


# ---------------------------------------------------------------------------
# Method not allowed
# ---------------------------------------------------------------------------


def test_post_to_cron_jobs_slash_id_returns_405(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("POST", "/cron/jobs/abc", {"context": _ctx_dict()}, backend=backend)
    assert status == 405


def test_patch_to_cron_jobs_root_returns_405(tmp_path):
    backend = _backend(tmp_path)
    status, _ = _call("PATCH", "/cron/jobs", {"context": _ctx_dict()}, backend=backend)
    assert status == 405
