"""Server-side handler for /workers/* control-plane API (M12B).

Routes (all POST):
    POST /workers/register              body {context, worker, now?, ttl_seconds?} -> {worker_id}
    POST /workers/<worker_id>/heartbeat body {context, slots, now?, ttl_seconds?}  -> {}
    POST /workers/<worker_id>/drain     body {context}                             -> {}
    POST /workers/recover-expired       body {context, now?}                       -> {recovered: [str]}
    POST /workers/list                  body {context}                             -> {workers: [...]}

Auth: when token is non-None, Authorization: Bearer *** is required before body read.
Context: scope dict from HttpWorkerRegistry._scope_payload(); run_id and job_id excluded.
         None or empty {} context maps to the default all-None scope.
Errors: sanitized; no raw worker IDs, slots, tokens, request bodies, DB paths, or
        backend exception text.
Storage: durable SQLite at AGENTOPS_WORKER_REGISTRY_DB_PATH (required, no cwd/tmp fallback).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.parse
import uuid
from typing import Any, Mapping

_WORKER_SCOPE_FIELDS = (
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

_DEFAULT_SCOPE = json.dumps([None] * len(_WORKER_SCOPE_FIELDS), separators=(",", ":"))

_VALID_COLLECTION_ACTIONS = frozenset({"register", "recover-expired", "list"})
_VALID_WORKER_ACTIONS = frozenset({"heartbeat", "drain"})


class SQLiteWorkerRegistryBackend:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workers ("
                "scope TEXT NOT NULL, "
                "worker_id TEXT NOT NULL, "
                "worker_json TEXT NOT NULL, "
                "slots_json TEXT, "
                "draining INTEGER NOT NULL DEFAULT 0, "
                "registered_at REAL, "
                "expires_at REAL, "
                "PRIMARY KEY (scope, worker_id)"
                ")"
            )
            conn.commit()

    def register(
        self,
        scope: str,
        worker: Any,
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> str:
        worker_record = dict(worker) if isinstance(worker, Mapping) else {}
        worker_id = str(worker_record.get("id") or uuid.uuid4())
        worker_record["id"] = worker_id
        clock = time.time() if now is None else now
        expires_at = None if ttl_seconds is None else clock + ttl_seconds
        worker_json = json.dumps(worker_record)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO workers "
                "(scope, worker_id, worker_json, slots_json, draining, registered_at, expires_at) "
                "VALUES (?, ?, ?, NULL, 0, ?, ?) "
                "ON CONFLICT(scope, worker_id) DO UPDATE SET "
                "worker_json=excluded.worker_json, "
                "slots_json=NULL, "
                "draining=0, "
                "registered_at=excluded.registered_at, "
                "expires_at=excluded.expires_at",
                (scope, worker_id, worker_json, clock, expires_at),
            )
            conn.commit()
        return worker_id

    def heartbeat(
        self,
        scope: str,
        worker_id: str,
        *,
        slots: Any,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        clock = time.time() if now is None else now
        slots_json = json.dumps(slots if slots is not None else {})
        with sqlite3.connect(self._db_path) as conn:
            if ttl_seconds is not None:
                expires_at = clock + ttl_seconds
                cursor = conn.execute(
                    "UPDATE workers SET slots_json=?, expires_at=? "
                    "WHERE scope=? AND worker_id=?",
                    (slots_json, expires_at, scope, worker_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE workers SET slots_json=? WHERE scope=? AND worker_id=?",
                    (slots_json, scope, worker_id),
                )
            if cursor.rowcount == 0:
                raise KeyError("worker is not registered")
            conn.commit()

    def mark_draining(self, scope: str, worker_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE workers SET draining=1 WHERE scope=? AND worker_id=?",
                (scope, worker_id),
            )
            if cursor.rowcount == 0:
                raise KeyError("worker is not registered")
            conn.commit()

    def recover_expired(self, scope: str, *, now: float | None = None) -> list[str]:
        clock = time.time() if now is None else now
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT worker_id FROM workers "
                "WHERE scope=? AND expires_at IS NOT NULL AND expires_at <= ? "
                "ORDER BY worker_id",
                (scope, clock),
            ).fetchall()
            worker_ids = [row[0] for row in rows]
            if worker_ids:
                placeholders = ",".join("?" * len(worker_ids))
                conn.execute(
                    f"DELETE FROM workers WHERE scope=? AND worker_id IN ({placeholders})",
                    [scope] + worker_ids,
                )
            conn.commit()
        return worker_ids

    def list_workers(self, scope: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT worker_id, worker_json, slots_json, draining, registered_at, expires_at "
                "FROM workers WHERE scope=? ORDER BY worker_id",
                (scope,),
            ).fetchall()
        result = []
        for worker_id, worker_json, slots_json, draining, registered_at, expires_at in rows:
            try:
                worker_data = json.loads(worker_json)
            except (json.JSONDecodeError, TypeError):
                worker_data = {}
            slots_data = None
            if slots_json is not None:
                try:
                    slots_data = json.loads(slots_json)
                except (json.JSONDecodeError, TypeError):
                    slots_data = None
            entry: dict[str, Any] = {
                "worker_id": worker_id,
                "worker": worker_data,
                "draining": bool(draining),
            }
            if slots_data is not None:
                entry["slots"] = slots_data
            if registered_at is not None:
                entry["registered_at"] = registered_at
            if expires_at is not None:
                entry["expires_at"] = expires_at
            result.append(entry)
        return result


def _parse_worker_route(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (endpoint, worker_id, error).

    endpoint: one of the five known actions
    worker_id: URL-decoded worker ID for per-worker routes, None for collection routes
    error: None if route is valid
    """
    if ";" in path:
        return None, None, "not found"
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "" or parts[1] != "workers":
        return None, None, "not found"
    if len(parts) == 3:
        action = parts[2]
        if action in _VALID_COLLECTION_ACTIONS:
            return action, None, None
        return None, None, "not found"
    if len(parts) == 4:
        raw_worker_id = parts[2]
        action = parts[3]
        if not raw_worker_id:
            return None, None, "not found"
        if action not in _VALID_WORKER_ACTIONS:
            return None, None, "not found"
        if "%2f" in raw_worker_id.lower():
            return None, None, "not found"
        try:
            worker_id = urllib.parse.unquote(raw_worker_id, errors="strict")
        except Exception:
            return None, None, "not found"
        if not worker_id:
            return None, None, "not found"
        return action, worker_id, None
    return None, None, "not found"


