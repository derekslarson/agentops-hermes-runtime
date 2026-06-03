"""Server-side handler for /memory/records control-plane API (M5B).

A pure, dependency-free function the compose API service delegates
/memory/records/* requests to.  Uses the MemoryRecordBackend contract
(BackendCapability.DEEP_MEMORY) so any backend satisfying that protocol
can be injected — LocalDeepMemoryBackend(partition=True) in production,
a lightweight fake in tests.

Routes:
    POST  /memory/records           → upsert_record
    GET   /memory/records/search    → search
    GET   /memory/records/get       → get_record
    POST  /memory/records/get_many  → get_many
    POST  /memory/records/import    → import (idempotent bulk upsert)

Auth: when token is non-None, Authorization: Bearer <token> is required.
Context: sanitised scope-only dict reconstructed into RuntimeContext.
         Missing or malformed context fails closed (400) before any store write.
Token safety: the raw token value never appears in error payloads or log messages.
Scope: all record operations are scoped by the seven context fields that
       _deep_memory_scope_key uses: mode, org_id, workspace_id, project_id,
       agent_profile_id, conversation_id, user_id.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.parse
from typing import Any

from agent.runtime_context import RuntimeContext

# The exact seven fields _deep_memory_scope_key/_session_scope_key keys on.
_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "project_id",
    "agent_profile_id",
    "conversation_id",
    "user_id",
)
_MAX_SEARCH_EXCERPT_CHARS = 512


def handle_memory_records_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
) -> tuple[int, dict[str, Any]]:
    """Dispatch one /memory/records request and return (status, response_dict).

    auth_header: the raw value of the Authorization header (may be None).
    token: the expected bearer token (from AGENTOPS_RUNTIME_TOKEN env var).
           When None, auth is not enforced.
    backend: any object implementing the MemoryRecordBackend protocol.
    """
    if token:
        if auth_header != f"Bearer {token}":
            # Never put the token value in the error message.
            return 401, {"error": "unauthorized"}

    if method == "POST" and path == "/memory/records":
        return _upsert(body_bytes, backend)
    if method == "GET" and path == "/memory/records/search":
        return _search(query_string, backend)
    if method == "GET" and path == "/memory/records/get":
        return _get(query_string, backend)
    if method == "POST" and path == "/memory/records/get_many":
        return _get_many(body_bytes, backend)
    if method == "POST" and path == "/memory/records/import":
        return _import(body_bytes, backend)

    return 404, {"error": "not found"}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _upsert(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    if not body_bytes:
        return 400, {"error": "context is required"}
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return 400, {"error": "invalid JSON body"}
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    if "context" not in body:
        return 400, {"error": "context is required"}
    ctx, err = _parse_context(body["context"])
    if err:
        return 400, {"error": err}

    rec_input = body.get("record")
    if not isinstance(rec_input, dict):
        return 400, {"error": "record must be an object"}

    text = rec_input.get("text", "")
    if not isinstance(text, str):
        return 400, {"error": "record.text must be a string"}

    try:
        record = backend.upsert_record(
            ctx,
            text=text,
            metadata=rec_input.get("metadata") or {},
            source=str(rec_input.get("source") or "unknown"),
            source_uri=str(rec_input.get("source_uri") or ""),
            timestamp=rec_input.get("timestamp"),
            record_kind=str(rec_input.get("record_kind") or "verbatim"),
            parent_id=rec_input.get("parent_id"),
            provenance=rec_input.get("provenance") or {},
            record_id=rec_input.get("record_id"),
        )
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"record": _record_to_dict(record)}


def _search(query_string: str, backend: Any) -> tuple[int, dict[str, Any]]:
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)

    if "context" not in params:
        return 400, {"error": "context is required"}
    try:
        context_raw = json.loads(params["context"][0])
    except (json.JSONDecodeError, IndexError):
        return 400, {"error": "invalid context JSON"}

    ctx, err = _parse_context(context_raw)
    if err:
        return 400, {"error": err}

    query = params.get("query", [""])[0]
    try:
        limit = int(params.get("limit", ["5"])[0])
        max_distance = float(params.get("max_distance", ["0.0"])[0])
    except (ValueError, IndexError):
        return 400, {"error": "invalid numeric parameter"}
    candidate_strategy = params.get("candidate_strategy", ["union"])[0]

    filters: Any = None
    if "filters" in params:
        try:
            filters = json.loads(params["filters"][0])
        except (json.JSONDecodeError, IndexError):
            return 400, {"error": "invalid filters JSON"}

    try:
        results = backend.search(
            ctx,
            query,
            filters=filters,
            limit=limit,
            max_distance=max_distance,
            candidate_strategy=candidate_strategy,
        )
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"results": [_search_result_to_dict(r) for r in results]}


def _get(query_string: str, backend: Any) -> tuple[int, dict[str, Any]]:
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)

    if "context" not in params:
        return 400, {"error": "context is required"}
    try:
        context_raw = json.loads(params["context"][0])
    except (json.JSONDecodeError, IndexError):
        return 400, {"error": "invalid context JSON"}

    ctx, err = _parse_context(context_raw)
    if err:
        return 400, {"error": err}

    record_id = params.get("id", [""])[0]

    try:
        record = backend.get_record(ctx, record_id)
    except Exception:
        return 500, {"error": "internal error"}

    return 200, {"record": _record_to_dict(record) if record is not None else None}


def _get_many(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    if not body_bytes:
        return 400, {"error": "context is required"}
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return 400, {"error": "invalid JSON body"}
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    if "context" not in body:
        return 400, {"error": "context is required"}
    ctx, err = _parse_context(body["context"])
    if err:
        return 400, {"error": err}

    ids = body.get("ids", [])
    if not isinstance(ids, list):
        return 400, {"error": "ids must be a list"}

    try:
        records = backend.get_many(ctx, [str(i) for i in ids])
    except Exception:
        return 500, {"error": "internal error"}

    # Omit missing entries; never return null members in the list.
    return 200, {"records": [_record_to_dict(r) for r in records if r is not None]}


def _import(body_bytes: bytes, backend: Any) -> tuple[int, dict[str, Any]]:
    if not body_bytes:
        return 400, {"error": "context is required"}
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return 400, {"error": "invalid JSON body"}
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    if "context" not in body:
        return 400, {"error": "context is required"}
    ctx, err = _parse_context(body["context"])
    if err:
        return 400, {"error": err}

    if "records" not in body:
        return 400, {"error": "records is required"}
    records_input = body["records"]
    if not isinstance(records_input, list):
        return 400, {"error": "records must be a list"}

    results = []
    for rec in records_input:
        item = _import_one(ctx, rec, backend)
        results.append(item)

    return 200, {"results": results}


def _import_one(ctx: Any, rec: Any, backend: Any) -> dict[str, Any]:
    if not isinstance(rec, dict):
        return {"status": "error", "error": "record must be an object"}

    text = rec.get("text", "")
    if not isinstance(text, str):
        return {"status": "error", "error": "record.text must be a string"}
    if not text:
        return {"status": "error", "error": "record.text must be non-empty"}

    try:
        record = backend.upsert_record(
            ctx,
            text=text,
            metadata=rec.get("metadata") or {},
            source=str(rec.get("source") or "unknown"),
            source_uri=str(rec.get("source_uri") or ""),
            timestamp=rec.get("timestamp"),
            record_kind=str(rec.get("record_kind") or "verbatim"),
            parent_id=rec.get("parent_id"),
            provenance=rec.get("provenance") or {},
            record_id=rec.get("record_id") or rec.get("id"),
        )
    except Exception:
        return {"status": "error", "error": "internal error"}

    return {"record": _record_to_dict(record), "status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    """Reconstruct a sanitised RuntimeContext from the client-supplied scope dict.

    Only the seven scope fields used by _deep_memory_scope_key are kept; all
    other keys (including any secret refs, run IDs, or metadata) are stripped.
    Returns (ctx, None) on success or (None, error_str) on failure.
    """
    if raw is None:
        return None, "context is required"
    if not isinstance(raw, dict):
        return None, "context must be an object"
    missing = [field for field in _SCOPE_FIELDS if field not in raw or raw[field] is None]
    if missing:
        return None, f"context missing required scope field: {missing[0]}"
    non_string = [field for field in _SCOPE_FIELDS if not isinstance(raw[field], str)]
    if non_string:
        return None, f"context scope field must be a string: {non_string[0]}"
    blank = [field for field in _SCOPE_FIELDS if not raw[field].strip()]
    if blank:
        return None, f"context scope field must be non-empty: {blank[0]}"
    scope = {k: raw[k] for k in _SCOPE_FIELDS if k in raw}
    try:
        ctx = RuntimeContext.from_mapping(scope)
    except (ValueError, TypeError):
        return None, "invalid runtime context"
    return ctx, None


def _record_to_dict(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        return dataclasses.asdict(record)
    return dict(getattr(record, "__dict__", {}))


def _search_result_to_dict(result: Any) -> dict[str, Any]:
    """Serialize a search hit without exposing the full verbatim record text."""
    data = _record_to_dict(result) or {}
    text = str(data.get("text") or "")
    raw_excerpt = str(data.get("excerpt") or text[:_MAX_SEARCH_EXCERPT_CHARS])[:_MAX_SEARCH_EXCERPT_CHARS]
    excerpt = _non_verbatim_excerpt(raw_excerpt, text)
    return {
        "id": data.get("id", ""),
        "score": data.get("score", 0.0),
        "rank": data.get("rank", 0),
        "excerpt": excerpt,
        # Keep the existing HTTP client's SearchResult shape compatible while
        # ensuring search never returns full verbatim text. Full text stays on
        # /memory/records/get and /memory/records/get_many only.
        "text": excerpt,
        "metadata": data.get("metadata") or {},
        "source": data.get("source", "unknown"),
        "source_uri": data.get("source_uri", ""),
        "timestamp": data.get("timestamp"),
        "record_kind": data.get("record_kind", "verbatim"),
        "distance": data.get("distance"),
        "bm25_score": data.get("bm25_score", 0.0),
        "matched_via": data.get("matched_via", "record"),
        "summary": str(data.get("summary") or "")[:_MAX_SEARCH_EXCERPT_CHARS],
        "summary_status": data.get("summary_status", ""),
        "summary_model": data.get("summary_model", ""),
        "summary_generated_at": data.get("summary_generated_at"),
        "summary_version": data.get("summary_version", ""),
    }


def _non_verbatim_excerpt(excerpt: str, full_text: str) -> str:
    """Return a bounded preview that is never byte-for-byte full text."""
    excerpt = excerpt[:_MAX_SEARCH_EXCERPT_CHARS]
    if not full_text or excerpt != full_text:
        return excerpt
    if len(full_text) == 1:
        return ""
    return full_text[: min(len(full_text) - 1, _MAX_SEARCH_EXCERPT_CHARS)]
