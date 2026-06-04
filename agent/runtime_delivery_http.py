"""HTTP delivery backend adapter (M12B).

Implements a provider-neutral :class:`~agent.runtime_backends.DeliveryBackend`
that transports the generic outbound-message delivery model to a remote
control-plane HTTP API using a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST /delivery/deliver`` body ``{context, message}`` -> 2xx / ``{}``

The optional bearer token is sent only in the ``Authorization`` header. URL
labels and raised errors intentionally omit response bodies, raw delivery_ref
values, message content, and raw credential material.

Message payloads are intentionally NOT redacted before transport: the delivery
control plane is the outbound-message router, and redacting legitimate message
fields would corrupt the delivery record.
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


def _contains_ascii_control(value: str) -> bool:
    return any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)


class HttpDeliveryBackend:
    """Remote delivery backend backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        raw = base_url or ""
        if raw != raw.strip() or _contains_ascii_control(raw):
            raise ValueError("HttpDeliveryBackend base_url must be an absolute http(s) URL")
        cleaned = raw
        if not cleaned:
            raise ValueError("HttpDeliveryBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpDeliveryBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpDeliveryBackend base_url must not contain credentials")
        invalid_port = False
        try:
            parsed.port
        except ValueError:
            invalid_port = True
        if invalid_port:
            raise ValueError("HttpDeliveryBackend base_url must be an absolute http(s) URL")
        if "?" in cleaned or "#" in cleaned:
            raise ValueError("HttpDeliveryBackend base_url must not contain query or fragment")
        invalid_timeout = False
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            invalid_timeout = True
            timeout_value = 0.0
        if invalid_timeout or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpDeliveryBackend timeout must be a finite positive number")
        token_value = token or None
        if token_value is not None and _contains_ascii_control(token_value):
            raise ValueError("HttpDeliveryBackend token must not contain control characters")
        self._base_url = cleaned.rstrip("/")
        self._token = token_value
        self._timeout = timeout_value

    def deliver(self, context: "RuntimeContext | None", message: Mapping[str, Any]) -> None:
        self._request_json(
            "POST",
            "/delivery/deliver",
            {"context": self._scope_payload(context), "message": dict(message)},
        )

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        return {
            "mode": context.mode,
            "org_id": context.org_id,
            "workspace_id": context.workspace_id,
            "workspace_type": context.workspace_type,
            "project_id": context.project_id,
            "external_channel_id": context.external_channel_id,
            "external_thread_id": context.external_thread_id,
            "conversation_id": context.conversation_id,
            "user_id": context.user_id,
            "agent_profile_id": context.agent_profile_id,
            "run_type": context.run_type,
            "parent_session_id": context.parent_session_id,
            "backend_profile": context.backend_profile,
            "delivery_ref": context.delivery_ref,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_json(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        return self._send(request, endpoint="POST /delivery/deliver")

    def _send(self, request: urllib.request.Request, *, endpoint: str) -> dict[str, Any]:
        http_status: int | None = None
        transport_failed = False
        status = 0
        raw = ""
        decode_failed = False
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                try:
                    raw = response.read().decode("utf-8")
                except UnicodeDecodeError:
                    decode_failed = True
        except urllib.error.HTTPError as exc:
            http_status = exc.code
        except urllib.error.URLError:
            transport_failed = True

        if http_status is not None:
            raise RuntimeError(
                f"HttpDeliveryBackend request to {endpoint} failed with HTTP {http_status}"
            ) from None
        if transport_failed:
            raise RuntimeError(
                f"HttpDeliveryBackend could not reach {endpoint}: transport error"
            ) from None
        if decode_failed:
            raise RuntimeError(
                f"HttpDeliveryBackend got a non-UTF-8 response from {endpoint}"
            ) from None

        if not 200 <= status < 300:
            raise RuntimeError(
                f"HttpDeliveryBackend request to {endpoint} returned non-2xx status {status}"
            )
        if not raw:
            return {}
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(
                f"HttpDeliveryBackend got a non-JSON response from {endpoint}"
            ) from None
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpDeliveryBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}"
            )
        return dict(decoded)


def register_http_delivery_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpDeliveryBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpDeliveryBackend:
        return HttpDeliveryBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.DELIVERY, factory, profile=profile)
