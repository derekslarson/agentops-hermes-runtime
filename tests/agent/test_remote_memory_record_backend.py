"""Tests for the remote HTTP deep-memory record adapter (M5B).

The adapter is the first real remote ``DEEP_MEMORY`` backend. These tests drive a
stdlib ``ThreadingHTTPServer`` standing in for the provider-neutral control plane,
so request shaping (sanitized scope, bearer-header-only auth) and JSON->local
record/result conversion are exercised end-to-end rather than against mocks.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent.local_memory.provider import fenced_record, search_result_payload
from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_memory_record_http import (
    HttpMemoryRecordBackend,
    register_http_memory_record_backend,
)

_SCOPE_KEYS = (
    "mode",
    "org_id",
    "workspace_id",
    "project_id",
    "agent_profile_id",
    "conversation_id",
    "user_id",
)


class _RecordControlPlaneHandler(BaseHTTPRequestHandler):
    store: dict[tuple[tuple[str, str | None], ...], dict[str, dict]] = {}
    requests: list[dict] = []
    expected_token: str | None = None

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> tuple[dict, str]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return (json.loads(raw) if raw else {}), raw

    def _authorized(self) -> bool:
        if not self.expected_token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.expected_token}"

    @staticmethod
    def _scope_key(context: dict) -> tuple[tuple[str, str | None], ...]:
        return tuple((key, context.get(key)) for key in _SCOPE_KEYS)

    @staticmethod
    def _record_payload(stored: dict) -> dict:
        return {
            "id": stored["id"],
            "text": stored["text"],
            "text_hash": "hash-" + stored["id"],
            "metadata": stored.get("metadata") or {},
            "source": stored.get("source", "unknown"),
            "source_uri": stored.get("source_uri", ""),
            "timestamp": stored.get("timestamp"),
            "record_kind": stored.get("record_kind", "verbatim"),
            "provenance": stored.get("provenance") or {},
            # An unknown forward-compat field the local dataclass does not define.
            "server_only_field": "ignore-me",
        }

    @classmethod
    def _result_payload(cls, stored: dict, rank: int) -> dict:
        return {
            "id": stored["id"],
            "score": 0.9 - rank * 0.1,
            "rank": rank,
            "excerpt": stored["text"][:32],
            "text": stored["text"],
            "metadata": stored.get("metadata") or {},
            "source": stored.get("source", "unknown"),
            "source_uri": stored.get("source_uri", ""),
            "timestamp": stored.get("timestamp"),
            "record_kind": stored.get("record_kind", "verbatim"),
            "distance": rank * 0.05,
        }

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        context = json.loads(query.get("context", ["{}"])[0])
        self.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "context": context,
            }
        )
        scope = self.store.get(self._scope_key(context), {})
        if parsed.path == "/memory/records/search":
            results = [self._result_payload(rec, i) for i, rec in enumerate(scope.values())]
            self._send_json(200, {"results": results})
            return
        if parsed.path == "/memory/records/get":
            rid = query.get("id", [""])[0]
            stored = scope.get(rid)
            self._send_json(200, {"record": self._record_payload(stored) if stored else None})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urllib.parse.urlparse(self.path)
        body, raw_body = self._read_body()
        context = body.get("context", {})
        self.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "raw_body": raw_body,
                "authorization": self.headers.get("Authorization"),
                "context": context,
            }
        )
        scope = self.store.setdefault(self._scope_key(context), {})
        if parsed.path == "/memory/records":
            record = dict(body["record"])
            rid = record.get("record_id") or f"mem_{len(scope)}"
            record["id"] = rid
            scope[rid] = record
            self._send_json(200, {"record": self._record_payload(record)})
            return
        if parsed.path == "/memory/records/get_many":
            ids = body.get("ids", [])
            records = [self._record_payload(scope[i]) for i in ids if i in scope]
            self._send_json(200, {"records": records})
            return
        if parsed.path == "/memory/records/import":
            import_records = body.get("records", [])
            results = []
            for rec in import_records:
                rid = rec.get("record_id") or rec.get("id") or f"mem_{len(scope)}"
                rec["id"] = rid
                scope[rid] = rec
                results.append({"record": self._record_payload(rec), "status": "ok"})
            self._send_json(200, {"results": results})
            return
        self._send_json(404, {"error": "not found"})


@pytest.fixture
def record_server():
    _RecordControlPlaneHandler.store = {}
    _RecordControlPlaneHandler.requests = []
    _RecordControlPlaneHandler.expected_token = "test-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordControlPlaneHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}", _RecordControlPlaneHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _ctx(user_id: str, conversation_id: str) -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-ws",
        workspace_type="slack",
        user_id=user_id,
        conversation_id=conversation_id,
        agent_profile_id="support-bot",
        project_id="runtime-mvp",
        backend_profile="compose-self-hosted",
    )


def test_round_trips_records_and_converts_to_local_shapes(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.upsert_record(
        ctx,
        text="Derek shipped the runtime adapter on Tuesday",
        metadata={"project": "runtime", "type": "decision"},
        source="slack",
        record_id="mem_decision",
    )

    results = backend.search(ctx, "runtime adapter", limit=3)
    assert len(results) == 1
    # Converts into the local SearchResult shape the native tools consume.
    payload = search_result_payload(results[0])
    assert payload["id"] == "mem_decision"
    assert payload["rank"] == 0
    assert "text" not in payload  # search payload must not leak full verbatim text

    record = backend.get_record(ctx, "mem_decision")
    fenced = fenced_record(record)
    assert fenced["id"] == "mem_decision"
    assert "Derek shipped the runtime adapter on Tuesday" in fenced["text"]
    assert fenced["metadata"] == {"project": "runtime", "type": "decision"}

    many = backend.get_many(ctx, ["mem_decision", "missing"])
    assert [r.id for r in many] == ["mem_decision"]


def test_get_record_returns_none_when_absent(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    assert backend.get_record(_ctx("derek", "thread-1"), "nope") is None


def test_keeps_runtime_context_scopes_isolated(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    derek = _ctx("derek", "thread-1")
    alex = _ctx("alex", "thread-2")

    backend.upsert_record(derek, text="Derek-only record", record_id="mem_d")
    backend.upsert_record(alex, text="Alex-only record", record_id="mem_a")

    assert backend.get_record(derek, "mem_a") is None
    assert backend.get_record(alex, "mem_d") is None
    assert backend.get_record(derek, "mem_d").text == "Derek-only record"


def test_sends_minimal_scope_without_metadata_refs_or_run_ids(record_server):
    _, base_url, handler = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-ws",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="support-bot",
        project_id="runtime-mvp",
        run_id="run-should-not-scope-memory",
        job_id="job-should-not-scope-memory",
        permissions_ref="policy-secret-ref",
        delivery_ref="delivery-secret-ref",
        backend_profile="compose-self-hosted",
        metadata={"secret_note": "SENTINEL_SCOPE_SECRET"},
    )

    backend.search(ctx, "anything")

    get_request = next(r for r in handler.requests if r["method"] == "GET")
    assert get_request["context"] == {
        "mode": "agentops",
        "org_id": "acme",
        "workspace_id": "slack-ws",
        "project_id": "runtime-mvp",
        "agent_profile_id": "support-bot",
        "conversation_id": "thread-1",
        "user_id": "derek",
    }
    assert "SENTINEL_SCOPE_SECRET" not in get_request["path"]
    assert "run-should-not-scope-memory" not in get_request["path"]


def test_token_is_header_only_never_in_url_or_body(record_server):
    _, base_url, handler = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.upsert_record(ctx, text="content", record_id="mem_x")
    backend.search(ctx, "content")

    for request in handler.requests:
        assert "test-token" not in request["path"]
        assert "test-token" not in request.get("raw_body", "")
        assert request["authorization"] == "Bearer test-token"


def test_errors_do_not_leak_scope_or_token(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="wrong-token")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="derek",
        conversation_id="thread-1",
        metadata={"secret_note": "SENTINEL_SCOPE_SECRET"},
    )

    with pytest.raises(RuntimeError) as excinfo:
        backend.search(ctx, "anything")

    message = str(excinfo.value)
    assert "SENTINEL_SCOPE_SECRET" not in message
    assert "wrong-token" not in message
    assert "context=" not in message


def test_registered_for_compose_profile(record_server):
    _, base_url, _ = record_server
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"deep_memory": "compose-self-hosted"},
                "options": {"deep_memory": {"base_url": base_url, "token": "test-token"}},
            }
        }
    )
    register_http_memory_record_backend(registry, profile="compose-self-hosted")
    ctx = _ctx("derek", "thread-1")

    backend = registry.get(BackendCapability.DEEP_MEMORY, ctx)
    assert isinstance(backend, HttpMemoryRecordBackend)

    backend.upsert_record(ctx, text="registered compose record", record_id="mem_reg")
    assert backend.get_record(ctx, "mem_reg").text == "registered compose record"


def test_agent_init_binds_configured_compose_http_deep_memory_backend(record_server):
    _, base_url, _ = record_server
    from agent.agent_init import _bind_deep_memory_backend
    from tools.memory_record_tools import (
        MEMORY_RECORD_TOOL_NAMES,
        clear_active_deep_memory_registry,
        get_active_deep_memory_registry,
    )

    ctx = _ctx("derek", "thread-1")
    agent = SimpleNamespace(
        runtime_context=ctx,
        tools=[],
        enabled_toolsets=["memory"],
        valid_tool_names=set(),
    )
    config = {
        "backends": {
            "capabilities": {"deep_memory": "compose-self-hosted"},
            "options": {"deep_memory": {"base_url": base_url, "token": "test-token"}},
        }
    }

    try:
        _bind_deep_memory_backend(agent, config)
        active_registry = get_active_deep_memory_registry()
        assert active_registry is not None
        backend = active_registry.get(BackendCapability.DEEP_MEMORY, ctx)
        assert isinstance(backend, HttpMemoryRecordBackend)
        assert set(MEMORY_RECORD_TOOL_NAMES).issubset(agent.valid_tool_names)
    finally:
        clear_active_deep_memory_registry()


def test_upsert_requires_record_in_success_response(monkeypatch):
    backend = HttpMemoryRecordBackend(base_url="https://example.test")
    monkeypatch.setattr(backend, "_post", lambda _path, _body: {})

    with pytest.raises(RuntimeError, match="record"):
        backend.upsert_record(_ctx("derek", "thread-1"), text="missing response record")


def test_agent_init_fails_closed_for_compose_deep_memory_missing_base_url():
    from agent.agent_init import _bind_deep_memory_backend
    from tools.memory_record_tools import (
        MEMORY_RECORD_TOOL_NAMES,
        clear_active_deep_memory_registry,
        get_active_deep_memory_registry,
    )

    ctx = _ctx("derek", "thread-1")
    agent = SimpleNamespace(
        runtime_context=ctx,
        tools=[],
        enabled_toolsets=["memory"],
        valid_tool_names=set(MEMORY_RECORD_TOOL_NAMES),
    )
    config = {"backends": {"capabilities": {"deep_memory": "compose-self-hosted"}}}

    try:
        _bind_deep_memory_backend(agent, config)
        assert get_active_deep_memory_registry() is None
        assert not set(MEMORY_RECORD_TOOL_NAMES) & agent.valid_tool_names
    finally:
        clear_active_deep_memory_registry()


def test_get_record_requires_record_key_in_success_response(monkeypatch):
    backend = HttpMemoryRecordBackend(base_url="https://example.test")
    monkeypatch.setattr(backend, "_get", lambda _path, _params: {})

    with pytest.raises(RuntimeError, match="record"):
        backend.get_record(_ctx("derek", "thread-1"), "mem_missing_key")


def test_get_record_allows_explicit_null_record_for_absent_id(monkeypatch):
    backend = HttpMemoryRecordBackend(base_url="https://example.test")
    monkeypatch.setattr(backend, "_get", lambda _path, _params: {"record": None})

    assert backend.get_record(_ctx("derek", "thread-1"), "mem_absent") is None


def test_get_many_rejects_null_record_entries(monkeypatch):
    backend = HttpMemoryRecordBackend(base_url="https://example.test")
    monkeypatch.setattr(backend, "_post", lambda _path, _body: {"records": [None]})

    with pytest.raises(RuntimeError, match="record"):
        backend.get_many(_ctx("derek", "thread-1"), ["mem_malformed"])


def test_fails_closed_without_base_url():
    with pytest.raises(ValueError, match="base_url"):
        HttpMemoryRecordBackend(base_url="")


@pytest.mark.parametrize(
    "base_url",
    [
        "agentops.example.test/memory",
        "file:///tmp/memory",
        "https://token@example.test",
        "https://example.test?token=secret",
        "https://example.test#fragment",
        "https://:443/path",
        "https://@example.test",
    ],
)
def test_rejects_unsafe_base_urls(base_url):
    with pytest.raises(ValueError, match="base_url"):
        HttpMemoryRecordBackend(base_url=base_url)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "not-a-number"])
def test_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        HttpMemoryRecordBackend(base_url="https://example.test", timeout=timeout)


# ---------------------------------------------------------------------------
# Import records
# ---------------------------------------------------------------------------


def test_import_records_sends_context_and_records_in_body(record_server):
    _, base_url, handler = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.import_records(ctx, [{"text": "imported record", "record_id": "mem_imp1"}])

    post_reqs = [r for r in handler.requests if r["method"] == "POST" and "/import" in r["path"]]
    assert len(post_reqs) == 1
    req = post_reqs[0]
    body = json.loads(req["raw_body"])
    assert "context" in body
    assert "records" in body
    assert body["records"][0]["record_id"] == "mem_imp1"


def test_import_records_token_is_header_only(record_server):
    _, base_url, handler = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.import_records(ctx, [{"text": "content", "record_id": "mem_tok"}])

    import_req = next(r for r in handler.requests if r["method"] == "POST" and "/import" in r["path"])
    assert "test-token" not in import_req["path"]
    assert "test-token" not in import_req.get("raw_body", "")
    assert import_req["authorization"] == "Bearer test-token"


def test_import_records_returns_results_list(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    results = backend.import_records(ctx, [{"text": "rec a", "record_id": "mem_ra"}, {"text": "rec b", "record_id": "mem_rb"}])

    assert isinstance(results, list)
    assert len(results) == 2


def test_import_records_idempotent_via_stable_ids(record_server):
    _, base_url, handler = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.import_records(ctx, [{"text": "stable record", "record_id": "mem_stable"}])
    backend.import_records(ctx, [{"text": "stable record", "record_id": "mem_stable"}])

    all_scopes = handler.store
    total_records = sum(len(s) for s in all_scopes.values())
    assert total_records == 1


def test_import_records_scope_isolated(record_server):
    _, base_url, _ = record_server
    backend = HttpMemoryRecordBackend(base_url=base_url, token="test-token")
    derek = _ctx("derek", "thread-1")
    alex = _ctx("alex", "thread-2")

    backend.import_records(derek, [{"text": "derek import", "record_id": "mem_di"}])

    results = backend.search(alex, "derek import")
    assert results == []
