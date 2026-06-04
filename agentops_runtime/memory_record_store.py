"""Relational MemoryRecordBackend for durable scope-isolated deep-memory storage.

SQLite uses stdlib sqlite3 for local durable storage. PostgreSQL uses a lazy
psycopg2 import for Compose/cloud deployments; explicit remote configuration
fails closed if the dependency, server, schema, or query path is unavailable.

Scope isolation: every read/write predicate includes all seven scope columns
(mode, org_id, workspace_id, project_id, agent_profile_id, conversation_id,
user_id) derived from RuntimeContext. A record ingested under scope A is never
visible or fetchable under scope B even when B knows the record_id.

Search uses local BM25 ranking for SQLite and PostgreSQL full-text/vector SQL
for remote stores while preserving scoped filtering, bounded excerpts, and
positive-score deterministic result ordering.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse, urlunparse

from agent.local_memory.store import MemoryRecord, SearchResult, _bm25_scores, build_extracted_signal_lines, get_embedding_function as _get_chroma_embedding_function
from agent.runtime_context import RuntimeContext

_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "project_id",
    "agent_profile_id",
    "conversation_id",
    "user_id",
)
_EXCERPT_MAX = 512
EMBEDDING_DIM = 384


def _scope_values(context: RuntimeContext | None) -> tuple[str, ...]:
    if context is None:
        return ("",) * len(_SCOPE_FIELDS)
    return tuple(str(getattr(context, f) or "") for f in _SCOPE_FIELDS)


def _generate_record_id(text: str) -> str:
    return "mem_" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _make_excerpt(text: str) -> str:
    if len(text) <= _EXCERPT_MAX:
        return text
    return f"{text[: _EXCERPT_MAX - 3]}..."


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["record_id"],
        text=row["text"],
        text_hash=row["text_hash"],
        metadata=json.loads(row["metadata_json"] or "{}"),
        source=row["source"],
        source_uri=row["source_uri"],
        timestamp=row["ts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        record_kind=row["record_kind"],
        parent_id=row["parent_id"],
        provenance=json.loads(row["provenance_json"] or "{}"),
    )


def _dict_to_record(row: Mapping[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=str(row["record_id"]),
        text=str(row["text"]),
        text_hash=str(row["text_hash"]),
        metadata=dict(row.get("metadata") or {}),
        source=str(row.get("source") or "unknown"),
        source_uri=str(row.get("source_uri") or ""),
        timestamp=row.get("ts"),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        record_kind=str(row.get("record_kind") or "verbatim"),
        parent_id=row.get("parent_id"),
        provenance=dict(row.get("provenance") or {}),
    )


def _redact_db_url(db_url: str) -> str:
    parsed = urlparse(db_url)
    netloc = parsed.netloc
    if "@" in netloc:
        _userinfo, hostinfo = netloc.rsplit("@", 1)
        netloc = f"[REDACTED]@{hostinfo}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _coerce_mapping_row(row: Any, description: Any) -> Mapping[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row
    columns = [column[0] for column in (description or [])]
    return dict(zip(columns, row, strict=False))


def _safe_rollback(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if rollback is None:
        return
    try:
        rollback()
    except Exception:  # noqa: BLE001 - preserve sanitized outer error
        return


def _postgres_search_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(tokens) if tokens else query


def _load_default_embed_fn(device: str = "auto"):
    try:
        return _get_chroma_embedding_function(device)
    except Exception:
        raise RuntimeError(
            "Postgres deep-memory embedding requires chromadb+onnxruntime; "
            "install 'hermes-agent[deep-memory]' or supply embed_fn= at construction time"
        ) from None


def _pgvector_literal(vec) -> str:
    try:
        raw = list(vec)
    except Exception:
        raise ValueError("deep-memory embedding must be a sequence of numeric values") from None
    if len(raw) != EMBEDDING_DIM:
        raise ValueError(
            f"deep-memory embedding must be exactly {EMBEDDING_DIM} dimensions, got {len(raw)}"
        )
    try:
        values = [float(v) for v in raw]
    except Exception:
        raise ValueError("deep-memory embedding values must be numeric") from None
    return "[" + ",".join(str(v) for v in values) + "]"


_SCOPE_PRED = (
    "scope_mode=? AND scope_org_id=? AND scope_workspace_id=? AND "
    "scope_project_id=? AND scope_agent_profile_id=? AND "
    "scope_conversation_id=? AND scope_user_id=?"
)

_SIGNAL_BOOST_LADDER = (0.40, 0.25, 0.15, 0.08, 0.04)
_PG_OVERFETCH_FACTOR = 4
_PG_OVERFETCH_CAP = 100


def _pg_overfetch_limit(safe_limit: int) -> int:
    return min(safe_limit * _PG_OVERFETCH_FACTOR, _PG_OVERFETCH_CAP)


class RelationalMemoryRecordBackend:
    """Relational MemoryRecordBackend with full RuntimeContext scope isolation.

    db_url: "sqlite:///absolute/path.db" for SQLite local mode.
             "postgresql://..." or "postgres://..." for the live PostgreSQL
             adapter used by Compose/cloud profiles. Explicit remote URLs fail
             closed instead of falling back to local storage.
    """

    def __init__(self, db_url: str, *, embed_fn=None) -> None:
        self._embed_fn = embed_fn
        self._driver, self._db_url_or_path = self._resolve_database(db_url)
        self._local = threading.local()
        if self._driver == "postgres":
            self._init_postgres()
        else:
            self._init_db()

    def _compute_pg_embedding(self, text: str) -> str | None:
        embed_fn = self._embed_fn
        if embed_fn is None:
            try:
                embed_fn = _load_default_embed_fn()
            except Exception:
                raise RuntimeError(
                    "Postgres deep-memory embedding unavailable; "
                    "install 'hermes-agent[deep-memory]' or supply embed_fn= at construction time"
                ) from None
        try:
            vectors = embed_fn([text])
        except Exception:
            raise RuntimeError("deep-memory embedding computation failed") from None
        try:
            vec = vectors[0]
        except Exception:
            raise RuntimeError("deep-memory embedding computation failed") from None
        try:
            return _pgvector_literal(vec)
        except ValueError:
            raise
        except Exception:
            raise RuntimeError("deep-memory embedding computation failed") from None

    def _resolve_database(self, db_url: str) -> tuple[str, str]:
        if not db_url:
            raise ValueError("deep-memory relational store requires a database URL")
        parsed = urlparse(db_url)
        if parsed.scheme in {"postgresql", "postgres"}:
            return "postgres", db_url
        if parsed.scheme == "sqlite":
            if not db_url.startswith("sqlite:///") or not parsed.path or parsed.path == "/":
                raise ValueError("deep-memory sqlite store requires sqlite:///absolute/path.db")
            return "sqlite", parsed.path
        if parsed.scheme:
            raise ValueError("unsupported deep-memory relational store URL scheme")
        raise ValueError("deep-memory relational store requires sqlite:///absolute/path.db")

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self._db_url_or_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _postgres_driver(self) -> Any:
        try:
            return __import__("psycopg2")
        except ImportError:
            raise RuntimeError(
                "PostgreSQL deep-memory store requires optional dependency psycopg2 "
                f"for {_redact_db_url(self._db_url_or_path)}"
            ) from None

    def _pg_conn(self) -> Any:
        if not getattr(self._local, "pg_conn", None):
            driver = self._postgres_driver()
            try:
                self._local.pg_conn = driver.connect(self._db_url_or_path)
            except Exception as exc:  # noqa: BLE001 - sanitize configuration failures
                raise RuntimeError(
                    "failed to initialize PostgreSQL deep-memory store "
                    f"for {_redact_db_url(self._db_url_or_path)}: {exc.__class__.__name__}"
                ) from None
        return self._local.pg_conn

    def _init_postgres(self) -> None:
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(_postgres_schema_sql())
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - fail closed without DSN leakage
            _safe_rollback(conn)
            raise RuntimeError(
                "failed to initialize PostgreSQL deep-memory schema "
                f"for {_redact_db_url(self._db_url_or_path)}: {exc.__class__.__name__}"
            ) from None

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_records (
                scope_mode TEXT NOT NULL DEFAULT '',
                scope_org_id TEXT NOT NULL DEFAULT '',
                scope_workspace_id TEXT NOT NULL DEFAULT '',
                scope_project_id TEXT NOT NULL DEFAULT '',
                scope_agent_profile_id TEXT NOT NULL DEFAULT '',
                scope_conversation_id TEXT NOT NULL DEFAULT '',
                scope_user_id TEXT NOT NULL DEFAULT '',
                record_id TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'unknown',
                source_uri TEXT NOT NULL DEFAULT '',
                ts REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                record_kind TEXT NOT NULL DEFAULT 'verbatim',
                parent_id TEXT,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (
                    scope_mode, scope_org_id, scope_workspace_id,
                    scope_project_id, scope_agent_profile_id,
                    scope_conversation_id, scope_user_id, record_id
                )
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_signals (
                scope_mode TEXT NOT NULL DEFAULT '',
                scope_org_id TEXT NOT NULL DEFAULT '',
                scope_workspace_id TEXT NOT NULL DEFAULT '',
                scope_project_id TEXT NOT NULL DEFAULT '',
                scope_agent_profile_id TEXT NOT NULL DEFAULT '',
                scope_conversation_id TEXT NOT NULL DEFAULT '',
                scope_user_id TEXT NOT NULL DEFAULT '',
                record_id TEXT NOT NULL,
                signal_line TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS memory_signals_scope_record_idx
               ON memory_signals (
                   scope_mode, scope_org_id, scope_workspace_id, scope_project_id,
                   scope_agent_profile_id, scope_conversation_id, scope_user_id,
                   record_id
               )"""
        )
        conn.commit()

    def _sqlite_signal_boosts(
        self,
        context: RuntimeContext | None,
        query: str,
        candidate_ids: list[str],
    ) -> dict[str, float]:
        if not candidate_ids:
            return {}
        try:
            scope = _scope_values(context)
            placeholders = ",".join("?" * len(candidate_ids))
            rows = self._conn().execute(
                f"SELECT record_id, signal_line FROM memory_signals "
                f"WHERE {_SCOPE_PRED} AND record_id IN ({placeholders})",
                (*scope, *candidate_ids),
            ).fetchall()
        except Exception:
            return {}
        signals_by_id: dict[str, list[str]] = {}
        for row in rows:
            rid = row["record_id"]
            if rid not in signals_by_id:
                signals_by_id[rid] = []
            signals_by_id[rid].append(row["signal_line"])
        if not signals_by_id:
            return {}
        best_by_id: list[tuple[float, str]] = []
        for rid, lines in signals_by_id.items():
            scores = _bm25_scores(query, lines)
            best = max((s for s in scores if s > 0), default=0.0)
            if best > 0:
                best_by_id.append((best, rid))
        if not best_by_id:
            return {}
        best_by_id.sort(key=lambda x: (-x[0], x[1]))
        return {
            rid: _SIGNAL_BOOST_LADDER[min(i, len(_SIGNAL_BOOST_LADDER) - 1)]
            for i, (_, rid) in enumerate(best_by_id)
        }

    def _signals_for_record(self, context: RuntimeContext | None, record_id: str) -> list[str]:
        scope = _scope_values(context)
        rows = self._conn().execute(
            f"SELECT signal_line FROM memory_signals WHERE {_SCOPE_PRED} AND record_id=?",
            (*scope, record_id),
        ).fetchall()
        return [row["signal_line"] for row in rows]

    def _ingest_signals(self, context: RuntimeContext | None, record_id: str, source_uri: str, text: str) -> None:
        try:
            lines = build_extracted_signal_lines(source_uri, [record_id], text)
        except Exception:
            return
        scope = _scope_values(context)
        conn = self._conn()
        try:
            conn.execute(
                f"DELETE FROM memory_signals WHERE {_SCOPE_PRED} AND record_id=?",
                (*scope, record_id),
            )
            conn.executemany(
                """INSERT INTO memory_signals (
                    scope_mode, scope_org_id, scope_workspace_id, scope_project_id,
                    scope_agent_profile_id, scope_conversation_id, scope_user_id,
                    record_id, signal_line
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(*scope, record_id, line) for line in lines],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            return

    def _pg_signal_boosts(
        self,
        context: RuntimeContext | None,
        query: str,
        candidate_ids: list[str],
    ) -> dict[str, float]:
        if not candidate_ids:
            return {}
        scope = _scope_values(context)
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT record_id, signal_line FROM memory_signals "
                    "WHERE scope_mode = %s AND scope_org_id = %s "
                    "AND scope_workspace_id = %s AND scope_project_id = %s "
                    "AND scope_agent_profile_id = %s AND scope_conversation_id = %s "
                    "AND scope_user_id = %s "
                    "AND record_id = ANY(%s)",
                    (*scope, candidate_ids),
                )
                rows = [
                    _coerce_mapping_row(r, getattr(cur, "description", None))
                    for r in cur.fetchall()
                ]
            conn.commit()
        except Exception:
            _safe_rollback(conn)
            return {}
        signals_by_id: dict[str, list[str]] = {}
        for row in rows:
            if row is None:
                continue
            rid = str(row["record_id"])
            line = row.get("signal_line")
            if line is not None:
                if rid not in signals_by_id:
                    signals_by_id[rid] = []
                signals_by_id[rid].append(str(line))
        if not signals_by_id:
            return {}
        best_by_id: list[tuple[float, str]] = []
        for rid, lines in signals_by_id.items():
            scores = _bm25_scores(query, lines)
            best = max((s for s in scores if s > 0), default=0.0)
            if best > 0:
                best_by_id.append((best, rid))
        if not best_by_id:
            return {}
        best_by_id.sort(key=lambda x: (-x[0], x[1]))
        return {
            rid: _SIGNAL_BOOST_LADDER[min(i, len(_SIGNAL_BOOST_LADDER) - 1)]
            for i, (_, rid) in enumerate(best_by_id)
        }

    def _pg_ingest_signals(self, context: RuntimeContext | None, record_id: str, source_uri: str, text: str) -> None:
        try:
            lines = build_extracted_signal_lines(source_uri, [record_id], text)
        except Exception:
            return
        scope = _scope_values(context)
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_signals WHERE "
                    "scope_mode=%s AND scope_org_id=%s AND scope_workspace_id=%s AND "
                    "scope_project_id=%s AND scope_agent_profile_id=%s AND "
                    "scope_conversation_id=%s AND scope_user_id=%s AND record_id=%s",
                    (*scope, record_id),
                )
                for line in lines:
                    cur.execute(
                        "INSERT INTO memory_signals ("
                        "scope_mode, scope_org_id, scope_workspace_id, scope_project_id, "
                        "scope_agent_profile_id, scope_conversation_id, scope_user_id, "
                        "record_id, signal_line"
                        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (*scope, record_id, line),
                    )
            conn.commit()
        except Exception:
            _safe_rollback(conn)
            return

    def _run_postgres_write(self, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = _coerce_mapping_row(cur.fetchone(), getattr(cur, "description", None)) if getattr(cur, "description", None) else None
            conn.commit()
            return row
        except Exception as exc:  # noqa: BLE001 - sanitize database/runtime failures
            _safe_rollback(conn)
            raise RuntimeError(
                "PostgreSQL deep-memory write failed "
                f"for {_redact_db_url(self._db_url_or_path)}: {exc.__class__.__name__}"
            ) from None

    def _fetchone_postgres(self, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = _coerce_mapping_row(cur.fetchone(), getattr(cur, "description", None))
            conn.commit()
            return row
        except Exception as exc:  # noqa: BLE001 - sanitize database/runtime failures
            _safe_rollback(conn)
            raise RuntimeError(
                "PostgreSQL deep-memory read failed "
                f"for {_redact_db_url(self._db_url_or_path)}: {exc.__class__.__name__}"
            ) from None

    def _fetchall_postgres(self, sql: str, params: tuple[Any, ...]) -> list[Mapping[str, Any]]:
        conn = self._pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [
                    row
                    for row in (_coerce_mapping_row(row, getattr(cur, "description", None)) for row in cur.fetchall())
                    if row is not None
                ]
            conn.commit()
            return rows
        except Exception as exc:  # noqa: BLE001 - sanitize database/runtime failures
            _safe_rollback(conn)
            raise RuntimeError(
                "PostgreSQL deep-memory read failed "
                f"for {_redact_db_url(self._db_url_or_path)}: {exc.__class__.__name__}"
            ) from None

    def _pg_upsert_record(
        self,
        context: RuntimeContext | None,
        *,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: float | None = None,
        record_kind: str = "verbatim",
        parent_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        record_id: str | None = None,
    ) -> MemoryRecord:
        scope = _scope_values(context)
        rid = record_id or _generate_record_id(text)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()
        metadata_dict = dict(metadata or {})
        provenance_dict = dict(provenance or {})
        stored_row = self._run_postgres_write(
            _postgres_upsert_sql(),
            (
                *scope,
                rid,
                text,
                text_hash,
                json.dumps(metadata_dict),
                json.dumps(provenance_dict),
                source,
                source_uri,
                timestamp,
                now,
                now,
                record_kind,
                parent_id,
                self._compute_pg_embedding(text),
            ),
        )
        self._pg_ingest_signals(context, rid, source_uri, text)
        if stored_row:
            return _dict_to_record(stored_row)
        return MemoryRecord(
            id=rid,
            text=text,
            text_hash=text_hash,
            metadata=metadata_dict,
            source=source,
            source_uri=source_uri,
            timestamp=timestamp,
            created_at=now,
            updated_at=now,
            record_kind=record_kind,
            parent_id=parent_id,
            provenance=provenance_dict,
        )

    def upsert_record(
        self,
        context: RuntimeContext | None,
        *,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: float | None = None,
        record_kind: str = "verbatim",
        parent_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        record_id: str | None = None,
    ) -> MemoryRecord:
        if self._driver == "postgres":
            return self._pg_upsert_record(
                context,
                text=text,
                metadata=metadata,
                source=source,
                source_uri=source_uri,
                timestamp=timestamp,
                record_kind=record_kind,
                parent_id=parent_id,
                provenance=provenance,
                record_id=record_id,
            )

        scope = _scope_values(context)
        rid = record_id or _generate_record_id(text)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()

        conn = self._conn()
        existing = conn.execute(
            f"SELECT created_at FROM memory_records WHERE {_SCOPE_PRED} AND record_id=?",
            (*scope, rid),
        ).fetchone()
        created_at = existing["created_at"] if existing else now

        conn.execute(
            """INSERT OR REPLACE INTO memory_records (
                scope_mode, scope_org_id, scope_workspace_id, scope_project_id,
                scope_agent_profile_id, scope_conversation_id, scope_user_id,
                record_id, text, text_hash, metadata_json, source, source_uri,
                ts, created_at, updated_at, record_kind, parent_id, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *scope,
                rid,
                text,
                text_hash,
                json.dumps(dict(metadata or {})),
                source,
                source_uri,
                timestamp,
                created_at,
                now,
                record_kind,
                parent_id,
                json.dumps(dict(provenance or {})),
            ),
        )
        conn.commit()
        self._ingest_signals(context, rid, source_uri, text)
        return MemoryRecord(
            id=rid,
            text=text,
            text_hash=text_hash,
            metadata=dict(metadata or {}),
            source=source,
            source_uri=source_uri,
            timestamp=timestamp,
            created_at=created_at,
            updated_at=now,
            record_kind=record_kind,
            parent_id=parent_id,
            provenance=dict(provenance or {}),
        )

    def get_record(self, context: RuntimeContext | None, record_id: str) -> MemoryRecord | None:
        scope = _scope_values(context)
        if self._driver == "postgres":
            row = self._fetchone_postgres(
                """
                SELECT *
                FROM memory_records
                WHERE scope_mode = %s AND scope_org_id = %s AND scope_workspace_id = %s
                  AND scope_project_id = %s AND scope_agent_profile_id = %s
                  AND scope_conversation_id = %s AND scope_user_id = %s
                  AND record_id = %s
                """,
                (*scope, record_id),
            )
            return _dict_to_record(row) if row else None
        row = self._conn().execute(
            f"SELECT * FROM memory_records WHERE {_SCOPE_PRED} AND record_id=?",
            (*scope, record_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_many(self, context: RuntimeContext | None, ids: Iterable[str]) -> list[MemoryRecord]:
        id_list = list(ids)
        if not id_list:
            return []
        scope = _scope_values(context)
        if self._driver == "postgres":
            rows = self._fetchall_postgres(
                """
                SELECT *
                FROM memory_records
                WHERE scope_mode = %s AND scope_org_id = %s AND scope_workspace_id = %s
                  AND scope_project_id = %s AND scope_agent_profile_id = %s
                  AND scope_conversation_id = %s AND scope_user_id = %s
                  AND record_id = ANY(%s)
                """,
                (*scope, id_list),
            )
            rows_by_id = {str(r["record_id"]): _dict_to_record(r) for r in rows}
            return [rows_by_id[rid] for rid in id_list if rid in rows_by_id]
        placeholders = ",".join("?" * len(id_list))
        rows = self._conn().execute(
            f"SELECT * FROM memory_records WHERE {_SCOPE_PRED} AND record_id IN ({placeholders})",
            (*scope, *id_list),
        ).fetchall()
        rows_by_id = {r["record_id"]: _row_to_record(r) for r in rows}
        return [rows_by_id[rid] for rid in id_list if rid in rows_by_id]

    def search(
        self,
        context: RuntimeContext | None,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> list[SearchResult]:
        if candidate_strategy not in {"union", "keyword"}:
            raise ValueError("unsupported deep-memory record candidate strategy")

        try:
            safe_limit = int(limit)
        except (TypeError, ValueError):
            safe_limit = 5
        safe_limit = max(1, min(safe_limit, 100))

        tokens = [t.lower() for t in query.split() if t]
        if not tokens:
            return []
        scope = _scope_values(context)
        if self._driver == "postgres":
            filter_json = json.dumps(dict(filters)) if filters else None
            ts_query = _postgres_search_query(query)
            vec_literal = self._compute_pg_embedding(query)
            overfetch_limit = _pg_overfetch_limit(safe_limit)
            rows = self._fetchall_postgres(
                _postgres_search_sql(),
                (
                    *scope,
                    filter_json,
                    filter_json,
                    vec_literal,
                    ts_query,
                    vec_literal,
                    ts_query,
                    overfetch_limit,
                ),
            )
            keyword_rows = self._fetchall_postgres(
                _postgres_keyword_candidates_sql(),
                (*scope, filter_json, filter_json, ts_query),
            )
            existing_ids = {str(r["record_id"]) for r in rows}
            for krow in keyword_rows:
                if str(krow["record_id"]) not in existing_ids:
                    rows.append(krow)
                    existing_ids.add(str(krow["record_id"]))
            okapi_scores = _bm25_scores(query, [str(row["text"]) for row in rows])
            positive_candidate_ids = [
                str(row["record_id"]) for row, bm25 in zip(rows, okapi_scores) if bm25 > 0
            ]
            signal_boosts = self._pg_signal_boosts(context, query, positive_candidate_ids)
            scored: list[tuple[float, float, str, Any]] = []
            for i, row in enumerate(rows):
                bm25 = okapi_scores[i]
                if bm25 <= 0:
                    continue
                rid = str(row["record_id"])
                boost = signal_boosts.get(rid, 0.0)
                effective_score = bm25 + boost
                scored.append((effective_score, bm25, rid, row))
            scored.sort(key=lambda x: (-x[0], x[2]))
            results = []
            for rank, (eff_score, bm25, rid, row) in enumerate(scored[:safe_limit]):
                excerpt = _make_excerpt(str(row["text"]))
                matched_via = "record+signal" if eff_score > bm25 else "bm25"
                results.append(
                    SearchResult(
                        id=rid,
                        score=eff_score,
                        rank=rank,
                        excerpt=excerpt,
                        text=excerpt,
                        metadata=dict(row.get("metadata") or {}),
                        source=str(row.get("source") or "unknown"),
                        source_uri=str(row.get("source_uri") or ""),
                        timestamp=row.get("ts"),
                        record_kind=str(row.get("record_kind") or "verbatim"),
                        bm25_score=bm25,
                        matched_via=matched_via,
                    )
                )
            return results
        rows = self._conn().execute(
            f"SELECT * FROM memory_records WHERE {_SCOPE_PRED}",
            scope,
        ).fetchall()

        candidates: list[tuple[sqlite3.Row, dict]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            if filters and any(metadata.get(k) != v for k, v in filters.items()):
                continue
            candidates.append((row, metadata))

        if not candidates:
            return []

        texts = [row["text"] for row, _ in candidates]
        bm25_raw = _bm25_scores(query, texts)
        positive_candidate_ids = [
            row["record_id"] for (row, _), bm25 in zip(candidates, bm25_raw) if bm25 > 0
        ]
        signal_boosts = self._sqlite_signal_boosts(context, query, positive_candidate_ids)

        scored: list[tuple[float, float, str, sqlite3.Row, dict, str]] = []
        for (row, metadata), bm25 in zip(candidates, bm25_raw):
            if bm25 > 0:
                rid = row["record_id"]
                boost = signal_boosts.get(rid, 0.0)
                effective_score = bm25 + boost
                matched_via = "record+signal" if boost > 0 else "bm25"
                scored.append((effective_score, bm25, rid, row, metadata, matched_via))

        scored.sort(key=lambda x: (-x[0], x[2]))

        results = []
        for rank, (eff_score, bm25, _, row, metadata, matched_via) in enumerate(scored[:safe_limit]):
            excerpt = _make_excerpt(row["text"])
            results.append(
                SearchResult(
                    id=row["record_id"],
                    score=eff_score,
                    rank=rank,
                    excerpt=excerpt,
                    text=excerpt,
                    metadata=metadata,
                    source=row["source"],
                    source_uri=row["source_uri"],
                    timestamp=row["ts"],
                    record_kind=row["record_kind"],
                    bm25_score=bm25,
                    matched_via=matched_via,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Postgres SQL for the live psycopg2/pgvector adapter
# ---------------------------------------------------------------------------
# These functions define SQL strings used by RelationalMemoryRecordBackend's
# PostgreSQL path. The psycopg2 dependency is imported lazily by the adapter so
# local-only SQLite users do not need the optional postgres extra installed.
# ---------------------------------------------------------------------------

_PG_SCOPE_COLS = (
    "scope_mode",
    "scope_org_id",
    "scope_workspace_id",
    "scope_project_id",
    "scope_agent_profile_id",
    "scope_conversation_id",
    "scope_user_id",
)
_PG_CONFLICT_COLS = (*_PG_SCOPE_COLS, "record_id")


def _postgres_schema_sql() -> str:
    scope_col_defs = "\n    ".join(f"{c} TEXT NOT NULL DEFAULT ''," for c in _PG_SCOPE_COLS)
    conflict_cols = ", ".join(_PG_CONFLICT_COLS)
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_records (
    {scope_col_defs}
    record_id TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}',
    provenance JSONB NOT NULL DEFAULT '{{}}',
    source TEXT NOT NULL DEFAULT 'unknown',
    source_uri TEXT NOT NULL DEFAULT '',
    ts DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    record_kind TEXT NOT NULL DEFAULT 'verbatim',
    parent_id TEXT,
    embedding vector({EMBEDDING_DIM}),
    ts_vec tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    PRIMARY KEY ({conflict_cols})
);

CREATE INDEX IF NOT EXISTS memory_records_embedding_idx
    ON memory_records USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS memory_records_ts_vec_gin_idx
    ON memory_records USING gin (ts_vec);

CREATE INDEX IF NOT EXISTS memory_records_metadata_gin_idx
    ON memory_records USING gin (metadata);

CREATE INDEX IF NOT EXISTS memory_records_provenance_gin_idx
    ON memory_records USING gin (provenance);

CREATE INDEX IF NOT EXISTS memory_records_scope_idx
    ON memory_records ({", ".join(_PG_SCOPE_COLS)});

CREATE TABLE IF NOT EXISTS memory_signals (
    {scope_col_defs}
    record_id TEXT NOT NULL,
    signal_line TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS memory_signals_scope_record_idx
    ON memory_signals ({", ".join(_PG_SCOPE_COLS)}, record_id);
"""


def _postgres_upsert_sql() -> str:
    all_cols = (
        *_PG_SCOPE_COLS,
        "record_id",
        "text",
        "text_hash",
        "metadata",
        "provenance",
        "source",
        "source_uri",
        "ts",
        "created_at",
        "updated_at",
        "record_kind",
        "parent_id",
        "embedding",
    )
    col_list = ", ".join(all_cols)
    placeholders = ", ".join("%s" for _ in all_cols)
    conflict_target = ", ".join(_PG_CONFLICT_COLS)
    mutable_cols = [
        c for c in all_cols
        if c not in {*_PG_CONFLICT_COLS, "created_at"}
    ]
    update_set = ",\n        ".join(f"{c} = EXCLUDED.{c}" for c in mutable_cols)
    returning_cols = ", ".join(c for c in all_cols if c != "embedding")
    return (
        f"INSERT INTO memory_records ({col_list})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET\n"
        f"        {update_set}\n"
        f"RETURNING {returning_cols};"
    )


def _postgres_keyword_candidates_sql() -> str:
    scope_filter = " AND ".join(f"{c} = %s" for c in _PG_SCOPE_COLS)
    return f"""
WITH scope_filtered AS (
    SELECT *
    FROM memory_records
    WHERE {scope_filter}
      AND (
        %s::jsonb IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM jsonb_each(%s::jsonb) AS filter(k, v)
            WHERE NOT (metadata ? filter.k AND metadata -> filter.k = filter.v)
        )
      )
)
SELECT
    record_id,
    text,
    text_hash,
    metadata,
    provenance,
    source,
    source_uri,
    ts,
    created_at,
    updated_at,
    record_kind,
    parent_id
FROM scope_filtered
WHERE ts_vec @@ websearch_to_tsquery('english', %s)
"""


def _postgres_search_sql() -> str:
    scope_filter = " AND ".join(f"{c} = %s" for c in _PG_SCOPE_COLS)
    return f"""
WITH scope_filtered AS (
    SELECT *
    FROM memory_records
    WHERE {scope_filter}
      AND (
        %s::jsonb IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM jsonb_each(%s::jsonb) AS filter(k, v)
            WHERE NOT (metadata ? filter.k AND metadata -> filter.k = filter.v)
        )
      )
),
ranked AS (
    SELECT
        record_id,
        text,
        text_hash,
        metadata,
        provenance,
        source,
        source_uri,
        ts,
        created_at,
        updated_at,
        record_kind,
        parent_id,
        (
            COALESCE(1.0 - (embedding <=> %s::vector), 0.0) * 0.6
            + COALESCE(ts_rank(ts_vec, websearch_to_tsquery('english', %s)), 0.0) * 0.4
        ) AS score
    FROM scope_filtered
    WHERE (%s::vector IS NOT NULL AND embedding IS NOT NULL)
       OR ts_vec @@ websearch_to_tsquery('english', %s)
)
SELECT *
FROM ranked
WHERE score > 0
ORDER BY score DESC, record_id ASC
LIMIT %s;
"""
