"""Server-side handler for /memory curated-memory control-plane API (M12B).

Contract mirrors HttpMemoryBackend in agent/runtime_memory_http.py:
  GET  /memory?target=<target>&context=<json> -> {"content": str | null}
  POST /memory with body {context, target, content, action} -> {}

Scope key: JSON of the 8 fixed client fields from HttpMemoryBackend._scope_payload
  (mode, org_id, workspace_id, project_id, external_channel_id,
   external_thread_id, conversation_id, user_id) — None preserved.
Empty context {} is accepted and treated as all-None scope.
Auth: when token is non-None, Authorization: Bearer <token> required before body parse.
Storage: durable SQLite (UPSERT = full-snapshot overwrite).
Safety: no raw content, tokens, context bodies, paths, or backend exception text in errors.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from typing import Any

_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "project_id",
    "external_channel_id",
    "external_thread_id",
    "conversation_id",
    "user_id",
)


class SQLiteCuratedMemoryStore:
    """Durable SQLite store for curated memory snapshots.

    Keyed by (scope, target) where scope is a canonical JSON string of the
    8 fixed client context fields. Writes are full-snapshot overwrites.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS curated_memory ("
                "scope TEXT NOT NULL, "
                "target TEXT NOT NULL, "
                "content TEXT NOT NULL, "
                "PRIMARY KEY (scope, target)"
                ")"
            )
            conn.commit()

    def read(self, scope: str, target: str) -> str | None:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT content FROM curated_memory WHERE scope = ? AND target = ?",
                (scope, target),
            ).fetchone()
            return row[0] if row else None

    def write(self, scope: str, target: str, content: str) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO curated_memory (scope, target, content) VALUES (?, ?, ?)",
                (scope, target, content),
            )
            conn.commit()


def _scope_key(context: dict[str, Any]) -> str:
    return json.dumps(
        {k: context.get(k) for k in _SCOPE_FIELDS},
        separators=(",", ":"),
    )


def handle_curated_memory_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    """Dispatch one /memory request and return (status, response_dict)."""
    if path != "/memory":
        return 404, {"error": "not found"}

    if token:
        if auth_header != f"Bearer {token}":
            return 401, {"error": "unauthorized"}

    if method == "GET":
        return _handle_get(query_string, backend)
    if method == "POST":
        return _handle_post(body_bytes, backend)
    return 405, {"error": "method not allowed"}


def _handle_get(query_string: str, backend: Any) -> tuple[int, dict[str, Any]]:
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    target = params.get("target", ["memory"])[0]

    context_raw = params.get("context", ["{}"])[0]
    try:
        context = json.loads(context_raw)
    except json.JSONDecodeError:
        return 400, {"error": "invalid context JSON"}

    if not isinstance(context, dict):
        return 400, {"error": "context must be an object"}

    scope = _scope_key(context)
    try:
        content = backend.read(scope, target)
    except Exception:
        return 503, {"error": "memory backend unavailable"}

    return 200, {"content": content}


def _handle_post(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return 400, {"error": "invalid JSON body"}

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    context = body.get("context", {})
    if not isinstance(context, dict):
        return 400, {"error": "context must be an object"}

    target = body.get("target", "memory")
    if not isinstance(target, str):
        return 400, {"error": "target must be a string"}

    content = body.get("content")
    if content is None:
        return 400, {"error": "content is required"}
    if not isinstance(content, str):
        return 400, {"error": "content must be a string"}

    scope = _scope_key(context)
    try:
        backend.write(scope, target, content)
    except Exception:
        return 503, {"error": "memory backend unavailable"}

    return 200, {}
