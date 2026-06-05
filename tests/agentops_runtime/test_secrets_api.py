"""Tests for server-side secrets store API (M12B)."""

from __future__ import annotations

import json

import pytest


_SECRET_CONTEXT = {
    "mode": "agentops",
    "org_id": "org-test",
    "workspace_id": "ws-test",
    "workspace_type": "team",
    "user_id": "user-test",
    "project_id": "project-test",
    "agent_profile_id": "agent-profile-test",
    "permissions_ref": "permissions-test",
    "backend_profile": "compose-self-hosted",
}

_SECRET_CONTEXT_B = {
    **_SECRET_CONTEXT,
    "org_id": "org-other",
}


def test_import_secrets_api():
    from agentops_runtime import secrets_api  # noqa: F401


@pytest.fixture
def _secret_backend(tmp_path):
    from agentops_runtime.secrets_api import SQLiteSecretStore

    return SQLiteSecretStore(str(tmp_path / "secrets.db"))


def test_put_get_roundtrip(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "my-api-key",
        "value": "sk-sentinelval",
    }).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/put", "", put_body, None, None, _secret_backend
    )
    assert status == 200
    assert resp == {}

    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "my-api-key"}).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/get", "", get_body, None, None, _secret_backend
    )
    assert status == 200
    assert resp["value"] == "sk-sentinelval"


def test_missing_get_returns_null(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "nonexistent-ref"}).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/get", "", get_body, None, None, _secret_backend
    )
    assert status == 200
    assert resp == {"value": None}


def test_no_put_echo(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "echo-test",
        "value": "ECHOLEAKSENTINEL",
    }).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/put", "", put_body, None, None, _secret_backend
    )
    assert status == 200
    assert "value" not in resp
    assert "ECHOLEAKSENTINEL" not in json.dumps(resp)


def test_bad_ref_rejection(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for ref in ("", None, 123):
        body_data = {"context": _SECRET_CONTEXT, "ref": ref}
        get_body = json.dumps(body_data).encode()
        status, _ = handle_secret_request(
            "POST", "/secrets/get", "", get_body, None, None, _secret_backend
        )
        assert status == 400, f"expected 400 for ref={ref!r}"

    for ref in ("", None, 123):
        body_data = {"context": _SECRET_CONTEXT, "ref": ref, "value": "v"}
        put_body = json.dumps(body_data).encode()
        status, _ = handle_secret_request(
            "POST", "/secrets/put", "", put_body, None, None, _secret_backend
        )
        assert status == 400, f"expected 400 for ref={ref!r} on put"


def test_bad_value_rejection(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for value in (123, None, ["list"], {}):
        put_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "x", "value": value}).encode()
        status, _ = handle_secret_request(
            "POST", "/secrets/put", "", put_body, None, None, _secret_backend
        )
        assert status == 400, f"expected 400 for value={value!r}"


def test_bad_context_rejection(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for ctx in ("not-a-dict", 123, ["list"]):
        put_body = json.dumps({"context": ctx, "ref": "x", "value": "v"}).encode()
        status, _ = handle_secret_request(
            "POST", "/secrets/put", "", put_body, None, None, _secret_backend
        )
        assert status == 400, f"expected 400 for context={ctx!r}"


def test_null_context_put_rejected(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for ctx in (None, {}):
        put_body = json.dumps({"context": ctx, "ref": "x", "value": "v"}).encode()
        status, _ = handle_secret_request(
            "POST", "/secrets/put", "", put_body, None, None, _secret_backend
        )
        assert status == 400, f"null context put must be rejected for context={ctx!r}"

    empty_str_body = json.dumps({"context": "", "ref": "x", "value": "v"}).encode()
    status, _ = handle_secret_request(
        "POST", "/secrets/put", "", empty_str_body, None, None, _secret_backend
    )
    assert status == 400, "empty string context put must be rejected"


def test_null_context_get_returns_null(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for ctx in (None, {}):
        get_body = json.dumps({"context": ctx, "ref": "x"}).encode()
        status, resp = handle_secret_request(
            "POST", "/secrets/get", "", get_body, None, None, _secret_backend
        )
        assert status == 200, f"null context get must return 200 for context={ctx!r}"
        assert resp == {"value": None}


def test_cross_scope_isolation(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "isolated-ref",
        "value": "scope-a-value",
    }).encode()
    status, _ = handle_secret_request("POST", "/secrets/put", "", put_body, None, None, _secret_backend)
    assert status == 200

    get_body = json.dumps({"context": _SECRET_CONTEXT_B, "ref": "isolated-ref"}).encode()
    status, resp = handle_secret_request("POST", "/secrets/get", "", get_body, None, None, _secret_backend)
    assert status == 200
    assert resp["value"] is None, "value from scope A must not be visible under scope B"


def test_cross_binding_field_isolation(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "binding-ref",
        "value": "binding-a-value",
    }).encode()
    status, _ = handle_secret_request("POST", "/secrets/put", "", put_body, None, None, _secret_backend)
    assert status == 200

    for field in ("user_id", "project_id", "agent_profile_id", "permissions_ref", "backend_profile"):
        other_context = {**_SECRET_CONTEXT, field: f"other-{field}"}
        get_body = json.dumps({"context": other_context, "ref": "binding-ref"}).encode()
        status, resp = handle_secret_request("POST", "/secrets/get", "", get_body, None, None, _secret_backend)
        assert status == 200, f"expected successful miss for changed {field}"
        assert resp["value"] is None, f"secret value must not leak across changed {field}"


def test_incomplete_binding_context_rejected(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    incomplete_context = dict(_SECRET_CONTEXT)
    incomplete_context.pop("user_id")

    put_body = json.dumps({"context": incomplete_context, "ref": "x", "value": "v"}).encode()
    status, _ = handle_secret_request("POST", "/secrets/put", "", put_body, None, None, _secret_backend)
    assert status == 400

    get_body = json.dumps({"context": incomplete_context, "ref": "x"}).encode()
    status, _ = handle_secret_request("POST", "/secrets/get", "", get_body, None, None, _secret_backend)
    assert status == 400


@pytest.mark.parametrize("field", ["user_id", "project_id", "agent_profile_id", "permissions_ref", "backend_profile"])
def test_null_or_blank_binding_context_rejected(_secret_backend, field):
    from agentops_runtime.secrets_api import handle_secret_request

    for value in (None, "", "   "):
        context = {**_SECRET_CONTEXT, field: value}
        put_body = json.dumps({"context": context, "ref": "x", "value": "v"}).encode()
        status, _ = handle_secret_request("POST", "/secrets/put", "", put_body, None, None, _secret_backend)
        assert status == 400, f"put must reject {field}={value!r}"

        get_body = json.dumps({"context": context, "ref": "x"}).encode()
        status, _ = handle_secret_request("POST", "/secrets/get", "", get_body, None, None, _secret_backend)
        assert status == 400, f"get must reject {field}={value!r}"


def test_durable_second_instance_read(tmp_path):
    from agentops_runtime.secrets_api import SQLiteSecretStore, handle_secret_request

    db_path = str(tmp_path / "durable.db")

    store1 = SQLiteSecretStore(db_path)
    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "durable-ref",
        "value": "durable-value",
    }).encode()
    status, _ = handle_secret_request("POST", "/secrets/put", "", put_body, None, None, store1)
    assert status == 200

    store2 = SQLiteSecretStore(db_path)
    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "durable-ref"}).encode()
    status, resp = handle_secret_request("POST", "/secrets/get", "", get_body, None, None, store2)
    assert status == 200
    assert resp["value"] == "durable-value"


