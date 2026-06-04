"""HTTP run-lease adapter (M12B).

Implements a provider-neutral :class:`~agent.runtime_backends.RunLeaseBackend`
that transports the generic one-active-owner-per-run lease model to a remote
control-plane HTTP API using a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST /run-leases/<key>/claim``          body ``{context, owner, ...}`` -> ``{claimed: bool}``
* ``POST /run-leases/<key>/renew``          body ``{context, owner, ...}`` -> ``{renewed: bool}``
* ``POST /run-leases/<key>/release``        body ``{context, owner}``      -> 2xx
* ``POST /run-leases/<key>/request-cancel`` body ``{context, requester}``  -> 2xx
* ``POST /run-leases/<key>/is-cancelled``   body ``{context}``             -> ``{cancelled: bool}``
* ``POST /run-leases/<key>/clear-cancel``   body ``{context}``             -> 2xx
* ``POST /run-leases/expire-stale``         body ``{context, now}``        -> ``{expired: int}``

Keys are URL-encoded with ``quote(safe="")``. The optional bearer token is sent
only in the ``Authorization`` header. URL labels and raised errors intentionally
omit query strings, response bodies, raw lease keys, and raw credential material.
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

_VALID_KEY_ACTIONS = frozenset({"claim", "renew", "release", "request-cancel", "is-cancelled", "clear-cancel"})


class HttpRunLeaseBackend:
    """Remote ``RunLeaseBackend`` backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpRunLeaseBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpRunLeaseBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpRunLeaseBackend base_url must not contain credentials")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("HttpRunLeaseBackend base_url must be an absolute http(s) URL") from exc
        if parsed.query or parsed.fragment:
            raise ValueError("HttpRunLeaseBackend base_url must not contain query or fragment")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("HttpRunLeaseBackend timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpRunLeaseBackend timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def claim(
        self,
        context: "RuntimeContext | None",
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        payload = self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/claim",
            {"context": self._scope_payload(context), "owner": owner, "now": now, "lease_seconds": lease_seconds},
        )
        claimed = payload.get("claimed")
        if not isinstance(claimed, bool):
            raise RuntimeError(
                f"HttpRunLeaseBackend expected boolean claimed from POST /run-leases/<key>/claim, got {type(claimed).__name__}"
            )
        return claimed

    def renew(
        self,
        context: "RuntimeContext | None",
        key: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        payload = self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/renew",
            {"context": self._scope_payload(context), "owner": owner, "now": now, "lease_seconds": lease_seconds},
        )
        renewed = payload.get("renewed")
        if not isinstance(renewed, bool):
            raise RuntimeError(
                f"HttpRunLeaseBackend expected boolean renewed from POST /run-leases/<key>/renew, got {type(renewed).__name__}"
            )
        return renewed

    def release(self, context: "RuntimeContext | None", key: str, *, owner: str) -> None:
        self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/release",
            {"context": self._scope_payload(context), "owner": owner},
        )

    def request_cancel(self, context: "RuntimeContext | None", key: str, *, requester: str) -> None:
        self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/request-cancel",
            {"context": self._scope_payload(context), "requester": requester},
        )

    def is_cancelled(self, context: "RuntimeContext | None", key: str) -> bool:
        payload = self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/is-cancelled",
            {"context": self._scope_payload(context)},
        )
        cancelled = payload.get("cancelled")
        if not isinstance(cancelled, bool):
            raise RuntimeError(
                f"HttpRunLeaseBackend expected boolean cancelled from POST /run-leases/<key>/is-cancelled, got {type(cancelled).__name__}"
            )
        return cancelled

    def clear_cancelled(self, context: "RuntimeContext | None", key: str) -> None:
        self._request_json(
            "POST",
            f"/run-leases/{self._quote_key(key)}/clear-cancel",
            {"context": self._scope_payload(context)},
        )

    def expire_stale(self, context: "RuntimeContext | None", *, now: float | None = None) -> int:
        payload = self._request_json(
            "POST",
            "/run-leases/expire-stale",
            {"context": self._scope_payload(context), "now": now},
        )
        expired = payload.get("expired")
        # Reject bool explicitly: bool is an int subclass, but a boolean response
        # signals a control-plane contract mismatch and must fail closed.
        if not isinstance(expired, int) or isinstance(expired, bool):
            raise RuntimeError(
                f"HttpRunLeaseBackend expected integer expired from POST /run-leases/expire-stale, got {type(expired).__name__}"
            )
        return expired

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        # run_id and job_id are intentionally omitted: the lease key carries run/job identity.
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

    @staticmethod
    def _quote_key(key: str) -> str:
        return urllib.parse.quote(str(key), safe="")

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
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            http_status = exc.code
        except urllib.error.URLError:
            transport_failed = True

        if http_status is not None:
            raise RuntimeError(f"HttpRunLeaseBackend request to {endpoint} failed with HTTP {http_status}")
        if transport_failed:
            raise RuntimeError(f"HttpRunLeaseBackend could not reach {endpoint}: transport error")

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpRunLeaseBackend request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(f"HttpRunLeaseBackend got a non-JSON response from {endpoint}")
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"HttpRunLeaseBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}")
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(method: str, path: str) -> str:
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "run-leases" and parts[1] == "expire-stale":
            return f"{method} /run-leases/expire-stale"
        if len(parts) == 3 and parts[0] == "run-leases" and parts[2] in _VALID_KEY_ACTIONS:
            return f"{method} /run-leases/<key>/{parts[2]}"
        return f"{method} /<redacted-endpoint>"


def register_http_run_lease_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpRunLeaseBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpRunLeaseBackend:
        return HttpRunLeaseBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.RUN_LEASE, factory, profile=profile)
