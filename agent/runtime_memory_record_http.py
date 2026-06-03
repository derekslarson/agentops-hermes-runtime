"""HTTP remote deep-memory record adapter (M5B).

Implements the first real remote :class:`~agent.runtime_backends.MemoryRecordBackend`
(the ``DEEP_MEMORY`` capability) against a cloud/database-agnostic control-plane
HTTP API. The native Hermes deep-memory surfaces are unchanged: the
``memory_record_search`` / ``memory_record_get`` / ``memory_record_get_many``
tools and the prefetch path keep dispatching through ``BackendCapability.DEEP_MEMORY``
and this adapter only ships scoped record operations to the control plane,
scoped by :class:`~agent.runtime_context.RuntimeContext`.

The control-plane contract is intentionally provider-neutral:

* ``GET  /memory/records/search?context=<json>&query=<q>&...`` -> ``{"results": [<result>...]}``
* ``GET  /memory/records/get?context=<json>&id=<id>`` -> ``{"record": <record>|null}``
* ``POST /memory/records/get_many`` body ``{"context": <dict>, "ids": [<id>...]}`` -> ``{"records": [<record>...]}``
* ``POST /memory/records`` body ``{"context": <dict>, "record": {<upsert fields>}}`` -> ``{"record": <record>}``
* ``POST /memory/records/import`` body ``{"context": <dict>, "records": [<upsert fields>...]}`` -> ``{"results": [<result>...]}``

JSON ``<result>`` / ``<record>`` objects are converted back into the local
:class:`~agent.local_memory.store.SearchResult` / :class:`MemoryRecord` shapes so
the native record tools (``search_result_payload`` / ``fenced_record`` /
``format_hints``) keep working unchanged.

An optional bearer token is sent only via the ``Authorization`` header, never in
the JSON payload or query string. The adapter fails closed: a missing/invalid
``base_url`` raises ``ValueError`` at construction, and any non-2xx response,
transport failure, or unparseable body raises ``RuntimeError``.

Stdlib only — no third-party HTTP client and no Chroma/ONNX import — so the
adapter stays dependency-free.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from agent.runtime_backends import BackendCapability

if TYPE_CHECKING:
    from agent.runtime_backends import RuntimeBackendRegistry
    from agent.runtime_context import RuntimeContext

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_PROFILE = "compose-self-hosted"


class HttpMemoryRecordBackend:
    """Remote ``MemoryRecordBackend`` talking to a control-plane-like HTTP API.

    Configured with a required ``base_url`` and optional ``token``/``timeout``. A
    durable deep-memory scope derived from :class:`RuntimeContext` is sent to the
    control plane so it can isolate records per org/workspace/project/conversation/user
    without leaking metadata, credential refs, or provider-specific knowledge into
    this client. RuntimeContext stays a per-call argument and is never bound into
    the cached backend instance.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> None:
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("HttpMemoryRecordBackend requires a non-empty base_url")
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HttpMemoryRecordBackend base_url must be an absolute http(s) URL")
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError("HttpMemoryRecordBackend base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("HttpMemoryRecordBackend base_url must not contain query or fragment")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("HttpMemoryRecordBackend timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("HttpMemoryRecordBackend timeout must be a finite positive number")
        self._base_url = cleaned.rstrip("/")
        self._token = token or None
        self._timeout = timeout_value

    def upsert_record(
        self,
        context: "RuntimeContext | None",
        *,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: float | None = None,
        record_kind: str = "verbatim",
        parent_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        record_id: str | None = None,
    ) -> Any:
        record = {
            "text": text,
            "metadata": dict(metadata) if metadata else {},
            "source": source,
            "source_uri": source_uri,
            "timestamp": timestamp,
            "record_kind": record_kind,
            "parent_id": parent_id,
            "provenance": dict(provenance) if provenance else {},
            "record_id": record_id,
        }
        payload = self._post("/memory/records", {"context": self._scope_payload(context), "record": record})
        upserted = payload.get("record")
        if upserted is None:
            # Fail closed: a 2xx upsert that omits the stored record is malformed —
            # never report success without a durable record echoed back.
            raise RuntimeError("HttpMemoryRecordBackend upsert response missing 'record'")
        return _to_record(upserted)

    def search(
        self,
        context: "RuntimeContext | None",
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> list[Any]:
        params = {
            "context": json.dumps(self._scope_payload(context)),
            "query": query,
            "limit": str(int(limit)),
            "max_distance": str(float(max_distance)),
            "candidate_strategy": candidate_strategy,
        }
        if filters:
            params["filters"] = json.dumps(dict(filters))
        payload = self._get("/memory/records/search", params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("HttpMemoryRecordBackend expected a list of results")
        return [_to_search_result(item) for item in results]

    def get_record(self, context: "RuntimeContext | None", record_id: str) -> Any:
        params = {"context": json.dumps(self._scope_payload(context)), "id": record_id}
        payload = self._get("/memory/records/get", params)
        if "record" not in payload:
            # Fail closed: a 2xx get that omits the 'record' key is malformed. An
            # explicit ``{"record": null}`` still means a clean not-found.
            raise RuntimeError("HttpMemoryRecordBackend get response missing 'record'")
        return _to_record(payload["record"])

    def import_records(self, context: "RuntimeContext | None", records: list[Any]) -> list[Any]:
        body = {"context": self._scope_payload(context), "records": list(records)}
        payload = self._post("/memory/records/import", body)
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("HttpMemoryRecordBackend expected a list of results from import")
        return results

    def get_many(self, context: "RuntimeContext | None", ids: Iterable[str]) -> list[Any]:
        body = {"context": self._scope_payload(context), "ids": [str(i) for i in ids]}
        payload = self._post("/memory/records/get_many", body)
        records = payload.get("records")
        if not isinstance(records, list):
            raise RuntimeError("HttpMemoryRecordBackend expected a list of records")
        if any(item is None for item in records):
            # Fail closed: a null entry in the records array is malformed. Missing
            # IDs are signalled by omission from the list, never by a null member.
            raise RuntimeError("HttpMemoryRecordBackend get_many response contained a null record entry")
        return [_to_record(item) for item in records]

    @staticmethod
    def _scope_payload(context: "RuntimeContext | None") -> dict[str, Any]:
        if context is None:
            return {}
        # Durable deep-memory scope mirrors the local store's tenant/user/project/
        # conversation partitioning (see ``_deep_memory_scope_key``) and excludes
        # per-execution identifiers (run/job IDs), metadata, and credential/delivery
        # refs so nothing sensitive or per-run lands in a URL or durable record store.
        return {
            "mode": context.mode,
            "org_id": context.org_id,
            "workspace_id": context.workspace_id,
            "project_id": context.project_id,
            "agent_profile_id": context.agent_profile_id,
            "conversation_id": context.conversation_id,
            "user_id": context.user_id,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}?{urllib.parse.urlencode(params)}",
            method="GET",
            headers=self._headers(),
        )
        return self._send(request)

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        endpoint = self._safe_endpoint_label(request)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"HttpMemoryRecordBackend request to {endpoint} failed with HTTP {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError(
                f"HttpMemoryRecordBackend could not reach {endpoint}: transport error"
            ) from None

        if not 200 <= status < 300:
            raise RuntimeError(
                f"HttpMemoryRecordBackend request to {endpoint} returned non-2xx status {status}"
            )

        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"HttpMemoryRecordBackend got a non-JSON response from {endpoint}"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise RuntimeError(
                f"HttpMemoryRecordBackend expected a JSON object from {endpoint}, got {type(decoded).__name__}"
            )
        return dict(decoded)

    @staticmethod
    def _safe_endpoint_label(request: urllib.request.Request) -> str:
        parsed = urllib.parse.urlparse(request.full_url)
        return f"{request.get_method()} {parsed.scheme}://{parsed.netloc}{parsed.path}"


def _to_record(payload: Any) -> Any:
    """Rebuild a local ``MemoryRecord`` from a control-plane JSON object (or ``None``)."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"HttpMemoryRecordBackend expected a record object, got {type(payload).__name__}")
    from agent.local_memory.store import MemoryRecord

    return _build_dataclass(MemoryRecord, payload)


def _to_search_result(payload: Any) -> Any:
    """Rebuild a local ``SearchResult`` from a control-plane JSON object."""
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"HttpMemoryRecordBackend expected a result object, got {type(payload).__name__}")
    from agent.local_memory.store import SearchResult

    return _build_dataclass(SearchResult, payload)


def _build_dataclass(cls: Any, payload: Mapping[str, Any]) -> Any:
    """Construct ``cls`` from ``payload``, keeping only known fields and required ones.

    Unknown keys are dropped (forward-compatible with control-plane additions) and
    missing keys fall back to dataclass defaults; a field with no default that is
    absent raises ``RuntimeError`` rather than a bare ``TypeError``.
    """
    accepted: dict[str, Any] = {}
    for field in dataclass_fields(cls):
        if field.name in payload:
            accepted[field.name] = payload[field.name]
    try:
        return cls(**accepted)
    except TypeError as exc:
        raise RuntimeError(f"HttpMemoryRecordBackend could not build {cls.__name__}: {exc}") from exc


def register_http_memory_record_backend(
    registry: "RuntimeBackendRegistry", profile: str = _DEFAULT_PROFILE
) -> None:
    """Register an :class:`HttpMemoryRecordBackend` factory for ``profile``.

    The factory reads ``base_url``/``token``/``timeout`` from the registry's
    deep-memory capability options, so deployment config — not RuntimeContext —
    supplies the connection settings. This is the remote/durable DEEP_MEMORY
    adapter that lets AgentOps/compose profiles route native deep-memory recall to
    the control plane instead of failing closed.
    """

    def factory(options: Mapping[str, Any]) -> HttpMemoryRecordBackend:
        return HttpMemoryRecordBackend(
            base_url=options.get("base_url", ""),
            token=options.get("token"),
            timeout=options.get("timeout") or _DEFAULT_TIMEOUT,
        )

    registry.register(BackendCapability.DEEP_MEMORY, factory, profile=profile)
