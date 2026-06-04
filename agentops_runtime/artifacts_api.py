"""Server-side handler for /artifacts control-plane API (M12B).

Routes:
    POST /artifacts          → put artifact (scope, ref, data_b64)
    GET  /artifacts/<ref>    → get artifact bytes
    GET  /artifacts          → list artifact refs for scope

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: sanitised scope-only dict reconstructed into RuntimeContext using the
         full 15-field artifact scope shape from _SCOPE_FIELDS.
Token safety: the raw token value never appears in error payloads.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

from agent.runtime_artifacts_audit import _normalize_ref
from agent.runtime_context import RuntimeContext

_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "workspace_type",
    "user_id",
    "conversation_id",
    "external_channel_id",
    "external_thread_id",
    "agent_profile_id",
    "project_id",
    "run_id",
    "run_type",
    "job_id",
    "parent_session_id",
    "backend_profile",
)


def handle_artifact_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any] | bytes]:
    if token:
        if auth_header != f"Bearer {token}":
            return 401, {"error": "unauthorized"}

    if method == "POST" and path == "/artifacts":
        return _put(body_bytes, backend)
    if method == "GET" and path == "/artifacts":
        return _list(query_string, backend)
    if method == "GET" and path.startswith("/artifacts/"):
        return _get(path, query_string, backend)
    return 404, {"error": "not found"}


def _put(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    if not body_bytes:
        return 400, {"error": "request body is required"}
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON body"}
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    if "scope" not in body:
        return 400, {"error": "scope is required"}
    ctx, err = _parse_context(body["scope"])
    if err:
        return 400, {"error": err}

    if "ref" not in body:
        return 400, {"error": "ref is required"}
    if not isinstance(body["ref"], str):
        return 400, {"error": "ref must be a string"}
    if "data_b64" not in body:
        return 400, {"error": "data_b64 is required"}
    if not isinstance(body["data_b64"], str):
        return 400, {"error": "data_b64 must be a string"}

    try:
        data = base64.b64decode(body["data_b64"], validate=True)
    except Exception:
        return 400, {"error": "invalid base64 encoding"}

    try:
        normalized = _normalize_ref(body["ref"])
    except ValueError:
        return 400, {"error": "invalid artifact ref"}

    try:
        result_ref = backend.put(ctx, normalized, data)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"ref": result_ref}


def _get(path: str, query_string: str, backend: Any) -> tuple[int, dict[str, Any] | bytes]:
    ref_raw = urllib.parse.unquote(path[len("/artifacts/"):])
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)

    if "scope" not in params:
        return 400, {"error": "scope is required"}
    try:
        scope_raw = json.loads(params["scope"][0])
    except (json.JSONDecodeError, IndexError):
        return 400, {"error": "invalid scope JSON"}

    ctx, err = _parse_context(scope_raw)
    if err:
        return 400, {"error": err}

    try:
        normalized = _normalize_ref(ref_raw)
    except ValueError:
        return 400, {"error": "invalid artifact ref"}

    try:
        data = backend.get(ctx, normalized)
    except Exception:
        return 500, {"error": "internal error"}

    if data is None:
        return 404, {"error": "not found"}
    return 200, bytes(data)


def _list(query_string: str, backend: Any) -> tuple[int, dict[str, Any]]:
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)

    if "scope" not in params:
        return 400, {"error": "scope is required"}
    try:
        scope_raw = json.loads(params["scope"][0])
    except (json.JSONDecodeError, IndexError):
        return 400, {"error": "invalid scope JSON"}

    ctx, err = _parse_context(scope_raw)
    if err:
        return 400, {"error": err}

    try:
        artifacts = backend.list_artifacts(ctx)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"artifacts": [str(a) for a in artifacts]}


def _parse_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    if raw is None:
        return None, "scope is required"
    if not isinstance(raw, dict):
        return None, "scope must be an object"
    mode = raw.get("mode")
    if not mode or not isinstance(mode, str) or not mode.strip():
        return None, "scope.mode is required"
    for field in _SCOPE_FIELDS:
        if field not in raw:
            return None, f"scope.{field} is required"
        value = raw[field]
        if value is not None and not isinstance(value, str):
            return None, f"scope.{field} must be a string or null"
    scope = {k: raw[k] for k in _SCOPE_FIELDS}
    try:
        ctx = RuntimeContext.from_mapping({k: v for k, v in scope.items() if v is not None})
    except (ValueError, TypeError):
        return None, "invalid scope"
    return ctx, None
