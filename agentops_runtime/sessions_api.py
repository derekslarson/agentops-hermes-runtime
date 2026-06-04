"""Server-side handler for /sessions control-plane API (M12B).

Routes (all POST):
    POST /sessions/create                     → {session_id: str}
    POST /sessions/append                     → {message_id: int} | {}
    POST /sessions/messages                   → {messages: [...]}
    POST /sessions/search                     → {results: [...]}
    POST /sessions/<session_id>/resume-target → {session_id: str}
    POST /sessions/turn-lock/claim            → {claimed: bool}
    POST /sessions/turn-lock/renew            → {renewed: bool}
    POST /sessions/turn-lock/release          → {}

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: reconstructed into RuntimeContext from the 14-field session scope
         shape sent by HttpSessionBackend._scope_payload().
Token/session-ID safety: raw tokens, session IDs, owners, search queries,
         backend exception text, and path values never appear in error payloads.
Transcript message payloads are intentionally NOT redacted before transport:
         the session control plane is the durable transcript store.
"""

from __future__ import annotations

import json
import math
import urllib.parse
from typing import Any

from agent.runtime_context import RuntimeContext

_MAX_SESSION_RESULT_LIMIT = 1000
_MAX_TURN_LOCK_TTL_SECONDS = 86_400

_SESSION_CONTEXT_STRING_FIELDS = (
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
    "permissions_ref",
    "backend_profile",
    "delivery_ref",
)
_SESSION_CONTEXT_FIELDS = _SESSION_CONTEXT_STRING_FIELDS + ("metadata",)


