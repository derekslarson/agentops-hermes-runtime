"""HTTP conversation-router adapter (M12B).

Implements a provider-neutral :class:`~agent.runtime_backends.ConversationRouter`
that transports the generic logical conversation/run model to a remote
control-plane HTTP API using a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST /conversations/resolve``          body ``{context, event}``  -> ``{conversation_id}``
* ``POST /conversations/<id>/active-run``  body ``{context}``         -> ``{run_id: str|null}``
* ``POST /conversations/<id>/route``       body ``{context, turn}``   -> ``{run_id}``

The optional bearer token is sent only in the ``Authorization`` header. URL
labels and raised errors intentionally omit query strings, response bodies, and
raw credential material.
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
_SECRET_KEYS = {"token", "secret", "password", "apikey", "authorization", "credential", "api_key"}


class HttpConversationRouter:
    """Remote ``ConversationRouter`` backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpConversationRouter requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpConversationRouter base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpConversationRouter base_url must not contain credentials")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("HttpConversationRouter base_url must be an absolute http(s) URL") from exc
        if parsed.query or parsed.fragment:
            raise ValueError("HttpConversationRouter base_url must not contain query or fragment")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("HttpConversationRouter timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpConversationRouter timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def resolve_conversation(self, context: "RuntimeContext | None", event: Mapping[str, Any]) -> str:
        payload = self._request_json(
            "POST",
            "/conversations/resolve",
            {"context": self._scope_payload(context), "event": self._safe_payload(event)},
        )
        conv_id = payload.get("conversation_id")
        if not isinstance(conv_id, str) or not conv_id:
            raise RuntimeError("HttpConversationRouter resolve_conversation expected non-empty string conversation_id")
        return conv_id

    def find_active_run(self, context: "RuntimeContext | None", conversation_id: str) -> str | None:
        payload = self._request_json(
            "POST",
            f"/conversations/{self._quote_id(conversation_id)}/active-run",
            {"context": self._scope_payload(context)},
        )
        run_id = payload.get("run_id")
        if run_id is None or run_id == "":
            return None
        if not isinstance(run_id, str):
            raise RuntimeError("HttpConversationRouter find_active_run expected string or null run_id")
        return run_id

    def route_turn(self, context: "RuntimeContext | None", conversation_id: str, turn: Mapping[str, Any]) -> str:
        payload = self._request_json(
            "POST",
            f"/conversations/{self._quote_id(conversation_id)}/route",
            {"context": self._scope_payload(context), "turn": self._safe_payload(turn)},
        )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("HttpConversationRouter route_turn expected non-empty string run_id")
        return run_id

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
    def _normalized_secret_key(cls, key: object) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    @classmethod
    def _safe_payload(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            safe: dict[str, Any] = {}
            for key, nested in value.items():
                lowered = cls._normalized_secret_key(key)
                if any(marker in lowered for marker in _SECRET_KEYS):
                    continue
                safe[str(key)] = cls._safe_payload(nested)
            return safe
        if isinstance(value, list):
            return [cls._safe_payload(item) for item in value]
        if isinstance(value, tuple):
            return [cls._safe_payload(item) for item in value]
        return value

    @staticmethod
    def _quote_id(conv_id: str) -> str:
        return urllib.parse.quote(str(conv_id), safe="")

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
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HttpConversationRouter request to {endpoint} failed with HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError(f"HttpConversationRouter could not reach {endpoint}: transport error") from None

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpConversationRouter request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"HttpConversationRouter got a non-JSON response from {endpoint}") from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"HttpConversationRouter expected a JSON object from {endpoint}, got {type(decoded).__name__}")
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(method: str, path: str) -> str:
        parts = path.strip("/").split("/")
        if parts == ["conversations", "resolve"]:
            logical_path = "/conversations/resolve"
        elif len(parts) == 3 and parts[0] == "conversations" and parts[2] == "active-run":
            logical_path = "/conversations/<conversation_id>/active-run"
        elif len(parts) == 3 and parts[0] == "conversations" and parts[2] == "route":
            logical_path = "/conversations/<conversation_id>/route"
        else:
            logical_path = "/<redacted-endpoint>"
        return f"{method} {logical_path}"


def register_http_conversation_router_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpConversationRouter` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpConversationRouter:
        return HttpConversationRouter(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.CONVERSATION_ROUTER, factory, profile=profile)
