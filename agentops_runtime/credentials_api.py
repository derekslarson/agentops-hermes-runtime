"""Server-side handler for /credentials/resolve control-plane API (M12B).

Routes:
    POST /credentials/resolve  → {secret_ref: str | null}

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: sparse credential-scope dict; empty {} is treated as None (parity
         with HttpCredentialResolver._scope_payload for null context). Non-empty
         contexts must specify mode=agentops — local mode is rejected fail-closed.
Token safety: the raw token, ref, and secret_ref values never appear in errors.
"""

from __future__ import annotations

import json
from typing import Any

from agent.runtime_context import RuntimeContext

_CREDENTIAL_CONTEXT_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "workspace_type",
    "user_id",
    "project_id",
    "agent_profile_id",
    "permissions_ref",
    "backend_profile",
)


def handle_credential_request(
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

    if method != "POST" or path != "/credentials/resolve":
        return 404, {"error": "not found"}

    return _resolve(body_bytes, backend)


def _resolve(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    if not body_bytes:
        return 400, {"error": "invalid request"}
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "invalid request"}
    if not isinstance(body, dict):
        return 400, {"error": "invalid request"}

    if "ref" not in body:
        return 400, {"error": "invalid request"}
    ref = body["ref"]
    if not isinstance(ref, str):
        return 400, {"error": "invalid request"}

    ctx_raw = body.get("context", {})
    ctx, err = _parse_context(ctx_raw)
    if err:
        return 400, {"error": err}

    try:
        secret_ref = backend.resolve(ctx, ref)
    except Exception:
        return 500, {"error": "internal error"}

    if secret_ref is not None and not isinstance(secret_ref, str):
        return 500, {"error": "internal error"}

    return 200, {"secret_ref": secret_ref}


def _parse_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    if raw is None:
        return None, "invalid context"
    if not isinstance(raw, dict):
        return None, "invalid context"
    if not raw:
        return None, None

    mode = raw.get("mode")
    if not mode or not isinstance(mode, str) or not mode.strip():
        return None, "invalid context"
    if mode.strip() != "agentops":
        return None, "invalid context"

    for required in ("org_id", "workspace_id", "agent_profile_id"):
        val = raw.get(required)
        if not isinstance(val, str) or not val.strip():
            return None, "invalid context"

    for field in _CREDENTIAL_CONTEXT_FIELDS:
        if field in raw:
            value = raw[field]
            if value is not None and not isinstance(value, str):
                return None, "invalid context"

    known = {k: raw[k] for k in _CREDENTIAL_CONTEXT_FIELDS if k in raw}
    try:
        ctx = RuntimeContext.from_mapping({k: v for k, v in known.items() if v is not None})
    except (ValueError, TypeError):
        return None, "invalid context"
    return ctx, None
