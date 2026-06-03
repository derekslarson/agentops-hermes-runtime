"""Tests for the server-side /memory/records handler (M5B).

Tests the pure handler function and scope isolation without Chroma/ONNX
by using an in-memory fake backend that mirrors the scope-partitioning
logic of LocalDeepMemoryBackend(partition=True).
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

import pytest

from agentops_runtime.memory_records_api import handle_memory_records_request
from agent.local_memory.store import MemoryRecord, SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCOPE_A = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "user_id": "alice",
    "conversation_id": "conv-alice",
    "agent_profile_id": "bot",
    "project_id": "proj1",
}
_SCOPE_B = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "user_id": "bob",
    "conversation_id": "conv-bob",
    "agent_profile_id": "bot",
    "project_id": "proj1",
}
_SCOPE_C_ORG2 = {
    "mode": "agentops",
    "org_id": "org2",
    "workspace_id": "ws2",
    "user_id": "carol",
    "conversation_id": "conv-carol",
    "agent_profile_id": "bot",
    "project_id": "proj2",
}
_TOKEN = "test-bearer-token"


class _FakeMemoryRecordBackend:
    """In-memory scope-partitioned backend that mirrors LocalDeepMemoryBackend(partition=True).

    Uses the same seven-field scope key as _session_scope_key / _deep_memory_scope_key.
    """

    def __init__(self) -> None:
        self._store: dict[tuple, dict[str, MemoryRecord]] = {}
        self._counter = 0

    def _scope_key(self, context: Any) -> tuple:
        if context is None:
            return ()
        return (
            context.mode,
            context.org_id,
            context.workspace_id,
            context.user_id,
            context.conversation_id,
            context.agent_profile_id,
            context.project_id,
        )

    def upsert_record(
        self,
        context: Any,
        *,
        text: str,
        metadata: Any = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: Optional[float] = None,
        record_kind: str = "verbatim",
        parent_id: Optional[str] = None,
        provenance: Any = None,
        record_id: Optional[str] = None,
    ) -> MemoryRecord:
        scope = self._scope_key(context)
        scoped = self._store.setdefault(scope, {})
        if record_id is None:
            self._counter += 1
            record_id = f"mem_{self._counter}"
        record = MemoryRecord(
            id=record_id,
            text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
            metadata=dict(metadata) if metadata else {},
            source=source,
            source_uri=source_uri or "",
            timestamp=timestamp,
            record_kind=record_kind,
            provenance=dict(provenance) if provenance else {},
            parent_id=parent_id,
        )
        scoped[record_id] = record
        return record

    def search(
        self,
        context: Any,
        query: str,
        *,
        filters: Any = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> List[SearchResult]:
        scope = self._scope_key(context)
        scoped = self._store.get(scope, {})
        matched = []
        for i, record in enumerate(scoped.values()):
            if not query or query.lower() in record.text.lower():
                matched.append(
                    SearchResult(
                        id=record.id,
                        score=1.0 - i * 0.05,
                        rank=i,
                        excerpt=record.text[:48],
                        text=record.text,
                        metadata=record.metadata,
                        source=record.source,
                        source_uri=record.source_uri,
                        timestamp=record.timestamp,
                        record_kind=record.record_kind,
                    )
                )
        return matched[:limit]

    def get_record(self, context: Any, record_id: str) -> Optional[MemoryRecord]:
        scope = self._scope_key(context)
        return self._store.get(scope, {}).get(record_id)

    def get_many(self, context: Any, ids: Iterable[str]) -> List[MemoryRecord]:
        scope = self._scope_key(context)
        scoped = self._store.get(scope, {})
        return [scoped[i] for i in ids if i in scoped]


def _post_upsert(backend: _FakeMemoryRecordBackend, scope: dict, text: str, record_id: str) -> tuple[int, dict]:
    body = json.dumps({"context": scope, "record": {"text": text, "record_id": record_id}}).encode()
    return handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)


def _get_search(backend: _FakeMemoryRecordBackend, scope: dict, query: str) -> tuple[int, dict]:
    qs = urllib.parse.urlencode({"context": json.dumps(scope), "query": query})
    return handle_memory_records_request("GET", "/memory/records/search", qs, b"", None, None, backend)


def _get_record(backend: _FakeMemoryRecordBackend, scope: dict, record_id: str) -> tuple[int, dict]:
    qs = urllib.parse.urlencode({"context": json.dumps(scope), "id": record_id})
    return handle_memory_records_request("GET", "/memory/records/get", qs, b"", None, None, backend)


def _post_get_many(backend: _FakeMemoryRecordBackend, scope: dict, ids: list) -> tuple[int, dict]:
    body = json.dumps({"context": scope, "ids": ids}).encode()
    return handle_memory_records_request("POST", "/memory/records/get_many", "", body, None, None, backend)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_upsert_returns_record_with_id():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "hello world", "record_id": "mem_abc"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)
    assert status == 200
    assert resp["record"]["id"] == "mem_abc"
    assert resp["record"]["text"] == "hello world"


def test_search_finds_upserted_record():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "runtime adapter shipped on Tuesday", "mem_x")
    status, resp = _get_search(backend, _SCOPE_A, "runtime")
    assert status == 200
    ids = [r["id"] for r in resp["results"]]
    assert "mem_x" in ids


def test_search_result_contains_excerpt_field():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "deep memory round trip test record", "mem_e")
    status, resp = _get_search(backend, _SCOPE_A, "round trip")
    assert status == 200
    assert resp["results"]
    hit = resp["results"][0]
    assert "excerpt" in hit
    assert hit["excerpt"]


def test_search_response_does_not_return_full_record_text():
    backend = _FakeMemoryRecordBackend()
    full_text = "deep memory search should not expose the full verbatim payload " + ("x" * 600)
    _post_upsert(backend, _SCOPE_A, full_text, "mem_bounded")

    status, resp = _get_search(backend, _SCOPE_A, "deep memory search")

    assert status == 200
    hit = resp["results"][0]
    assert hit["excerpt"] != full_text
    assert hit.get("text") != full_text
    assert len(hit["excerpt"]) <= 512


def test_search_response_does_not_return_short_full_record_text():
    backend = _FakeMemoryRecordBackend()
    full_text = "short sensitive search payload"
    _post_upsert(backend, _SCOPE_A, full_text, "mem_short_bounded")

    status, resp = _get_search(backend, _SCOPE_A, "short sensitive")

    assert status == 200
    hit = resp["results"][0]
    assert hit["excerpt"] != full_text
    assert hit.get("text") != full_text


def test_search_response_does_not_return_short_full_record_text_ending_with_ellipsis():
    backend = _FakeMemoryRecordBackend()
    full_text = "short sensitive payload…"
    _post_upsert(backend, _SCOPE_A, full_text, "mem_ellipsis_bounded")

    status, resp = _get_search(backend, _SCOPE_A, "short sensitive")

    assert status == 200
    hit = resp["results"][0]
    assert hit["excerpt"] != full_text
    assert hit.get("text") != full_text


def test_get_returns_full_record_by_id():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "full verbatim text for get test", "mem_g")
    status, resp = _get_record(backend, _SCOPE_A, "mem_g")
    assert status == 200
    assert resp["record"]["id"] == "mem_g"
    assert "full verbatim text" in resp["record"]["text"]


def test_get_returns_null_for_absent_id():
    backend = _FakeMemoryRecordBackend()
    status, resp = _get_record(backend, _SCOPE_A, "mem_does_not_exist")
    assert status == 200
    assert resp["record"] is None


def test_get_many_returns_records_for_valid_ids():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "record one text", "mem_1")
    _post_upsert(backend, _SCOPE_A, "record two text", "mem_2")
    status, resp = _post_get_many(backend, _SCOPE_A, ["mem_1", "mem_2"])
    assert status == 200
    ids = [r["id"] for r in resp["records"]]
    assert "mem_1" in ids
    assert "mem_2" in ids


def test_full_round_trip_upsert_search_get_get_many():
    backend = _FakeMemoryRecordBackend()

    status, up = _post_upsert(backend, _SCOPE_A, "Derek shipped the runtime adapter on Tuesday", "mem_rt")
    assert status == 200
    assert up["record"]["id"] == "mem_rt"

    status, sr = _get_search(backend, _SCOPE_A, "runtime adapter")
    assert status == 200
    assert any(r["id"] == "mem_rt" for r in sr["results"])

    status, gr = _get_record(backend, _SCOPE_A, "mem_rt")
    assert status == 200
    assert gr["record"]["id"] == "mem_rt"
    assert "Derek shipped" in gr["record"]["text"]

    status, gm = _post_get_many(backend, _SCOPE_A, ["mem_rt", "mem_missing"])
    assert status == 200
    assert [r["id"] for r in gm["records"]] == ["mem_rt"]


# ---------------------------------------------------------------------------
# Cross-scope isolation
# ---------------------------------------------------------------------------


def test_cross_user_isolation_search():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "alice private record", "mem_alice")
    status, resp = _get_search(backend, _SCOPE_B, "alice private")
    assert status == 200
    assert resp["results"] == []


def test_cross_user_isolation_get():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "alice private record", "mem_alice")
    status, resp = _get_record(backend, _SCOPE_B, "mem_alice")
    assert status == 200
    assert resp["record"] is None


def test_cross_org_isolation_search():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "org1 private record", "mem_org1")
    status, resp = _get_search(backend, _SCOPE_C_ORG2, "org1 private")
    assert status == 200
    assert resp["results"] == []


def test_cross_org_isolation_get():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "org1 private record", "mem_org1")
    status, resp = _get_record(backend, _SCOPE_C_ORG2, "mem_org1")
    assert status == 200
    assert resp["record"] is None


def test_cross_project_isolation():
    scope_proj2 = dict(_SCOPE_A, project_id="proj-other")
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "proj1 record", "mem_p1")
    status, resp = _get_record(backend, scope_proj2, "mem_p1")
    assert status == 200
    assert resp["record"] is None


def test_cross_conversation_isolation():
    scope_conv2 = dict(_SCOPE_A, conversation_id="conv-other")
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "conv1 record", "mem_c1")
    status, resp = _get_record(backend, scope_conv2, "mem_c1")
    assert status == 200
    assert resp["record"] is None


def test_guessed_id_cross_scope_fetch_returns_none():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "sensitive data belonging to alice", "mem_secret")
    # Bob tries to guess alice's ID
    status, resp = _get_record(backend, _SCOPE_B, "mem_secret")
    assert status == 200
    assert resp["record"] is None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_required_returns_401_without_header():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "text"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, _TOKEN, backend)
    assert status == 401


def test_auth_required_returns_401_with_wrong_token():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "text"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, "Bearer wrong-token", _TOKEN, backend)
    assert status == 401


def test_auth_passes_with_correct_token():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "authed record", "record_id": "mem_auth"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, f"Bearer {_TOKEN}", _TOKEN, backend)
    assert status == 200
    assert resp["record"]["id"] == "mem_auth"


def test_no_auth_required_when_token_not_configured():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "open access record", "record_id": "mem_open"}}).encode()
    # token=None means no auth configured → request passes without header
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)
    assert status == 200


def test_token_not_leaked_in_401_payload():
    backend = _FakeMemoryRecordBackend()
    secret_token = "super-secret-value-9x7z"
    body = json.dumps({"context": _SCOPE_A, "record": {"text": "x"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, secret_token, backend)
    assert status == 401
    payload_str = json.dumps(resp)
    assert secret_token not in payload_str


def test_auth_applied_to_search():
    backend = _FakeMemoryRecordBackend()
    qs = urllib.parse.urlencode({"context": json.dumps(_SCOPE_A), "query": "anything"})
    status, _ = handle_memory_records_request("GET", "/memory/records/search", qs, b"", None, _TOKEN, backend)
    assert status == 401


def test_auth_applied_to_get():
    backend = _FakeMemoryRecordBackend()
    qs = urllib.parse.urlencode({"context": json.dumps(_SCOPE_A), "id": "mem_x"})
    status, _ = handle_memory_records_request("GET", "/memory/records/get", qs, b"", None, _TOKEN, backend)
    assert status == 401


def test_auth_applied_to_get_many():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": _SCOPE_A, "ids": []}).encode()
    status, _ = handle_memory_records_request("POST", "/memory/records/get_many", "", body, None, _TOKEN, backend)
    assert status == 401


# ---------------------------------------------------------------------------
# Malformed / missing context fails closed
# ---------------------------------------------------------------------------


def test_missing_context_in_upsert_body_fails_closed():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"record": {"text": "no context"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)
    assert status == 400
    assert "context" in resp.get("error", "").lower()


def test_malformed_context_json_in_search_fails_closed():
    backend = _FakeMemoryRecordBackend()
    qs = "context=not-valid-json&query=anything"
    status, resp = handle_memory_records_request("GET", "/memory/records/search", qs, b"", None, None, backend)
    assert status == 400


def test_missing_context_in_search_query_fails_closed():
    backend = _FakeMemoryRecordBackend()
    qs = "query=anything"
    status, resp = handle_memory_records_request("GET", "/memory/records/search", qs, b"", None, None, backend)
    assert status == 400


def test_invalid_mode_in_context_fails_closed():
    bad_scope = dict(_SCOPE_A, mode="not-a-valid-mode")
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": bad_scope, "record": {"text": "x"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)
    assert status == 400


def test_invalid_context_error_does_not_echo_raw_value():
    secret_like_mode = "sentinel-secret-mode-token-123"
    bad_scope = dict(_SCOPE_A, mode=secret_like_mode)
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": bad_scope, "record": {"text": "x"}}).encode()

    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)

    assert status == 400
    assert secret_like_mode not in json.dumps(resp)


def test_non_object_context_fails_closed():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": "not-an-object", "record": {"text": "x"}}).encode()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)
    assert status == 400


def test_missing_required_scope_field_fails_closed_before_write():
    scope_without_user = dict(_SCOPE_A)
    scope_without_user.pop("user_id")
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": scope_without_user, "record": {"text": "must not be stored"}}).encode()

    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)

    assert status == 400
    assert "user_id" in resp.get("error", "")
    assert backend._store == {}


def test_empty_scope_context_fails_closed_before_write():
    backend = _FakeMemoryRecordBackend()
    body = json.dumps({"context": {}, "record": {"text": "must not be stored"}}).encode()

    status, resp = handle_memory_records_request("POST", "/memory/records", "", body, None, None, backend)

    assert status == 400
    assert "mode" in resp.get("error", "")
    assert backend._store == {}


def test_empty_body_upsert_fails_closed():
    backend = _FakeMemoryRecordBackend()
    status, resp = handle_memory_records_request("POST", "/memory/records", "", b"", None, None, backend)
    assert status == 400


# ---------------------------------------------------------------------------
# get_many contract
# ---------------------------------------------------------------------------


def test_get_many_omits_missing_records():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "existing record", "mem_exists")
    status, resp = _post_get_many(backend, _SCOPE_A, ["mem_exists", "mem_ghost"])
    assert status == 200
    ids = [r["id"] for r in resp["records"]]
    assert "mem_exists" in ids
    assert "mem_ghost" not in ids


def test_get_many_omits_cross_scope_records():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "alice data", "mem_alice")
    # Bob's get_many includes alice's ID — must be omitted
    status, resp = _post_get_many(backend, _SCOPE_B, ["mem_alice"])
    assert status == 200
    assert resp["records"] == []


def test_get_many_never_contains_null_entries():
    backend = _FakeMemoryRecordBackend()
    _post_upsert(backend, _SCOPE_A, "existing", "mem_real")
    status, resp = _post_get_many(backend, _SCOPE_A, ["mem_real", "mem_nope", "mem_also_nope"])
    assert status == 200
    assert all(r is not None for r in resp["records"])


def test_get_many_with_empty_ids_returns_empty_list():
    backend = _FakeMemoryRecordBackend()
    status, resp = _post_get_many(backend, _SCOPE_A, [])
    assert status == 200
    assert resp["records"] == []


# ---------------------------------------------------------------------------
# Unknown path
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404():
    backend = _FakeMemoryRecordBackend()
    status, resp = handle_memory_records_request("GET", "/memory/records/unknown", "", b"", None, None, backend)
    assert status == 404


def test_unknown_method_on_memory_records_returns_404():
    backend = _FakeMemoryRecordBackend()
    status, _ = handle_memory_records_request("DELETE", "/memory/records", "", b"", None, None, backend)
    assert status == 404
