"""Artifact and audit runtime backends for distributed AgentOps runs.

The core contracts live in :mod:`agent.runtime_backends`; this module provides
M10's concrete local-durable and provider-neutral remote adapters without
introducing cloud/database-specific assumptions into the contracts.
"""

from __future__ import annotations

import base64
import json
import math
import posixpath
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.runtime_backends import (
    BackendCapability,
    BackendSelectionError,
    RuntimeBackendRegistry,
    _redact_audit_text,
)
from agent.runtime_context import RuntimeContext

_SENSITIVE_KEYS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "credential",
    "authorization",
    "auth",
)
_PATH_KEYS = ("path", "file", "filename", "dirname", "cwd", "workdir", "directory")
_REMOTE_PROFILE = "compose-self-hosted"
_SCOPE_FIELDS = (
    "mode",
    "org_id",
    "workspace_id",
    "workspace_type",
    "user_id",
    "conversation_id",
    "external_channel_id",
    "external_thread_id",
    "agent_profile_id",
    "project_id",
    "run_id",
    "run_type",
    "job_id",
    "parent_session_id",
    "backend_profile",
)


@dataclass(frozen=True, slots=True)
class SanitizedAuditEvent:
    """Native-shaped audit event accepted by audit backends.

    ``payload`` is sanitized before persistence/transmission. The dataclass is a
    convenience for callers that want explicit event fields; plain mappings are
    still supported by every backend for compatibility with earlier milestones.
    """

    event_type: str
    action: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {"event_type": self.event_type}
        if self.action is not None:
            data["action"] = self.action
        data["payload"] = dict(self.payload)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


def _scope(context: RuntimeContext | None) -> dict[str, Any]:
    scoped = context or RuntimeContext()
    data = scoped.to_dict()
    return {key: data.get(key) for key in _SCOPE_FIELDS}


def _scope_parts(context: RuntimeContext | None) -> list[str]:
    scope = _scope(context)
    return [_safe_segment(f"{key}={_tagged_scope_value(scope[key])}") for key in sorted(scope)]


def _tagged_scope_value(value: Any) -> str:
    if value is None:
        return "null:"
    return f"value:{value}"


def _safe_segment(value: str) -> str:
    raw = str(value).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return encoded or "_"


def _normalize_ref(ref: str) -> str:
    raw = str(ref or "").strip().replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if (
        not raw
        or raw.startswith("/")
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or "/../" in f"/{normalized}/"
    ):
        raise ValueError(f"unsafe artifact ref: {ref!r}")
    return normalized


def _sanitize_audit_value(value: Any, *, key: str | None = None) -> Any:
    key_l = (key or "").lower()
    normalized_key = "".join(ch for ch in key_l if ch.isalnum())
    normalized_sensitive = tuple("".join(ch for ch in marker.lower() if ch.isalnum()) for marker in _SENSITIVE_KEYS)
    if value is not None and (
        any(marker in key_l for marker in _SENSITIVE_KEYS)
        or any(marker and marker in normalized_key for marker in normalized_sensitive)
    ):
        return "[REDACTED]"
    normalized_path_keys = tuple("".join(ch for ch in marker.lower() if ch.isalnum()) for marker in _PATH_KEYS)
    if value is not None and (
        any(marker == key_l or key_l.endswith(f"_{marker}") for marker in _PATH_KEYS)
        or any(marker and marker in normalized_key for marker in normalized_path_keys)
    ):
        return "[REDACTED_PATH]"
    if isinstance(value, Mapping):
        return {str(k): _sanitize_audit_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, str):
        return _redact_audit_text(value)
    return value


def _event_mapping(event: Mapping[str, Any] | SanitizedAuditEvent) -> dict[str, Any]:
    if isinstance(event, SanitizedAuditEvent):
        raw = event.to_mapping()
    else:
        raw = dict(event)
    return _sanitize_audit_value(raw)


class LocalFileArtifactBackend:
    """Local durable artifact backend scoped by RuntimeContext."""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    def _scope_dir(self, context: RuntimeContext | None) -> Path:
        return self._root.joinpath("artifacts", *_scope_parts(context))

    def _contained_path(self, context: RuntimeContext | None, normalized: str, *, create_parent: bool) -> Path:
        base = self._scope_dir(context)
        target = base / normalized
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        root = base.resolve()
        check = target.resolve(strict=False)
        try:
            check.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"unsafe artifact ref: {normalized!r}") from exc
        return target

    def put(self, context: RuntimeContext | None, ref: str, data: bytes) -> str:
        normalized = _normalize_ref(ref)
        with self._lock:
            target = self._contained_path(context, normalized, create_parent=True)
            target.write_bytes(bytes(data))
        return normalized

    def get(self, context: RuntimeContext | None, ref: str) -> bytes | None:
        normalized = _normalize_ref(ref)
        path = self._contained_path(context, normalized, create_parent=False)
        if not path.exists() or not path.is_file():
            return None
        return path.read_bytes()

    def list_artifacts(self, context: RuntimeContext | None) -> list[str]:
        base = self._scope_dir(context)
        if not base.exists():
            return []
        refs = [p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()]
        return sorted(refs)


class LocalFileAuditBackend:
    """Append-only local audit JSONL backend scoped by RuntimeContext."""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    def _log_path(self, context: RuntimeContext | None) -> Path:
        return self._root.joinpath("audit", *_scope_parts(context), "events.jsonl")

    def record(self, context: RuntimeContext | None, event: Mapping[str, Any] | SanitizedAuditEvent) -> None:
        sanitized = _event_mapping(event)
        if isinstance(event, SanitizedAuditEvent):
            entry = {"scope": _scope(context), **sanitized}
        else:
            entry = {"scope": _scope(context), "event": sanitized}
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            path = self._log_path(context)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def list_events(self, context: RuntimeContext | None, *, limit: int | None = None) -> list[dict[str, Any]]:
        path = self._log_path(context)
        if not path.exists():
            return []
        if limit is not None and limit <= 0:
            return []
        events: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                events.append(json.loads(line))
                if limit is not None and len(events) >= limit:
                    break
        return events


