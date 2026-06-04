"""Server-side handler for /audit control-plane API (M12B).

Routes:
    POST /audit   → record audit event (scope, event)
    GET  /audit   → return redacted event-count/readback for scope

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: sanitised scope-only dict reconstructed into RuntimeContext using the
         full 15-field artifact scope shape from _SCOPE_FIELDS.
Token safety: the raw token value never appears in error payloads.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from agent.runtime_context import RuntimeContext

AUDIT_READBACK_LIMIT = 100

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


def handle_audit_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    if token:
        if auth_header != f"Bearer {token}":
            return 401, {"error": "unauthorized"}

    if method == "POST" and path == "/audit":
        return _record(body_bytes, backend)
    if method == "GET" and path == "/audit":
        return _list(query_string, backend)
    return 404, {"error": "not found"}


def _record(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
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

    if "event" not in body:
        return 400, {"error": "event is required"}
    if not isinstance(body["event"], dict):
        return 400, {"error": "event must be an object"}

    try:
        backend.record(ctx, body["event"])
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"ok": True}


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
        events = backend.list_events(ctx, limit=AUDIT_READBACK_LIMIT + 1)
    except Exception:
        return 500, {"error": "internal error"}

    truncated = len(events) > AUDIT_READBACK_LIMIT
    redacted = [{"redacted": True} for _ in events[:AUDIT_READBACK_LIMIT]]
    return 200, {"events": redacted, "count": len(redacted), "returned": len(redacted), "truncated": truncated}


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
