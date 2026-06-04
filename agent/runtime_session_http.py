"""HTTP session backend adapter (M12B).

Implements a provider-neutral session backend that transports the generic
transcript/session lifecycle model to a remote control-plane HTTP API using
a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST /sessions/create``                       body ``{context, session_id?, ...}``  -> ``{session_id: str}``
* ``POST /sessions/append``                       body ``{context, message}``           -> ``{message_id?: int}``
* ``POST /sessions/messages``                     body ``{context, session_id?, limit?}`` -> ``{messages: [{...}, ...]}``
* ``POST /sessions/search``                       body ``{context, query}``             -> ``{results: [{...}, ...]}``
* ``POST /sessions/<session_id>/resume-target``   body ``{}``                           -> ``{session_id: str}``
* ``POST /sessions/turn-lock/claim``              body ``{context, owner, ttl_seconds}`` -> ``{claimed: bool}``
* ``POST /sessions/turn-lock/renew``              body ``{context, owner, ttl_seconds}`` -> ``{renewed: bool}``
* ``POST /sessions/turn-lock/release``            body ``{context, owner}``             -> 2xx / ``{}``

Session IDs are URL-encoded with ``quote(safe="")``. The optional bearer token
is sent only in the ``Authorization`` header. URL labels and raised errors
intentionally omit response bodies, raw session IDs, owner identifiers, query
strings, and raw credential material.

Transcript message payloads are intentionally NOT redacted before transport:
the session control plane is the durable transcript store, and redacting
legitimate message/tool-call fields would corrupt the transcript record.
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


class HttpSessionBackend:
    """Remote session backend backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        raw_base_url = base_url or ""
        if raw_base_url != raw_base_url.strip() or _contains_ascii_control(raw_base_url):
            raise ValueError("HttpSessionBackend base_url must be an absolute http(s) URL")
        cleaned = raw_base_url
        if not cleaned:
            raise ValueError("HttpSessionBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpSessionBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpSessionBackend base_url must not contain credentials")
        invalid_port = False
        try:
            parsed.port
        except ValueError:
            invalid_port = True
        if invalid_port:
            raise ValueError("HttpSessionBackend base_url must be an absolute http(s) URL")
        if "?" in cleaned or "#" in cleaned:
            raise ValueError("HttpSessionBackend base_url must not contain query or fragment")
        invalid_timeout = False
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            invalid_timeout = True
            timeout_value = 0.0
        if invalid_timeout or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpSessionBackend timeout must be a finite positive number")
        token_value = token or None
        if token_value is not None and _contains_ascii_control(token_value):
            raise ValueError("HttpSessionBackend token must not contain control characters")
        self._base_url = cleaned.rstrip("/")
        self._token = token_value
        self._timeout = timeout_value

    def create_session(
        self,
        context: "RuntimeContext | None",
        *,
        session_id: str | None = None,
        source: str | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        model_config: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"context": self._scope_payload(context)}
        if session_id is not None:
            body["session_id"] = session_id
        if source is not None:
            body["source"] = source
        if parent_session_id is not None:
            body["parent_session_id"] = parent_session_id
        if model is not None:
            body["model"] = model
        if model_config is not None:
            body["model_config"] = dict(model_config)
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if user_id is not None:
            body["user_id"] = user_id
        payload = self._request_json("POST", "/sessions/create", body)
        result_id = payload.get("session_id")
        if not isinstance(result_id, str) or not result_id:
            raise RuntimeError(
                "HttpSessionBackend expected non-empty string session_id from POST /sessions/create"
            )
        return result_id

    def append_message(self, context: "RuntimeContext | None", message: Mapping[str, Any]) -> int | None:
        payload = self._request_json(
            "POST",
            "/sessions/append",
            {"context": self._scope_payload(context), "message": dict(message)},
        )
        message_id = payload.get("message_id")
        if message_id is None:
            return None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise RuntimeError(
                "HttpSessionBackend expected integer message_id from POST /sessions/append"
            )
        return message_id

    def read_messages(
        self,
        context: "RuntimeContext | None",
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        body: dict[str, Any] = {"context": self._scope_payload(context)}
        if session_id is not None:
            body["session_id"] = session_id
        if limit is not None:
            body["limit"] = limit
        payload = self._request_json("POST", "/sessions/messages", body)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise RuntimeError(
                "HttpSessionBackend expected list messages from POST /sessions/messages"
            )
        for item in messages:
            if not isinstance(item, Mapping):
                raise RuntimeError(
                    "HttpSessionBackend messages list contained non-object item from POST /sessions/messages"
                )
        return list(messages)

    def append(self, context: "RuntimeContext | None", event: Mapping[str, Any]) -> None:
        if "role" in event or "content" in event:
            self.append_message(context, event)
            return
        self.append_message(context, {"role": "event", "content": json.dumps(dict(event), sort_keys=True)})

    def read(self, context: "RuntimeContext | None", *, limit: int | None = None) -> list[Any]:
        return self.read_messages(context, limit=limit)

    def search(self, context: "RuntimeContext | None", query: str) -> list[Any]:
        if not query or not query.strip():
            return []
        payload = self._request_json(
            "POST",
            "/sessions/search",
            {"context": self._scope_payload(context), "query": query},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError(
                "HttpSessionBackend expected list results from POST /sessions/search"
            )
        for item in results:
            if not isinstance(item, Mapping):
                raise RuntimeError(
                    "HttpSessionBackend results list contained non-object item from POST /sessions/search"
                )
        return list(results)

    def resolve_resume_session_id(self, session_id: str) -> str:
        path = f"/sessions/{urllib.parse.quote(str(session_id), safe='')}/resume-target"
        payload = self._request_json("POST", path, {})
        result_id = payload.get("session_id")
        if not isinstance(result_id, str) or not result_id:
            raise RuntimeError(
                "HttpSessionBackend expected non-empty string session_id from POST /sessions/<session_id>/resume-target"
            )
        return result_id

    def claim_turn_lock(
        self,
        context: "RuntimeContext | None",
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        payload = self._request_json(
            "POST",
            "/sessions/turn-lock/claim",
            {"context": self._scope_payload(context), "owner": owner, "ttl_seconds": ttl_seconds},
        )
        claimed = payload.get("claimed")
        if not isinstance(claimed, bool):
            raise RuntimeError(
                "HttpSessionBackend expected bool claimed from POST /sessions/turn-lock/claim"
            )
        return claimed

    def renew_turn_lock(
        self,
        context: "RuntimeContext | None",
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        payload = self._request_json(
            "POST",
            "/sessions/turn-lock/renew",
            {"context": self._scope_payload(context), "owner": owner, "ttl_seconds": ttl_seconds},
        )
        renewed = payload.get("renewed")
        if not isinstance(renewed, bool):
            raise RuntimeError(
                "HttpSessionBackend expected bool renewed from POST /sessions/turn-lock/renew"
            )
        return renewed

    def release_turn_lock(self, context: "RuntimeContext | None", *, owner: str) -> None:
        self._request_json(
            "POST",
            "/sessions/turn-lock/release",
            {"context": self._scope_payload(context), "owner": owner},
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
            raise RuntimeError(f"HttpSessionBackend request to {endpoint} failed with HTTP {http_status}") from None
        if transport_failed:
            raise RuntimeError(f"HttpSessionBackend could not reach {endpoint}: transport error") from None
        if decode_failed:
            raise RuntimeError(f"HttpSessionBackend got a non-UTF-8 response from {endpoint}") from None

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpSessionBackend request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(f"HttpSessionBackend got a non-JSON response from {endpoint}") from None
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpSessionBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}"
            )
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(method: str, path: str) -> str:
        parts = path.strip("/").split("/")
        if parts == ["sessions", "create"]:
            return f"{method} /sessions/create"
        if parts == ["sessions", "append"]:
            return f"{method} /sessions/append"
        if parts == ["sessions", "messages"]:
            return f"{method} /sessions/messages"
        if parts == ["sessions", "search"]:
            return f"{method} /sessions/search"
        if parts == ["sessions", "turn-lock", "claim"]:
            return f"{method} /sessions/turn-lock/claim"
        if parts == ["sessions", "turn-lock", "renew"]:
            return f"{method} /sessions/turn-lock/renew"
        if parts == ["sessions", "turn-lock", "release"]:
            return f"{method} /sessions/turn-lock/release"
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "resume-target":
            return f"{method} /sessions/<session_id>/resume-target"
        return f"{method} /<redacted-endpoint>"


def register_http_session_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpSessionBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpSessionBackend:
        return HttpSessionBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.SESSION, factory, profile=profile)
