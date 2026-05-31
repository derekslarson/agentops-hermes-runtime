from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_memory_http import HttpMemoryBackend, register_http_memory_backend
from tools.memory_tool import MemoryStore


class _MemoryControlPlaneHandler(BaseHTTPRequestHandler):
    store: dict[tuple[tuple[str, str | None], ...], dict[str, str]] = {}
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
        keys = (
            "mode",
            "org_id",
            "workspace_id",
            "project_id",
            "external_channel_id",
            "external_thread_id",
            "conversation_id",
            "user_id",
        )
        return tuple((key, context.get(key)) for key in keys)

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/memory":
            self._send_json(404, {"error": "not found"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        context = json.loads(query.get("context", ["{}"])[0])
        target = query.get("target", ["memory"])[0]
        self.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "context": context,
                "target": target,
            }
        )
        content = self.store.get(self._scope_key(context), {}).get(target)
        self._send_json(200, {"content": content})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if urllib.parse.urlparse(self.path).path != "/memory":
            self._send_json(404, {"error": "not found"})
            return
        body, raw_body = self._read_body()
        context = body["context"]
        target = body.get("target", "memory")
        content = body["content"]
        action = body.get("action", "add")
        self.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "raw_body": raw_body,
                "authorization": self.headers.get("Authorization"),
                "context": context,
                "target": target,
                "action": action,
                "content": content,
            }
        )
        self.store.setdefault(self._scope_key(context), {})[target] = content
        self._send_json(200, {"ok": True})


@pytest.fixture
def memory_server():
    _MemoryControlPlaneHandler.store = {}
    _MemoryControlPlaneHandler.requests = []
    _MemoryControlPlaneHandler.expected_token = "test-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MemoryControlPlaneHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}", _MemoryControlPlaneHandler
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


def test_http_memory_backend_round_trips_through_native_memory_tool(memory_server):
    _, base_url, handler = memory_server
    backend = HttpMemoryBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")
    store = MemoryStore(backend=backend, context=ctx)
    store.load_from_disk()

    assert store.add("memory", "Derek likes concise runtime updates")["success"] is True
    assert store.read("memory")["entries"] == ["Derek likes concise runtime updates"]

    assert handler.requests[0]["method"] == "GET"
    assert handler.requests[-1]["method"] == "GET"
    assert all(request["context"]["org_id"] == "acme" for request in handler.requests)
    assert all(request["context"]["user_id"] == "derek" for request in handler.requests)


def test_http_memory_backend_keeps_runtime_context_scopes_isolated(memory_server):
    _, base_url, _ = memory_server
    backend = HttpMemoryBackend(base_url=base_url, token="test-token")
    derek = _ctx("derek", "thread-1")
    alex = _ctx("alex", "thread-2")

    derek_store = MemoryStore(backend=backend, context=derek)
    derek_store.load_from_disk()
    alex_store = MemoryStore(backend=backend, context=alex)
    alex_store.load_from_disk()

    derek_store.add("memory", "Derek-only remote fact")
    alex_store.add("memory", "Alex-only remote fact")

    derek_reader = MemoryStore(backend=backend, context=derek)
    derek_reader.load_from_disk()
    alex_reader = MemoryStore(backend=backend, context=alex)
    alex_reader.load_from_disk()

    assert derek_reader.read("memory")["entries"] == ["Derek-only remote fact"]
    assert alex_reader.read("memory")["entries"] == ["Alex-only remote fact"]
    assert "Alex-only remote fact" not in derek_reader.memory_entries
    assert "Derek-only remote fact" not in alex_reader.memory_entries


def test_http_memory_backend_registered_for_compose_profile(memory_server):
    _, base_url, _ = memory_server
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"memory": "compose-self-hosted"},
                "options": {"memory": {"base_url": base_url, "token": "test-token"}},
            }
        }
    )
    register_http_memory_backend(registry, profile="compose-self-hosted")
    ctx = _ctx("derek", "thread-1")

    backend = registry.get(BackendCapability.MEMORY, ctx)
    assert isinstance(backend, HttpMemoryBackend)

    store = MemoryStore(backend=backend, context=ctx)
    store.load_from_disk()
    assert store.add("memory", "registered compose memory")["success"] is True
    assert store.read("memory")["entries"] == ["registered compose memory"]


def test_local_multi_profile_remains_available_through_local_contract(tmp_path):
    from tools.memory_tool import LocalFileMemoryBackend

    backend = LocalFileMemoryBackend(base_dir=tmp_path, scope_by_context=True)
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="local-dev",
        user_id="derek",
        conversation_id="thread-1",
        backend_profile="local-multi",
    )
    store = MemoryStore(backend=backend, context=ctx)
    store.load_from_disk()

    assert store.add("memory", "local-multi memory contract")["success"] is True
    assert store.read("memory")["entries"] == ["local-multi memory contract"]


def test_http_memory_backend_fails_closed_without_base_url():
    with pytest.raises(ValueError, match="base_url"):
        HttpMemoryBackend(base_url="")


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
def test_http_memory_backend_rejects_unsafe_base_urls(base_url):
    with pytest.raises(ValueError, match="base_url"):
        HttpMemoryBackend(base_url=base_url)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "not-a-number"])
def test_http_memory_backend_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        HttpMemoryBackend(base_url="https://example.test", timeout=timeout)


def test_http_memory_backend_does_not_send_secret_token_in_payload(memory_server):
    _, base_url, handler = memory_server
    backend = HttpMemoryBackend(base_url=base_url, token="test-token")
    ctx = _ctx("derek", "thread-1")

    backend.write(ctx, "safe content", target="memory", action="add")

    post_request = next(request for request in handler.requests if request["method"] == "POST")
    assert "test-token" not in post_request["path"]
    assert "test-token" not in post_request["raw_body"]
    assert post_request["authorization"] == "Bearer test-token"


def test_http_memory_backend_errors_do_not_leak_scope_or_token(memory_server):
    _, base_url, _ = memory_server
    backend = HttpMemoryBackend(base_url=base_url, token="wrong-token")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="derek",
        conversation_id="thread-1",
        metadata={"secret_note": "SENTINEL_SCOPE_SECRET"},
    )

    with pytest.raises(RuntimeError) as excinfo:
        backend.read(ctx, target="memory")

    message = str(excinfo.value)
    assert "SENTINEL_SCOPE_SECRET" not in message
    assert "wrong-token" not in message
    assert "context=" not in message


def test_http_memory_backend_sends_minimal_scope_without_metadata_refs_or_run_ids(memory_server):
    _, base_url, handler = memory_server
    backend = HttpMemoryBackend(base_url=base_url, token="test-token")
    ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-ws",
        user_id="derek",
        conversation_id="thread-1",
        external_thread_id="slack-thread-1",
        project_id="runtime-mvp",
        run_id="run-should-not-scope-memory",
        job_id="job-should-not-scope-memory",
        permissions_ref="policy-secret-ref",
        delivery_ref="delivery-secret-ref",
        backend_profile="compose-self-hosted",
        metadata={"secret_note": "SENTINEL_SCOPE_SECRET"},
    )

    backend.read(ctx, target="memory")

    get_request = next(request for request in handler.requests if request["method"] == "GET")
    context = get_request["context"]
    assert context == {
        "mode": "agentops",
        "org_id": "acme",
        "workspace_id": "slack-ws",
        "project_id": "runtime-mvp",
        "external_channel_id": None,
        "external_thread_id": "slack-thread-1",
        "conversation_id": "thread-1",
        "user_id": "derek",
    }
    assert "SENTINEL_SCOPE_SECRET" not in get_request["path"]
