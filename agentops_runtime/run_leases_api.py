"""Server-side handler for /run-leases/* control-plane API (M12B).

Routes (all POST):
    POST /run-leases/<key>/claim          body {context, owner, now?, lease_seconds?} -> {claimed: bool}
    POST /run-leases/<key>/renew          body {context, owner, now?, lease_seconds?} -> {renewed: bool}
    POST /run-leases/<key>/release        body {context, owner}                       -> {}
    POST /run-leases/<key>/request-cancel body {context, requester}                   -> {}
    POST /run-leases/<key>/is-cancelled   body {context}                              -> {cancelled: bool}
    POST /run-leases/<key>/clear-cancel   body {context}                              -> {}
    POST /run-leases/expire-stale         body {context, now?}                        -> {expired: int}

Auth: when token is non-None, Authorization: Bearer *** is required before body read.
Context: scope dict from HttpRunLeaseBackend._scope_payload() in agent/runtime_run_lease_http.py.
         Null or empty {} context maps to the default all-None scope.
         run_id and job_id are excluded from scope.
Errors: sanitized; no raw keys, owners/requesters, tokens, request bodies, DB paths, or
        backend exception text.
Storage: durable SQLite at AGENTOPS_RUN_LEASE_DB_PATH (required, no cwd/tmp fallback).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.parse
from typing import Any

_VALID_KEY_ACTIONS = frozenset({
    "claim", "renew", "release", "request-cancel", "is-cancelled", "clear-cancel",
})

_RUN_LEASE_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "workspace_type",
    "project_id",
    "external_channel_id",
    "external_thread_id",
    "conversation_id",
    "user_id",
    "agent_profile_id",
    "run_type",
    "parent_session_id",
    "backend_profile",
    "delivery_ref",
)

_DEFAULT_SCOPE = json.dumps([None] * len(_RUN_LEASE_SCOPE_FIELDS), separators=(",", ":"))


class SQLiteRunLeaseBackend:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS run_leases ("
                "context_scope TEXT NOT NULL, "
                "lease_key TEXT NOT NULL, "
                "owner TEXT NOT NULL, "
                "expires_at REAL, "
                "PRIMARY KEY (context_scope, lease_key)"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS run_lease_cancels ("
                "context_scope TEXT NOT NULL, "
                "lease_key TEXT NOT NULL, "
                "PRIMARY KEY (context_scope, lease_key)"
                ")"
            )
            conn.commit()

    def claim(
        self,
        scope: str,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        clock = time.time() if now is None else now
        expires_at = None if lease_seconds is None else clock + lease_seconds

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO run_leases (context_scope, lease_key, owner, expires_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(context_scope, lease_key) DO UPDATE SET "
                "  owner = CASE "
                "    WHEN owner = excluded.owner OR (expires_at IS NOT NULL AND expires_at <= ?) "
                "    THEN excluded.owner ELSE owner END, "
                "  expires_at = CASE "
                "    WHEN owner = excluded.owner OR (expires_at IS NOT NULL AND expires_at <= ?) "
                "    THEN excluded.expires_at ELSE expires_at END",
                (scope, key, owner, expires_at, clock, clock),
            )
            conn.commit()
            row = conn.execute(
                "SELECT owner FROM run_leases WHERE context_scope=? AND lease_key=?",
                (scope, key),
            ).fetchone()
            return row is not None and row[0] == owner

    def renew(
        self,
        scope: str,
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        clock = time.time() if now is None else now
        next_expires_at = None if lease_seconds is None else clock + lease_seconds

        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE run_leases SET expires_at=CASE WHEN ? IS NULL THEN expires_at ELSE ? END "
                "WHERE context_scope=? AND lease_key=? AND owner=? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (next_expires_at, next_expires_at, scope, key, owner, clock),
            )
            conn.commit()
            return cursor.rowcount == 1

    def release(self, scope: str, key: str, *, owner: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "DELETE FROM run_leases WHERE context_scope=? AND lease_key=? AND owner=?",
                (scope, key, owner),
            )
            conn.commit()

    def request_cancel(self, scope: str, key: str, *, requester: str | None = None) -> None:
        del requester
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO run_lease_cancels (context_scope, lease_key) VALUES (?, ?)",
                (scope, key),
            )
            conn.commit()

    def is_cancelled(self, scope: str, key: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM run_lease_cancels WHERE context_scope=? AND lease_key=?",
                (scope, key),
            ).fetchone()
            return row is not None

    def clear_cancel(self, scope: str, key: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "DELETE FROM run_lease_cancels WHERE context_scope=? AND lease_key=?",
                (scope, key),
            )
            conn.commit()

    def clear_cancelled(self, scope: str, key: str) -> None:
        self.clear_cancel(scope, key)

    def expire_stale(self, scope: str, *, now: float | None = None) -> int:
        clock = time.time() if now is None else now
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM run_leases WHERE context_scope=? AND expires_at IS NOT NULL AND expires_at <= ?",
                (scope, clock),
            )
            conn.commit()
            return cursor.rowcount


def _parse_run_lease_route(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (key, action, error). key=None means expire-stale action."""
    segments = path.split("/")
    if len(segments) < 3 or segments[0] != "" or segments[1] != "run-leases":
        return None, None, "not found"
    if any(";" in seg for seg in segments):
        return None, None, "not found"
    if len(segments) == 3:
        if segments[2] == "expire-stale":
            return None, "expire-stale", None
        return None, None, "not found"
    if len(segments) == 4:
        raw_key = segments[2]
        action = segments[3]
        if not raw_key:
            return None, None, "not found"
        if action not in _VALID_KEY_ACTIONS:
            return None, None, "not found"
        try:
            key = urllib.parse.unquote(raw_key, errors="strict")
        except Exception:
            return None, None, "not found"
        return key, action, None
    return None, None, "not found"


