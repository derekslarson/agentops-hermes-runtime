"""Server-side handler for /queue/* control-plane API (M12B).

Routes (all POST):
    POST /queue/enqueue      body {context, payload, idempotency_key?} -> {receipt: str}
    POST /queue/claim        body {context, capability?}               -> {item: dict|null}
    POST /queue/ack          body {context, receipt}                   -> {}
    POST /queue/nack         body {context, receipt, requeue}          -> {}
    POST /queue/extend-lease body {context, receipt, seconds}          -> {}

Auth: when token is non-None, Authorization: Bearer <token> is required before body read.
Context: scope dict from HttpQueueBackend._scope_payload() in agent/runtime_queue_http.py.
Receipts: in JSON API bodies only; never in URLs, query strings, logs, or errors.
Errors: sanitized; no raw receipts, payloads, tokens, request bodies, DB paths, or backend exception text.
Storage: durable SQLite at AGENTOPS_QUEUE_DB_PATH (required, no fallback).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

_VALID_PATHS = frozenset({
    "/queue/enqueue",
    "/queue/claim",
    "/queue/ack",
    "/queue/nack",
    "/queue/extend-lease",
})
_LEASE_DEFAULT_SECONDS = 30
_QUEUE_SCOPE_FIELDS = (
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
    "run_id",
    "job_id",
)


class SQLiteQueueBackend:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS queue_items ("
                "receipt TEXT PRIMARY KEY, "
                "context_scope TEXT NOT NULL, "
                "payload TEXT NOT NULL, "
                "idempotency_key TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "lease_expires_at REAL, "
                "created_at REAL NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency "
                "ON queue_items(context_scope, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            conn.commit()

    def enqueue(self, context_scope: str, payload: dict, *, idempotency_key: str | None = None) -> str:
        receipt = str(uuid.uuid4())
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO queue_items "
                    "(receipt, context_scope, payload, idempotency_key, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (receipt, context_scope, json.dumps(payload), idempotency_key, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                if idempotency_key is None:
                    raise
                row = conn.execute(
                    "SELECT receipt FROM queue_items WHERE context_scope=? AND idempotency_key=?",
                    (context_scope, idempotency_key),
                ).fetchone()
                if row:
                    return row[0]
                raise
        return receipt

    def claim(self, context_scope: str) -> dict | None:
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT receipt, payload FROM queue_items "
                "WHERE context_scope=? AND "
                "(status='pending' OR (status='claimed' AND lease_expires_at < ?)) "
                "ORDER BY created_at LIMIT 1",
                (context_scope, now),
            ).fetchone()
            if not row:
                return None
            receipt, payload_json = row
            cursor = conn.execute(
                "UPDATE queue_items SET status='claimed', lease_expires_at=? "
                "WHERE receipt=? AND context_scope=? AND "
                "(status='pending' OR (status='claimed' AND lease_expires_at < ?))",
                (now + _LEASE_DEFAULT_SECONDS, receipt, context_scope, now),
            )
            if cursor.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
        return {"receipt": receipt, "payload": json.loads(payload_json)}

    def ack(self, context_scope: str, receipt: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE queue_items SET status='acked' WHERE receipt=? AND context_scope=?",
                (receipt, context_scope),
            )
            conn.commit()

    def nack(self, context_scope: str, receipt: str, *, requeue: bool) -> None:
        status = "pending" if requeue else "discarded"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE queue_items SET status=?, lease_expires_at=NULL "
                "WHERE receipt=? AND context_scope=?",
                (status, receipt, context_scope),
            )
            conn.commit()

    def extend_lease(self, context_scope: str, receipt: str, *, seconds: int) -> None:
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE queue_items SET lease_expires_at=? "
                "WHERE receipt=? AND context_scope=? AND status='claimed'",
                (now + seconds, receipt, context_scope),
            )
            conn.commit()


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


def _parse_context_scope(raw: Any) -> tuple[str | None, str | None]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return None, None
    if not isinstance(raw, dict):
        return None, "invalid context"
    org_id = raw.get("org_id")
    workspace_id = raw.get("workspace_id")
    if not isinstance(org_id, str) or not org_id.strip():
        return None, "invalid context"
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None, "invalid context"
    scope_values: list[str | None] = []
    for field in _QUEUE_SCOPE_FIELDS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            return None, "invalid context"
        scope_values.append(value.strip() if field in {"org_id", "workspace_id"} and isinstance(value, str) else value)
    scope = json.dumps(scope_values, separators=(",", ":"))
    return scope, None


def handle_queue_request(
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

    body, err = _parse_body(body_bytes)
    if err is not None:
        return err

    if path == "/queue/enqueue":
        return _enqueue(body, backend)
    if path == "/queue/claim":
        return _claim(body, backend)
    if path == "/queue/ack":
        return _ack(body, backend)
    if path == "/queue/nack":
        return _nack(body, backend)
    return _extend_lease(body, backend)


def _enqueue(body: dict, backend: Any) -> tuple[int, dict[str, Any]]:
    ctx_raw = body.get("context")
    scope, ctx_err = _parse_context_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    if scope is None:
        return 400, {"error": "invalid context"}

    payload = body.get("payload")
    if not isinstance(payload, dict):
        return 400, {"error": "invalid request"}

    idempotency_key = body.get("idempotency_key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return 400, {"error": "invalid request"}

    try:
        receipt = backend.enqueue(scope, payload, idempotency_key=idempotency_key)
    except Exception:
        return 503, {"error": "queue backend unavailable"}

    return 200, {"receipt": receipt}


def _claim(body: dict, backend: Any) -> tuple[int, dict[str, Any]]:
    ctx_raw = body.get("context")
    scope, ctx_err = _parse_context_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    if scope is None:
        return 200, {"item": None}

    try:
        item = backend.claim(scope)
    except Exception:
        return 503, {"error": "queue backend unavailable"}

    return 200, {"item": item}


def _ack(body: dict, backend: Any) -> tuple[int, dict[str, Any]]:
    ctx_raw = body.get("context")
    scope, ctx_err = _parse_context_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    if scope is None:
        return 400, {"error": "invalid context"}

    receipt = body.get("receipt")
    if not isinstance(receipt, str) or not receipt:
        return 400, {"error": "invalid request"}

    try:
        backend.ack(scope, receipt)
    except Exception:
        return 503, {"error": "queue backend unavailable"}

    return 200, {}


def _nack(body: dict, backend: Any) -> tuple[int, dict[str, Any]]:
    ctx_raw = body.get("context")
    scope, ctx_err = _parse_context_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    if scope is None:
        return 400, {"error": "invalid context"}

    receipt = body.get("receipt")
    if not isinstance(receipt, str) or not receipt:
        return 400, {"error": "invalid request"}

    requeue = body.get("requeue")
    if not isinstance(requeue, bool):
        return 400, {"error": "invalid request"}

    try:
        backend.nack(scope, receipt, requeue=requeue)
    except Exception:
        return 503, {"error": "queue backend unavailable"}

    return 200, {}


def _extend_lease(body: dict, backend: Any) -> tuple[int, dict[str, Any]]:
    ctx_raw = body.get("context")
    scope, ctx_err = _parse_context_scope(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}
    if scope is None:
        return 400, {"error": "invalid context"}

    receipt = body.get("receipt")
    if not isinstance(receipt, str) or not receipt:
        return 400, {"error": "invalid request"}

    seconds = body.get("seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        return 400, {"error": "invalid request"}

    try:
        backend.extend_lease(scope, receipt, seconds=seconds)
    except Exception:
        return 503, {"error": "queue backend unavailable"}

    return 200, {}
