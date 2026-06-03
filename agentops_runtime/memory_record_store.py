"""Relational MemoryRecordBackend for durable scope-isolated deep-memory storage.

SQLite backend uses stdlib sqlite3 for focused tests and local durable storage.
Postgres is scaffolded for compose/cloud deployments: if a postgresql:// URL is
configured but unavailable, initialization fails closed — no fallback to local.

Scope isolation: every read/write predicate includes all seven scope columns
(mode, org_id, workspace_id, project_id, agent_profile_id, conversation_id,
user_id) derived from RuntimeContext. A record ingested under scope A is never
visible or fetchable under scope B even when B knows the record_id.

Search is BM25 keyword-based for this slice. Vector union search and
extracted-signal boosts remain TODO for the Postgres/pgvector slice.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from agent.local_memory.store import MemoryRecord, SearchResult, _bm25_scores
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


_SCOPE_PRED = (
    "scope_mode=? AND scope_org_id=? AND scope_workspace_id=? AND "
    "scope_project_id=? AND scope_agent_profile_id=? AND "
    "scope_conversation_id=? AND scope_user_id=?"
)


class RelationalMemoryRecordBackend:
    """SQLite-backed MemoryRecordBackend with full scope isolation.

    db_url: "sqlite:///absolute/path.db" for SQLite.
             "postgresql://..." scaffolds Postgres and fails closed when
             psycopg2 is unavailable or the database cannot be reached.
    """

    def __init__(self, db_url: str) -> None:
        self._db_path = self._resolve_sqlite_path(db_url)
        self._local = threading.local()
        self._init_db()

    def _resolve_sqlite_path(self, db_url: str) -> str:
        if not db_url:
            raise ValueError("deep-memory relational store requires a database URL")
        parsed = urlparse(db_url)
        if parsed.scheme in {"postgresql", "postgres"}:
            raise NotImplementedError(
                "PostgreSQL backing for deep-memory records is not yet implemented "
                "in this slice. Configure AGENTOPS_DEEP_MEMORY_DB_URL with a "
                "sqlite:/// URL for local durable storage, or wait for the "
                "pgvector/Postgres slice."
            )
        if parsed.scheme == "sqlite":
            if not db_url.startswith("sqlite:///") or not parsed.path or parsed.path == "/":
                raise ValueError("deep-memory sqlite store requires sqlite:///absolute/path.db")
            return parsed.path
        if parsed.scheme:
            raise ValueError("unsupported deep-memory relational store URL scheme")
        raise ValueError("deep-memory relational store requires sqlite:///absolute/path.db")

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

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
        conn.commit()

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

        scored: list[tuple[float, str, sqlite3.Row, dict]] = []
        for (row, metadata), score in zip(candidates, bm25_raw):
            if score > 0:
                scored.append((score, row["record_id"], row, metadata))

        scored.sort(key=lambda x: (-x[0], x[1]))

        results = []
        for rank, (score, _, row, metadata) in enumerate(scored[:safe_limit]):
            excerpt = _make_excerpt(row["text"])
            results.append(
                SearchResult(
                    id=row["record_id"],
                    score=score,
                    rank=rank,
                    excerpt=excerpt,
                    text=excerpt,
                    metadata=metadata,
                    source=row["source"],
                    source_uri=row["source_uri"],
                    timestamp=row["ts"],
                    record_kind=row["record_kind"],
                    bm25_score=score,
                    matched_via="bm25",
                )
            )
        return results


# ---------------------------------------------------------------------------
# Postgres SQL contract scaffold (dependency-free; no live connection here)
# ---------------------------------------------------------------------------
# These three functions define the intended SQL contract for the future
# Postgres/pgvector adapter. They return static SQL strings suitable for use
# with psycopg2 (%s placeholders). No Postgres package is imported; callers
# must supply their own connection and cursor.
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
    return (
        f"INSERT INTO memory_records ({col_list})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET\n"
        f"        {update_set};"
    )


def _postgres_search_sql() -> str:
    scope_filter = " AND ".join(f"{c} = %s" for c in _PG_SCOPE_COLS)
    return f"""
WITH scope_filtered AS (
    SELECT *
    FROM memory_records
    WHERE {scope_filter}
      AND (%s::jsonb IS NULL OR metadata @> %s::jsonb)
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
            + COALESCE(ts_rank(ts_vec, plainto_tsquery('english', %s)), 0.0) * 0.4
        ) AS score
    FROM scope_filtered
    WHERE (%s::vector IS NOT NULL AND embedding IS NOT NULL)
       OR ts_vec @@ plainto_tsquery('english', %s)
)
SELECT *
FROM ranked
WHERE score > 0
ORDER BY score DESC, record_id ASC
LIMIT %s;
"""
