"""HTTP worker-registry adapter (M12B).

Implements a provider-neutral :class:`~agent.runtime_backends.WorkerRegistry`
that transports the generic fleet-capacity lifecycle model to a remote
control-plane HTTP API using a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST /workers/register``              body ``{context, worker, ...}`` -> ``{worker_id: str}``
* ``POST /workers/<worker_id>/heartbeat`` body ``{context, slots, ...}``  -> 2xx
* ``POST /workers/<worker_id>/drain``     body ``{context}``              -> 2xx
* ``POST /workers/recover-expired``       body ``{context, now}``         -> ``{recovered: [str, ...]}``
* ``POST /workers/list``                  body ``{context}``              -> ``{workers: [{...}, ...]}``

Worker IDs are URL-encoded with ``quote(safe="")``. The optional bearer token is
sent only in the ``Authorization`` header. URL labels and raised errors
intentionally omit response bodies, raw worker IDs, and raw credential material.
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
_SECRET_KEYS = frozenset({"token", "authorization", "password", "secret", "credential", "api_key", "apikey"})


def _contains_ascii_control(value: str) -> bool:
    return any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)


class HttpWorkerRegistry:
    """Remote ``WorkerRegistry`` backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpWorkerRegistry requires a non-empty base_url")
        if _contains_ascii_control(cleaned):
            raise ValueError("HttpWorkerRegistry base_url must be an absolute http(s) URL")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpWorkerRegistry base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpWorkerRegistry base_url must not contain credentials")
        invalid_port = False
        try:
            parsed.port
        except ValueError:
            invalid_port = True
        if invalid_port:
            raise ValueError("HttpWorkerRegistry base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("HttpWorkerRegistry base_url must not contain query or fragment")
        invalid_timeout = False
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            invalid_timeout = True
            timeout_value = 0.0
        if invalid_timeout or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpWorkerRegistry timeout must be a finite positive number")
        token_value = token or None
        if token_value is not None and _contains_ascii_control(token_value):
            raise ValueError("HttpWorkerRegistry token must not contain control characters")
        self._base_url = cleaned.rstrip("/")
        self._token = token_value
        self._timeout = timeout_value

    def register(
        self,
        context: "RuntimeContext | None",
        worker: Mapping[str, Any],
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> str:
        payload = self._request_json(
            "POST",
            "/workers/register",
            {
                "context": self._scope_payload(context),
                "worker": self._redact_payload(worker),
                "now": now,
                "ttl_seconds": ttl_seconds,
            },
        )
        worker_id = payload.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id:
            raise RuntimeError(
                "HttpWorkerRegistry expected non-empty string worker_id from POST /workers/register"
            )
        return worker_id

    def heartbeat(
        self,
        context: "RuntimeContext | None",
        worker_id: str,
        *,
        slots: Mapping[str, Any],
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self._request_json(
            "POST",
            f"/workers/{self._quote_worker_id(worker_id)}/heartbeat",
            {
                "context": self._scope_payload(context),
                "slots": self._redact_payload(slots),
                "now": now,
                "ttl_seconds": ttl_seconds,
            },
        )

    def mark_draining(self, context: "RuntimeContext | None", worker_id: str) -> None:
        self._request_json(
            "POST",
            f"/workers/{self._quote_worker_id(worker_id)}/drain",
            {"context": self._scope_payload(context)},
        )

    def recover_expired(self, context: "RuntimeContext | None", *, now: float | None = None) -> list[str]:
        payload = self._request_json(
            "POST",
            "/workers/recover-expired",
            {"context": self._scope_payload(context), "now": now},
        )
        recovered = payload.get("recovered")
        if not isinstance(recovered, list):
            raise RuntimeError(
                "HttpWorkerRegistry expected list recovered from POST /workers/recover-expired"
            )
        for item in recovered:
            if not isinstance(item, str):
                raise RuntimeError(
                    "HttpWorkerRegistry recovered list contained non-string item from POST /workers/recover-expired"
                )
        return recovered

    def list_workers(self, context: "RuntimeContext | None") -> list[Mapping[str, Any]]:
        payload = self._request_json(
            "POST",
            "/workers/list",
            {"context": self._scope_payload(context)},
        )
        workers = payload.get("workers")
        if not isinstance(workers, list):
            raise RuntimeError(
                "HttpWorkerRegistry expected list workers from POST /workers/list"
            )
        for item in workers:
            if not isinstance(item, Mapping):
                raise RuntimeError(
                    "HttpWorkerRegistry workers list contained non-object item from POST /workers/list"
                )
        return workers

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

    @classmethod
    def _normalized_key(cls, key: object) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    @classmethod
    def _redact_payload(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, nested in value.items():
                normalized = cls._normalized_key(key)
                if any(marker in normalized for marker in _SECRET_KEYS):
                    continue
                result[str(key)] = cls._redact_payload(nested)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._redact_payload(item) for item in value]
        return value

    @staticmethod
    def _quote_worker_id(worker_id: str) -> str:
        return urllib.parse.quote(str(worker_id), safe="")

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
        return self._send(request, endpoint=self._safe_endpoint_label(method, path))

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
            raise RuntimeError(f"HttpWorkerRegistry request to {endpoint} failed with HTTP {http_status}")
        if transport_failed:
            raise RuntimeError(f"HttpWorkerRegistry could not reach {endpoint}: transport error")
        if decode_failed:
            raise RuntimeError(f"HttpWorkerRegistry got a non-UTF-8 response from {endpoint}")

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpWorkerRegistry request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(f"HttpWorkerRegistry got a non-JSON response from {endpoint}")
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"HttpWorkerRegistry expected a JSON object from {endpoint}, got {type(decoded).__name__}")
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(method: str, path: str) -> str:
        parts = path.strip("/").split("/")
        if parts == ["workers", "register"]:
            return f"{method} /workers/register"
        if parts == ["workers", "recover-expired"]:
            return f"{method} /workers/recover-expired"
        if parts == ["workers", "list"]:
            return f"{method} /workers/list"
        if len(parts) == 3 and parts[0] == "workers" and parts[2] == "heartbeat":
            return f"{method} /workers/<worker_id>/heartbeat"
        if len(parts) == 3 and parts[0] == "workers" and parts[2] == "drain":
            return f"{method} /workers/<worker_id>/drain"
        return f"{method} /<redacted-endpoint>"


def register_http_worker_registry_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpWorkerRegistry` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpWorkerRegistry:
        return HttpWorkerRegistry(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.WORKER_REGISTRY, factory, profile=profile)
