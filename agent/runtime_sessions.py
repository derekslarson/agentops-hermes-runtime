"""Session backend adapters for AgentOps runtime contexts.

This module keeps Hermes' native session database as the local compatibility
implementation while exposing the worker-safe operations the distributed runtime
needs: scoped transcript append/read/search, resume lineage, and per-conversation
turn locking. Remote implementations can satisfy the same methods without the
native callers changing their surface.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from agent.runtime_context import RuntimeContext
from hermes_state import SessionDB


class LocalSQLiteSessionBackend:
    """Runtime session backend backed by the existing Hermes SessionDB.

    Scope mapping is intentionally conservative for M6:
    * ``RuntimeContext`` scope is encoded into AgentOps session ids when present.
    * session rows carry ``source`` and ``user_id`` from the active runtime scope.
    * message search is filtered back to the scoped session id to prevent local
      multi-run callers from seeing another conversation's transcript.

    The backing ``SessionDB`` already owns WAL setup, write retries, FTS indexes,
    message encoding/decoding, token counters, and compression lineage. This
    adapter wraps those native semantics instead of creating a sidecar transcript
    store.
    """

    def __init__(self, db_path: str | Path | None = None, db: SessionDB | None = None) -> None:
        if db is not None:
            self._db = db
        else:
            self._db = SessionDB(db_path=Path(db_path) if db_path is not None else None)
        self._ensure_turn_lock_table()

    def close(self) -> None:
        close = getattr(self._db, "close", None)
        if callable(close):
            close()

    def create_session(
        self,
        context: RuntimeContext | None,
        *,
        session_id: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
    ) -> str:
        resolved_session_id = self._session_id(context, session_id)
        resolved_source = source or self._source(context)
        self._db.create_session(
            resolved_session_id,
            resolved_source,
            model=model,
            model_config=dict(model_config) if model_config else None,
            system_prompt=system_prompt,
            user_id=user_id or (context.user_id if context else None),
            parent_session_id=parent_session_id,
        )
        return resolved_session_id

    def read_session(self, context: RuntimeContext | None, *, session_id: str | None = None) -> dict[str, Any] | None:
        return self._db.get_session(self._session_id(context, session_id))

    def end_session(self, context: RuntimeContext | None, end_reason: str, *, session_id: str | None = None) -> None:
        self._db.end_session(self._session_id(context, session_id), end_reason)

    def append_message(self, context: RuntimeContext | None, message: Mapping[str, Any]) -> int:
        session_id = self._session_id(context, str(message.get("session_id")) if message.get("session_id") else None)
        if self._db.get_session(session_id) is None:
            self.create_session(context, session_id=session_id)
        return self._db.append_message(
            session_id=session_id,
            role=str(message.get("role") or "unknown"),
            content=message.get("content"),
            tool_name=message.get("tool_name"),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
            token_count=message.get("token_count"),
            finish_reason=message.get("finish_reason"),
            reasoning=message.get("reasoning"),
            reasoning_content=message.get("reasoning_content"),
            reasoning_details=message.get("reasoning_details"),
            codex_reasoning_items=message.get("codex_reasoning_items"),
            codex_message_items=message.get("codex_message_items"),
            platform_message_id=message.get("platform_message_id") or message.get("message_id"),
            observed=bool(message.get("observed", False)),
        )

    def read_messages(self, context: RuntimeContext | None, *, session_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        messages = self._db.get_messages(self._session_id(context, session_id))
        if limit is None:
            return messages
        if limit <= 0:
            return []
        return messages[-limit:]

    def append(self, context: RuntimeContext | None, event: Mapping[str, Any]) -> None:
        """SessionBackend compatibility: append a transcript/event mapping."""
        if "role" in event or "content" in event:
            self.append_message(context, event)
            return
        self.append_message(context, {"role": "event", "content": json.dumps(dict(event), sort_keys=True)})

    def read(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[Any]:
        return self.read_messages(context, limit=limit)

    def search(self, context: RuntimeContext | None, query: str) -> list[Any]:
        if not query or not query.strip():
            return []
        session_id = self._session_id(context)
        # Always scan the scoped transcript as the source of truth. Global FTS
        # results are intentionally not trusted for authorization because they
        # cannot be tenant-filtered before ranking/limit in the legacy API.
        lowered = query.lower()
        scoped_messages = [
            message for message in self.read_messages(context) if lowered in str(message.get("content", "")).lower()
        ]
        if scoped_messages:
            return scoped_messages
        hits = self._db.search_messages(query, limit=100)
        scoped = [hit for hit in hits if hit.get("session_id") == session_id]
        if scoped and all("content" in hit for hit in scoped):
            return scoped
        # FTS may be unavailable or may omit raw content in reduced test/runtime
        # builds. Fall back to an empty scoped result rather than leaking global
        # matches from a different tenant/session.
        return []

    def resolve_resume_session_id(self, session_id: str) -> str:
        return self._db.resolve_resume_session_id(session_id)

    def claim_turn_lock(self, context: RuntimeContext | None, *, owner: str, ttl_seconds: float = 300.0) -> bool:
        session_id = self._session_id(context)
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            conn.execute("DELETE FROM agentops_session_turn_locks WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT OR IGNORE INTO agentops_session_turn_locks "
                "(session_id, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (session_id, owner, now, expires_at),
            )
            row = conn.execute(
                "SELECT owner FROM agentops_session_turn_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None and row["owner"] == owner

        return bool(self._db._execute_write(_do))

    def renew_turn_lock(self, context: RuntimeContext | None, *, owner: str, ttl_seconds: float = 300.0) -> bool:
        session_id = self._session_id(context)
        now = time.time()
        expires_at = now + ttl_seconds

        def _do(conn):
            conn.execute("DELETE FROM agentops_session_turn_locks WHERE expires_at < ?", (now,))
            cursor = conn.execute(
                "UPDATE agentops_session_turn_locks SET expires_at = ? "
                "WHERE session_id = ? AND owner = ? AND expires_at >= ?",
                (expires_at, session_id, owner, now),
            )
            return cursor.rowcount == 1

        return bool(self._db._execute_write(_do))

    def release_turn_lock(self, context: RuntimeContext | None, *, owner: str) -> None:
        session_id = self._session_id(context)

        def _do(conn):
            conn.execute(
                "DELETE FROM agentops_session_turn_locks WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            )

        self._db._execute_write(_do)

    def _ensure_turn_lock_table(self) -> None:
        def _do(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agentops_session_turn_locks ("
                "session_id TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, "
                "acquired_at REAL NOT NULL, "
                "expires_at REAL NOT NULL)"
            )

        self._db._execute_write(_do)

    @staticmethod
    def _session_id(context: RuntimeContext | None, raw_session_id: str | None = None) -> str:
        raw = raw_session_id or (
            (context.conversation_id or context.parent_session_id or context.run_id) if context is not None else None
        ) or "local"
        if context is None:
            return str(raw)
        scope_parts = [
            context.mode,
            context.org_id,
            context.workspace_id,
            context.workspace_type,
            context.user_id,
            context.conversation_id,
            context.external_channel_id,
            context.external_thread_id,
            context.agent_profile_id,
            context.project_id,
        ]
        scope = "\x1f".join(str(part or "") for part in scope_parts)
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        raw_text = str(raw)
        if raw_text.startswith(f"agentops-{digest}-"):
            return raw_text
        return f"agentops-{digest}-{raw_text}"

    @staticmethod
    def _source(context: RuntimeContext | None) -> str:
        if context is None:
            return "agentops-local"
        return context.workspace_type or context.mode or "agentops"
