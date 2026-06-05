"""Server-side handler for /skills control-plane API (M12B).

Routes (all POST):
    POST /skills/list   body {context, category?}            -> {skills: [...]}
    POST /skills/view   body {context, name, file_path?, preprocess?} -> {...}
    POST /skills/manage body {context, action, name, ...}    -> {...}

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: reconstructed into RuntimeContext from the scope payload sent by
    HttpSkillBackend._scope_payload(). skill_write_approved is extracted from
    the top-level scope dict and injected into RuntimeContext.metadata so that
    ScopedSkillBackend._metadata_approved() correctly gates shared writes.
Storage: durable SQLite at AGENTOPS_SKILL_DB_PATH (required by compose_services).
Safety: raw skill names, content, file_path, tokens, DB paths, and backend
    exception text never appear in error payloads or logs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from agent.runtime_context import RuntimeContext
from agent.runtime_skills import ScopedSkillBackend

_VALID_PATHS = frozenset({"/skills/list", "/skills/view", "/skills/manage"})


class SQLiteScopedSkillStore:
    """Durable SQLite-backed skill store implementing the SkillBackend contract.

    For reads, all rows are loaded into a transient ScopedSkillBackend and the
    native read surface is delegated.  For manage, a per-instance lock serializes
    load → mutate → replace-all so that native policy (precedence, visibility,
    approval, bundled-read-only) is enforced without re-implementing it.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS skill_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, "
                "scope TEXT NOT NULL, "
                "content TEXT NOT NULL DEFAULT '', "
                "description TEXT NOT NULL DEFAULT '', "
                "category TEXT, "
                "files TEXT NOT NULL DEFAULT '{}', "
                "org_id TEXT, "
                "workspace_id TEXT, "
                "project_id TEXT, "
                "user_id TEXT, "
                "conversation_id TEXT, "
                "pinned INTEGER NOT NULL DEFAULT 0, "
                "readiness_status TEXT NOT NULL DEFAULT 'available', "
                "setup_needed INTEGER NOT NULL DEFAULT 0, "
                "tags TEXT NOT NULL DEFAULT '[]', "
                "required_environment_variables TEXT NOT NULL DEFAULT '[]'"
                ")"
            )
            conn.commit()

    def _load_all_records(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT name, scope, content, description, category, "
            "files, org_id, workspace_id, project_id, user_id, "
            "conversation_id, pinned, readiness_status, setup_needed, "
            "tags, required_environment_variables "
            "FROM skill_records"
        ).fetchall()
        records = []
        for row in rows:
            records.append({
                "name": row[0],
                "scope": row[1],
                "content": row[2],
                "description": row[3],
                "category": row[4],
                "files": json.loads(row[5] or "{}"),
                "org_id": row[6],
                "workspace_id": row[7],
                "project_id": row[8],
                "user_id": row[9],
                "conversation_id": row[10],
                "pinned": bool(row[11]),
                "readiness_status": row[12] or "available",
                "setup_needed": bool(row[13]),
                "tags": json.loads(row[14] or "[]"),
                "required_environment_variables": json.loads(row[15] or "[]"),
            })
        return records

    def _save_all_records(self, conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM skill_records")
        for rec in records:
            conn.execute(
                "INSERT INTO skill_records "
                "(name, scope, content, description, category, files, org_id, workspace_id, "
                "project_id, user_id, conversation_id, pinned, readiness_status, setup_needed, "
                "tags, required_environment_variables) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec["name"],
                    rec["scope"],
                    rec.get("content", ""),
                    rec.get("description", ""),
                    rec.get("category"),
                    json.dumps(rec.get("files", {})),
                    rec.get("org_id"),
                    rec.get("workspace_id"),
                    rec.get("project_id"),
                    rec.get("user_id"),
                    rec.get("conversation_id"),
                    int(bool(rec.get("pinned", False))),
                    rec.get("readiness_status", "available"),
                    int(bool(rec.get("setup_needed", False))),
                    json.dumps(rec.get("tags", [])),
                    json.dumps(rec.get("required_environment_variables", [])),
                ),
            )
        conn.commit()

    def _build_backend(self, records: list[dict[str, Any]]) -> ScopedSkillBackend:
        backend = ScopedSkillBackend()
        for rec in records:
            backend.add_skill(**{k: v for k, v in rec.items()})
        return backend

    def seed_records(self, records: list[dict[str, Any]]) -> None:
        """Replace all stored records (used for bootstrapping bundled skills)."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                self._save_all_records(conn, records)

    def list_skills(
        self,
        context: RuntimeContext | None,
        *,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            records = self._load_all_records(conn)
        return self._build_backend(records).list_skills(context, category=category)

    def load_skill(
        self,
        context: RuntimeContext | None,
        name: str,
        *,
        file_path: str | None = None,
        preprocess: bool = True,
    ) -> dict[str, Any]:
        with sqlite3.connect(self._db_path) as conn:
            records = self._load_all_records(conn)
        return self._build_backend(records).load_skill(
            context,
            name,
            file_path=file_path,
            preprocess=preprocess,
        )

    def manage_skill(
        self,
        context: RuntimeContext | None,
        *,
        action: str,
        name: str,
        **fields: Any,
    ) -> dict[str, Any]:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                records = self._load_all_records(conn)
                backend = self._build_backend(records)
                result = backend.manage_skill(context, action=action, name=name, **fields)
                new_records = backend.export_records()
                self._save_all_records(conn, new_records)
        return result


def _parse_skill_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "context must be an object"

    mapping = dict(raw)
    skill_write_approved = mapping.pop("skill_write_approved", None)

    if skill_write_approved is not None:
        if not isinstance(skill_write_approved, bool):
            return None, "skill_write_approved must be a boolean"
        existing_meta = mapping.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            return None, "invalid context"
        nested_approval = existing_meta.get("skill_write_approved")
        if nested_approval is not None and not isinstance(nested_approval, bool):
            return None, "skill_write_approved must be a boolean"
        meta = dict(existing_meta)
        meta["skill_write_approved"] = skill_write_approved
        mapping["metadata"] = meta
    else:
        existing_meta = mapping.get("metadata")
        if isinstance(existing_meta, dict):
            nested_approval = existing_meta.get("skill_write_approved")
            if nested_approval is not None and not isinstance(nested_approval, bool):
                return None, "skill_write_approved must be a boolean"

    try:
        ctx = RuntimeContext.from_mapping(mapping)
    except (ValueError, TypeError):
        return None, "invalid context"

    return ctx, None


def _sanitize_failure_result(result: Any) -> dict[str, Any]:
    """Return a client-safe failure payload from a native skill backend result."""
    payload = dict(result) if isinstance(result, dict) else {"success": False}
    if payload.get("success") is not False:
        return payload
    sanitized: dict[str, Any] = {
        "success": False,
        "error": "skill operation failed",
    }
    policy = payload.get("policy")
    if isinstance(policy, str):
        sanitized["policy"] = policy
    return sanitized


def handle_skill_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    if path not in _VALID_PATHS:
        return 404, {"error": "not found"}

    if token:
        if auth_header != f"Bearer {token}":
            return 401, {"error": "unauthorized"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    if not body_bytes:
        return 400, {"error": "request body is required"}

    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON body"}

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    ctx, ctx_err = _parse_skill_context(body.get("context"))
    if ctx_err:
        return 400, {"error": ctx_err}

    if path == "/skills/list":
        return _list(body, ctx, backend)
    if path == "/skills/view":
        return _view(body, ctx, backend)
    return _manage(body, ctx, backend)


def _list(body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    category = body.get("category")
    if category is not None and not isinstance(category, str):
        return 400, {"error": "category must be a string"}
    try:
        skills = backend.list_skills(ctx, category=category)
    except Exception:
        return 503, {"error": "skill backend unavailable"}
    return 200, {"skills": skills}


def _view(body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return 400, {"error": "name is required"}

    file_path = body.get("file_path")
    preprocess = bool(body.get("preprocess", True))

    kwargs: dict[str, Any] = {}
    if file_path is not None:
        kwargs["file_path"] = file_path

    try:
        result = backend.load_skill(ctx, name, preprocess=preprocess, **kwargs)
    except Exception:
        return 503, {"error": "skill backend unavailable"}

    if result is None:
        return 200, {"success": False, "error": "Skill not found."}
    if isinstance(result, dict) and result.get("success") is False:
        return 200, _sanitize_failure_result(result)
    return 200, dict(result)


def _manage(body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    action = body.get("action")
    if not isinstance(action, str) or not action:
        return 400, {"error": "action is required"}

    name = body.get("name")
    if not isinstance(name, str) or not name:
        return 400, {"error": "name is required"}

    fields = {k: v for k, v in body.items() if k not in ("context", "action", "name")}
    if "allow_shared_write" in fields and not isinstance(fields["allow_shared_write"], bool):
        return 400, {"error": "allow_shared_write must be a boolean"}

    try:
        result = backend.manage_skill(ctx, action=action, name=name, **fields)
    except Exception:
        return 503, {"error": "skill backend unavailable"}

    if isinstance(result, dict) and result.get("success") is False:
        return 200, _sanitize_failure_result(result)
    return 200, dict(result)
