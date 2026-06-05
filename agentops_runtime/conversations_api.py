"""Server-side handler for /conversations/* control-plane API (M12B).

Routes (all POST):
    POST /conversations/resolve             body {context, event}  -> {conversation_id}
    POST /conversations/<id>/active-run     body {context}         -> {run_id: str|null}
    POST /conversations/<id>/route          body {context, turn}   -> {run_id}

Auth: when token is non-None, Authorization: Bearer *** is required before body read.
Context: scope dict from HttpConversationRouter._scope_payload(); run_id and job_id excluded.
Errors: sanitized; no raw event/turn payloads, tokens, DB paths, or backend exception text.
Storage: durable SQLite with WAL mode; route_turn is atomic get-or-create.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
import uuid
from typing import Any

_CONVERSATION_SCOPE_FIELDS = (
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

_RESOLVE_SCOPE_FIELDS = tuple(f for f in _CONVERSATION_SCOPE_FIELDS if f != "conversation_id")

_DEFAULT_SCOPE = json.dumps([None] * len(_CONVERSATION_SCOPE_FIELDS), separators=(",", ":"))

_VALID_ACTIONS = frozenset({"active-run", "route"})


class SQLiteConversationRouterBackend:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS active_runs ("
                "scope TEXT NOT NULL, "
                "conversation_id TEXT NOT NULL, "
                "run_id TEXT NOT NULL, "
                "PRIMARY KEY (scope, conversation_id)"
                ")"
            )
            conn.commit()

    def resolve_conversation(self, context: dict | None, event: dict | None) -> str:
        ctx = context or {}
        conv_id = ctx.get("conversation_id")
        if conv_id and isinstance(conv_id, str):
            return conv_id
        ev = event or {}
        ev_conv = ev.get("conversation_id") or ev.get("thread_id")
        if ev_conv and isinstance(ev_conv, str):
            return ev_conv
        return _derive_conversation_id(ctx)

    def get_active_run(self, context: dict | None, conversation_id: str) -> str | None:
        scope = _scope_string(context)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM active_runs WHERE scope=? AND conversation_id=?",
                (scope, conversation_id),
            ).fetchone()
        return row[0] if row else None

    def route_turn(self, context: dict | None, conversation_id: str, turn: dict | None) -> str:
        scope = _scope_string(context)
        new_run_id = str(uuid.uuid4())
        with sqlite3.connect(self._db_path, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT OR IGNORE INTO active_runs (scope, conversation_id, run_id) VALUES (?, ?, ?)",
                (scope, conversation_id, new_run_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT run_id FROM active_runs WHERE scope=? AND conversation_id=?",
                (scope, conversation_id),
            ).fetchone()
        return row[0]


def _scope_string(context: dict | None) -> str:
    if context is None:
        return _DEFAULT_SCOPE
    values = [context.get(f) for f in _CONVERSATION_SCOPE_FIELDS]
    return json.dumps(values, separators=(",", ":"))


def _derive_conversation_id(context: dict) -> str:
    values = [context.get(f) for f in _RESOLVE_SCOPE_FIELDS]
    canonical = json.dumps(values, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"conv-{digest[:32]}"


def _parse_conversation_route(path: str) -> tuple[str | None, str | None, str | None]:
    if ";" in path:
        return None, None, "rejected"
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "" and parts[1] == "conversations" and parts[2] == "resolve":
        return "resolve", None, None
    if len(parts) == 4 and parts[0] == "" and parts[1] == "conversations" and parts[3] in _VALID_ACTIONS:
        try:
            conv_id = urllib.parse.unquote(parts[2])
        except Exception:
            return None, None, "invalid id"
        if not conv_id:
            return None, None, "invalid id"
        return parts[3], conv_id, None
    return None, None, "not found"


def is_conversation_route(path: str) -> bool:
    _endpoint, _conv_id, error = _parse_conversation_route(path)
    return error is None


def _parse_body(body_bytes: bytes) -> tuple[dict | None, tuple[int, dict] | None]:
    if not body_bytes:
        return None, (400, {"error": "invalid request"})
    try:
        body = json.loads(body_bytes)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None, (400, {"error": "invalid request"})
    if not isinstance(body, dict):
        return None, (400, {"error": "invalid request"})
    return body, None


def _parse_context(raw: Any) -> tuple[dict | None, str | None]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return None, None
    if not isinstance(raw, dict):
        return None, "invalid context"
    filtered: dict[str, Any] = {}
    for field in _CONVERSATION_SCOPE_FIELDS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            return None, "invalid context"
        filtered[field] = value
    return filtered, None


def handle_conversation_request(
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

    endpoint, conv_id, route_error = _parse_conversation_route(path)
    if route_error:
        return 404, {"error": "not found"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    body, body_err = _parse_body(body_bytes)
    if body_err is not None:
        return body_err
    assert body is not None

    ctx_raw = body.get("context")
    ctx, ctx_err = _parse_context(ctx_raw)
    if ctx_err:
        return 400, {"error": "invalid context"}

    if endpoint == "resolve":
        return _handle_resolve(body, ctx, backend)
    if endpoint == "active-run":
        assert conv_id is not None
        return _handle_active_run(ctx, conv_id, backend)
    assert conv_id is not None
    return _handle_route(body, ctx, conv_id, backend)


def _handle_resolve(body: dict, ctx: dict | None, backend: Any) -> tuple[int, dict[str, Any]]:
    event_raw = body.get("event") or {}
    if not isinstance(event_raw, dict):
        event_raw = {}
    event: dict[str, Any] = {}
    for key in ("conversation_id", "thread_id"):
        val = event_raw.get(key)
        if isinstance(val, str):
            event[key] = val
    try:
        conv_id = backend.resolve_conversation(ctx, event)
    except Exception:
        return 503, {"error": "conversation router unavailable"}
    if not isinstance(conv_id, str) or not conv_id:
        return 503, {"error": "conversation router unavailable"}
    return 200, {"conversation_id": conv_id}


def _handle_active_run(ctx: dict | None, conv_id: str, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        run_id = backend.get_active_run(ctx, conv_id)
    except Exception:
        return 503, {"error": "conversation router unavailable"}
    return 200, {"run_id": run_id}


def _handle_route(body: dict, ctx: dict | None, conv_id: str, backend: Any) -> tuple[int, dict[str, Any]]:
    turn_raw = body.get("turn", {})
    if not isinstance(turn_raw, dict):
        return 400, {"error": "invalid request"}
    try:
        run_id = backend.route_turn(ctx, conv_id, turn_raw)
    except Exception:
        return 503, {"error": "conversation router unavailable"}
    if not isinstance(run_id, str) or not run_id:
        return 503, {"error": "conversation router unavailable"}
    return 200, {"run_id": run_id}
