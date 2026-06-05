"""Server-side handler for /cron/jobs control-plane API (M12B).

Routes:
    GET    /cron/jobs                     context: X-Hermes-Runtime-Context  -> {jobs: [...]}
    POST   /cron/jobs                     body {context, job}                 -> {id: str}
    PATCH  /cron/jobs/<id>                body {context, job}                 -> {}
    GET    /cron/jobs/<id>                context: X-Hermes-Runtime-Context  -> {job: {...}|null}
    DELETE /cron/jobs/<id>                context: X-Hermes-Runtime-Context  -> {}
    POST   /cron/jobs/<id>/pause          body {context}                      -> {}
    POST   /cron/jobs/<id>/resume         body {context}                      -> {}
    GET    /cron/jobs/<id>/history        context: X-Hermes-Runtime-Context  -> {history: [...]}
    POST   /cron/jobs/claim-due           body {context, owner, ...}          -> {jobs: [...]}
    POST   /cron/jobs/<id>/renew-lease    body {context, owner, ...}          -> {renewed: bool}
    POST   /cron/jobs/<id>/release-lease  body {context, owner}               -> {}
    POST   /cron/jobs/<id>/complete       body {context, owner, ...}          -> {...}
    POST   /cron/jobs/<id>/fail           body {context, owner, error, ...}   -> {...}

Auth: when token is non-None, Authorization: Bearer *** is required before body read.
Context: GET/DELETE pass scope in X-Hermes-Runtime-Context header.
         POST/PATCH pass scope in body "context" field.
         Null or empty {} context maps to None (default all-None scope).
Errors: sanitized; no raw job IDs, content, output/error strings, tokens,
        context bodies, DB paths, or backend exception text.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.parse
from typing import Any

from agent.runtime_backends import CronLeaseError
from agent.runtime_context import RuntimeContext

_CRON_JOB_POST_ACTIONS = frozenset({"pause", "resume", "renew-lease", "release-lease", "complete", "fail"})
_CRON_JOB_GET_ACTIONS = frozenset({"history"})
_CRON_JOB_ALL_ACTIONS = _CRON_JOB_POST_ACTIONS | _CRON_JOB_GET_ACTIONS
_MAX_CLAIM_LIMIT = 10_000
_MAX_FINITE_FLOAT_INPUT = sys.float_info.max


def _decode_job_id_segment(seg: str) -> str | None:
    try:
        job_id = urllib.parse.unquote(seg, errors="strict")
    except Exception:
        return None
    if not job_id or "/" in job_id or "\\" in job_id:
        return None
    return job_id


def _parse_cron_route(raw_path: str) -> tuple[str | None, str | None, str | None]:
    """Return (route_type, job_id, error).

    route_type values: 'jobs', 'job', 'claim-due', and per-job action names.
    job_id is set for routes that include a job ID segment.
    Rejects semicolons, unknown actions, and malformed segment counts.
    """
    if ";" in raw_path:
        return None, None, "not found"
    path = raw_path.split("?", 1)[0]
    segments = path.split("/")
    if len(segments) < 3 or segments[0] != "" or segments[1] != "cron" or segments[2] != "jobs":
        return None, None, "not found"
    rest = segments[3:]
    if not rest:
        return "jobs", None, None
    if len(rest) == 1:
        seg = rest[0]
        if not seg:
            return None, None, "not found"
        if seg == "claim-due":
            return "claim-due", None, None
        job_id = _decode_job_id_segment(seg)
        if job_id is None:
            return None, None, "not found"
        return "job", job_id, None
    if len(rest) == 2:
        job_id_raw, action = rest[0], rest[1]
        if not job_id_raw or not action:
            return None, None, "not found"
        if action not in _CRON_JOB_ALL_ACTIONS:
            return None, None, "not found"
        job_id = _decode_job_id_segment(job_id_raw)
        if job_id is None:
            return None, None, "not found"
        return action, job_id, None
    return None, None, "not found"


def is_cron_route(raw_path: str) -> bool:
    """Return True only for paths accepted by the cron API contract."""
    _, _, error = _parse_cron_route(raw_path.split("?", 1)[0])
    return error is None


def _parse_context(raw: Any) -> tuple[RuntimeContext | None, str | None]:
    if raw is None or (isinstance(raw, dict) and not raw):
        return None, None
    if not isinstance(raw, dict):
        return None, "invalid context"
    try:
        ctx = RuntimeContext.from_mapping(raw)
    except (TypeError, ValueError):
        return None, "invalid context"
    return ctx, None


def _parse_context_header(header: str | None) -> tuple[RuntimeContext | None, str | None]:
    if not header:
        return None, None
    try:
        raw = json.loads(header)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, "invalid context"
    return _parse_context(raw)


def _parse_body(body_bytes: bytes) -> tuple[dict | None, tuple[int, dict] | None]:
    if not body_bytes:
        return None, (400, {"error": "invalid request"})
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, (400, {"error": "invalid request"})
    if not isinstance(body, dict):
        return None, (400, {"error": "invalid request"})
    return body, None


def _is_finite_number(value: int | float) -> bool:
    if isinstance(value, float):
        return not (math.isnan(value) or math.isinf(value))
    return abs(value) <= _MAX_FINITE_FLOAT_INPUT


def _parse_now(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid now"
    if not isinstance(value, (int, float)):
        return None, "invalid now"
    if not _is_finite_number(value):
        return None, "invalid now"
    return float(value), None


def _parse_lease_seconds(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid lease_seconds"
    if not isinstance(value, (int, float)):
        return None, "invalid lease_seconds"
    if not _is_finite_number(value) or value <= 0:
        return None, "invalid lease_seconds"
    return float(value), None


def _parse_limit(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid limit"
    if not isinstance(value, (int, float)):
        return None, "invalid limit"
    if not _is_finite_number(value):
        return None, "invalid limit"
    if isinstance(value, float) and not value.is_integer():
        return None, "invalid limit"
    limit = int(value)
    if limit <= 0 or limit > _MAX_CLAIM_LIMIT:
        return None, "invalid limit"
    return limit, None


def _parse_next_run_at(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "invalid next_run_at"
    if not isinstance(value, (int, float)):
        return None, "invalid next_run_at"
    if not _is_finite_number(value):
        return None, "invalid next_run_at"
    return float(value), None


def _validate_job_payload(job: dict[str, Any]) -> str | None:
    if "next_run_at" in job:
        _, err = _parse_next_run_at(job.get("next_run_at"))
        if err:
            return err
    return None


def handle_cron_request(
    method: str,
    path: str,
    query_string: str,
    body_bytes: bytes,
    auth_header: str | None,
    token: str | None,
    backend: Any,
    *,
    context_header: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if token and auth_header != f"Bearer {token}":
        return 401, {"error": "unauthorized"}

    route_type, job_id, route_error = _parse_cron_route(path)
    if route_error:
        return 404, {"error": "not found"}

    if route_type == "jobs":
        if method == "GET":
            ctx, ctx_err = _parse_context_header(context_header)
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _list_jobs(ctx, backend)
        if method == "POST":
            body, body_err = _parse_body(body_bytes)
            if body_err:
                return body_err
            ctx, ctx_err = _parse_context(body.get("context"))
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _create_job(body, ctx, backend)
        return 405, {"error": "method not allowed"}

    if route_type == "claim-due":
        if method != "POST":
            return 405, {"error": "method not allowed"}
        body, body_err = _parse_body(body_bytes)
        if body_err:
            return body_err
        ctx, ctx_err = _parse_context(body.get("context"))
        if ctx_err:
            return 400, {"error": "invalid context"}
        return _claim_due(body, ctx, backend)

    if route_type == "job":
        if method == "GET":
            ctx, ctx_err = _parse_context_header(context_header)
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _get_job(job_id, ctx, backend)
        if method == "PATCH":
            body, body_err = _parse_body(body_bytes)
            if body_err:
                return body_err
            ctx, ctx_err = _parse_context(body.get("context"))
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _update_job(job_id, body, ctx, backend)
        if method == "DELETE":
            ctx, ctx_err = _parse_context_header(context_header)
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _delete_job(job_id, ctx, backend)
        return 405, {"error": "method not allowed"}

    if route_type == "history":
        if method == "GET":
            ctx, ctx_err = _parse_context_header(context_header)
            if ctx_err:
                return 400, {"error": "invalid context"}
            return _history(job_id, ctx, backend)
        return 405, {"error": "method not allowed"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    body, body_err = _parse_body(body_bytes)
    if body_err:
        return body_err
    ctx, ctx_err = _parse_context(body.get("context"))
    if ctx_err:
        return 400, {"error": "invalid context"}

    if route_type == "pause":
        return _pause(job_id, ctx, backend)
    if route_type == "resume":
        return _resume(job_id, ctx, backend)
    if route_type == "renew-lease":
        return _renew_lease(job_id, body, ctx, backend)
    if route_type == "release-lease":
        return _release_lease(job_id, body, ctx, backend)
    if route_type == "complete":
        return _complete(job_id, body, ctx, backend)
    if route_type == "fail":
        return _fail(job_id, body, ctx, backend)

    return 404, {"error": "not found"}


def _list_jobs(ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        jobs = backend.list_jobs(ctx)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"jobs": jobs}


def _create_job(body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    job = body.get("job")
    if not isinstance(job, dict):
        return 400, {"error": "invalid request"}
    if _validate_job_payload(job):
        return 400, {"error": "invalid request"}
    try:
        job_id = backend.create(ctx, job)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"id": job_id}


def _get_job(job_id: str, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        job = backend.get_job(ctx, job_id)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"job": job}


def _update_job(job_id: str, body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    job = body.get("job")
    if not isinstance(job, dict):
        return 400, {"error": "invalid request"}
    if _validate_job_payload(job):
        return 400, {"error": "invalid request"}
    try:
        backend.update(ctx, job_id, job)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {}


def _delete_job(job_id: str, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        backend.remove(ctx, job_id)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {}


def _pause(job_id: str, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        backend.pause(ctx, job_id)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {}


def _resume(job_id: str, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        backend.resume(ctx, job_id)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {}


def _history(job_id: str, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    try:
        history = backend.run_history(ctx, job_id)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"history": history}


def _claim_due(body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ls_raw = body.get("lease_seconds")
    lease_seconds, ls_err = _parse_lease_seconds(ls_raw)
    if ls_err:
        return 400, {"error": "invalid request"}

    limit_raw = body.get("limit")
    limit, limit_err = _parse_limit(limit_raw)
    if limit_err:
        return 400, {"error": "invalid request"}

    kwargs: dict[str, Any] = {"owner": owner}
    draining = body.get("draining", False)
    if not isinstance(draining, bool):
        return 400, {"error": "invalid request"}
    if now is not None:
        kwargs["now"] = now
    if lease_seconds is not None:
        kwargs["lease_seconds"] = lease_seconds
    if limit is not None:
        kwargs["limit"] = limit
    if draining:
        kwargs["draining"] = True

    try:
        jobs = backend.claim_due(ctx, **kwargs)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"jobs": jobs}


def _renew_lease(job_id: str, body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    ls_raw = body.get("lease_seconds")
    lease_seconds, ls_err = _parse_lease_seconds(ls_raw)
    if ls_err:
        return 400, {"error": "invalid request"}

    kwargs: dict[str, Any] = {"owner": owner}
    if now is not None:
        kwargs["now"] = now
    if lease_seconds is not None:
        kwargs["lease_seconds"] = lease_seconds

    try:
        renewed = backend.renew_lease(ctx, job_id, **kwargs)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {"renewed": renewed}


def _release_lease(job_id: str, body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}
    try:
        backend.release_lease(ctx, job_id, owner=owner)
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, {}


def _complete(job_id: str, body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    kwargs: dict[str, Any] = {"owner": owner}
    output = body.get("output")
    if output is not None:
        kwargs["output"] = str(output) if not isinstance(output, str) else output
    delivery_error = body.get("delivery_error")
    if delivery_error is not None:
        kwargs["delivery_error"] = str(delivery_error) if not isinstance(delivery_error, str) else delivery_error
    if now is not None:
        kwargs["now"] = now
    next_run_at_raw = body.get("next_run_at")
    next_run_at, next_run_at_err = _parse_next_run_at(next_run_at_raw)
    if next_run_at_err:
        return 400, {"error": "invalid request"}
    if next_run_at is not None:
        kwargs["next_run_at"] = next_run_at

    try:
        result = backend.complete_run(ctx, job_id, **kwargs)
    except CronLeaseError:
        return 409, {"error": "lease not held"}
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, dict(result) if result else {}


def _fail(job_id: str, body: dict, ctx: RuntimeContext | None, backend: Any) -> tuple[int, dict[str, Any]]:
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner:
        return 400, {"error": "invalid request"}

    error = body.get("error")
    if not isinstance(error, str) or not error:
        return 400, {"error": "invalid request"}

    now_raw = body.get("now")
    now, now_err = _parse_now(now_raw)
    if now_err:
        return 400, {"error": "invalid request"}

    kwargs: dict[str, Any] = {"owner": owner, "error": error}
    if now is not None:
        kwargs["now"] = now
    next_run_at_raw = body.get("next_run_at")
    next_run_at, next_run_at_err = _parse_next_run_at(next_run_at_raw)
    if next_run_at_err:
        return 400, {"error": "invalid request"}
    if next_run_at is not None:
        kwargs["next_run_at"] = next_run_at

    try:
        result = backend.fail_run(ctx, job_id, **kwargs)
    except CronLeaseError:
        return 409, {"error": "lease not held"}
    except Exception:
        return 503, {"error": "cron backend unavailable"}
    return 200, dict(result) if result else {}