def is_worker_route(path: str) -> bool:
    """Return True only for exact raw /workers/* routes accepted by the contract."""
    _endpoint, _worker_id, error = _parse_worker_route(path)
    return error is None


def _parse_body(body_bytes: bytes) -> tuple[dict | None, tuple[int, dict] | None]:
    if not body_bytes:
        return None, (400, {"error": "invalid request"})
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return None, (400, {"error": "invalid request"})
    if not isinstance(body, dict):
        return None, (400, {"error": "invalid request"})
    return body, None


def _parse_worker_scope(raw: Any) -> tuple[str | None, str | None]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return _DEFAULT_SCOPE, None
    if not isinstance(raw, dict):
        return None, "invalid context"
    values: list[Any] = []
    for field in _WORKER_SCOPE_FIELDS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            return None, "invalid context"
        values.append(value)
    return json.dumps(values, separators=(",", ":")), None


def _parse_now(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid now"
    if not isinstance(value, (int, float)):
        return None, "invalid now"
    try:
        if math.isnan(value) or math.isinf(value):
            return None, "invalid now"
        return float(value), None
    except (OverflowError, ValueError):
        return None, "invalid now"


def _parse_ttl_seconds(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid ttl_seconds"
    if not isinstance(value, (int, float)):
        return None, "invalid ttl_seconds"
    try:
        if math.isnan(value) or math.isinf(value) or value <= 0:
            return None, "invalid ttl_seconds"
        return float(value), None
    except (OverflowError, ValueError):
        return None, "invalid ttl_seconds"


def handle_worker_request(
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

    endpoint, worker_id, route_error = _parse_worker_route(path)
    if route_error:
        return 404, {"error": "not found"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    body, body_err = _parse_body(body_bytes)
    if body_err is not None:
        return body_err
    assert body is not None

    ctx_raw = body.get("context")
    scope, ctx_err = _parse_worker_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    assert scope is not None

    if endpoint == "register":
        return _handle_register(body, scope, backend)
    if endpoint == "heartbeat":
        assert worker_id is not None
        return _handle_heartbeat(body, scope, worker_id, backend)
    if endpoint == "drain":
        assert worker_id is not None
        return _handle_drain(scope, worker_id, backend)
    if endpoint == "recover-expired":
        return _handle_recover_expired(body, scope, backend)
    return _handle_list(scope, backend)


def _handle_register(body: dict, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    worker = body.get("worker") or {}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ttl_raw = body.get("ttl_seconds")
    ttl_seconds, ttl_err = _parse_ttl_seconds(ttl_raw)
    if ttl_err:
        return 400, {"error": "invalid request"}

    try:
        worker_id = backend.register(scope, worker, now=now, ttl_seconds=ttl_seconds)
    except Exception:
        return 503, {"error": "worker registry unavailable"}
    if not isinstance(worker_id, str) or not worker_id:
        return 503, {"error": "worker registry unavailable"}
    return 200, {"worker_id": worker_id}


def _handle_heartbeat(body: dict, scope: str, worker_id: str, backend: Any) -> tuple[int, dict[str, Any]]:
    slots = body.get("slots") or {}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ttl_raw = body.get("ttl_seconds")
    ttl_seconds, ttl_err = _parse_ttl_seconds(ttl_raw)
    if ttl_err:
        return 400, {"error": "invalid request"}

    try:
        backend.heartbeat(scope, worker_id, slots=slots, now=now, ttl_seconds=ttl_seconds)
    except KeyError:
        return 404, {"error": "not found"}
    except Exception:
        return 503, {"error": "worker registry unavailable"}
    return 200, {}


def _handle_drain(scope: str, worker_id: str, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        backend.mark_draining(scope, worker_id)
    except KeyError:
        return 404, {"error": "not found"}
    except Exception:
        return 503, {"error": "worker registry unavailable"}
    return 200, {}


def _handle_recover_expired(body: dict, scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    try:
        recovered = backend.recover_expired(scope, now=now)
    except Exception:
        return 503, {"error": "worker registry unavailable"}
    if not isinstance(recovered, list):
        return 503, {"error": "worker registry unavailable"}
    return 200, {"recovered": recovered}


def _handle_list(scope: str, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        workers = backend.list_workers(scope)
    except Exception:
        return 503, {"error": "worker registry unavailable"}
    if not isinstance(workers, list):
        return 503, {"error": "worker registry unavailable"}
    return 200, {"workers": workers}
