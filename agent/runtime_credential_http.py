"""HTTP credential-resolver adapter (M12B).

Implements a provider-neutral CredentialResolver that transports resolve()
operations to a remote control-plane HTTP API using a RuntimeContext-derived
scope.

Control-plane contract (provider neutral):

* ``POST /credentials/resolve`` body ``{context, ref}`` -> ``{"secret_ref": "..."}`` or ``{"secret_ref": null}``

Refs and secret_refs are carried in JSON request bodies only — never in URL
paths, query parameters, or error messages.

The optional bearer token is sent only in the ``Authorization`` header.  URL
labels and raised errors intentionally omit response bodies, raw ref values,
raw secret_ref values, hostnames, URLs, and raw credential material.
"""

from __future__ import annotations

import http.client
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


class HttpCredentialResolver:
    """Remote CredentialResolver backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if not isinstance(base_url, str):
            raise ValueError("HttpCredentialResolver base_url must be an absolute http(s) URL")
        raw = base_url or ""
        if raw != raw.strip() or _contains_ascii_control(raw):
            raise ValueError("HttpCredentialResolver base_url must be an absolute http(s) URL")
        cleaned = raw
        if not cleaned:
            raise ValueError("HttpCredentialResolver requires a non-empty base_url")
        invalid_url = False
        try:
            parsed = urllib.parse.urlparse(cleaned)
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            invalid_url = True
            parsed = urllib.parse.urlparse("http://invalid.local")
            hostname = None
        if invalid_url or parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
            raise ValueError("HttpCredentialResolver base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpCredentialResolver base_url must not contain credentials")
        if "?" in cleaned or "#" in cleaned:
            raise ValueError("HttpCredentialResolver base_url must not contain query or fragment")
        invalid_timeout = False
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            invalid_timeout = True
            timeout_value = 0.0
        if invalid_timeout or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpCredentialResolver timeout must be a finite positive number")
        if token is not None and not isinstance(token, str):
            raise ValueError("HttpCredentialResolver token must be a string")
        token_value = token or None
        if token_value is not None and _contains_ascii_control(token_value):
            raise ValueError("HttpCredentialResolver token must not contain control characters")
        self._base_url = cleaned.rstrip("/")
        self._token = token_value
        self._timeout = timeout_value

    def resolve(self, context: "RuntimeContext | None", ref: str) -> str | None:
        self._validate_ref(ref)
        result = self._request_json(
            "POST",
            "/credentials/resolve",
            {"context": self._scope_payload(context), "ref": ref},
        )
        if "secret_ref" not in result:
            raise RuntimeError(
                "HttpCredentialResolver expected 'secret_ref' key from POST /credentials/resolve; got malformed response"
            )
        secret_ref = result["secret_ref"]
        if secret_ref is None:
            return None
        if not isinstance(secret_ref, str):
            raise RuntimeError(
                f"HttpCredentialResolver expected string or null secret_ref from POST /credentials/resolve, got {type(secret_ref).__name__}"
            )
        return secret_ref

    @staticmethod
    def _validate_ref(ref: str) -> None:
        if not isinstance(ref, str):
            raise TypeError("HttpCredentialResolver credential ref must be a string")

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        return {
            "mode": context.mode,
            "org_id": context.org_id,
            "workspace_id": context.workspace_id,
            "workspace_type": context.workspace_type,
            "user_id": context.user_id,
            "project_id": context.project_id,
            "agent_profile_id": context.agent_profile_id,
            "permissions_ref": context.permissions_ref,
            "backend_profile": context.backend_profile,
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
        endpoint = f"{method} {path}"
        return self._send(request, endpoint=endpoint)

    def _send(self, request: urllib.request.Request, *, endpoint: str) -> dict[str, Any]:
        http_status: int | None = None
        transport_failed = False
        decode_failed = False
        status = 0
        raw = ""
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            http_status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead):
            transport_failed = True
        except UnicodeDecodeError:
            decode_failed = True

        if http_status is not None:
            raise RuntimeError(
                f"HttpCredentialResolver request to {endpoint} failed with HTTP {http_status}"
            ) from None
        if transport_failed:
            raise RuntimeError(
                f"HttpCredentialResolver could not reach {endpoint}: transport error"
            ) from None
        if decode_failed:
            raise RuntimeError(
                f"HttpCredentialResolver got a non-UTF-8 response from {endpoint}"
            ) from None

        if not 200 <= status < 300:
            raise RuntimeError(
                f"HttpCredentialResolver request to {endpoint} returned non-2xx status {status}"
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
                f"HttpCredentialResolver got a non-JSON response from {endpoint}"
            ) from None
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpCredentialResolver expected a JSON object from {endpoint}, got {type(decoded).__name__}"
            )
        return dict(decoded)


def register_http_credential_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpCredentialResolver` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpCredentialResolver:
        return HttpCredentialResolver(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.CREDENTIAL, factory, profile=profile)
