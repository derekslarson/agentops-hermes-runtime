"""HTTP remote cron adapter (M8).

Implements the first remote :class:`~agent.runtime_backends.CronBackend`
against a cloud/database-agnostic control-plane HTTP API. The native Hermes
``cronjob`` tool still owns prompt validation, schedule parsing, and local
compatibility; this adapter transports the generic logical cron model to a
remote scheduler/worker backend using a RuntimeContext-derived scope.

Control-plane contract (provider neutral):

* ``POST   /cron/jobs`` -> ``{"id": str}``
* ``PATCH  /cron/jobs/<id>``
* ``GET    /cron/jobs`` -> ``{"jobs": [...]}``
* ``GET    /cron/jobs/<id>`` -> ``{"job": {...}|null}``
* ``DELETE /cron/jobs/<id>``
* ``POST   /cron/jobs/<id>/pause`` / ``resume``
* ``GET    /cron/jobs/<id>/history`` -> ``{"history": [...]}``
* ``POST   /cron/jobs/claim-due`` -> ``{"jobs": [...]}``
* ``POST   /cron/jobs/<id>/renew-lease`` -> ``{"renewed": bool}``
* ``POST   /cron/jobs/<id>/release-lease``
* ``POST   /cron/jobs/<id>/complete`` / ``fail`` -> run-history entry

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

from agent.runtime_backends import BackendCapability, _redact_audit_text

if TYPE_CHECKING:
    from agent.runtime_backends import RuntimeBackendRegistry
    from agent.runtime_context import RuntimeContext

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_PROFILE = "compose-self-hosted"
_SECRET_JOB_KEYS = {"token", "secret", "password", "apikey", "authorization", "permissionsref", "deliveryref", "credential"}


class HttpCronBackend:
    """Remote ``CronBackend`` talking to a control-plane-like HTTP API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpCronBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpCronBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpCronBackend base_url must not contain credentials")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("HttpCronBackend base_url must be an absolute http(s) URL") from exc
        if parsed.query or parsed.fragment:
            raise ValueError("HttpCronBackend base_url must not contain query or fragment")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("HttpCronBackend timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpCronBackend timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def create(self, context: "RuntimeContext | None", job: Mapping[str, Any]) -> str:
        payload = self._request_json("POST", "/cron/jobs", {"context": self._scope_payload(context), "job": self._safe_job(job)})
        job_id = payload.get("id") or payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("HttpCronBackend create expected non-empty string id")
        return job_id

    def update(self, context: "RuntimeContext | None", job_id: str, job: Mapping[str, Any]) -> None:
        self._request_json("PATCH", f"/cron/jobs/{self._quote_id(job_id)}", {"context": self._scope_payload(context), "job": self._safe_job(job)})

    def get_job(self, context: "RuntimeContext | None", job_id: str) -> Mapping[str, Any] | None:
        payload = self._request_json("GET", f"/cron/jobs/{self._quote_id(job_id)}", context=self._scope_payload(context))
        job = payload.get("job")
        if job is None:
            return None
        if not isinstance(job, Mapping):
            raise RuntimeError(f"HttpCronBackend expected job object, got {type(job).__name__}")
        return dict(job)

    def pause(self, context: "RuntimeContext | None", job_id: str) -> None:
        self._request_json("POST", f"/cron/jobs/{self._quote_id(job_id)}/pause", {"context": self._scope_payload(context)})

    def resume(self, context: "RuntimeContext | None", job_id: str) -> None:
        self._request_json("POST", f"/cron/jobs/{self._quote_id(job_id)}/resume", {"context": self._scope_payload(context)})

    def remove(self, context: "RuntimeContext | None", job_id: str) -> None:
        self._request_json("DELETE", f"/cron/jobs/{self._quote_id(job_id)}", context=self._scope_payload(context))

    def list_jobs(self, context: "RuntimeContext | None") -> list[Any]:
        payload = self._request_json("GET", "/cron/jobs", context=self._scope_payload(context))
        if "jobs" not in payload:
            raise RuntimeError("HttpCronBackend expected jobs list, got missing")
        jobs = payload["jobs"]
        if not isinstance(jobs, list):
            raise RuntimeError(f"HttpCronBackend expected jobs list, got {type(jobs).__name__}")
        if not all(isinstance(job, Mapping) for job in jobs):
            raise RuntimeError("HttpCronBackend expected every job to be an object")
        return jobs

    def run_history(self, context: "RuntimeContext | None", job_id: str) -> list[Any]:
        payload = self._request_json("GET", f"/cron/jobs/{self._quote_id(job_id)}/history", context=self._scope_payload(context))
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise RuntimeError(f"HttpCronBackend expected history list, got {type(history).__name__}")
        return history

    def claim_due(
        self,
        context: "RuntimeContext | None",
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
        draining: bool = False,
        limit: int | None = None,
    ) -> list[Mapping[str, Any]]:
        if draining:
            return []
        body = {
            "context": self._scope_payload(context),
            "owner": owner,
            "now": now,
            "lease_seconds": lease_seconds,
            "limit": limit,
        }
        payload = self._request_json("POST", "/cron/jobs/claim-due", body)
        if "jobs" not in payload:
            raise RuntimeError("HttpCronBackend expected claimed jobs list, got missing")
        jobs = payload["jobs"]
        if not isinstance(jobs, list):
            raise RuntimeError(f"HttpCronBackend expected claimed jobs list, got {type(jobs).__name__}")
        if not all(isinstance(job, Mapping) for job in jobs):
            raise RuntimeError("HttpCronBackend expected every claimed job to be an object")
        return jobs

    def renew_lease(
        self,
        context: "RuntimeContext | None",
        job_id: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> bool:
        payload = self._request_json(
            "POST",
            f"/cron/jobs/{self._quote_id(job_id)}/renew-lease",
            {"context": self._scope_payload(context), "owner": owner, "now": now, "lease_seconds": lease_seconds},
        )
        renewed = payload.get("renewed")
        if not isinstance(renewed, bool):
            raise RuntimeError(f"HttpCronBackend expected boolean renewed, got {type(renewed).__name__}")
        return renewed

    def release_lease(self, context: "RuntimeContext | None", job_id: str, *, owner: str) -> None:
        self._request_json("POST", f"/cron/jobs/{self._quote_id(job_id)}/release-lease", {"context": self._scope_payload(context), "owner": owner})

    def complete_run(
        self,
        context: "RuntimeContext | None",
        job_id: str,
        *,
        owner: str,
        output: str | None = None,
        delivery_error: str | None = None,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            f"/cron/jobs/{self._quote_id(job_id)}/complete",
            {
                "context": self._scope_payload(context),
                "owner": owner,
                "output": output,
                "delivery_error": _redact_audit_text(delivery_error) if delivery_error is not None else None,
                "now": now,
                "next_run_at": next_run_at,
            },
        )

    def fail_run(
        self,
        context: "RuntimeContext | None",
        job_id: str,
        *,
        owner: str,
        error: str,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            f"/cron/jobs/{self._quote_id(job_id)}/fail",
            {"context": self._scope_payload(context), "owner": owner, "error": _redact_audit_text(error), "now": now, "next_run_at": next_run_at},
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

    @classmethod
    def _safe_job(cls, job: Mapping[str, Any]) -> dict[str, Any]:
        sanitized = cls._sanitize_value(dict(job))
        if not isinstance(sanitized, dict):
            return {}
        sanitized.pop("binding", None)
        return sanitized

    @classmethod
    def _normalized_secret_key(cls, key: object) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            safe: dict[str, Any] = {}
            for key, nested in value.items():
                lowered = cls._normalized_secret_key(key)
                if any(marker in lowered for marker in _SECRET_JOB_KEYS):
                    continue
                safe[str(key)] = cls._sanitize_value(nested)
            return safe
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_value(item) for item in value]
        return value

    @staticmethod
    def _quote_id(job_id: str) -> str:
        return urllib.parse.quote(str(job_id), safe="")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if query:
            encoded = urllib.parse.urlencode(
                {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in query.items()}
            )
            url = f"{url}?{encoded}"
        data = None
        headers = self._headers()
        if context is not None:
            headers["X-Hermes-Runtime-Context"] = json.dumps(context, separators=(",", ":"))
        if body is not None:
            data = json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        endpoint = self._safe_endpoint_label(request)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HttpCronBackend request to {endpoint} failed with HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError(f"HttpCronBackend could not reach {endpoint}: transport error") from None

        if not 200 <= status < 300:
            raise RuntimeError(f"HttpCronBackend request to {endpoint} returned non-2xx status {status}")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"HttpCronBackend got a non-JSON response from {endpoint}") from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError(f"HttpCronBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}")
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(request: urllib.request.Request) -> str:
        parsed = urllib.parse.urlparse(request.full_url)
        return f"{request.get_method()} {parsed.scheme}://{parsed.netloc}{parsed.path}"


def register_http_cron_backend(registry: "RuntimeBackendRegistry", profile: str = _DEFAULT_PROFILE) -> None:
    """Register an :class:`HttpCronBackend` factory for ``profile``."""

    def factory(options: Mapping[str, Any]) -> HttpCronBackend:
        return HttpCronBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout", _DEFAULT_TIMEOUT),
        )

    registry.register(BackendCapability.CRON, factory, profile=profile)