def is_run_lease_route(path: str) -> bool:
    """Return True only for exact raw /run-leases/* routes accepted by the contract."""
    _key, _action, error = _parse_run_lease_route(path)
    return error is None


def _parse_run_lease_scope(raw: Any) -> tuple[str | None, str | None]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return _DEFAULT_SCOPE, None
    if not isinstance(raw, dict):
        return None, "invalid context"
    values: list[Any] = []
    for field in _RUN_LEASE_SCOPE_FIELDS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            return None, "invalid context"
        values.append(value)
    return json.dumps(values, separators=(",", ":")), None


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


def _parse_now(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid now"
    if not isinstance(value, (int, float)):
        return None, "invalid now"
    if math.isnan(value) or math.isinf(value):
        return None, "invalid now"
    return float(value), None


def _parse_lease_seconds(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid lease_seconds"
    if not isinstance(value, (int, float)):
        return None, "invalid lease_seconds"
    if math.isnan(value) or math.isinf(value) or value <= 0:
        return None, "invalid lease_seconds"
    return float(value), None


def handle_run_lease_request(
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

    key, action, route_error = _parse_run_lease_route(path)
    if route_error:
        return 404, {"error": "not found"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    body, body_err = _parse_body(body_bytes)
    if body_err is not None:
        return body_err

    ctx_raw = body.get("context")
    scope, ctx_err = _parse_run_lease_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}

    if action == "expire-stale":
        return _expire_stale(body, scope, backend)
    if action == "claim":
        return _claim(body, key, scope, backend)
    if action == "renew":
        return _renew(body, key, scope, backend)
    if action == "release":
        return _release(body, key, scope, backend)
    if action == "request-cancel":
        return _request_cancel(body, key, scope, backend)
    if action == "is-cancelled":
        return _is_cancelled(key, scope, backend)
    return _clear_cancel(key, scope, backend)


def _claim(body: dict, key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ls_raw = body.get("lease_seconds")
    lease_seconds, ls_err = _parse_lease_seconds(ls_raw)
    if ls_err:
        return 400, {"error": "invalid request"}

    try:
        claimed = backend.claim(scope, key, owner=owner, now=now, lease_seconds=lease_seconds)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {"claimed": claimed}


def _renew(body: dict, key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ls_raw = body.get("lease_seconds")
    lease_seconds, ls_err = _parse_lease_seconds(ls_raw)
    if ls_err:
        return 400, {"error": "invalid request"}

    try:
        renewed = backend.renew(scope, key, owner=owner, now=now, lease_seconds=lease_seconds)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {"renewed": renewed}


def _release(body: dict, key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    try:
        backend.release(scope, key, owner=owner)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {}


def _request_cancel(body: dict, key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    requester = body.get("requester")
    if not isinstance(requester, str) or not requester:
        return 400, {"error": "invalid request"}

    try:
        backend.request_cancel(scope, key, requester=requester)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {}


def _is_cancelled(key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        cancelled = backend.is_cancelled(scope, key)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {"cancelled": cancelled}


def _clear_cancel(key: str, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        if hasattr(backend, "clear_cancelled"):
            backend.clear_cancelled(scope, key)
        else:
            backend.clear_cancel(scope, key)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {}


def _expire_stale(body: dict, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    try:
        expired = backend.expire_stale(scope, now=now)
    except Exception:
        return 503, {"error": "run-lease backend unavailable"}
    return 200, {"expired": expired}
