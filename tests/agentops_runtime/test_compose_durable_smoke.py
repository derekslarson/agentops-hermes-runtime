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
        _make_curated_memory_backend,
        _make_memory_backend,
        _make_queue_backend,
        _make_secret_backend,
        _make_session_backend,
        _make_worker_registry_backend,
    )

    queue_db = str(tmp_path / "queue.db")
    worker_db = str(tmp_path / "workers.db")
    conv_db = str(tmp_path / "conversations.db")
    secret_db = str(tmp_path / "secrets.db")
    curated_mem_db = str(tmp_path / "curated_memory.db")
    session_db = str(tmp_path / "sessions.db")
    deep_mem_db = str(tmp_path / "deep_memory.db")

    api_server, api_thread, api_base = _start_server(
        "api",
        queue_backend=_make_queue_backend({"AGENTOPS_QUEUE_DB_PATH": queue_db}),
        worker_registry_backend=_make_worker_registry_backend({"AGENTOPS_WORKER_REGISTRY_DB_PATH": worker_db}),
        conversation_router_backend=_make_conversation_router_backend({"AGENTOPS_CONVERSATION_ROUTER_DB_PATH": conv_db}),
        curated_memory_backend=_make_curated_memory_backend({"AGENTOPS_CURATED_MEMORY_DB_PATH": curated_mem_db}),
        session_backend=_make_session_backend({"AGENTOPS_SESSION_DB_PATH": session_db}),
        memory_backend=_make_memory_backend({"AGENTOPS_DEEP_MEMORY_DB_URL": "sqlite:///" + deep_mem_db}),
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


def test_run_durable_smoke_none_environ_threads_process_env_to_scale_step(monkeypatch):
    """CLI/default environ=None must still let worker_fleet_scale see process env vars."""
    from agentops_runtime import compose_durable_smoke

    monkeypatch.setenv("AGENTOPS_SMOKE_EXPECTED_WORKERS", "3")
    monkeypatch.setattr(
        compose_durable_smoke,
        "configure_compose_runtime_backends",
        lambda registry, *, environ: None,
    )
    for name in (
        "_step_worker_fleet",
        "_step_queue_tenant_isolation",
        "_step_conversation_routing",
        "_step_secret_roundtrip",
        "_step_native_state_continuity",
    ):
        monkeypatch.setattr(
            compose_durable_smoke,
            name,
            lambda registry, step=name.removeprefix("_step_"): {"step": step, "ok": True},
        )

    captured: list[dict[str, str] | None] = []

    def fake_scale_step(registry, *, environ=None):
        captured.append(environ)
        expected = (environ or {}).get("AGENTOPS_SMOKE_EXPECTED_WORKERS")
        return {"step": "worker_fleet_scale", "ok": expected == "3", "enforced": expected == "3"}

    monkeypatch.setattr(compose_durable_smoke, "_step_worker_fleet_scale", fake_scale_step)

    result = run_durable_smoke(environ=None)

    assert result["ok"] is True
    scale_step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert scale_step["enforced"] is True
    assert captured and captured[0] is not None
    assert captured[0].get("AGENTOPS_SMOKE_EXPECTED_WORKERS") == "3"


# ---------------------------------------------------------------------------
# native_state_continuity step
# ---------------------------------------------------------------------------


def test_native_state_continuity_step_in_steps_list(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    names = {s["step"] for s in result["steps"]}
    assert "native_state_continuity" in names


def test_native_state_continuity_step_passes(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "native_state_continuity")
    assert step["ok"] is True


def test_native_state_continuity_reports_all_surfaces(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    step = next(s for s in result["steps"] if s["step"] == "native_state_continuity")
    assert step.get("curated_memory_ok") is True
    assert step.get("session_ok") is True
    assert step.get("deep_memory_ok") is True
    assert step.get("tenant_b_isolated") is True


def test_native_state_continuity_exercises_session_search_and_deep_memory_get_many():
    from agentops_runtime.compose_durable_smoke import _step_native_state_continuity

    calls: list[str] = []

    class FakeMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def write(self, ctx, content, *, target, action):
            calls.append("memory.write")

        def read(self, ctx, *, target):
            calls.append("memory.read")
            return "smoke-curated-mem-sentinel" if self.visible else None

    class FakeSession:
        def __init__(self, visible: bool):
            self.visible = visible

        def create_session(self, ctx):
            calls.append("session.create_session")
            return "session-id"

        def append_message(self, ctx, message):
            calls.append("session.append_message")

        def read_messages(self, ctx, *, session_id):
            calls.append("session.read_messages")
            return [{"session_id": session_id, "content": "smoke-session-sentinel"}] if self.visible else []

        def search(self, ctx, query):
            calls.append("session.search")
            return [{"session_id": "session-id", "content": query}] if self.visible else []

    class FakeRecord:
        id = "record-id"
        text = "smoke-deep-mem-sentinel"

    class FakeDeepMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def upsert_record(self, ctx, *, text, source):
            calls.append("deep_memory.upsert_record")
            return FakeRecord()

        def get_record(self, ctx, record_id):
            calls.append("deep_memory.get_record")
            return FakeRecord() if self.visible else None

        def get_many(self, ctx, ids):
            calls.append("deep_memory.get_many")
            return [FakeRecord()] if self.visible else []

        def search(self, ctx, query, *, limit):
            calls.append("deep_memory.search")
            return [FakeRecord()] if self.visible else []

    class FakeRegistry:
        def get(self, cap, ctx):
            visible = "smoke-tenant-a" in ctx.org_id
            if cap.value == "memory":
                return FakeMemory(visible)
            if cap.value == "session":
                return FakeSession(visible)
            if cap.value == "deep_memory":
                return FakeDeepMemory(visible)
            raise AssertionError(cap)

    step = _step_native_state_continuity(FakeRegistry())

    assert step["ok"] is True
    assert "session.search" in calls
    assert calls.count("deep_memory.get_many") == 2
    assert calls.count("deep_memory.search") == 2


def test_native_state_continuity_fails_when_session_search_returns_wrong_session():
    from agentops_runtime.compose_durable_smoke import _step_native_state_continuity

    class FakeMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def write(self, ctx, content, *, target, action):
            pass

        def read(self, ctx, *, target):
            return "smoke-curated-mem-sentinel" if self.visible else None

    class FakeSession:
        def __init__(self, visible: bool):
            self.visible = visible

        def create_session(self, ctx):
            return "session-id"

        def append_message(self, ctx, message):
            pass

        def read_messages(self, ctx, *, session_id):
            return [{"session_id": session_id, "content": "smoke-session-sentinel"}] if self.visible else []

        def search(self, ctx, query):
            return [{"session_id": "wrong-session-id", "content": query}] if self.visible else []

    class FakeRecord:
        id = "record-id"
        text = "smoke-deep-mem-sentinel"

    class FakeDeepMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def upsert_record(self, ctx, *, text, source):
            return FakeRecord()

        def get_record(self, ctx, record_id):
            return FakeRecord() if self.visible else None

        def get_many(self, ctx, ids):
            return [FakeRecord()] if self.visible else []

        def search(self, ctx, query, *, limit):
            return [FakeRecord()] if self.visible else []

    class FakeRegistry:
        def get(self, cap, ctx):
            visible = "smoke-tenant-a" in ctx.org_id
            if cap.value == "memory":
                return FakeMemory(visible)
            if cap.value == "session":
                return FakeSession(visible)
            if cap.value == "deep_memory":
                return FakeDeepMemory(visible)
            raise AssertionError(cap)

    step = _step_native_state_continuity(FakeRegistry())

    assert step["ok"] is False
    assert step["session_ok"] is False


def test_native_state_continuity_fails_when_tenant_b_deep_memory_get_many_leaks():
    from agentops_runtime.compose_durable_smoke import _step_native_state_continuity

    class FakeMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def write(self, ctx, content, *, target, action):
            pass

        def read(self, ctx, *, target):
            return "smoke-curated-mem-sentinel" if self.visible else None

    class FakeSession:
        def __init__(self, visible: bool):
            self.visible = visible

        def create_session(self, ctx):
            return "session-id"

        def append_message(self, ctx, message):
            pass

        def read_messages(self, ctx, *, session_id):
            return [{"content": "smoke-session-sentinel"}] if self.visible else []

        def search(self, ctx, query):
            return [{"session_id": "session-id", "content": query}] if self.visible else []

    class FakeRecord:
        id = "record-id"
        text = "smoke-deep-mem-sentinel"

    class FakeDeepMemory:
        def __init__(self, visible: bool):
            self.visible = visible

        def upsert_record(self, ctx, *, text, source):
            return FakeRecord()

        def get_record(self, ctx, record_id):
            return FakeRecord() if self.visible else None

        def get_many(self, ctx, ids):
            return [FakeRecord()]

        def search(self, ctx, query, *, limit):
            return [FakeRecord()] if self.visible else []

    class FakeRegistry:
        def get(self, cap, ctx):
            visible = "smoke-tenant-a" in ctx.org_id
            if cap.value == "memory":
                return FakeMemory(visible)
            if cap.value == "session":
                return FakeSession(visible)
            if cap.value == "deep_memory":
                return FakeDeepMemory(visible)
            raise AssertionError(cap)

    step = _step_native_state_continuity(FakeRegistry())

    assert step["ok"] is False
    assert step["tenant_b_isolated"] is False


def test_native_state_continuity_no_sentinel_text_in_report(_two_servers):
    result = run_durable_smoke(environ=_two_servers)
    text = json.dumps(result)
    for sentinel in (
        "smoke-curated-mem-sentinel",
        "smoke-session-sentinel",
        "smoke-deep-mem-sentinel",
    ):
        assert sentinel not in text, f"sentinel {sentinel!r} leaked into report"


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


# ---------------------------------------------------------------------------
# RED tests: worker_fleet_scale gated durable smoke step (M12B scaled smoke)
# ---------------------------------------------------------------------------


def _register_fleet_worker_http(api_base: str, worker_id: str, *, smoke_fleet_run_id: str | None = None) -> None:
    """Register a fleet worker via HTTP POST to /workers/register."""
    fleet_scope = {
        "mode": "agentops",
        "org_id": "__fleet__",
        "workspace_id": "__fleet__",
        "workspace_type": "infrastructure",
        "user_id": "__fleet__",
        "project_id": "__fleet__",
        "agent_profile_id": "__fleet__",
        "run_type": "manual",
        "backend_profile": "compose-self-hosted",
        "conversation_id": None,
        "external_channel_id": None,
        "external_thread_id": None,
        "parent_session_id": None,
        "delivery_ref": None,
    }
    worker = {"id": worker_id, "capabilities": ["run"]}
    if smoke_fleet_run_id is not None:
        worker["smoke_fleet_run_id"] = smoke_fleet_run_id
    body = json.dumps({
        "context": fleet_scope,
        "worker": worker,
        "ttl_seconds": 300,
    }).encode()
    status, resp = _http_post(f"{api_base}/workers/register", body)
    assert status == 200, f"fleet worker register failed ({status}): {resp}"


def test_worker_fleet_scale_step_in_steps_list(_two_servers):
    """worker_fleet_scale step is always included in the smoke steps list."""
    result = run_durable_smoke(environ=_two_servers)
    names = {s["step"] for s in result["steps"]}
    assert "worker_fleet_scale" in names


def test_worker_fleet_scale_step_gated_off_when_env_unset(_two_servers):
    """Returns ok=True, enforced=False when AGENTOPS_SMOKE_EXPECTED_WORKERS is not set."""
    env = dict(_two_servers)
    env.pop("AGENTOPS_SMOKE_EXPECTED_WORKERS", None)
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is True
    assert step["enforced"] is False


def test_worker_fleet_scale_step_gated_off_when_env_zero(_two_servers):
    """Returns ok=True, enforced=False when AGENTOPS_SMOKE_EXPECTED_WORKERS=0."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "0"
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is True
    assert step["enforced"] is False


def test_worker_fleet_scale_fails_closed_for_invalid_expected_workers(_two_servers):
    """A typo in AGENTOPS_SMOKE_EXPECTED_WORKERS must not silently disable scale proof."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "not-an-int"

    result = run_durable_smoke(environ=env)

    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is False
    assert step["enforced"] is True
    assert step.get("error") == "invalid expected worker count"


def test_worker_fleet_scale_passes_with_enough_fleet_workers(_two_servers):
    """Passes when >= N distinct fleet workers are pre-registered under the fleet context."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "2"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "fleet-worker-1")
    _register_fleet_worker_http(api_base, "fleet-worker-2")
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is True
    assert step["enforced"] is True
    assert step.get("enough") is True


def test_worker_fleet_scale_filters_to_current_smoke_fleet_run_id(_two_servers):
    """When a smoke run id is set, previous-run fleet workers cannot satisfy scale proof."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "1"
    env["AGENTOPS_SMOKE_FLEET_RUN_ID"] = "current-smoke-run"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "previous-run-worker", smoke_fleet_run_id="previous-smoke-run")

    result = run_durable_smoke(environ=env)

    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is False
    assert step["enforced"] is True
    assert step.get("distinct_count") == 0
    assert step.get("enough") is False


def test_worker_fleet_scale_accepts_current_smoke_fleet_run_id(_two_servers):
    """Workers tagged with the current smoke run id count toward the live scale proof."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "1"
    env["AGENTOPS_SMOKE_FLEET_RUN_ID"] = "current-smoke-run"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "current-run-worker", smoke_fleet_run_id="current-smoke-run")

    result = run_durable_smoke(environ=env)

    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is True
    assert step["enforced"] is True
    assert step.get("distinct_count") == 1
    assert step.get("enough") is True


def test_worker_fleet_scale_fails_with_insufficient_fleet_workers(_two_servers):
    """Fails when fleet worker count < expected (n-1 registered, expected n)."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "3"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "fleet-worker-a")
    _register_fleet_worker_http(api_base, "fleet-worker-b")
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step["ok"] is False
    assert step["enforced"] is True
    assert step.get("enough") is False


def test_worker_fleet_scale_duplicate_ids_count_once(_two_servers):
    """Duplicate worker_ids registered under fleet scope count as one distinct worker."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "2"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "fleet-dup-worker")
    _register_fleet_worker_http(api_base, "fleet-dup-worker")
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step.get("distinct_count", 0) == 1
    assert step["ok"] is False


def test_worker_fleet_scale_tenant_scope_cannot_list_fleet_workers(_two_servers):
    """Tenant-scoped list cannot see fleet workers."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "1"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "fleet-worker-x")
    result = run_durable_smoke(environ=env)
    step = next(s for s in result["steps"] if s["step"] == "worker_fleet_scale")
    assert step.get("tenant_isolated") is True


def test_worker_fleet_scale_report_contains_no_worker_ids(_two_servers):
    """Report from worker_fleet_scale contains no raw worker IDs or hostnames."""
    env = dict(_two_servers)
    env["AGENTOPS_SMOKE_EXPECTED_WORKERS"] = "1"
    api_base = env["AGENTOPS_API_URL"]
    _register_fleet_worker_http(api_base, "fleet-worker-secret-id")
    result = run_durable_smoke(environ=env)
    text = json.dumps(result)
    assert "fleet-worker-secret-id" not in text
    assert "__fleet__" not in text


def test_fleet_ctx_has_reserved_org_and_workspace():
    """_fleet_ctx returns a RuntimeContext with reserved __fleet__ identifiers."""
    from agentops_runtime.compose_durable_smoke import _fleet_ctx
    ctx = _fleet_ctx()
    assert ctx.org_id == "__fleet__"
    assert ctx.workspace_id == "__fleet__"
    assert ctx.backend_profile == "compose-self-hosted"
    assert ctx.mode == "agentops"


def test_fleet_ctx_differs_from_tenant_ctx():
    """Fleet context produces a different scope than any tenant smoke context."""
    from agentops_runtime.compose_durable_smoke import _fleet_ctx, _ctx
    from agent.runtime_worker_registry_http import HttpWorkerRegistry
    fleet_scope = HttpWorkerRegistry._scope_payload(_fleet_ctx())
    tenant_scope = HttpWorkerRegistry._scope_payload(_ctx("smoke-tenant-a"))
    assert fleet_scope != tenant_scope