def handle_session_request(
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

    if method != "POST":
        return 404, {"error": "not found"}

    if path == "/sessions/create":
        return _create(body_bytes, backend)
    if path == "/sessions/append":
        return _append(body_bytes, backend)
    if path == "/sessions/messages":
        return _messages(body_bytes, backend)
    if path == "/sessions/search":
        return _search(body_bytes, backend)
    if path == "/sessions/turn-lock/claim":
        return _turn_lock_claim(body_bytes, backend)
    if path == "/sessions/turn-lock/renew":
        return _turn_lock_renew(body_bytes, backend)
    if path == "/sessions/turn-lock/release":
        return _turn_lock_release(body_bytes, backend)

    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "resume-target":
        session_id = urllib.parse.unquote(parts[1])
        return _resume_target(session_id, backend)

    return 404, {"error": "not found"}


def _parse_json_body(body_bytes: bytes) -> tuple[dict[str, Any] | None, str | None]:
    if not body_bytes:
        return None, "request body is required"
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid JSON body"
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    return body, None


def _parse_result_limit(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return _MAX_SESSION_RESULT_LIMIT, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "limit must be an integer"
    if value < 1 or value > _MAX_SESSION_RESULT_LIMIT:
        return None, f"limit must be between 1 and {_MAX_SESSION_RESULT_LIMIT}"
    return value, None


def _parse_ttl(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, "ttl_seconds must be a finite number"
    ttl = float(value)
    if not math.isfinite(ttl) or ttl <= 0 or ttl > _MAX_TURN_LOCK_TTL_SECONDS:
        return None, f"ttl_seconds must be finite and between 0 and {_MAX_TURN_LOCK_TTL_SECONDS}"
    return ttl, None


def _parse_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    if raw is None:
        return None, "context is required"
    if not isinstance(raw, dict):
        return None, "context must be an object"
    if not raw:
        return None, None
    mode = raw.get("mode")
    if not mode or not isinstance(mode, str) or not mode.strip():
        return None, "context.mode is required"
    for field in _SESSION_CONTEXT_STRING_FIELDS:
        if field in raw:
            value = raw[field]
            if value is not None and not isinstance(value, str):
                return None, f"context.{field} must be a string or null"
    if "metadata" in raw and raw["metadata"] is not None and not isinstance(raw["metadata"], dict):
        return None, "context.metadata must be an object or null"
    known = {k: raw.get(k) for k in _SESSION_CONTEXT_FIELDS if k in raw}
    try:
        ctx = RuntimeContext.from_mapping({k: v for k, v in known.items() if v is not None})
    except (ValueError, TypeError):
        return None, "invalid context"
    return ctx, None


def _create(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    kwargs: dict[str, Any] = {}
    for field in ("session_id", "source", "parent_session_id", "model", "system_prompt", "user_id"):
        if field in body:
            kwargs[field] = body[field]
    if "model_config" in body:
        mc = body["model_config"]
        kwargs["model_config"] = dict(mc) if isinstance(mc, dict) else mc

    try:
        session_id = backend.create_session(ctx, **kwargs)
    except Exception:
        return 500, {"error": "internal error"}

    if not isinstance(session_id, str) or not session_id:
        return 500, {"error": "internal error"}
    return 200, {"session_id": session_id}


def _append(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    if "message" not in body:
        return 400, {"error": "message is required"}
    if not isinstance(body["message"], dict):
        return 400, {"error": "message must be an object"}

    try:
        message_id = backend.append_message(ctx, body["message"])
    except Exception:
        return 500, {"error": "internal error"}

    if message_id is None:
        return 200, {}
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return 200, {"message_id": message_id}
    return 200, {}


def _messages(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    kwargs: dict[str, Any] = {}
    if "session_id" in body:
        kwargs["session_id"] = body["session_id"]
    limit, err = _parse_result_limit(body.get("limit"))
    if err:
        return 400, {"error": err}
    kwargs["limit"] = limit

    try:
        messages = backend.read_messages(ctx, **kwargs)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"messages": messages}


def _search(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    if "query" not in body:
        return 400, {"error": "query is required"}
    query = body["query"]
    if not isinstance(query, str):
        return 400, {"error": "query must be a string"}
    if not query.strip():
        return 200, {"results": []}

    try:
        results = backend.search(ctx, query)
    except Exception:
        return 500, {"error": "internal error"}
    if not isinstance(results, list):
        return 500, {"error": "internal error"}
    return 200, {"results": results[:_MAX_SESSION_RESULT_LIMIT]}

def _resume_target(session_id: str, backend: Any) -> tuple[int, dict[str, Any]]:
    if not session_id:
        return 400, {"error": "session_id is required"}

    try:
        result_id = backend.resolve_resume_session_id(session_id)
    except Exception:
        return 500, {"error": "internal error"}

    if not isinstance(result_id, str) or not result_id:
        return 500, {"error": "internal error"}
    return 200, {"session_id": result_id}


def _turn_lock_claim(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    if "owner" not in body:
        return 400, {"error": "owner is required"}
    owner = body["owner"]
    if not isinstance(owner, str):
        return 400, {"error": "owner must be a string"}

    kwargs: dict[str, Any] = {"owner": owner}
    if "ttl_seconds" in body:
        ttl, err = _parse_ttl(body["ttl_seconds"])
        if err:
            return 400, {"error": err}
        kwargs["ttl_seconds"] = ttl

    try:
        claimed = backend.claim_turn_lock(ctx, **kwargs)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"claimed": bool(claimed)}


def _turn_lock_renew(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    if "owner" not in body:
        return 400, {"error": "owner is required"}
    owner = body["owner"]
    if not isinstance(owner, str):
        return 400, {"error": "owner must be a string"}

    kwargs: dict[str, Any] = {"owner": owner}
    if "ttl_seconds" in body:
        ttl, err = _parse_ttl(body["ttl_seconds"])
        if err:
            return 400, {"error": err}
        kwargs["ttl_seconds"] = ttl

    try:
        renewed = backend.renew_turn_lock(ctx, **kwargs)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"renewed": bool(renewed)}


def _turn_lock_release(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_json_body(body_bytes)
    if err:
        return 400, {"error": err}

    ctx, err = _parse_context(body.get("context"))
    if err:
        return 400, {"error": err}

    if "owner" not in body:
        return 400, {"error": "owner is required"}
    owner = body["owner"]
    if not isinstance(owner, str):
        return 400, {"error": "owner must be a string"}

    try:
        backend.release_turn_lock(ctx, owner=owner)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {}