def test_auth_handling(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "x"}).encode()

    status, _ = handle_secret_request(
        "POST", "/secrets/get", "", get_body, None, "expected-token", _secret_backend
    )
    assert status == 401, "missing auth header must return 401"

    status, _ = handle_secret_request(
        "POST", "/secrets/get", "", get_body, "Bearer wrong-token", "expected-token", _secret_backend
    )
    assert status == 401, "wrong token must return 401"

    status, _ = handle_secret_request(
        "POST", "/secrets/get", "", get_body, "Bearer expected-token", "expected-token", _secret_backend
    )
    assert status == 200, "correct token must succeed"


def test_backend_exception_sanitization(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    class _ExplodingBackend:
        def get(self, scope, ref):
            raise RuntimeError("LEAKSENTINEL path=/data password=secret")

        def put(self, scope, ref, value):
            raise RuntimeError("LEAKSENTINEL path=/data password=secret")

    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "x"}).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/get", "", get_body, None, None, _ExplodingBackend()
    )
    assert status == 503
    encoded = json.dumps(resp)
    assert "LEAKSENTINEL" not in encoded
    assert "password=secret" not in encoded

    put_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "x", "value": "v"}).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/put", "", put_body, None, None, _ExplodingBackend()
    )
    assert status == 503
    encoded = json.dumps(resp)
    assert "LEAKSENTINEL" not in encoded


def test_ref_non_leakage():
    from agentops_runtime.secrets_api import handle_secret_request, SQLiteSecretStore

    sentinel_ref = "REFLEAKSENTINEL-api-key"

    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": sentinel_ref}).encode()
    _, resp = handle_secret_request(
        "POST", "/secrets/get", "", get_body, "Bearer wrong", "expected-token", None
    )
    encoded = json.dumps(resp)
    assert sentinel_ref not in encoded


def test_value_non_leakage():
    from agentops_runtime.secrets_api import handle_secret_request

    class _ExplodingPutBackend:
        def put(self, scope, ref, value):
            raise RuntimeError(f"stored value: {value}")

        def get(self, scope, ref):
            return None

    sentinel_value = "VALUELEAKSENTINEL-secret-value"
    put_body = json.dumps({
        "context": _SECRET_CONTEXT,
        "ref": "x",
        "value": sentinel_value,
    }).encode()
    status, resp = handle_secret_request(
        "POST", "/secrets/put", "", put_body, None, None, _ExplodingPutBackend()
    )
    assert status == 503
    encoded = json.dumps(resp)
    assert sentinel_value not in encoded


def test_token_non_leakage():
    from agentops_runtime.secrets_api import handle_secret_request

    sentinel_token = "TOKENLEAKSENTINEL-bearer"
    get_body = json.dumps({"context": _SECRET_CONTEXT, "ref": "x"}).encode()
    _, resp = handle_secret_request(
        "POST", "/secrets/get", "", get_body, f"Bearer {sentinel_token}", sentinel_token + "wrong", None
    )
    encoded = json.dumps(resp)
    assert sentinel_token not in encoded


def test_unknown_route_returns_404(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for path in ("/secrets/unknown", "/secrets/", "/secrets"):
        status, _ = handle_secret_request(
            "POST", path, "", b"{}", None, None, _secret_backend
        )
        assert status == 404, f"expected 404 for path={path!r}"


def test_unknown_method_returns_405(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    for method in ("GET", "DELETE", "PUT", "PATCH"):
        status, _ = handle_secret_request(
            method, "/secrets/get", "", b"{}", None, None, _secret_backend
        )
        assert status == 405, f"expected 405 for method={method}"


def test_secrets_prefix_overmatch_returns_404(_secret_backend):
    from agentops_runtime.secrets_api import handle_secret_request

    status, _ = handle_secret_request(
        "POST", "/secretsXYZ", "", b"{}", None, None, _secret_backend
    )
    assert status == 404
