"""HTTP remote memory adapter (M5).

Implements the first real remote :class:`~agent.runtime_backends.MemoryBackend`
against a cloud/database-agnostic control-plane HTTP API. The native Hermes
memory tool is unchanged: :class:`~tools.memory_tool.MemoryStore` keeps owning
threat scanning, char limits, entry matching, parsing/rendering, and success
responses, while this adapter only stores and retrieves the §-delimited
snapshot for a target, scoped by :class:`~agent.runtime_context.RuntimeContext`.

The control-plane contract is intentionally provider-neutral:

* ``GET  /memory?target=<target>&context=<json>`` -> ``{"content": str|null}``
* ``POST /memory`` with body
  ``{"context": <dict>, "target": <target>, "content": <content>, "action": <action>}``

An optional bearer token is sent only via the ``Authorization`` header, never
in the JSON payload or query string. The adapter fails closed: a missing
``base_url`` raises ``ValueError`` at construction, and any non-2xx response,
transport failure, or unparseable body raises ``RuntimeError``.

Stdlib only — no third-party HTTP client — so the adapter stays dependency-free.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Mapping

from agent.runtime_backends import BackendCapability

if TYPE_CHECKING:
    from agent.runtime_backends import RuntimeBackendRegistry
    from agent.runtime_context import RuntimeContext

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_PROFILE = "compose-self-hosted"


class HttpMemoryBackend:
    """Remote ``MemoryBackend`` talking to a control-plane-like HTTP API.

    Configured with a required ``base_url`` and optional ``token``/``timeout``.
    A minimal memory scope derived from :class:`RuntimeContext` is sent to the
    control plane so it can isolate memory per org/workspace/project/thread/user
    without leaking metadata, credential refs, or provider-specific knowledge
    into this client.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpMemoryBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpMemoryBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpMemoryBackend base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("HttpMemoryBackend base_url must not contain query or fragment")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("HttpMemoryBackend timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpMemoryBackend timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def read(self, context: "RuntimeContext | None", *, target: str = "memory") -> str | None:
        query = urllib.parse.urlencode(
            {
                "target": target,
                "context": json.dumps(self._scope_payload(context)),
            }
        )
        request = urllib.request.Request(
            f"{self._base_url}/memory?{query}",
            method="GET",
            headers=self._headers(),
        )
        payload = self._send(request)
        content = payload.get("content")
        if content is None:
            return None
        if not isinstance(content, str):
            raise RuntimeError(f"HttpMemoryBackend expected string content, got {type(content).__name__}")
        return content

    def write(
        self,
        context: "RuntimeContext | None",
        content: str,
        *,
        target: str = "memory",
        action: str = "add",
    ) -> None:
        body = json.dumps(
            {
                "context": self._scope_payload(context),
                "target": target,
                "content": content,
                "action": action,
            }
        ).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base_url}/memory",
            data=body,
            method="POST",
            headers=headers,
        )
        self._send(request)

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        # Memory scope mirrors LocalFileMemoryBackend's context partitioning and
        # intentionally excludes metadata, credential refs, delivery refs, and
        # run/job IDs. Those fields may contain sensitive or per-run values that
        # should not appear in URLs or fragment durable user/thread memory.
        return {
            "mode": context.mode,
            "org_id": context.org_id,
            "workspace_id": context.workspace_id,
            "project_id": context.project_id,
            "external_channel_id": context.external_channel_id,
            "external_thread_id": context.external_thread_id,
            "conversation_id": context.conversation_id,
            "user_id": context.user_id,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        endpoint = self._safe_endpoint_label(request)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"HttpMemoryBackend request to {endpoint} failed with HTTP {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError(
                f"HttpMemoryBackend could not reach {endpoint}: transport error"
            ) from None

        if not 200 <= status < 300:
            raise RuntimeError(
                f"HttpMemoryBackend request to {endpoint} returned non-2xx status {status}"
            )

        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"HttpMemoryBackend got a non-JSON response from {endpoint}"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpMemoryBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}"
            )
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(request: urllib.request.Request) -> str:
        parsed = urllib.parse.urlparse(request.full_url)
        return f"{request.get_method()} {parsed.scheme}://{parsed.netloc}{parsed.path}"


def register_http_memory_backend(registry: "RuntimeBackendRegistry", profile: str = _DEFAULT_PROFILE) -> None:
    """Register an :class:`HttpMemoryBackend` factory for ``profile``.

    The factory reads ``base_url``/``token``/``timeout`` from the registry's
    memory capability options, so deployment config — not RuntimeContext —
    supplies the connection settings. RuntimeContext stays a method-level scope
    argument, never bound into the cached backend instance.
    """

    def factory(options: Mapping[str, Any]) -> HttpMemoryBackend:
        return HttpMemoryBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout") or _DEFAULT_TIMEOUT,
        )

    registry.register(BackendCapability.MEMORY, factory, profile=profile)
