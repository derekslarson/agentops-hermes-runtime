"""HTTP queue adapter (M12B).

Implements a provider-neutral QueueBackend that transports enqueue/claim/ack/nack/
extend-lease operations to a remote control-plane HTTP API using a RuntimeContext-
derived scope.

Control-plane contract (provider neutral):

* ``POST /queue/enqueue``      body ``{context, payload, idempotency_key}`` -> ``{"receipt": "..."}``
* ``POST /queue/claim``        body ``{context, capability}``                -> ``{"item": {...}}`` or ``{"item": null}``
* ``POST /queue/ack``          body ``{context, receipt}``                   -> 2xx
* ``POST /queue/nack``         body ``{context, receipt, requeue}``          -> 2xx
* ``POST /queue/extend-lease`` body ``{context, receipt, seconds}``          -> 2xx

Queue receipts are carried in JSON request bodies, never in URL paths.  Queue
payloads are product data: they are preserved verbatim in transport, including
any secret-looking keys; they are never redacted or inspected.

The optional bearer token is sent only in the ``Authorization`` header.  URL
labels and raised errors intentionally omit response bodies, raw receipt values,
payload content, idempotency keys, hostnames, URLs, and raw credential material.
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

_VALID_ENDPOINTS = frozenset({"enqueue", "claim", "ack", "nack", "extend-lease"})


class HttpQueueBackend:
    """Remote QueueBackend backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        raw_url = base_url or ""
        if raw_url != raw_url.strip() or any(ord(ch) <= 32 or ord(ch) == 127 for ch in raw_url):
            raise ValueError("HttpQueueBackend base_url must be an absolute http(s) URL")
        cleaned = raw_url
        if not cleaned:
            raise ValueError("HttpQueueBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpQueueBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpQueueBackend base_url must not contain credentials")
        invalid_port = False
        try:
            parsed.port
        except ValueError:
            invalid_port = True
        if invalid_port:
            raise ValueError("HttpQueueBackend base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment or "?" in cleaned or "#" in cleaned:
            raise ValueError("HttpQueueBackend base_url must not contain query or fragment")
        if token is not None:
            if not isinstance(token, str) or any(ord(ch) <= 32 or ord(ch) == 127 for ch in token):
                raise ValueError("HttpQueueBackend token must be a string without control characters")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            timeout_value = math.nan
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpQueueBackend timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def enqueue(
        self,
        context: "RuntimeContext | None",
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"context": self._scope_payload(context), "payload": dict(payload)}
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        result = self._request_json("POST", "/queue/enqueue", body)
        receipt = result.get("receipt")
        if not isinstance(receipt, str):
            raise RuntimeError(
                f"HttpQueueBackend expected string receipt from POST /queue/enqueue, got {type(receipt).__name__}"
            )
        return receipt

    def claim(self, context: "RuntimeContext | None", *, capability: str | None = None) -> Any | None:
        body: dict[str, Any] = {"context": self._scope_payload(context)}
        if capability is not None:
            body["capability"] = capability
        result = self._request_json("POST", "/queue/claim", body)
        if "item" not in result:
            raise RuntimeError(
                "HttpQueueBackend expected 'item' key from POST /queue/claim; got malformed response"
            )
        item = result["item"]
        if item is None:
            return None
        if not isinstance(item, dict):
            raise RuntimeError(
                f"HttpQueueBackend expected dict or null item from POST /queue/claim, got {type(item).__name__}"
            )
        return item

    def ack(self, context: "RuntimeContext | None", receipt: Any) -> None:
        self._request_json(
            "POST",
            "/queue/ack",
            {"context": self._scope_payload(context), "receipt": self._receipt_value(receipt)},
        )

    def nack(self, context: "RuntimeContext | None", receipt: Any, *, requeue: bool = True) -> None:
        self._request_json(
            "POST",
            "/queue/nack",
            {"context": self._scope_payload(context), "receipt": self._receipt_value(receipt), "requeue": requeue},
        )

    def extend_lease(self, context: "RuntimeContext | None", receipt: Any, *, seconds: int) -> None:
        self._request_json(
            "POST",
            "/queue/extend-lease",
            {"context": self._scope_payload(context), "receipt": self._receipt_value(receipt), "seconds": seconds},
        )

    @staticmethod
    def _receipt_value(receipt: Any) -> str:
        raw_receipt = receipt.get("receipt") if isinstance(receipt, Mapping) else receipt
        if not isinstance(raw_receipt, str) or not raw_receipt:
            raise ValueError("HttpQueueBackend requires a non-empty string receipt")
        return raw_receipt

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
            "run_id": context.run_id,
            "job_id": context.job_id,
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
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        return self._send(request, endpoint=self._safe_endpoint_label(method, path))

    def _send(self, request: urllib.request.Request, *, endpoint: str) -> dict[str, Any]:
        http_status: int | None = None
        transport_failed = False
        status = 0
        raw = ""
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            http_status = exc.code
        except urllib.error.URLError:
            transport_failed = True

        if http_status is not None:
            raise RuntimeError(f"HttpQueueBackend request to {endpoint} failed with HTTP {http_status}")
        if transport_failed:
            raise RuntimeError(f"HttpQueueBackend could not reach {endpoint}: transport error")

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpQueueBackend request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(f"HttpQueueBackend got a non-JSON response from {endpoint}")
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"HttpQueueBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}")
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(method: str, path: str) -> str:
        segment = path.strip("/").split("/")[-1] if "/" in path.strip("/") else path.strip("/")
        if segment in _VALID_ENDPOINTS:
            normalized = path.strip("/")
            return f"{method} /{normalized}"
        return f"{method} /<redacted-endpoint>"


def register_http_queue_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpQueueBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpQueueBackend:
        return HttpQueueBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.QUEUE, factory, profile=profile)