RequestFn = Callable[[str, str], tuple[int, Mapping[str, str], bytes]]


def _default_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout or 10.0) as response:  # noqa: S310 - configured runtime endpoint
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _options(options: Mapping[str, Any]) -> tuple[str, str | None, float]:
    base_url = str(options.get("base_url") or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise BackendSelectionError("HTTP artifact/audit backend requires a credential-free absolute http(s) base_url")
    token = options.get("token")
    try:
        timeout = float(options.get("timeout", 10.0))
    except (TypeError, ValueError) as exc:
        raise BackendSelectionError("HTTP artifact/audit backend requires a finite positive timeout") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise BackendSelectionError("HTTP artifact/audit backend requires a finite positive timeout")
    return base_url, str(token) if token else None, timeout


def _json_request(
    request: Callable[..., tuple[int, Mapping[str, str], bytes]],
    method: str,
    url: str,
    *,
    token: str | None,
    payload: Mapping[str, Any] | None = None,
    timeout: float,
    ok_statuses: set[int] | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    status, response_headers, response_body = request(method, url, headers=headers, body=body, timeout=timeout)
    allowed = ok_statuses or set()
    if status >= 400 and status not in allowed:
        raise RuntimeError(f"runtime backend request failed with HTTP {status}")
    return status, response_headers, response_body


class HttpArtifactBackend:
    """Provider-neutral HTTP artifact backend for AgentOps control planes."""

    def __init__(self, *, base_url: str, token: str | None = None, timeout: float = 10.0, request: Callable[..., tuple[int, Mapping[str, str], bytes]] = _default_request) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._request = request

    def put(self, context: RuntimeContext | None, ref: str, data: bytes) -> str:
        normalized = _normalize_ref(ref)
        payload = {
            "scope": _scope(context),
            "ref": normalized,
            "data_b64": base64.b64encode(bytes(data)).decode("ascii"),
        }
        _, _, body = _json_request(
            self._request,
            "POST",
            f"{self._base_url}/artifacts",
            token=self._token,
            payload=payload,
            timeout=self._timeout,
        )
        response = json.loads(body.decode() or "{}")
        return str(response.get("ref") or normalized)

    def get(self, context: RuntimeContext | None, ref: str) -> bytes | None:
        normalized = _normalize_ref(ref)
        query = urllib.parse.urlencode({"scope": json.dumps(_scope(context), sort_keys=True)})
        url = f"{self._base_url}/artifacts/{urllib.parse.quote(normalized, safe='')}?{query}"
        status, _, body = _json_request(
            self._request,
            "GET",
            url,
            token=self._token,
            timeout=self._timeout,
            ok_statuses={404},
        )
        if status in {204, 404}:
            return None
        return body

    def list_artifacts(self, context: RuntimeContext | None) -> list[str]:
        query = urllib.parse.urlencode({"scope": json.dumps(_scope(context), sort_keys=True)})
        _, _, body = _json_request(
            self._request,
            "GET",
            f"{self._base_url}/artifacts?{query}",
            token=self._token,
            timeout=self._timeout,
        )
        response = json.loads(body.decode() or "{}")
        return [str(item) for item in response.get("artifacts", [])]


class HttpAuditBackend:
    """Provider-neutral HTTP audit backend for AgentOps control planes."""

    def __init__(self, *, base_url: str, token: str | None = None, timeout: float = 10.0, request: Callable[..., tuple[int, Mapping[str, str], bytes]] = _default_request) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._request = request

    def record(self, context: RuntimeContext | None, event: Mapping[str, Any] | SanitizedAuditEvent) -> None:
        payload = {"scope": _scope(context), "event": _event_mapping(event)}
        _json_request(
            self._request,
            "POST",
            f"{self._base_url}/audit",
            token=self._token,
            payload=payload,
            timeout=self._timeout,
        )


def register_http_artifact_backend(
    registry: RuntimeBackendRegistry,
    *,
    profile: str = _REMOTE_PROFILE,
    request: Callable[..., tuple[int, Mapping[str, str], bytes]] = _default_request,
) -> None:
    """Register the HTTP artifact adapter for a deployment profile."""

    def factory(options: Mapping[str, Any]) -> HttpArtifactBackend:
        base_url, token, timeout = _options(options)
        return HttpArtifactBackend(base_url=base_url, token=token, timeout=timeout, request=request)

    registry.register(BackendCapability.ARTIFACT, factory, profile=profile)


def register_http_audit_backend(
    registry: RuntimeBackendRegistry,
    *,
    profile: str = _REMOTE_PROFILE,
    request: Callable[..., tuple[int, Mapping[str, str], bytes]] = _default_request,
) -> None:
    """Register the HTTP audit adapter for a deployment profile."""

    def factory(options: Mapping[str, Any]) -> HttpAuditBackend:
        base_url, token, timeout = _options(options)
        return HttpAuditBackend(base_url=base_url, token=token, timeout=timeout, request=request)

    registry.register(BackendCapability.AUDIT, factory, profile=profile)


__all__ = [
    "HttpArtifactBackend",
    "HttpAuditBackend",
    "LocalFileArtifactBackend",
    "LocalFileAuditBackend",
    "SanitizedAuditEvent",
    "register_http_artifact_backend",
    "register_http_audit_backend",
]
