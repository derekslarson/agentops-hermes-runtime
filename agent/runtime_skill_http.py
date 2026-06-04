"""HTTP skill backend adapter (M12B).

Implements a provider-neutral SkillBackend that transports list/load/manage
operations to a remote control-plane HTTP API using a RuntimeContext-derived
scope.

Control-plane contract (provider neutral):

* ``POST /skills/list``   body ``{context, category?}``                   -> ``{"skills": [...]}``
* ``POST /skills/view``   body ``{context, name, file_path?, preprocess?}`` -> ``{...skill_view payload...}`` or ``null``
* ``POST /skills/manage`` body ``{context, action, name, ...fields}``     -> ``{...result...}``

Skill names and linked ``file_path`` values are carried in JSON request bodies
only — never in URL paths, query parameters, or error messages.  Mutation
fields (``content``, ``file_content``, ``old_string``, ``new_string``) are
product payload transported verbatim but never surfaced in errors.

The optional bearer token is sent only in the ``Authorization`` header.  URL
labels and raised errors intentionally omit response bodies, raw name values,
raw file_path values, hostnames, URLs, and raw credential material.
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


class HttpSkillBackend:
    """Remote SkillBackend backed by a control-plane HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if not isinstance(base_url, str):
            raise ValueError("HttpSkillBackend base_url must be an absolute http(s) URL")
        raw = base_url or ""
        if raw != raw.strip() or _contains_ascii_control(raw):
            raise ValueError("HttpSkillBackend base_url must be an absolute http(s) URL")
        cleaned = raw
        if not cleaned:
            raise ValueError("HttpSkillBackend requires a non-empty base_url")
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
            raise ValueError("HttpSkillBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpSkillBackend base_url must not contain credentials")
        if "?" in cleaned or "#" in cleaned:
            raise ValueError("HttpSkillBackend base_url must not contain query or fragment")
        invalid_timeout = False
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            invalid_timeout = True
            timeout_value = 0.0
        if invalid_timeout or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpSkillBackend timeout must be a finite positive number")
        if token is not None and not isinstance(token, str):
            raise ValueError("HttpSkillBackend token must be a string")
        token_value = token or None
        if token_value is not None and _contains_ascii_control(token_value):
            raise ValueError("HttpSkillBackend token must not contain control characters")
        self._base_url = cleaned.rstrip("/")
        self._token = token_value
        self._timeout = timeout_value

    def list_skills(
        self,
        context: "RuntimeContext | None",
        *,
        category: str | None = None,
    ) -> list[Mapping[str, Any]]:
        body: dict[str, Any] = {"context": self._scope_payload(context)}
        if category is not None:
            body["category"] = category
        result = self._request_json_value("POST", "/skills/list", body)
        if isinstance(result, list):
            return self._validate_skill_list(result)
        if not isinstance(result, Mapping):
            raise RuntimeError(
                f"HttpSkillBackend expected a JSON object or list from POST /skills/list, got {type(result).__name__}"
            )
        if "skills" not in result:
            raise RuntimeError(
                "HttpSkillBackend expected 'skills' key from POST /skills/list; got malformed response"
            )
        skills = result["skills"]
        if not isinstance(skills, list):
            raise RuntimeError(
                f"HttpSkillBackend expected a list for 'skills' from POST /skills/list, got {type(skills).__name__}"
            )
        return self._validate_skill_list(skills)

    def load_skill(
        self,
        context: "RuntimeContext | None",
        name: str,
        *,
        file_path: str | None = None,
        preprocess: bool = True,
    ) -> Mapping[str, Any] | None:
        self._validate_name(name)
        body: dict[str, Any] = {
            "context": self._scope_payload(context),
            "name": name,
            "preprocess": preprocess,
        }
        if file_path is not None:
            body["file_path"] = file_path
        return self._request_json_or_null("POST", "/skills/view", body)

    def manage_skill(
        self,
        context: "RuntimeContext | None",
        *,
        action: str,
        name: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        self._validate_name(name)
        body: dict[str, Any] = {
            "context": self._scope_payload(context),
            "action": action,
            "name": name,
            **fields,
        }
        return self._request_json("POST", "/skills/manage", body)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str):
            raise TypeError("HttpSkillBackend skill name must be a string")

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        scope: dict[str, Any] = {
            "mode": context.mode,
            "org_id": context.org_id,
            "workspace_id": context.workspace_id,
            "workspace_type": context.workspace_type,
            "user_id": context.user_id,
            "project_id": context.project_id,
            "conversation_id": context.conversation_id,
            "agent_profile_id": context.agent_profile_id,
            "permissions_ref": context.permissions_ref,
            "backend_profile": context.backend_profile,
        }
        if isinstance(context.metadata, Mapping) and "skill_write_approved" in context.metadata:
            scope["skill_write_approved"] = bool(context.metadata["skill_write_approved"])
        return scope

    @staticmethod
    def _validate_skill_list(skills: list[Any]) -> list[Mapping[str, Any]]:
        validated: list[Mapping[str, Any]] = []
        for skill in skills:
            if not isinstance(skill, Mapping):
                raise RuntimeError(
                    f"HttpSkillBackend expected skill entry object from POST /skills/list; got {type(skill).__name__}"
                )
            name = skill.get("name")
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    f"HttpSkillBackend expected skill entry name string from POST /skills/list; got {type(name).__name__}"
                )
            category = skill.get("category")
            if category is not None and not isinstance(category, str):
                raise RuntimeError(
                    f"HttpSkillBackend expected skill entry category string from POST /skills/list; got {type(category).__name__}"
                )
            validated.append(dict(skill))
        return validated

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_json(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self._request_json_or_null(method, path, body)
        if result is None:
            raise RuntimeError(
                f"HttpSkillBackend expected a JSON object from {method} {path}, got NoneType"
            )
        return result

    def _request_json_or_null(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        decoded = self._request_json_value(method, path, body)
        if decoded is None:
            return None
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpSkillBackend expected a JSON object from {method} {path}, got {type(decoded).__name__}"
            )
        return dict(decoded)

    def _request_json_value(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        endpoint = f"{method} {path}"
        return self._send(request, endpoint=endpoint)

    def _send(self, request: urllib.request.Request, *, endpoint: str) -> Any:
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
                f"HttpSkillBackend request to {endpoint} failed with HTTP {http_status}"
            ) from None
        if transport_failed:
            raise RuntimeError(
                f"HttpSkillBackend could not reach {endpoint}: transport error"
            ) from None
        if decode_failed:
            raise RuntimeError(
                f"HttpSkillBackend got a non-UTF-8 response from {endpoint}"
            ) from None

        if not 200 <= status < 300:
            raise RuntimeError(
                f"HttpSkillBackend request to {endpoint} returned non-2xx status {status}"
            )
        if not raw:
            raise RuntimeError(
                f"HttpSkillBackend got an empty response from {endpoint}"
            ) from None
        non_json = object()
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError:
            decoded = non_json
        if decoded is non_json:
            raise RuntimeError(
                f"HttpSkillBackend got a non-JSON response from {endpoint}"
            ) from None
        return decoded


def register_http_skill_backend(
    registry: "RuntimeBackendRegistry",
    profile: str = _DEFAULT_PROFILE,
) -> None:
    """Register an :class:`HttpSkillBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpSkillBackend:
        return HttpSkillBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.SKILL, factory, profile=profile)
