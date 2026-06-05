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

_MAX_SESSION_REQUEST_BODY_BYTES = 1_048_576
_MAX_CREDENTIAL_REQUEST_BODY_BYTES = 1_048_576
_MAX_SECRET_REQUEST_BODY_BYTES = 1_048_576
_MAX_CURATED_MEMORY_REQUEST_BODY_BYTES = 1_048_576
_MAX_SKILL_REQUEST_BODY_BYTES = 1_048_576
_MAX_QUEUE_REQUEST_BODY_BYTES = 1_048_576
_MAX_RUN_LEASE_REQUEST_BODY_BYTES = 1_048_576
_MAX_CRON_REQUEST_BODY_BYTES = 1_048_576
_MAX_CONVERSATION_REQUEST_BODY_BYTES = 1_048_576

_SKILL_EXACT_PATHS = frozenset({"/skills/list", "/skills/view", "/skills/manage"})
_QUEUE_EXACT_PATHS = frozenset({
    "/queue/enqueue",
    "/queue/claim",
    "/queue/ack",
    "/queue/nack",
    "/queue/extend-lease",
})

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
        raw_path = self.path.split("?", 1)[0]
        if parsed.path.startswith("/memory/records"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_memory_records("GET", parsed)
            return
        if parsed.path == "/memory" and raw_path == "/memory":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_curated_memory("GET", parsed)
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
        if raw_path == "/cron/jobs" or raw_path.startswith("/cron/jobs/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_cron("GET", raw_path)
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
        raw_path = self.path.split("?", 1)[0]
        if parsed.path.startswith("/memory/records"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_memory_records("POST", parsed)
            return
        if parsed.path == "/memory" and raw_path == "/memory":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_curated_memory("POST", parsed)
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
        if parsed.path.startswith("/sessions/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_sessions("POST", parsed)
            return
        if parsed.path == "/credentials/resolve":
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_credentials("POST", parsed)
            return
        if parsed.path in ("/secrets/get", "/secrets/put"):
            if self.server.service_name != "local-secrets":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_secrets("POST", parsed)
            return
        # Queue and Skills: exact raw-path match prevents semicolon alias normalization by urllib.
        raw_path = self.path.split("?", 1)[0]
        if raw_path in _QUEUE_EXACT_PATHS:
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_queue(raw_path)
            return
        if raw_path in _SKILL_EXACT_PATHS:
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_skills(raw_path)
            return
        if raw_path.startswith("/run-leases/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_run_leases(raw_path)
            return
        if raw_path == "/cron/jobs" or raw_path.startswith("/cron/jobs/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_cron("POST", raw_path)
            return
        if raw_path == "/conversations/resolve" or raw_path.startswith("/conversations/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_conversations(raw_path)
            return
        self.send_error(404)

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        raw_path = self.path.split("?", 1)[0]
        if raw_path.startswith("/cron/jobs/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_cron("PATCH", raw_path)
            return
        self.send_error(404)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        raw_path = self.path.split("?", 1)[0]
        if raw_path.startswith("/cron/jobs/"):
            if self.server.service_name != "api":  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._dispatch_cron("DELETE", raw_path)
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

    def _dispatch_curated_memory(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.curated_memory_api import handle_curated_memory_request

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

        body_bytes = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json_error(400, "invalid content length")
                return
            if length < 0:
                self._send_json_error(400, "invalid content length")
                return
            if length > _MAX_CURATED_MEMORY_REQUEST_BODY_BYTES:
                self._send_json_error(413, "request body too large")
                return
            body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "curated_memory_backend", None) or _get_or_create_curated_memory_backend()
        except Exception:
            error_body = json.dumps({"error": "curated memory backend unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_curated_memory_request(
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

    def _dispatch_sessions(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.sessions_api import handle_session_request

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

        body_bytes = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json_error(400, "invalid content length")
                return
            if length < 0:
                self._send_json_error(400, "invalid content length")
                return
            if length > _MAX_SESSION_REQUEST_BODY_BYTES:
                self._send_json_error(413, "request body too large")
                return
            body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "session_backend", None) or _get_or_create_session_backend()
        except Exception:
            error_body = json.dumps({"error": "session backend unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_session_request(
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

    def _dispatch_credentials(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.credentials_api import handle_credential_request

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

        body_bytes = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json_error(400, "invalid content length")
                return
            if length < 0:
                self._send_json_error(400, "invalid content length")
                return
            if length > _MAX_CREDENTIAL_REQUEST_BODY_BYTES:
                self._send_json_error(413, "request body too large")
                return
            body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "credential_backend", None) or _get_or_create_credential_resolver()
        except Exception:
            error_body = json.dumps({"error": "credential resolver unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_credential_request(
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

    def _dispatch_secrets(self, method: str, parsed: urllib.parse.ParseResult) -> None:
        from agentops_runtime.secrets_api import handle_secret_request

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

        body_bytes = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json_error(400, "invalid content length")
                return
            if length < 0:
                self._send_json_error(400, "invalid content length")
                return
            if length > _MAX_SECRET_REQUEST_BODY_BYTES:
                self._send_json_error(413, "request body too large")
                return
            body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "secret_backend", None) or _get_or_create_secret_backend()
        except Exception:
            error_body = json.dumps({"error": "secret store unavailable"}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        status, response = handle_secret_request(
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

    def _dispatch_skills(self, raw_path: str) -> None:
        from agentops_runtime.skills_api import handle_skill_request

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")

        if token and auth_header != f"Bearer {token}":
            self._send_json_error(401, "unauthorized")
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json_error(400, "invalid content length")
            return
        if length < 0:
            self._send_json_error(400, "invalid content length")
            return
        if length > _MAX_SKILL_REQUEST_BODY_BYTES:
            self._send_json_error(413, "request body too large")
            return
        body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "skill_backend", None) or _get_or_create_skill_backend()
        except Exception:
            self._send_json_error(503, "skill backend unavailable")
            return

        status, response = handle_skill_request(
            method="POST",
            path=raw_path,
            query_string="",
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

    def _dispatch_queue(self, raw_path: str) -> None:
        from agentops_runtime.queue_api import handle_queue_request

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")

        if token and auth_header != f"Bearer {token}":
            self._send_json_error(401, "unauthorized")
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json_error(400, "invalid content length")
            return
        if length < 0:
            self._send_json_error(400, "invalid content length")
            return
        if length > _MAX_QUEUE_REQUEST_BODY_BYTES:
            self._send_json_error(413, "request body too large")
            return
        body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "queue_backend", None) or _get_or_create_queue_backend()
        except Exception:
            self._send_json_error(503, "queue backend unavailable")
            return

        status, response = handle_queue_request(
            method="POST",
            path=raw_path,
            query_string="",
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

    def _dispatch_run_leases(self, raw_path: str) -> None:
        from agentops_runtime.run_leases_api import handle_run_lease_request, is_run_lease_route

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")

        if token and auth_header != f"Bearer {token}":
            self._send_json_error(401, "unauthorized")
            return

        if not is_run_lease_route(raw_path):
            self._send_json_error(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json_error(400, "invalid content length")
            return
        if length < 0:
            self._send_json_error(400, "invalid content length")
            return
        if length > _MAX_RUN_LEASE_REQUEST_BODY_BYTES:
            self._send_json_error(413, "request body too large")
            return
        body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "run_lease_backend", None) or _get_or_create_run_lease_backend()
        except Exception:
            self._send_json_error(503, "run-lease backend unavailable")
            return

        status, response = handle_run_lease_request(
            method="POST",
            path=raw_path,
            query_string="",
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

    def _dispatch_cron(self, method: str, raw_path: str) -> None:
        from agentops_runtime.cron_api import handle_cron_request, is_cron_route

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")

        if token and auth_header != f"Bearer {token}":
            self._send_json_error(401, "unauthorized")
            return

        if not is_cron_route(raw_path):
            self._send_json_error(404, "not found")
            return

        body_bytes = b""
        if method in {"POST", "PATCH"}:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json_error(400, "invalid content length")
                return
            if length < 0:
                self._send_json_error(400, "invalid content length")
                return
            if length > _MAX_CRON_REQUEST_BODY_BYTES:
                self._send_json_error(413, "request body too large")
                return
            body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "cron_backend", None) or _get_or_create_cron_backend()
        except Exception:
            self._send_json_error(503, "cron backend unavailable")
            return

        context_header = self.headers.get("X-Hermes-Runtime-Context")
        status, response = handle_cron_request(
            method=method,
            path=raw_path,
            query_string="",
            body_bytes=body_bytes,
            auth_header=auth_header,
            token=token,
            backend=backend,
            context_header=context_header,
        )

        body = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch_conversations(self, raw_path: str) -> None:
        from agentops_runtime.conversations_api import handle_conversation_request, is_conversation_route

        token = os.getenv("AGENTOPS_RUNTIME_TOKEN") or None
        auth_header = self.headers.get("Authorization")

        if token and auth_header != f"Bearer {token}":
            self._send_json_error(401, "unauthorized")
            return

        if not is_conversation_route(raw_path):
            self._send_json_error(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json_error(400, "invalid content length")
            return
        if length < 0:
            self._send_json_error(400, "invalid content length")
            return
        if length > _MAX_CONVERSATION_REQUEST_BODY_BYTES:
            self._send_json_error(413, "request body too large")
            return
        body_bytes = self.rfile.read(length)

        try:
            backend = getattr(self.server, "conversation_router_backend", None) or _get_or_create_conversation_router_backend()
        except Exception:
            self._send_json_error(503, "conversation router unavailable")
            return

        status, response = handle_conversation_request(
            method="POST",
            path=raw_path,
            query_string="",
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

    def _send_json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
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
    curated_memory_backend: Any = None
    artifact_backend: Any = None
    audit_backend: Any = None
    session_backend: Any = None
    credential_backend: Any = None
    secret_backend: Any = None
    skill_backend: Any = None
    queue_backend: Any = None
    run_lease_backend: Any = None
    cron_backend: Any = None
    conversation_router_backend: Any = None


_memory_backend_lock = threading.Lock()
_memory_backend_instance: Any = None

_artifact_backend_lock = threading.Lock()
_artifact_backend_instance: Any = None

_audit_backend_lock = threading.Lock()
_audit_backend_instance: Any = None

_session_backend_lock = threading.Lock()
_session_backend_instance: Any = None

_credential_resolver_lock = threading.Lock()
_credential_resolver_instance: Any = None

_secret_backend_lock = threading.Lock()
_secret_backend_instance: Any = None

_curated_memory_backend_lock = threading.Lock()
_curated_memory_backend_instance: Any = None

_skill_backend_lock = threading.Lock()
_skill_backend_instance: Any = None

_queue_backend_lock = threading.Lock()
_queue_backend_instance: Any = None

_run_lease_backend_lock = threading.Lock()
_run_lease_backend_instance: Any = None

_cron_backend_lock = threading.Lock()
_cron_backend_instance: Any = None

_conversation_router_backend_lock = threading.Lock()
_conversation_router_backend_instance: Any = None

_SESSION_STATIC_PATH_SEGMENTS = frozenset({
    "create", "append", "messages", "search", "turn-lock",
})


def _sanitize_log_message(message: str) -> str:
    """Redact sensitive path segments and query strings from access logs."""

    def _redact_artifact_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_secret_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_session_id_path(match: re.Match[str]) -> str:
        segment = match.group(2)
        if segment in _SESSION_STATIC_PATH_SEGMENTS:
            return match.group(0)
        rest = match.group(3) or ""
        query = "?<redacted>" if match.group(4) else ""
        return f"{match.group(1)}/<redacted>{rest}{query}"

    def _redact_queue_invalid_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}<redacted>{query}"

    def _redact_encoded_queue_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_queue_prefix_lookalike(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_run_lease_invalid_path(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_run_lease_prefix_lookalike(match: re.Match[str]) -> str:
        query = "?<redacted>" if match.group(2) else ""
        return f"{match.group(1)}/<redacted>{query}"

    def _redact_run_lease_key_path(match: re.Match[str]) -> str:
        key_seg = match.group(2)
        action = match.group(3)
        query = "?<redacted>" if match.group(4) else ""
        if key_seg == "expire-stale" and action is None:
            return f"{match.group(1)}/expire-stale{query}"
        action_str = "" if action is None else action.split(";", 1)[0]
        return f"{match.group(1)}/<redacted>{action_str}{query}"

    def _redact_cron_job_id_path(match: re.Match[str]) -> str:
        prefix = match.group(1)
        job_id_seg = match.group(2)
        rest = match.group(3)
        query = "?<redacted>" if match.group(4) else ""
        if job_id_seg == "claim-due" and rest is None:
            return f"{prefix}/claim-due{query}"
        if rest is None:
            return f"{prefix}/<redacted>{query}"
        rest_str = rest.split(";", 1)[0]
        if rest_str in {"/pause", "/resume", "/renew-lease", "/release-lease", "/complete", "/fail", "/history"}:
            return f"{prefix}/<redacted>{rest_str}{query}"
        return f"{prefix}/<redacted>/<redacted>{query}"

    message = re.sub(r"(/artifacts)/[^\s?\"]+(\?[^\s\"]+)?", _redact_artifact_path, message)
    message = re.sub(
        r"(/secrets)/(?!get(?:[?\s\"]|$)|put(?:[?\s\"]|$))[^\s?\"]+(\?[^\s\"]+)?",
        _redact_secret_path,
        message,
    )
    message = re.sub(
        r"(/sessions)/([^/\s?\"]+)(/[^\s?\"]*)?(\?[^\s\"]+)?",
        _redact_session_id_path,
        message,
    )
    message = re.sub(
        r"(/queue)%2[fF][^\s?\"]+(\?[^\s\"]+)?",
        _redact_encoded_queue_path,
        message,
    )
    message = re.sub(
        r"(/queue)(?!/|%2[fF]|[?\s\"]|$)[^\s?\"]+(\?[^\s\"]+)?",
        _redact_queue_prefix_lookalike,
        message,
    )
    message = re.sub(
        r"(/queue/)(?!enqueue(?:[?\s\"]|$)|claim(?:[?\s\"]|$)|ack(?:[?\s\"]|$)|nack(?:[?\s\"]|$)|extend-lease(?:[?\s\"]|$))[^\s?\"]+(\?[^\s\"]+)?",
        _redact_queue_invalid_path,
        message,
    )
    message = re.sub(
        r"(/run-leases)%2[fF][^\s?\"]+(\?[^\s\"]+)?",
        _redact_run_lease_invalid_path,
        message,
    )
    message = re.sub(
        r"(/run-leases)(?!/|%2[fF]|[?\s\"]|$)[^\s?\"]+(\?[^\s\"]+)?",
        _redact_run_lease_prefix_lookalike,
        message,
    )
    message = re.sub(
        r"(/run-leases)/([^/\s?\"]+)(/[^\s?\"]*)?(\?[^\s\"]+)?",
        _redact_run_lease_key_path,
        message,
    )
    # Cron: encoded %2F after /cron or /cron/jobs
    message = re.sub(
        r"(/cron(?:/jobs)?)%2[fF][^\s?\"]*(\?[^\s\"]+)?",
        lambda m: f"{m.group(1)}/<redacted>{'?<redacted>' if m.group(2) else ''}",
        message,
    )
    # Cron: prefix lookalike (/cronXYZ, /cron/jobsXYZ)
    message = re.sub(
        r"(/cron)(?!/|%2[fF]|[?\s\"]|$)[^\s?\"]+(\?[^\s\"]+)?",
        lambda m: f"{m.group(1)}/<redacted>{'?<redacted>' if m.group(2) else ''}",
        message,
    )
    message = re.sub(
        r"(/cron/jobs)(?!/|%2[fF]|[?\s\"]|$)[^\s?\"]+(\?[^\s\"]+)?",
        lambda m: f"{m.group(1)}/<redacted>{'?<redacted>' if m.group(2) else ''}",
        message,
    )
    # Cron: /cron/jobs/<job_id>[/<action>] — redact job ID, keep static action name
    message = re.sub(
        r"(/cron/jobs)/([^/\s?\"]+)(/[^\s?\"]*)?(\?[^\s\"]+)?",
        _redact_cron_job_id_path,
        message,
    )
    def _redact_conversation_id_path(match: re.Match[str]) -> str:
        prefix = match.group(1)
        seg = match.group(2)
        rest = match.group(3)
        query = "?<redacted>" if match.group(4) else ""
        if seg == "resolve" and rest is None:
            return f"{prefix}/{seg}{query}"
        if rest is None:
            return f"{prefix}/<redacted>{query}"
        rest_clean = rest.split(";", 1)[0]
        if rest_clean in {"/active-run", "/route"}:
            return f"{prefix}/<redacted>{rest_clean}{query}"
        return f"{prefix}/<redacted>/<redacted>{query}"

    # Conversations: encoded %2F after /conversations
    message = re.sub(
        r"(/conversations)%2[fF][^\s?\"]+(\?[^\s\"]+)?",
        lambda m: f"{m.group(1)}/<redacted>{'?<redacted>' if m.group(2) else ''}",
        message,
    )
    # Conversations: prefix lookalike (/conversations2/...)
    message = re.sub(
        r"(/conversations)(?!/|%2[fF]|[?\s\"]|$)[^\s?\"]+(\?[^\s\"]+)?",
        lambda m: f"{m.group(1)}/<redacted>{'?<redacted>' if m.group(2) else ''}",
        message,
    )
    # Conversations: /conversations/<id>[/<action>] — redact id, keep static action
    message = re.sub(
        r"(/conversations)/([^/\s?\"]+)(/[^\s?\"]*)?(\?[^\s\"]+)?",
        _redact_conversation_id_path,
        message,
    )
    message = re.sub(
        r"(/(?:memory/records|memory|artifacts|audit|sessions|credentials|secrets|skills|queue|run-leases|cron/jobs|cron|conversations)[^\s?\"]*)\?[^\s]+",
        r"\1?<redacted>",
        message,
    )
    return message


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


def _make_session_backend(environ: dict[str, str]) -> Any:
    db_path = environ.get("AGENTOPS_SESSION_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose session backend requires AGENTOPS_SESSION_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    from agent.runtime_sessions import LocalSQLiteSessionBackend

    return LocalSQLiteSessionBackend(db_path=db_path)


def _get_or_create_session_backend() -> Any:
    global _session_backend_instance
    with _session_backend_lock:
        if _session_backend_instance is None:
            _session_backend_instance = _make_session_backend(dict(os.environ))
        return _session_backend_instance


def _make_credential_resolver(environ: dict[str, str]) -> Any:
    """Create a compose-safe deterministic credential resolver.

    Does not read local env vars, profiles, or secret files. Returns a stable
    opaque secret-ref derived from credential-scope + logical ref. A later
    bootstrap/secret-store slice will seed the actual secret values behind
    these refs.
    """
    return _DeterministicCredentialResolver()


def _get_or_create_credential_resolver() -> Any:
    global _credential_resolver_instance
    with _credential_resolver_lock:
        if _credential_resolver_instance is None:
            _credential_resolver_instance = _make_credential_resolver(dict(os.environ))
        return _credential_resolver_instance


def _make_secret_backend(environ: dict[str, str]) -> Any:
    db_path = environ.get("AGENTOPS_SECRET_STORE_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose secret store requires AGENTOPS_SECRET_STORE_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    from agentops_runtime.secrets_api import SQLiteSecretStore

    return SQLiteSecretStore(db_path)


def _get_or_create_secret_backend() -> Any:
    global _secret_backend_instance
    with _secret_backend_lock:
        if _secret_backend_instance is None:
            _secret_backend_instance = _make_secret_backend(dict(os.environ))
        return _secret_backend_instance


def _make_curated_memory_backend(environ: dict[str, str]) -> Any:
    """Create a SQLiteCuratedMemoryStore from the given environment mapping.

    AGENTOPS_CURATED_MEMORY_DB_PATH is required; blank, in-memory, and
    relative paths are rejected. No cwd/tmp/local-profile fallback.
    """
    db_path = environ.get("AGENTOPS_CURATED_MEMORY_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose curated-memory backend requires AGENTOPS_CURATED_MEMORY_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_CURATED_MEMORY_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_CURATED_MEMORY_DB_PATH must be an absolute path"
        )
    from agentops_runtime.curated_memory_api import SQLiteCuratedMemoryStore

    return SQLiteCuratedMemoryStore(db_path)


def _get_or_create_curated_memory_backend() -> Any:
    global _curated_memory_backend_instance
    with _curated_memory_backend_lock:
        if _curated_memory_backend_instance is None:
            _curated_memory_backend_instance = _make_curated_memory_backend(dict(os.environ))
        return _curated_memory_backend_instance


def _make_skill_backend(environ: dict[str, str]) -> Any:
    """Create a SQLiteScopedSkillStore from the given environment mapping.

    AGENTOPS_SKILL_DB_PATH is required; blank, ':memory:', and relative paths
    are rejected. No cwd/tmp fallback is provided for compose/cloud profiles.
    """
    db_path = environ.get("AGENTOPS_SKILL_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose skill backend requires AGENTOPS_SKILL_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_SKILL_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_SKILL_DB_PATH must be an absolute path"
        )
    from agentops_runtime.skills_api import SQLiteScopedSkillStore

    return SQLiteScopedSkillStore(db_path)


def _get_or_create_skill_backend() -> Any:
    global _skill_backend_instance
    with _skill_backend_lock:
        if _skill_backend_instance is None:
            _skill_backend_instance = _make_skill_backend(dict(os.environ))
        return _skill_backend_instance


def _make_queue_backend(environ: dict[str, str]) -> Any:
    db_path = environ.get("AGENTOPS_QUEUE_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose queue backend requires AGENTOPS_QUEUE_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_QUEUE_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_QUEUE_DB_PATH must be an absolute path"
        )
    from agentops_runtime.queue_api import SQLiteQueueBackend

    return SQLiteQueueBackend(db_path)


def _get_or_create_queue_backend() -> Any:
    global _queue_backend_instance
    with _queue_backend_lock:
        if _queue_backend_instance is None:
            _queue_backend_instance = _make_queue_backend(dict(os.environ))
        return _queue_backend_instance


def _make_run_lease_backend(environ: dict[str, str]) -> Any:
    db_path = environ.get("AGENTOPS_RUN_LEASE_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose run-lease backend requires AGENTOPS_RUN_LEASE_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_RUN_LEASE_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_RUN_LEASE_DB_PATH must be an absolute path"
        )
    from agentops_runtime.run_leases_api import SQLiteRunLeaseBackend

    return SQLiteRunLeaseBackend(db_path)


def _get_or_create_run_lease_backend() -> Any:
    global _run_lease_backend_instance
    with _run_lease_backend_lock:
        if _run_lease_backend_instance is None:
            _run_lease_backend_instance = _make_run_lease_backend(dict(os.environ))
        return _run_lease_backend_instance


def _make_cron_backend(environ: dict[str, str]) -> Any:
    """Create a SQLiteCronBackend from the given environment mapping.

    AGENTOPS_CRON_DB_PATH is required; blank, ':memory:', and relative paths
    are rejected. No cwd/tmp fallback is provided for compose/cloud profiles.
    """
    db_path = environ.get("AGENTOPS_CRON_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose cron backend requires AGENTOPS_CRON_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_CRON_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_CRON_DB_PATH must be an absolute path"
        )
    from agent.runtime_cron_sqlite import SQLiteCronBackend

    return SQLiteCronBackend(db_path)


def _get_or_create_cron_backend() -> Any:
    global _cron_backend_instance
    with _cron_backend_lock:
        if _cron_backend_instance is None:
            _cron_backend_instance = _make_cron_backend(dict(os.environ))
        return _cron_backend_instance


def _make_conversation_router_backend(environ: dict[str, str]) -> Any:
    """Create a SQLiteConversationRouterBackend from the given environment mapping.

    AGENTOPS_CONVERSATION_ROUTER_DB_PATH is required; blank, ':memory:', and
    relative paths are rejected. No cwd/tmp fallback for compose/cloud profiles.
    """
    db_path = environ.get("AGENTOPS_CONVERSATION_ROUTER_DB_PATH", "").strip()
    if not db_path:
        raise ValueError(
            "compose conversation-router backend requires AGENTOPS_CONVERSATION_ROUTER_DB_PATH; "
            "no local fallback is provided for compose/cloud profiles"
        )
    if db_path == ":memory:":
        raise ValueError(
            "AGENTOPS_CONVERSATION_ROUTER_DB_PATH must be a durable file path, not ':memory:'"
        )
    if not db_path.startswith("/"):
        raise ValueError(
            "AGENTOPS_CONVERSATION_ROUTER_DB_PATH must be an absolute path"
        )
    from agentops_runtime.conversations_api import SQLiteConversationRouterBackend

    return SQLiteConversationRouterBackend(db_path)


def _get_or_create_conversation_router_backend() -> Any:
    global _conversation_router_backend_instance
    with _conversation_router_backend_lock:
        if _conversation_router_backend_instance is None:
            _conversation_router_backend_instance = _make_conversation_router_backend(dict(os.environ))
        return _conversation_router_backend_instance


class _DeterministicCredentialResolver:
    """Compose-safe credential resolver that maps scope+ref to a stable opaque ref.

    No local env/profile reads. Returns None for null context (missing scope).
    Stable non-secret refs are seeds for a later bootstrap/secret-store slice.
    """

    def resolve(self, context: Any, ref: str) -> str | None:
        import hashlib

        if context is None:
            return None
        canonical = json.dumps(
            [
                context.org_id or "",
                context.workspace_id or "",
                context.workspace_type or "",
                context.user_id or "",
                context.project_id or "",
                context.agent_profile_id or "",
                context.permissions_ref or "",
                context.backend_profile or "",
                ref,
            ],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"cred-{digest[:32]}"


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
