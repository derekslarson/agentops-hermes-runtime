"""Server-side handler for /secrets/get and /secrets/put control-plane API (M12B).

Routes:
    POST /secrets/get  -> {"value": str | null}
    POST /secrets/put  -> {}

Auth: when token is non-None, Authorization: Bearer <token> is required before body read.
Context: scope dict with mode=agentops, org_id, workspace_id required for non-null scopes.
         Empty/null context on get returns {"value": null} safely.
         Null context on put is rejected with 400.
Token safety: secret values, refs, and tokens never appear in error responses or logs.
Storage: durable SQLite at AGENTOPS_SECRET_STORE_PATH (required, no fallback).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_VALID_PATHS = frozenset({"/secrets/get", "/secrets/put"})
_SCOPE_FIELDS = (
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


class SQLiteSecretStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                "scope TEXT NOT NULL, "
                "ref TEXT NOT NULL, "
                "value TEXT NOT NULL, "
                "PRIMARY KEY (scope, ref)"
                ")"
            )
            conn.commit()

    def get(self, scope: str, ref: str) -> str | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT value FROM secrets WHERE scope = ? AND ref = ?",
                (scope, ref),
            ).fetchone()
            return row[0] if row else None

    def put(self, scope: str, ref: str, value: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO secrets (scope, ref, value) VALUES (?, ?, ?) "
                "ON CONFLICT(scope, ref) DO UPDATE SET value = excluded.value",
                (scope, ref, value),
            )
            conn.commit()


def handle_secret_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    if token and auth_header != f"Bearer {token}":
        return 401, {"error": "unauthorized"}

    if path not in _VALID_PATHS:
        return 404, {"error": "not found"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    if path == "/secrets/get":
        return _secret_get(body_bytes, backend)
    return _secret_put(body_bytes, backend)


def _parse_body(body_bytes: bytes) -> tuple[dict | None, tuple[int, dict] | None]:
    if not body_bytes:
        return None, (400, {"error": "invalid request"})
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, (400, {"error": "invalid request"})
    if not isinstance(body, dict):
        return None, (400, {"error": "invalid request"})
    return body, None


def _parse_ref(body: dict) -> tuple[str | None, tuple[int, dict] | None]:
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref:
        return None, (400, {"error": "invalid request"})
    return ref, None


def _parse_scope(raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict) or not raw:
        return None, "invalid context"
    if any(field not in raw for field in _SCOPE_FIELDS):
        return None, "invalid context"
    mode = raw.get("mode")
    if mode != "agentops":
        return None, "invalid context"
    org_id = raw.get("org_id")
    workspace_id = raw.get("workspace_id")
    if not isinstance(org_id, str) or not org_id.strip():
        return None, "invalid context"
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None, "invalid context"

    normalized: list[str] = [mode, org_id.strip(), workspace_id.strip()]
    for field in _SCOPE_FIELDS[3:]:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, "invalid context"
        normalized.append(value.strip())
    scope = json.dumps(normalized, separators=(",", ":"))
    return scope, None


def _secret_get(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_body(body_bytes)
    if err is not None:
        return err

    ref, err = _parse_ref(body)
    if err is not None:
        return err

    ctx_raw = body.get("context")
    if ctx_raw is None or (isinstance(ctx_raw, dict) and not ctx_raw):
        return 200, {"value": None}
    if not isinstance(ctx_raw, dict):
        return 400, {"error": "invalid context"}

    scope, ctx_err = _parse_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}

    try:
        value = backend.get(scope, ref)
    except Exception:
        return 503, {"error": "secret store unavailable"}

    return 200, {"value": value}


def _secret_put(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    body, err = _parse_body(body_bytes)
    if err is not None:
        return err

    ref, err = _parse_ref(body)
    if err is not None:
        return err

    value = body.get("value")
    if not isinstance(value, str):
        return 400, {"error": "invalid request"}

    ctx_raw = body.get("context")
    if ctx_raw is None or (isinstance(ctx_raw, dict) and not ctx_raw):
        return 400, {"error": "invalid context"}
    if not isinstance(ctx_raw, dict):
        return 400, {"error": "invalid context"}

    scope, ctx_err = _parse_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}

    try:
        backend.put(scope, ref, value)
    except Exception:
        return 503, {"error": "secret store unavailable"}

    return 200, {}
