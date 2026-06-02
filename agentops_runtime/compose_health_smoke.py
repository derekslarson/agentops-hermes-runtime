"""Running Compose service health/readiness smoke surface (M12).

This helper checks the *running* compose-self-hosted services over HTTP and
returns a structured report.  It is meant to be invoked against a live stack,
e.g.::

    docker compose -f deploy/compose/docker-compose.yml up --build -d
    docker compose -f deploy/compose/docker-compose.yml exec api \
        python -m agentops_runtime.compose_health_smoke

The ``api``, ``worker``, and ``scheduler`` services expose both ``/healthz`` and
``/readyz`` (readiness fails closed when the runtime backends are not wired);
``local-secrets`` is a health-only service.  The smoke surface fails closed:
any unreachable service, non-200 status, or ``ok: false`` payload makes the
overall report unhealthy.  Service URLs default to in-network Compose DNS names
and can be overridden by environment variables so the same helper works from a
sidecar container or the host.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

Fetcher = Callable[[str], "tuple[int, dict[str, Any]]"]

_HTTP_TIMEOUT_SECONDS = 5.0

# Each service: default in-network URL env var, fallback URL, probed endpoints.
_SERVICES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("api", "AGENTOPS_API_URL", "http://api:8710", ("/healthz", "/readyz")),
    ("worker", "AGENTOPS_WORKER_URL", "http://worker:8711", ("/healthz", "/readyz")),
    ("scheduler", "AGENTOPS_SCHEDULER_URL", "http://scheduler:8712", ("/healthz", "/readyz")),
    ("local-secrets", "AGENTOPS_SECRET_STORE_URL", "http://local-secrets:8713", ("/healthz",)),
)


def _default_fetch(url: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed http scheme
            status = int(getattr(response, "status", 0) or response.getcode())
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _check_endpoint(url: str, fetch: Fetcher) -> dict[str, Any]:
    try:
        status, payload = fetch(url)
    except Exception as exc:  # noqa: BLE001 - any transport error fails closed
        return {"url": url, "ok": False, "error": str(exc)}
    service_ok = payload.get("ok") is True
    ok = status == 200 and service_ok
    result: dict[str, Any] = {"url": url, "ok": ok, "status": status, "service_ok": service_ok}
    backend_error = payload.get("backend_error")
    if backend_error:
        result["backend_error"] = backend_error
    return result


def check_compose_services(
    *,
    fetch: Fetcher | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Probe every Compose service and return a structured, fail-closed report."""

    fetch = fetch or _default_fetch
    env = os.environ if environ is None else environ

    services: list[dict[str, Any]] = []
    for service, env_var, fallback, endpoints in _SERVICES:
        base = env.get(env_var, fallback).rstrip("/")
        checks = [_check_endpoint(f"{base}{endpoint}", fetch) for endpoint in endpoints]
        services.append(
            {
                "service": service,
                "url": base,
                "ok": all(check["ok"] for check in checks),
                "checks": checks,
            }
        )

    return {"ok": all(service["ok"] for service in services), "services": services}


def main(argv: list[str] | None = None) -> int:
    _ = argv if argv is not None else sys.argv[1:]
    report = check_compose_services(fetch=_default_fetch)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
