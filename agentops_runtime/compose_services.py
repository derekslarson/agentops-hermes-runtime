"""Minimal compose-self-hosted service processes for AgentOps Runtime.

These processes intentionally expose only health and configuration surfaces.
The real distributed behavior continues to live behind Hermes runtime backend
contracts; M12 composes those processes with database/queue/artifact/secret
services so follow-up slices can wire durable adapters without changing the
compose topology.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent.runtime_backends import RuntimeBackendRegistry
from agentops_runtime.compose_backends import configure_compose_runtime_backends

_SERVICE_PORTS = {
    "api": 8710,
    "worker": 8711,
    "scheduler": 8712,
    "local-secrets": 8713,
}

_REQUIRED_ENV = (
    "HERMES_RUNTIME_MODE",
    "HERMES_BACKEND_PROFILE",
    "AGENTOPS_DATABASE_URL",
    "AGENTOPS_QUEUE_URL",
    "AGENTOPS_ARTIFACT_ENDPOINT",
    "AGENTOPS_SECRET_STORE_URL",
)


def _health_payload(service: str) -> dict[str, Any]:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    payload: dict[str, Any] = {
        "ok": not missing,
        "service": service,
        "runtime_mode": os.getenv("HERMES_RUNTIME_MODE", ""),
        "backend_profile": os.getenv("HERMES_BACKEND_PROFILE", ""),
        "missing": missing,
    }
    if service == "worker":
        payload["max_concurrent_runs"] = int(os.getenv("AGENTOPS_WORKER_MAX_CONCURRENT_RUNS", "1"))
    if service in {"api", "worker", "scheduler"}:
        try:
            configure_compose_runtime_backends(RuntimeBackendRegistry(), environ=dict(os.environ))
            payload["compose_backends_configured"] = True
        except Exception as exc:  # noqa: BLE001 - fail closed on any wiring error
            payload["ok"] = False
            payload["compose_backends_configured"] = False
            payload["backend_error"] = str(exc)
    return payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentOpsRuntimeCompose/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/memory/records"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_memory_records("GET", parsed)
            return
        if self.path not in {"/healthz", "/readyz"}:
            self.send_error(404)
            return
        payload = _health_payload(self.server.service_name)  # type: ignore[attr-defined]
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200 if payload["ok"] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/memory/records"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_memory_records("POST", parsed)
            return
        self.send_error(404)

    def _dispatch_memory_records(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.memory_records_api import handle_memory_records_request

        body_bytes = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or "0")
            body_bytes = self.rfile.read(length)

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")
        backend = getattr(self.server, "memory_backend", None) or _get_or_create_memory_backend()

        status, response = handle_memory_records_request(
            method=method,
            path=parsed.path,
            query_string=parsed.query,
            body_bytes=body_bytes,
            auth_header=auth_header,
            token=token,
            backend=backend,
        )

        body = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        rendered = _sanitize_log_message(format % args)
        print(f"{self.server.service_name}: " + rendered, file=sys.stderr)  # type: ignore[attr-defined]


class _Server(ThreadingHTTPServer):
    service_name: str
    memory_backend: Any = None


_memory_backend_lock = threading.Lock()
_memory_backend_instance: Any = None


def _sanitize_log_message(message: str) -> str:
    """Redact /memory/records query strings from stdlib access logs."""
    return re.sub(r"(/memory/records[^\s?\"]*)\?[^\s\"]+", r"\1?<redacted>", message)


def _get_or_create_memory_backend() -> Any:
    global _memory_backend_instance
    with _memory_backend_lock:
        if _memory_backend_instance is None:
            from agent.runtime_backends import LocalDeepMemoryBackend

            _memory_backend_instance = LocalDeepMemoryBackend(partition=True)
        return _memory_backend_instance


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    service = args[0] if args else "api"
    if service not in _SERVICE_PORTS:
        print(f"unknown AgentOps compose service: {service}", file=sys.stderr)
        return 2

    port = int(os.getenv("AGENTOPS_SERVICE_PORT", str(_SERVICE_PORTS[service])))
    server = _Server(("0.0.0.0", port), _Handler)
    server.service_name = service
    stopped = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"AgentOps {service} listening on :{port}", flush=True)
    server.serve_forever(poll_interval=0.2)
    stopped.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
