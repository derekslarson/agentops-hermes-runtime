"""Server-side handler for POST /delivery/deliver (M12B).

Contract:
    POST /delivery/deliver  body {context?, message}  -> 200 {}

Auth: when token is non-None, Authorization: Bearer *** is required before body read.
Context: optional dict; missing, empty, and all-None values are accepted.
Errors: sanitized; no raw exception text, backend paths, or credential material.
Storage: append-only SQLite outbox at the path provided to SQLiteDeliveryBackend.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_EXACT_PATH = "/delivery/deliver"
_SCOPE_FIELDS = (
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


class SQLiteDeliveryBackend:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "message_json TEXT NOT NULL, "
                "context_json TEXT NOT NULL"
                ")"
            )
            conn.commit()

    def deliver(self, context: Any, message: Any) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO deliveries (message_json, context_json) VALUES (?, ?)",
                (json.dumps(message), json.dumps(context or {})),
            )
            conn.commit()

    def all_rows(self) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute("SELECT id, message_json, context_json FROM deliveries").fetchall()

    def count(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]


def _normalize_context(raw_context: Any) -> tuple[dict[str, str | None], dict[str, Any] | None]:
    if raw_context is None:
        return {}, None
    if not isinstance(raw_context, dict):
        return {}, {"error": "invalid context"}

    normalized: dict[str, str | None] = {}
    for field in _SCOPE_FIELDS:
        if field not in raw_context:
            continue
        value = raw_context[field]
        if value is not None and not isinstance(value, str):
            return {}, {"error": "invalid context"}
        normalized[field] = value
    return normalized, None


def is_delivery_route(path: str) -> bool:
    return path == _EXACT_PATH


def handle_delivery_request(
    *,
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    if path != _EXACT_PATH:
        return 404, {"error": "not found"}
    if method != "POST":
        return 405, {"error": "method not allowed"}
    if token and auth_header != f"Bearer {token}":
        return 401, {"error": "unauthorized"}

    if not body_bytes:
        return 400, {"error": "empty body"}

    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return 400, {"error": "invalid JSON"}

    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}

    context, context_error = _normalize_context(body.get("context"))
    if context_error is not None:
        return 400, context_error

    if "message" not in body:
        return 400, {"error": "missing required field: message"}

    message = body["message"]
    if not isinstance(message, dict):
        return 400, {"error": "message must be a JSON object"}

    try:
        backend.deliver(context, message)
    except Exception:
        return 503, {"error": "delivery backend unavailable"}

    return 200, {}
