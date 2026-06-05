"""Tests for M12B compose durable smoke (worker_fleet, queue isolation, conversation routing, secret roundtrip)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from agentops_runtime import compose_services
from agentops_runtime.compose_durable_smoke import main, run_durable_smoke


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http_post(url: str, body: bytes) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode())
            return status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _start_server(service_name: str, **backends: Any) -> tuple[compose_services._Server, threading.Thread, str]:
    server = compose_services._Server(("127.0.0.1", 0), compose_services._Handler)
    server.service_name = service_name
    for attr, value in backends.items():
        setattr(server, attr, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    return server, thread, base


def _stop_server(server: compose_services._Server, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


# ---------------------------------------------------------------------------
# fixtures: two real SQLite-backed servers (api + local-secrets)
# ---------------------------------------------------------------------------


@pytest.fixture
def _two_servers(tmp_path):
    from agentops_runtime.compose_services import (
        _make_conversation_router_backend,
        _make_queue_backend,
        _make_secret_backend,
        _make_worker_registry_backend,
    )

    queue_db = str(tmp_path / "queue.db")
    worker_db = str(tmp_path / "workers.db")
    conv_db = str(tmp_path / "conversations.db")
    secret_db = str(tmp_path / "secrets.db")

    api_server, api_thread, api_base = _start_server(
        "api",
        queue_backend=_make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": queue_db}),
        worker_registry_backend=_make_worker_registry_backend({"AGENTOPS_WORKER_REGISTRY_DB_PATH": worker_db}),
        conversation_router_backend=_make_conversation_router_backend({"AGENTOPS_CONVERSATION_ROUTER_DB_PATH": conv_db}),
    )
    secret_server, secret_thread, secret_base = _start_server(
        "local-secrets",
        secret_backend=_make_secret_backend({"AGENTOPS_SECRET_STORE_PATH": secret_db}),
    )

    environ = {
        "AGENTOPS_API_URL": api_base,
        "AGENTOPS_SECRET_STORE_URL": secret_base,
    }
    try:
        yield environ
    finally:
        _stop_server(api_server, api_thread)
        _stop_server(secret_server, secret_thread)


# ---------------------------------------------------------------------------
# run_durable_smoke: result shape
# ---------------------------------------------------------------------------


def test_run_durable_smoke_returns_ok_true_with_live_servers(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    assert result["ok"] is True


def test_run_durable_smoke_returns_steps_list(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    assert isinstance(result.get("steps"), list)
    assert len(result["steps"]) > 0


def test_run_durable_smoke_all_steps_pass(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    for step in result["steps"]:
        assert step["ok"] is True, f"step {step['step']!r} failed: {step}"


def test_run_durable_smoke_step_names_present(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    names = {s["step"] for s in result["steps"]}
    assert {"worker_fleet", "queue_tenant_isolation", "conversation_routing", "secret_roundtrip"}.issubset(names)


# ---------------------------------------------------------------------------
# run_durable_smoke: result is JSON-safe and sanitized
# ---------------------------------------------------------------------------


def test_run_durable_smoke_result_is_json_serializable(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    serialized = json.dumps(result)
    assert isinstance(serialized, str)


def test_run_durable_smoke_result_contains_no_raw_urls(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    text = json.dumps(result)
    api_url = _two_servers["AGENTOPS_API_URL"]
    secret_url = _two_servers["AGENTOPS_SECRET_STORE_URL"]
    assert api_url not in text
    assert secret_url not in text


def test_run_durable_smoke_result_contains_no_sentinel_secret_value(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    text = json.dumps(result)
    assert "durable-smoke-sentinel" not in text


def test_run_durable_smoke_result_contains_no_raw_ids(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    text = json.dumps(result)
    for raw_value in (
        "org-smoke-tenant-a",
        "org-smoke-tenant-b",
        "ws-smoke-tenant-a",
        "user-smoke-tenant-a",
        "proj-smoke-tenant-a",
        "run-smoke-tenant-a",
        "thread://smoke/smoke-tenant-a",
        "smoke-sentinel-ref",
    ):
        assert raw_value not in text


# ---------------------------------------------------------------------------
# worker_fleet step
# ---------------------------------------------------------------------------


def test_worker_fleet_step_reports_registered_count(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet")
    assert step["ok"] is True
    assert step.get("registered") == 2
    assert step.get("listed") == 2


# ---------------------------------------------------------------------------
# queue_tenant_isolation step
# ---------------------------------------------------------------------------


def test_queue_tenant_isolation_step_passes(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "queue_tenant_isolation")
    assert step["ok"] is True


def test_queue_tenant_isolation_reports_isolation(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "queue_tenant_isolation")
    assert step.get("tenant_a_claimed") is True
    assert step.get("tenant_b_isolated") is True


# ---------------------------------------------------------------------------
# conversation_routing step
# ---------------------------------------------------------------------------


def test_conversation_routing_step_passes(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "conversation_routing")
    assert step["ok"] is True


def test_conversation_routing_step_reports_stable_resolution(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "conversation_routing")
    assert step.get("resolve_stable") is True
    assert step.get("routed") is True
    assert step.get("active_run_found") is True


# ---------------------------------------------------------------------------
# secret_roundtrip step
# ---------------------------------------------------------------------------


def test_secret_roundtrip_step_passes(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "secret_roundtrip")
    assert step["ok"] is True


def test_secret_roundtrip_reports_put_get_and_isolation(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "secret_roundtrip")
    assert step.get("put_ok") is True
    assert step.get("get_ok") is True
    assert step.get("cross_tenant_isolated") is True


# ---------------------------------------------------------------------------
# fail-closed: missing env URL
# ---------------------------------------------------------------------------


def test_run_durable_smoke_fails_closed_on_missing_env():
    result = run_durable_smoke(environ={})
    assert result["ok"] is False
    assert "error" in result


def test_run_durable_smoke_error_does_not_expose_env_values():
    result = run_durable_smoke(environ={"AGENTOPS_API_URL": "http://127.0.0.1:19999"})
    text = json.dumps(result)
    assert "127.0.0.1:19999" not in text


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_main_exits_0_with_live_servers(_two_servers):
    captured: list[str] = []
    import sys
    from unittest.mock import patch

    with patch("sys.stdout") as mock_stdout:
        mock_stdout.write.side_effect = lambda s: captured.append(s)
        code = main(argv=[], environ=_two_servers)
    assert code == 0


def test_main_exits_1_on_missing_env():
    code = main(argv=[], environ={})
    assert code == 1


def test_main_prints_json(_two_servers, capsys):
    code = main(argv=[], environ=_two_servers)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "ok" in data
    assert code == 0


# ---------------------------------------------------------------------------
# RED tests — prove specific spec-review blockers before fixes
# ---------------------------------------------------------------------------


def test_run_durable_smoke_none_environ_passes_none_to_configure(monkeypatch):
    """environ=None must propagate None (not {}) to configure_compose_runtime_backends."""
    captured: list = []

    def fake_configure(registry, *, environ):
        captured.append(environ)
        raise RuntimeError("stop")

    monkeypatch.setattr(
        "agentops_runtime.compose_durable_smoke.configure_compose_runtime_backends",
        fake_configure,
    )
    run_durable_smoke(environ=None)
    assert len(captured) == 1
    assert captured[0] is None, f"expected None but configure received {captured[0]!r}"


def test_queue_tenant_isolation_b_claim_before_a_claim():
    """tenant B claim must happen before tenant A claim+ack (item still pending)."""
    call_log: list[str] = []

    class FakeClaimed:
        receipt = "r1"

    class FakeQueueA:
        def enqueue(self, ctx, payload):
            call_log.append("enqueue_a")

        def claim(self, ctx):
            call_log.append("claim_a")
            return FakeClaimed()

        def ack(self, ctx, receipt):
            call_log.append("ack_a")

    class FakeQueueB:
        def claim(self, ctx):
            call_log.append("claim_b")
            return None

    from agentops_runtime.compose_durable_smoke import _step_queue_tenant_isolation

    qa, qb = FakeQueueA(), FakeQueueB()

    class FakeRegistry:
        def get(self, cap, ctx):
            return qa if "smoke-tenant-a" in ctx.org_id else qb

    _step_queue_tenant_isolation(FakeRegistry())

    assert "claim_b" in call_log and "claim_a" in call_log, f"unexpected log: {call_log}"
    assert call_log.index("claim_b") < call_log.index("claim_a"), (
        f"tenant B claim must happen before tenant A claim; got order: {call_log}"
    )
