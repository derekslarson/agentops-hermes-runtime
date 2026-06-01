"""Tests for the M8 HTTP cron backend adapter.

The adapter is the first remote cron transport: it preserves the generic
CronBackend logical job model while keeping tenant scope in RuntimeContext and
secrets out of URLs/payloads.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_cron_http import HttpCronBackend, register_http_cron_backend


class _CronHandler(BaseHTTPRequestHandler):
    server: "_CronServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.path == "/cron/jobs":
            self._json({"id": "remote-job-1"})
            return
        if self.path == "/cron/jobs/claim-due":
            if getattr(self.server, "missing_claim_jobs", False):
                self._json({})
            else:
                self._json({"jobs": [{"id": "remote-job-1", "owner": body["owner"]}]})
            return
        if self.path == "/cron/jobs/remote-job-1/renew-lease":
            if getattr(self.server, "invalid_renewed", False):
                self._json({"renewed": "false"})
            else:
                self._json({"renewed": True})
            return
        if self.path == "/cron/jobs/remote-job-1/release-lease":
            self._json({})
            return
        if self.path == "/cron/jobs/remote-job-1/complete":
            self._json({"status": "success", "delivery": "skipped_silent", "silent": True})
            return
        if self.path == "/cron/jobs/remote-job-1/fail":
            self._json({"status": "error", "delivery": "error_alert"})
            return
        if self.path.endswith("/pause") or self.path.endswith("/resume"):
            self._json({})
            return
        self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        parsed = urlparse(self.path)
        context = json.loads(self.headers["X-Hermes-Runtime-Context"])
        assert "perm-ref-should-not-leak" not in self.path
        assert "delivery:telegram:home" not in self.path
        if parsed.path == "/cron/jobs":
            if getattr(self.server, "missing_list_jobs", False):
                self._json({})
            elif getattr(self.server, "invalid_jobs", False):
                self._json({"jobs": ["not-an-object"]})
            else:
                self._json({"jobs": [{"id": "remote-job-1", "binding": context}]})
            return
        if parsed.path == "/cron/jobs/remote-job-1":
            self._json({"job": {"id": "remote-job-1", "binding": context}})
            return
        if parsed.path == "/cron/jobs/remote-job-1/history":
            self._json({"history": [{"status": "success"}]})
            return
        self.send_error(404)

    def do_PATCH(self) -> None:  # noqa: N802
        self.server.requests.append(("PATCH", self.path, self.headers.get("Authorization"), self._body()))
        self._json({})

    def do_DELETE(self) -> None:  # noqa: N802
        self.server.requests.append(("DELETE", self.path, self.headers.get("Authorization"), None))
        self._json({})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _CronServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any] | None]]
    invalid_renewed: bool
    invalid_jobs: bool
    missing_list_jobs: bool
    missing_claim_jobs: bool


def _ctx(**overrides: Any) -> RuntimeContext:
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="agentops-runtime",
        run_type="cron",
        backend_profile="compose-self-hosted",
        delivery_ref="delivery:telegram:home",
        permissions_ref="secret:perm-ref-should-not-leak",
    )
    base.update(overrides)
    return RuntimeContext(**base)


@pytest.fixture
def cron_server():
    server = _CronServer(("127.0.0.1", 0), _CronHandler)
    server.requests = []
    server.invalid_renewed = False
    server.invalid_jobs = False
    server.missing_list_jobs = False
    server.missing_claim_jobs = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_cron_backend_round_trips_contract_methods_with_secret_safe_scope(cron_server):
    backend = HttpCronBackend(f"http://127.0.0.1:{cron_server.server_port}", token="sentinel-token")
    context = _ctx()

    job_id = backend.create(
        context,
        {
            "prompt": "p",
            "schedule": "30m",
            "delivery_ref": "secret:nope",
            "metadata": {"api_token": "nested-sentinel-token", "apiKey": "nested-sentinel-api-key", "safe": "kept"},
            "headers": {"X-Api-Key": "nested-sentinel-header-key"},
            "tool_options": [{"password": "nested-sentinel-password"}],
        },
    )
    assert job_id == "remote-job-1"
    assert backend.list_jobs(context)[0]["binding"]["org_id"] == "acme"
    assert backend.get_job(context, job_id)["id"] == job_id
    backend.update(context, job_id, {"prompt": "p2", "binding": {"conversation_id": "evil", "delivery_ref": "evil"}})
    backend.pause(context, job_id)
    backend.resume(context, job_id)
    assert backend.claim_due(context, owner="worker-a", now=150.0, lease_seconds=60.0)[0]["owner"] == "worker-a"
    assert backend.renew_lease(context, job_id, owner="worker-a", now=151.0, lease_seconds=60.0) is True
    backend.release_lease(context, job_id, owner="worker-a")
    assert backend.complete_run(
        context,
        job_id,
        owner="worker-a",
        output="[SILENT]",
        delivery_error="failed token=delivery-secret-sentinel",
        now=160.0,
    )["silent"] is True
    assert backend.fail_run(context, job_id, owner="worker-a", error="boom token=failure-secret-sentinel", now=170.0)["delivery"] == "error_alert"
    assert backend.run_history(context, job_id)[0]["status"] == "success"
    backend.remove(context, job_id)

    bodies = [body for *_prefix, body in cron_server.requests if body]
    assert all("sentinel-token" not in repr(body) for body in bodies)
    assert all("nested-sentinel-token" not in repr(body) for body in bodies)
    assert all("nested-sentinel-api-key" not in repr(body) for body in bodies)
    assert all("nested-sentinel-header-key" not in repr(body) for body in bodies)
    assert all("nested-sentinel-password" not in repr(body) for body in bodies)
    assert all("secret:nope" not in repr(body) for body in bodies)
    assert all("perm-ref-should-not-leak" not in repr(body) for body in bodies)
    assert all("delivery-secret-sentinel" not in repr(body) for body in bodies)
    assert all("failure-secret-sentinel" not in repr(body) for body in bodies)
    assert any(body.get("context", {}).get("delivery_ref") == "delivery:telegram:home" for body in bodies)
    job_bodies = [body.get("job", {}) for method, _path, _auth, body in cron_server.requests if method in {"POST", "PATCH"} and body and "job" in body]
    assert job_bodies
    assert all("binding" not in job for job in job_bodies)
    assert all("evil" not in repr(job) for job in job_bodies)
    assert all(auth in {"Bearer sentinel-token", None} for _method, _path, auth, _body in cron_server.requests)
    assert all("?" not in path for method, path, _auth, _body in cron_server.requests if method in {"GET", "DELETE"})


def test_http_cron_backend_fails_closed_for_malformed_control_plane_responses(cron_server):
    backend = HttpCronBackend(f"http://127.0.0.1:{cron_server.server_port}")
    cron_server.invalid_renewed = True
    with pytest.raises(RuntimeError, match="boolean renewed"):
        backend.renew_lease(_ctx(), "remote-job-1", owner="worker-a")

    cron_server.invalid_jobs = True
    with pytest.raises(RuntimeError, match="every job"):
        backend.list_jobs(_ctx())
    cron_server.invalid_jobs = False

    cron_server.missing_list_jobs = True
    with pytest.raises(RuntimeError, match="jobs list"):
        backend.list_jobs(_ctx())
    cron_server.missing_list_jobs = False

    cron_server.missing_claim_jobs = True
    with pytest.raises(RuntimeError, match="claimed jobs list"):
        backend.claim_due(_ctx(), owner="worker-a")


def test_http_cron_backend_error_messages_do_not_include_context_or_response_body(cron_server):
    backend = HttpCronBackend(f"http://127.0.0.1:{cron_server.server_port}", token="sentinel-token")

    with pytest.raises(RuntimeError) as exc_info:
        backend.get_job(_ctx(delivery_ref="delivery:secret-sentinel"), "missing")

    message = str(exc_info.value)
    assert "sentinel-token" not in message
    assert "delivery:secret-sentinel" not in message
    assert "perm-ref-should-not-leak" not in message
    assert "Error response" not in message


def test_agentops_cron_binding_registers_http_backend_for_remote_profile(cron_server):
    from agent.agent_init import _bind_agentops_cron_backend
    from agent.runtime_cron import clear_active_cron_backend, get_active_cron_backend

    clear_active_cron_backend()
    context = _ctx()
    try:
        _bind_agentops_cron_backend(
            SimpleNamespace(runtime_context=context),
            {
                "backends": {
                    "capabilities": {"cron": "compose-self-hosted"},
                    "options": {"cron": {"base_url": f"http://127.0.0.1:{cron_server.server_port}"}},
                }
            },
        )

        backend = get_active_cron_backend(context)
        assert isinstance(backend, HttpCronBackend)
        assert backend.create(context, {"prompt": "p"}) == "remote-job-1"
    finally:
        clear_active_cron_backend()


def test_agentops_cron_binding_fails_closed_when_remote_profile_is_misconfigured():
    from agent.agent_init import _bind_agentops_cron_backend
    from agent.runtime_cron import clear_active_cron_backend, get_active_cron_backend

    clear_active_cron_backend()
    context = _ctx()
    try:
        _bind_agentops_cron_backend(
            SimpleNamespace(runtime_context=context),
            {"backends": {"capabilities": {"cron": "compose-self-hosted"}, "options": {"cron": {}}}},
        )

        backend = get_active_cron_backend(context)
        assert backend is not None
        with pytest.raises(RuntimeError, match="AgentOps cron backend is unavailable"):
            backend.create(context, {"prompt": "p"})
    finally:
        clear_active_cron_backend()


def test_http_cron_backend_registers_for_compose_profile(cron_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"cron": "compose-self-hosted"},
                "options": {"cron": {"base_url": f"http://127.0.0.1:{cron_server.server_port}", "token": "sentinel-token"}},
            }
        }
    )
    register_http_cron_backend(registry)

    backend = registry.get(BackendCapability.CRON, _ctx())
    assert isinstance(backend, HttpCronBackend)
    assert backend.create(_ctx(), {"prompt": "p"}) == "remote-job-1"


def test_http_cron_backend_registry_preserves_fail_closed_timeout_validation(cron_server):
    registry = RuntimeBackendRegistry(
        {
            "backends": {
                "capabilities": {"cron": "compose-self-hosted"},
                "options": {"cron": {"base_url": f"http://127.0.0.1:{cron_server.server_port}", "timeout": 0}},
            }
        }
    )
    register_http_cron_backend(registry)

    with pytest.raises(ValueError, match="timeout"):
        registry.get(BackendCapability.CRON, _ctx())


def test_http_cron_backend_fails_closed_for_unsafe_connection_settings():
    with pytest.raises(ValueError):
        HttpCronBackend("")
    with pytest.raises(ValueError):
        HttpCronBackend("https://user:pass@example.test")
    with pytest.raises(ValueError):
        HttpCronBackend("https://example.test/path?token=***")
    with pytest.raises(ValueError):
        HttpCronBackend("https://example.test:bad-port")
    with pytest.raises(ValueError):
        HttpCronBackend("https://example.test", timeout=float("nan"))
