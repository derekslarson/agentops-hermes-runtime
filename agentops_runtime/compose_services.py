"""Compose-self-hosted service processes for AgentOps Runtime.

These lightweight processes expose health/readiness surfaces plus the MVP
control-plane endpoints needed by distributed Hermes workers. Durable runtime
behavior still lives behind Hermes backend contracts; compose API wiring selects
explicit SQLite or PostgreSQL/pgvector deep-memory backends without changing the
topology, and missing deep-memory configuration fails closed.
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
from agentops_runtime.compose_backends import (
    COMPOSE_REQUIRED_CAPABILITIES,
    configure_compose_runtime_backends,
    validate_compose_backend_registration,
)

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
    if service == "api" and not (os.getenv("AGENTOPS_DEEP_MEMORY_DB_URL") or "").strip():
        missing = missing + ["AGENTOPS_DEEP_MEMORY_DB_URL"]
    payload: dict[str, Any] = {
        "ok": not missing,
        "service": service,
        "missing": missing,
    }
    if service == "worker":
        payload["max_concurrent_runs"] = int(os.getenv("AGENTOPS_WORKER_MAX_CONCURRENT_RUNS", "1"))
    if service in {"api", "worker", "scheduler"}:
        try:
            registry = RuntimeBackendRegistry()
            configure_compose_runtime_backends(registry, environ=dict(os.environ))
            validate_compose_backend_registration(registry)
            payload["compose_backends_configured"] = True
        except Exception as exc:  # noqa: BLE001 - fail closed on any wiring error
            payload["ok"] = False
            payload["compose_backends_configured"] = False
            payload["backend_error"] = _safe_backend_error(exc)
    return payload


def _safe_backend_error(exc: Exception) -> str:
    message = str(exc)
    exact_safe_messages = {
        "compose backend wiring requires a control-plane base URL",
        "compose backend base URL must be an absolute http(s) URL",
        "compose backend base URL must not contain credentials",
        "compose backend base URL must not contain query or fragment",
    }
    if message in exact_safe_messages:
        return message
    prefix = "compose backend registration incomplete; missing capabilities: "
    if message.startswith(prefix):
        names = message.removeprefix(prefix).split(", ")
        allowed = {cap.value for cap in COMPOSE_REQUIRED_CAPABILITIES}
        if names and all(name in allowed for name in names):
            return f"{prefix}{', '.join(names)}"
    return "compose backend wiring failed"


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
        if parsed.path == "/artifacts" or parsed.path.startswith("/artifacts/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_artifacts("GET", parsed)
            return
        if parsed.path == "/audit":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_audit("GET", parsed)
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
        if parsed.path == "/artifacts":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_artifacts("POST", parsed)
            return
        if parsed.path == "/audit":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_audit("POST", parsed)
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
        try:
            backend = getattr(self.server, "memory_backend", None) or _get_or_create_memory_backend()
        except Exception:
            error_body = json.dumps({"error": "memory backend unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

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

    def _dispatch_artifacts(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.artifacts_api import handle_artifact_request

        body_bytes = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or "0")
            body_bytes = self.rfile.read(length)

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")
        if token and auth_header != f"Bearer {token}":
            error_body = json.dumps({"error": "unauthorized"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        try:
            backend = getattr(self.server, "artifact_backend", None) or _get_or_create_artifact_backend()
        except Exception:
            error_body = json.dumps({"error": "artifact backend unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_artifact_request(
            method=method,
            path=parsed.path,
            query_string=parsed.query,
            body_bytes=body_bytes,
            auth_header=auth_header,
            token=token,
            backend=backend,
        )

        if isinstance(response, bytes):
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        else:
            body = json.dumps(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _dispatch_audit(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.audit_api import handle_audit_request

        body_bytes = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or "0")
            body_bytes = self.rfile.read(length)

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")
        if token and auth_header != f"Bearer {token}":
            error_body = json.dumps({"error": "unauthorized"}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        try:
            backend = getattr(self.server, "audit_backend", None) or _get_or_create_audit_backend()
        except Exception:
            error_body = json.dumps({"error": "audit backend unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_audit_request(
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
    artifact_backend: Any = None
    audit_backend: Any = None


_memory_backend_lock = threading.Lock()
_memory_backend_instance: Any = None

_artifact_backend_lock = threading.Lock()
_artifact_backend_instance: Any = None

_audit_backend_lock = threading.Lock()
_audit_backend_instance: Any = None


def _sanitize_log_message(message: str) -> str:
    """Redact /memory/records query strings and /artifacts refs/query strings from access logs."""

    def _redact_artifact_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    message = re.sub(r"(/artifacts)/[^\s?\"]+(\?[^\s\"]+)?", _redact_artifact_path, message)
    return re.sub(r"(/(?:memory/records|artifacts|audit)[^\s?\"]*)\?[^\s]+", r"\1?<redacted>", message)


def _make_memory_backend(environ: dict[str, str]) -> Any:
    """Create a MemoryRecordBackend from the given environment mapping.

    AGENTOPS_DEEP_MEMORY_DB_URL is required for compose API memory-record
    backend construction.  No local fallback is provided: missing or unsafe
    deep-memory configuration fails closed with a sanitized error.

    Selection order:
    1. AGENTOPS_DEEP_MEMORY_DB_URL=sqlite:///... → RelationalMemoryRecordBackend
    2. AGENTOPS_DEEP_MEMORY_STORE=sqlite + explicit AGENTOPS_DEEP_MEMORY_DB_URL → same
    3. AGENTOPS_DEEP_MEMORY_DB_URL=postgresql://... → live PostgreSQL/pgvector adapter

    Raises ValueError when unconfigured, when AGENTOPS_DEEP_MEMORY_STORE is set
    without an explicit AGENTOPS_DEEP_MEMORY_DB_URL, or when the URL scheme does
    not match the requested store type.
    """
    db_url = environ.get("AGENTOPS_DEEP_MEMORY_DB_URL", "").strip()
    store_type = environ.get("AGENTOPS_DEEP_MEMORY_STORE", "").lower().strip()

    if store_type and store_type not in ("sqlite", "postgres", "postgresql"):
        raise ValueError("unsupported deep-memory store type")

    if not db_url:
        raise ValueError(
            "compose deep-memory backend requires an explicit AGENTOPS_DEEP_MEMORY_DB_URL; "
            "no local fallback is provided for compose/cloud profiles"
        )

    parsed = urllib.parse.urlparse(db_url)
    if store_type in ("postgres", "postgresql") and parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError("postgres deep-memory store requires a postgres:// or postgresql:// URL")
    if store_type == "sqlite" and parsed.scheme != "sqlite":
        raise ValueError("sqlite deep-memory store requires a sqlite:/// URL")

    from agentops_runtime.memory_record_store import make_relational_memory_backend

    return make_relational_memory_backend(db_url)


def _get_or_create_memory_backend() -> Any:
    global _memory_backend_instance
    with _memory_backend_lock:
        if _memory_backend_instance is None:
            _memory_backend_instance = _make_memory_backend(dict(os.environ))
        return _memory_backend_instance


def _make_artifact_backend(environ: dict[str, str]) -> Any:
    root = environ.get("AGENTOPS_ARTIFACT_ROOT", "").strip()
    if not root:
        raise ValueError(
            "compose artifact backend requires AGENTOPS_ARTIFACT_ROOT; "
            "no local fallback is provided for compose/cloud profiles"
        )
    from agent.runtime_artifacts_audit import LocalFileArtifactBackend

    return LocalFileArtifactBackend(root=root)


def _get_or_create_artifact_backend() -> Any:
    global _artifact_backend_instance
    with _artifact_backend_lock:
        if _artifact_backend_instance is None:
            _artifact_backend_instance = _make_artifact_backend(dict(os.environ))
        return _artifact_backend_instance


def _make_audit_backend(environ: dict[str, str]) -> Any:
    root = environ.get("AGENTOPS_ARTIFACT_ROOT", "").strip()
    if not root:
        raise ValueError(
            "compose audit backend requires AGENTOPS_ARTIFACT_ROOT; "
            "no local fallback is provided for compose/cloud profiles"
        )
    from agent.runtime_artifacts_audit import LocalFileAuditBackend

    return LocalFileAuditBackend(root=root)


def _get_or_create_audit_backend() -> Any:
    global _audit_backend_instance
    with _audit_backend_lock:
        if _audit_backend_instance is None:
            _audit_backend_instance = _make_audit_backend(dict(os.environ))
        return _audit_backend_instance


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
