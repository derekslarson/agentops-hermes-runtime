"""Tests for the running Compose service health/readiness smoke surface (M12).

These tests exercise the smoke helper without Docker or network by injecting a
fake HTTP fetcher.  They prove the helper targets the Compose service URLs,
checks ``/healthz`` and ``/readyz`` where appropriate, fails closed on
missing/unhealthy services, and is invokable as a module entrypoint.
"""

from __future__ import annotations

import io
import json
import urllib.error
from contextlib import redirect_stdout
from email.message import Message

from agentops_runtime import compose_health_smoke


def _healthy_fetch(_url: str) -> tuple[int, dict]:
    return 200, {"ok": True}


def _recording_fetch(calls: list[str]):
    def _fetch(url: str) -> tuple[int, dict]:
        calls.append(url)
        return 200, {"ok": True}

    return _fetch


def test_all_services_healthy_returns_ok():
    report = compose_health_smoke.check_compose_services(fetch=_healthy_fetch, environ={})

    assert report["ok"] is True
    services = {s["service"]: s for s in report["services"]}
    assert set(services) == {"api", "worker", "scheduler", "local-secrets"}
    assert all(s["ok"] for s in report["services"])


def test_targets_compose_service_urls_and_endpoints():
    calls: list[str] = []
    compose_health_smoke.check_compose_services(fetch=_recording_fetch(calls), environ={})

    # api/worker/scheduler are checked on both health and readiness.
    assert "http://api:8710/healthz" in calls
    assert "http://api:8710/readyz" in calls
    assert "http://worker:8711/healthz" in calls
    assert "http://worker:8711/readyz" in calls
    assert "http://scheduler:8712/healthz" in calls
    assert "http://scheduler:8712/readyz" in calls
    # local-secrets is a health-only service; readiness is not probed.
    assert "http://local-secrets:8713/healthz" in calls
    assert "http://local-secrets:8713/readyz" not in calls


def test_service_urls_overridable_via_env():
    calls: list[str] = []
    environ = {
        "AGENTOPS_API_URL": "http://api.internal:9000",
        "AGENTOPS_SECRET_STORE_URL": "http://secrets.internal:9100",
    }
    compose_health_smoke.check_compose_services(fetch=_recording_fetch(calls), environ=environ)

    assert "http://api.internal:9000/healthz" in calls
    assert "http://api.internal:9000/readyz" in calls
    assert "http://secrets.internal:9100/healthz" in calls


def test_fails_closed_on_unreachable_service():
    def _fetch(url: str) -> tuple[int, dict]:
        if url.startswith("http://worker:8711"):
            raise OSError("connection refused")
        return 200, {"ok": True}

    report = compose_health_smoke.check_compose_services(fetch=_fetch, environ={})

    assert report["ok"] is False
    worker = next(s for s in report["services"] if s["service"] == "worker")
    assert worker["ok"] is False
    failed = [c for c in worker["checks"] if not c["ok"]]
    assert failed and "connection refused" in failed[0]["error"]


def test_fails_closed_when_service_reports_unhealthy():
    def _fetch(url: str) -> tuple[int, dict]:
        if url == "http://scheduler:8712/readyz":
            return 503, {"ok": False, "backend_error": "control-plane unreachable"}
        return 200, {"ok": True}

    report = compose_health_smoke.check_compose_services(fetch=_fetch, environ={})

    assert report["ok"] is False
    scheduler = next(s for s in report["services"] if s["service"] == "scheduler")
    assert scheduler["ok"] is False


def test_fails_closed_when_status_ok_but_payload_not_ok():
    def _fetch(url: str) -> tuple[int, dict]:
        if url == "http://api:8710/readyz":
            return 200, {"ok": False}
        return 200, {"ok": True}

    report = compose_health_smoke.check_compose_services(fetch=_fetch, environ={})

    assert report["ok"] is False


def test_default_fetch_preserves_http_error_status_and_payload(monkeypatch):
    body = io.BytesIO(b'{"ok": false, "backend_error": "control-plane unreachable"}')

    def _urlopen(_request, *, timeout: float):
        raise urllib.error.HTTPError(
            "http://api:8710/readyz",
            503,
            "Service Unavailable",
            hdrs=Message(),
            fp=body,
        )

    monkeypatch.setattr(compose_health_smoke.urllib.request, "urlopen", _urlopen)

    status, payload = compose_health_smoke._default_fetch("http://api:8710/readyz")

    assert status == 503
    assert payload == {"ok": False, "backend_error": "control-plane unreachable"}


def test_main_prints_json_and_exits_nonzero_when_unhealthy(monkeypatch):
    def _fetch(url: str) -> tuple[int, dict]:
        raise OSError("connection refused")

    monkeypatch.setattr(compose_health_smoke, "_default_fetch", _fetch)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = compose_health_smoke.main([])

    assert code == 1
    report = json.loads(buffer.getvalue())
    assert report["ok"] is False


def test_main_exits_zero_when_all_healthy(monkeypatch):
    monkeypatch.setattr(compose_health_smoke, "_default_fetch", _healthy_fetch)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = compose_health_smoke.main([])

    assert code == 0
    assert json.loads(buffer.getvalue())["ok"] is True
