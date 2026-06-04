from __future__ import annotations

import base64
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

_SCOPE_FIELDS_15 = (
    "mode", "org_id", "workspace_id", "workspace_type", "user_id", "conversation_id",
    "external_channel_id", "external_thread_id", "agent_profile_id", "project_id",
    "run_id", "run_type", "job_id", "parent_session_id", "backend_profile",
)


class _FakeArtifactBackend:
    def __init__(self):
        self._store: dict = {}
        self.put_calls: list = []

    def _ctx_key(self, context):
        if context is None:
            return ()
        d = context.to_dict()
        return tuple(sorted((k, d.get(k)) for k in d if k != "metadata"))

    def put(self, context, ref, data):
        self.put_calls.append((context, ref, data))
        self._store[(self._ctx_key(context), ref)] = bytes(data)
        return ref

    def get(self, context, ref):
        return self._store.get((self._ctx_key(context), ref))

    def list_artifacts(self, context):
        scope = self._ctx_key(context)
        return sorted(k[1] for k in self._store if k[0] == scope)


class _BrokenArtifactBackend:
    def put(self, context, ref, data):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def get(self, context, ref):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")

    def list_artifacts(self, context):
        raise RuntimeError("LEAKSENTINEL backend exploded password=secret")


def _call(method, path, qs="", body=b"", auth=None, token=None, backend=None):
    from agentops_runtime.artifacts_api import handle_artifact_request
    if backend is None:
        backend = _FakeArtifactBackend()
    return handle_artifact_request(
        method=method,
        path=path,
        query_string=qs,
        body_bytes=body,
        auth_header=auth,
        token=token,
        backend=backend,
    )


def _post_body(scope=None, ref="test.txt", data=b"hello"):
    return json.dumps({
        "scope": scope if scope is not None else _SCOPE,
        "ref": ref,
        "data_b64": base64.b64encode(data).decode(),
    }).encode()


def test_import_succeeds():
    from agentops_runtime.artifacts_api import handle_artifact_request
    assert callable(handle_artifact_request)


def test_post_artifact_round_trip():
    fake = _FakeArtifactBackend()
    status, resp = _call("POST", "/artifacts", body=_post_body(data=b"artifact-bytes"), backend=fake)
    assert status == 200
    assert resp == {"ref": "test.txt"}

    qs = "scope=" + json.dumps(_SCOPE)
    status2, data = _call("GET", "/artifacts/test.txt", qs=qs, backend=fake)
    assert status2 == 200
    assert data == b"artifact-bytes"


def test_cross_tenant_isolation():
    fake = _FakeArtifactBackend()
    _call("POST", "/artifacts", body=_post_body(scope=_SCOPE, data=b"alice-only"), backend=fake)

    qs = "scope=" + json.dumps(_SCOPE_BOB)
    status, result = _call("GET", "/artifacts/test.txt", qs=qs, backend=fake)
    assert status == 404
    assert "error" in result


def test_full_scope_fidelity_preserves_artifact_scope_fields():
    fake = _FakeArtifactBackend()
    full_scope = dict(_SCOPE, run_id="run-full", workspace_type="team", backend_profile="compose-self-hosted")
    status, _ = _call("POST", "/artifacts", body=_post_body(scope=full_scope, ref="scoped.txt"), backend=fake)
    assert status == 200
    assert len(fake.put_calls) == 1
    ctx = fake.put_calls[0][0]
    assert ctx.run_id == "run-full"
    assert ctx.workspace_type == "team"
    assert ctx.backend_profile == "compose-self-hosted"


def test_missing_scope_identity_field_returns_400():
    incomplete_scope = dict(_SCOPE)
    incomplete_scope.pop("conversation_id")
    body = _post_body(scope=incomplete_scope)

    status, resp = _call("POST", "/artifacts", body=body)

    assert status == 400
    assert resp == {"error": "scope.conversation_id is required"}


def test_non_string_scope_identity_field_returns_400_without_leaking_value():
    malformed_scope = dict(_SCOPE, user_id={"LEAKSENTINEL": "secret-user"})
    body = _post_body(scope=malformed_scope)

    status, resp = _call("POST", "/artifacts", body=body)

    assert status == 400
    assert resp == {"error": "scope.user_id must be a string or null"}
    assert "LEAKSENTINEL" not in str(resp)
    assert "secret-user" not in str(resp)


def test_post_invalid_json_utf8_returns_400():
    status, resp = _call("POST", "/artifacts", body=b"\xff")
    assert status == 400
    assert resp == {"error": "invalid JSON body"}


def test_post_invalid_base64_returns_400():
    body = json.dumps({"scope": _SCOPE, "ref": "x.txt", "data_b64": "not!!valid##base64==="}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert "error" in resp


def test_post_non_string_ref_returns_400():
    body = json.dumps({"scope": _SCOPE, "ref": 123, "data_b64": base64.b64encode(b"x").decode()}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert resp == {"error": "ref must be a string"}


def test_post_non_string_data_b64_returns_400_without_coercion():
    body = json.dumps({"scope": _SCOPE, "ref": "x.txt", "data_b64": 123}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert resp == {"error": "data_b64 must be a string"}


def test_post_unsafe_ref_returns_400():
    body = json.dumps({"scope": _SCOPE, "ref": "../escape", "data_b64": base64.b64encode(b"x").decode()}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert "error" in resp


def test_auth_gating_rejects_without_token_when_required():
    status, resp = _call("POST", "/artifacts", body=_post_body(), auth=None, token="secret-token")
    assert status == 401
    assert resp == {"error": "unauthorized"}
    assert "secret-token" not in str(resp)


def test_auth_gating_allows_when_no_token_configured():
    fake = _FakeArtifactBackend()
    status, resp = _call("POST", "/artifacts", body=_post_body(), auth=None, token=None, backend=fake)
    assert status == 200


def test_list_artifacts_returns_artifact_list():
    fake = _FakeArtifactBackend()
    _call("POST", "/artifacts", body=_post_body(ref="a.txt", data=b"a"), backend=fake)
    _call("POST", "/artifacts", body=_post_body(ref="b.txt", data=b"b"), backend=fake)

    qs = "scope=" + json.dumps(_SCOPE)
    status, resp = _call("GET", "/artifacts", qs=qs, backend=fake)
    assert status == 200
    assert "artifacts" in resp
    assert "a.txt" in resp["artifacts"]
    assert "b.txt" in resp["artifacts"]


def test_get_artifact_missing_returns_404():
    fake = _FakeArtifactBackend()
    qs = "scope=" + json.dumps(_SCOPE)
    status, resp = _call("GET", "/artifacts/nonexistent.txt", qs=qs, backend=fake)
    assert status == 404
    assert "error" in resp


def test_backend_failure_returns_sanitized_500():
    broken = _BrokenArtifactBackend()
    status, resp = _call("POST", "/artifacts", body=_post_body(), backend=broken)
    assert status == 500
    assert "LEAKSENTINEL" not in str(resp)
    assert "password" not in str(resp)
    assert resp == {"error": "internal error"}


def test_get_missing_scope_param_returns_400():
    status, resp = _call("GET", "/artifacts/some.txt", qs="")
    assert status == 400
    assert "error" in resp


def test_post_missing_ref_returns_400():
    body = json.dumps({"scope": _SCOPE, "data_b64": base64.b64encode(b"x").decode()}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert "error" in resp


def test_post_missing_data_b64_returns_400():
    body = json.dumps({"scope": _SCOPE, "ref": "x.txt"}).encode()
    status, resp = _call("POST", "/artifacts", body=body)
    assert status == 400
    assert "error" in resp
