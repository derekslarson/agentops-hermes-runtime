from __future__ import annotations

import json
import os

from agent.runtime_artifacts_audit import (
    HttpAuditBackend,
    LocalFileArtifactBackend,
    LocalFileAuditBackend,
    SanitizedAuditEvent,
    register_http_artifact_backend,
    register_http_audit_backend,
)
from agent.runtime_backends import BackendCapability, BackendSelectionError, RuntimeBackendRegistry, _redact_audit_value
from agent.runtime_context import RuntimeContext


def _context(user: str, *, run: str = "run-1") -> RuntimeContext:
    return RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="workspace",
        user_id=user,
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="proj",
        run_id=run,
        backend_profile="compose-self-hosted",
    )


def test_local_file_artifacts_are_scoped_and_store_by_stable_ref(tmp_path):
    backend = LocalFileArtifactBackend(root=tmp_path)
    derek = _context("derek")
    alex = _context("alex")

    derek_ref = backend.put(derek, "tool-output/result.txt", b"derek artifact")
    alex_ref = backend.put(alex, "tool-output/result.txt", b"alex artifact")

    assert derek_ref == "tool-output/result.txt"
    assert alex_ref == "tool-output/result.txt"
    assert backend.get(derek, derek_ref) == b"derek artifact"
    assert backend.get(alex, alex_ref) == b"alex artifact"
    assert backend.list_artifacts(derek) == ["tool-output/result.txt"]

    # Reopening the local backend keeps artifacts durable for worker restart.
    reopened = LocalFileArtifactBackend(root=tmp_path)
    assert reopened.get(derek, derek_ref) == b"derek artifact"
    assert reopened.get(alex, alex_ref) == b"alex artifact"


def test_local_file_artifacts_reject_path_escape_refs(tmp_path):
    backend = LocalFileArtifactBackend(root=tmp_path)

    for ref in ("../escape", "/absolute", "nested/../../escape"):
        try:
            backend.put(_context("derek"), ref, b"nope")
        except ValueError as exc:
            assert "artifact ref" in str(exc)
        else:  # pragma: no cover - assertion path
            raise AssertionError(f"accepted unsafe ref {ref!r}")


def test_local_file_artifacts_reject_symlink_escape(tmp_path):
    backend = LocalFileArtifactBackend(root=tmp_path)
    context = _context("derek")
    backend.put(context, "safe.txt", b"safe")
    scope_dir = next((tmp_path / "artifacts").rglob("safe.txt")).parent
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    os.symlink(outside, scope_dir / "link.txt")

    for operation in (lambda: backend.get(context, "link.txt"), lambda: backend.put(context, "link.txt", b"escape")):
        try:
            operation()
        except ValueError as exc:
            assert "artifact ref" in str(exc)
        else:  # pragma: no cover - assertion path
            raise AssertionError("accepted symlink artifact escape")

    assert outside.read_bytes() == b"outside"


def test_local_file_audit_sanitizes_paths_and_secret_values(tmp_path):
    backend = LocalFileAuditBackend(root=tmp_path)
    context = _context("derek")

    backend.record(
        context,
        SanitizedAuditEvent(
            event_type="tool.call",
            action="terminal",
            payload={
                "path": "/Users/derek/.hermes/.env",
                "api_key": "sk-secret",
                "nested": {"token": "secret-token", "safe": "kept"},
            },
        ),
    )

    events = backend.list_events(context)
    assert len(events) == 1
    stored = events[0]
    assert stored["event_type"] == "tool.call"
    assert stored["action"] == "terminal"
    assert stored["payload"] == {
        "path": "[REDACTED_PATH]",
        "api_key": "[REDACTED]",
        "nested": {"token": "[REDACTED]", "safe": "kept"},
    }
    raw_log = next(tmp_path.rglob("*.jsonl")).read_text()
    assert "/Users/derek" not in raw_log
    assert "sk-secret" not in raw_log
    assert "secret-token" not in raw_log


def test_local_file_audit_sanitizes_common_auth_headers(tmp_path):
    backend = LocalFileAuditBackend(root=tmp_path)
    context = _context("derek")

    backend.record(
        context,
        {
            "event_type": "tool.call",
            "headers": {
                "authorization": "Bearer raw-token",
                "x-api-key": "raw-key",
                "api-key": "raw-api-key",
                "safe": "kept",
            },
        },
    )

    stored = backend.list_events(context)[0]
    assert stored["event"]["headers"] == {
        "authorization": "[REDACTED]",
        "x-api-key": "[REDACTED]",
        "api-key": "[REDACTED]",
        "safe": "kept",
    }
    raw_log = next(tmp_path.rglob("*.jsonl")).read_text()
    assert "raw-token" not in raw_log
    assert "raw-key" not in raw_log


def test_local_file_audit_keeps_trusted_scope_when_event_supplies_scope(tmp_path):
    backend = LocalFileAuditBackend(root=tmp_path)
    context = _context("derek")

    backend.record(context, {"event_type": "tool.call", "scope": {"org_id": "forged"}})

    stored = backend.list_events(context)[0]
    assert stored["scope"]["org_id"] == "acme"
    assert stored["event"]["scope"]["org_id"] == "forged"


def test_local_file_audit_sanitizes_camel_case_path_fields(tmp_path):
    backend = LocalFileAuditBackend(root=tmp_path)
    context = _context("derek")

    backend.record(
        context,
        {
            "event_type": "tool.call",
            "filePath": "/Users/derek/.hermes/.env",
            "localPath": "/private/tmp/secret.txt",
            "absolutePath": "/etc/passwd",
            "workingDirectory": "/Users/derek/project",
        },
    )

    stored = backend.list_events(context)[0]["event"]
    assert stored["filePath"] == "[REDACTED_PATH]"
    assert stored["localPath"] == "[REDACTED_PATH]"
    assert stored["absolutePath"] == "[REDACTED_PATH]"
    assert stored["workingDirectory"] == "[REDACTED_PATH]"
    raw_log = next(tmp_path.rglob("*.jsonl")).read_text()
    assert "/Users/derek" not in raw_log
    assert "/private/tmp" not in raw_log


def test_local_file_scope_segments_do_not_collapse_distinct_tenant_values(tmp_path):
    backend = LocalFileArtifactBackend(root=tmp_path)
    slash = RuntimeContext(mode="agentops", org_id="acme/prod", user_id="derek")
    colon = RuntimeContext(mode="agentops", org_id="acme:prod", user_id="derek")
    underscore = RuntimeContext(mode="agentops", org_id="acme_prod", user_id="derek")
    missing_org = RuntimeContext(mode="agentops", user_id="derek")
    literal_fallback = RuntimeContext(mode="agentops", org_id="local-org", user_id="derek")
    local_mode = RuntimeContext(mode="local", org_id="same", user_id="derek")
    agentops_mode = RuntimeContext(mode="agentops", org_id="same", user_id="derek")

    backend.put(slash, "result.txt", b"slash")
    backend.put(colon, "result.txt", b"colon")
    backend.put(underscore, "result.txt", b"underscore")
    backend.put(missing_org, "result.txt", b"missing")
    backend.put(literal_fallback, "result.txt", b"literal")
    backend.put(local_mode, "result.txt", b"local")
    backend.put(agentops_mode, "result.txt", b"agentops")

    assert backend.get(slash, "result.txt") == b"slash"
    assert backend.get(colon, "result.txt") == b"colon"
    assert backend.get(underscore, "result.txt") == b"underscore"
    assert backend.get(missing_org, "result.txt") == b"missing"
    assert backend.get(literal_fallback, "result.txt") == b"literal"
    assert backend.get(local_mode, "result.txt") == b"local"
    assert backend.get(agentops_mode, "result.txt") == b"agentops"


def test_local_file_scope_omits_secret_bearing_runtime_refs(tmp_path):
    backend = LocalFileAuditBackend(root=tmp_path)
    context = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="derek",
        permissions_ref="secret-permissions-ref",
        delivery_ref="private-delivery-ref",
    )

    backend.record(context, {"event_type": "tool.call"})

    event = backend.list_events(context)[0]
    raw_log = next(tmp_path.rglob("*.jsonl")).read_text()
    assert "permissions_ref" not in event["scope"]
    assert "delivery_ref" not in event["scope"]
    assert "secret-permissions-ref" not in raw_log
    assert "private-delivery-ref" not in raw_log
    assert not any("secret-permissions-ref" in str(path) for path in tmp_path.rglob("*"))


def test_registry_can_select_http_artifact_and_audit_backends(monkeypatch):
    seen: list[tuple[str, str, dict]] = []

    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        decoded = json.loads(body.decode()) if body else {}
        seen.append((method, url, decoded))
        if url.endswith("/artifacts") and method == "POST":
            return 200, {"content-type": "application/json"}, json.dumps({"ref": decoded["ref"]}).encode()
        if url.endswith("/audit") and method == "POST":
            return 202, {"content-type": "application/json"}, b"{}"
        if "/artifacts/" in url and method == "GET":
            return 200, {"content-type": "application/octet-stream"}, b"remote bytes"
        if "/artifacts?" in url and method == "GET":
            return 200, {"content-type": "application/json"}, json.dumps({"artifacts": ["a.txt"]}).encode()
        raise AssertionError((method, url))

    registry = RuntimeBackendRegistry(
        config={
            "backends": {
                "capabilities": {"artifact": "compose-self-hosted", "audit": "compose-self-hosted"},
                "options": {
                    "artifact": {"base_url": "https://control.example", "token": "bearer-secret"},
                    "audit": {"base_url": "https://control.example", "token": "bearer-secret"},
                },
            }
        }
    )
    register_http_artifact_backend(registry, request=fake_request)
    register_http_audit_backend(registry, request=fake_request)
    context = _context("derek")

    artifacts = registry.get(BackendCapability.ARTIFACT, context)
    audit = registry.get(BackendCapability.AUDIT, context)

    assert artifacts.put(context, "a.txt", b"remote bytes") == "a.txt"
    assert artifacts.get(context, "a.txt") == b"remote bytes"
    assert artifacts.list_artifacts(context) == ["a.txt"]
    audit.record(context, {"event_type": "credential.resolved", "secret": "raw", "path": "/private/tmp/key"})

    artifact_payload = seen[0][2]
    audit_payload = seen[-1][2]
    assert artifact_payload["scope"]["mode"] == "agentops"
    assert artifact_payload["scope"]["org_id"] == "acme"
    assert artifact_payload["scope"]["workspace_id"] == "workspace"
    assert artifact_payload["scope"]["project_id"] == "proj"
    assert artifact_payload["scope"]["conversation_id"] == "thread-1"
    assert artifact_payload["scope"]["user_id"] == "derek"
    assert artifact_payload["scope"]["agent_profile_id"] == "default"
    assert artifact_payload["scope"]["run_id"] == "run-1"
    assert artifact_payload["scope"]["job_id"] is None
    assert artifact_payload["data_b64"]
    assert audit_payload["event"]["secret"] == "[REDACTED]"
    assert audit_payload["event"]["path"] == "[REDACTED_PATH]"
    assert "bearer-secret" not in json.dumps([payload for _, _, payload in seen])


def test_http_backend_rejects_secret_bearing_or_invalid_base_urls():
    registry = RuntimeBackendRegistry(
        config={
            "backends": {
                "capabilities": {"artifact": "compose-self-hosted", "audit": "compose-self-hosted"},
                "options": {"artifact": {"base_url": "https://user:secret@example.com/runtime"}},
            }
        }
    )
    register_http_artifact_backend(registry)

    try:
        registry.get(BackendCapability.ARTIFACT, _context("derek"))
    except BackendSelectionError as exc:
        assert "base_url" in str(exc)
    else:  # pragma: no cover - assertion path
        raise AssertionError("accepted credential-bearing base_url")


def test_http_artifact_get_returns_none_for_not_found():
    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        assert method == "GET"
        return 404, {"content-type": "application/json"}, b"{}"

    registry = RuntimeBackendRegistry(
        config={
            "backends": {
                "capabilities": {"artifact": "compose-self-hosted"},
                "options": {"artifact": {"base_url": "https://control.example"}},
            }
        }
    )
    register_http_artifact_backend(registry, request=fake_request)

    artifacts = registry.get(BackendCapability.ARTIFACT, _context("derek"))

    assert artifacts.get(_context("derek"), "missing.txt") is None


def test_registry_local_backends_emit_audit_events_for_required_runtime_surfaces():
    registry = RuntimeBackendRegistry()
    context = RuntimeContext(
        mode="agentops",
        org_id="acme",
        workspace_id="workspace",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="proj",
        run_id="run-audit",
        backend_profile="local",
    )

    registry.get(BackendCapability.MEMORY, context).write(context, "memory", target="memory", action="add")
    registry.get(BackendCapability.SKILL, context).list_skills(context)
    registry.get(BackendCapability.SESSION, context).append(context, {"role": "user", "content": "hi"})
    cron = registry.get(BackendCapability.CRON, context)
    job_id = cron.create(context, {"prompt": "summarize", "one_shot": True})
    cron.claim_due(context, owner="worker-1", now=1.0)
    cron.complete_run(context, job_id, owner="worker-1", output="done", now=2.0)
    queue = registry.get(BackendCapability.QUEUE, context)
    receipt = queue.enqueue(context, {"type": "turn"}, idempotency_key="turn-1")
    queue.claim(context)
    queue.ack(context, receipt)
    leases = registry.get(BackendCapability.RUN_LEASE, context)
    leases.claim(context, "run-audit", owner="worker-1")
    leases.renew(context, "run-audit", owner="worker-1")
    leases.release(context, "run-audit", owner="worker-1")
    workers = registry.get(BackendCapability.WORKER_REGISTRY, context)
    worker_id = workers.register(context, {"id": "worker-1", "max_concurrent_runs": 2})
    workers.heartbeat(context, worker_id, slots={"0": "idle"})
    registry.get(BackendCapability.ARTIFACT, context).put(
        context,
        "tool-output/result.txt",
        b"bytes from /Users/derek/private and token=secret",
    )
    registry.get(BackendCapability.DELIVERY, context).deliver(
        context,
        {"route": "slack", "content": "done", "webhook_token": "secret-token"},
    )

    audit = registry.get(BackendCapability.AUDIT, context)
    events = audit._events[(
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.conversation_id,
        context.agent_profile_id,
        context.project_id,
        context.run_id,
        context.job_id,
    )]
    actions = {event["action"] for event in events}

    assert {
        "memory.write",
        "skill.list_skills",
        "session.append",
        "cron.create",
        "cron.claim_due",
        "cron.complete_run",
        "queue.enqueue",
        "queue.claim",
        "queue.ack",
        "run_lease.claim",
        "run_lease.renew",
        "run_lease.release",
        "worker_registry.register",
        "worker_registry.heartbeat",
        "artifact.put",
        "delivery.deliver",
    }.issubset(actions)
    assert "secret-token" not in repr(events)
    assert "/Users/derek" not in repr(events)


def test_backend_failure_audit_redacts_secret_like_exception_text():
    class ExplodingMemoryBackend:
        def read(self, context, *, target="memory"):
            return None

        def write(self, context, content, *, target="memory", action="add"):
            raise RuntimeError("failed with token=secret-token and /Users/derek/private.txt")

    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.MEMORY, lambda options: ExplodingMemoryBackend(), profile="local")
    context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-failure")

    try:
        registry.get(BackendCapability.MEMORY, context).write(context, "memory")
    except RuntimeError:
        pass
    else:  # pragma: no cover - assertion path
        raise AssertionError("expected backend failure")

    events = registry.get(BackendCapability.AUDIT, context)._events[
        (
            context.mode,
            context.org_id,
            context.workspace_id,
            context.user_id,
            context.conversation_id,
            context.agent_profile_id,
            context.project_id,
            context.run_id,
            context.job_id,
        )
    ]
    assert events[-1]["success"] is False
    assert "secret-token" not in repr(events)
    assert "/Users/derek" not in repr(events)


def test_registry_can_audit_slotted_protocol_backends_without_requiring_instance_mutation():
    class SlottedMemoryBackend:
        __slots__ = ()

        def read(self, context, *, target="memory"):
            return None

        def write(self, context, content, *, target="memory", action="add"):
            return None

    registry = RuntimeBackendRegistry()
    registry.register(BackendCapability.MEMORY, lambda options: SlottedMemoryBackend(), profile="local")
    context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-slotted")

    backend = registry.get(BackendCapability.MEMORY, context)
    backend.write(context, "memory")

    events = registry.get(BackendCapability.AUDIT, context)._events[
        (
            context.mode,
            context.org_id,
            context.workspace_id,
            context.user_id,
            context.conversation_id,
            context.agent_profile_id,
            context.project_id,
            context.run_id,
            context.job_id,
        )
    ]
    assert events[-1]["action"] == "memory.write"
    assert events[-1]["success"] is True


def test_shared_backend_instances_audit_to_each_registry_scope():
    class SharedMemoryBackend:
        def read(self, context, *, target="memory"):
            return None

        def write(self, context, content, *, target="memory", action="add"):
            return None

    shared = SharedMemoryBackend()
    first = RuntimeBackendRegistry()
    second = RuntimeBackendRegistry()
    first.register(BackendCapability.MEMORY, lambda options: shared, profile="local")
    second.register(BackendCapability.MEMORY, lambda options: shared, profile="local")
    first_context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-first")
    second_context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-second")

    first.get(BackendCapability.MEMORY, first_context).write(first_context, "first")
    second.get(BackendCapability.MEMORY, second_context).write(second_context, "second")

    first_events = first.get(BackendCapability.AUDIT, first_context)._events[
        (
            first_context.mode,
            first_context.org_id,
            first_context.workspace_id,
            first_context.user_id,
            first_context.conversation_id,
            first_context.agent_profile_id,
            first_context.project_id,
            first_context.run_id,
            first_context.job_id,
        )
    ]
    second_events = second.get(BackendCapability.AUDIT, second_context)._events[
        (
            second_context.mode,
            second_context.org_id,
            second_context.workspace_id,
            second_context.user_id,
            second_context.conversation_id,
            second_context.agent_profile_id,
            second_context.project_id,
            second_context.run_id,
            second_context.job_id,
        )
    ]
    assert [event["action"] for event in first_events] == ["memory.write"]
    assert [event["action"] for event in second_events] == ["memory.write"]
    assert second_context.run_id not in {
        key[-2] for key in first.get(BackendCapability.AUDIT, first_context)._events
    }


def test_local_audit_backend_redacts_secret_and_path_text_at_boundary(tmp_path):
    context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-audit-boundary")
    backend = LocalFileAuditBackend(root=tmp_path / "audit")

    backend.record(
        context,
        {
            "action": "tool.unsafe",
            "error": "failed with token=secret-token in /Users/derek/private.txt",
            "result_preview": '{"path": "/Users/derek/private.txt", "token": "secret-token"}',
        },
    )

    events = backend.list_events(context)
    assert "secret-token" not in repr(events)
    assert "/Users/derek" not in repr(events)
    assert events[0]["event"]["error"] == "failed with token=[REDACTED] in [REDACTED_PATH]"


def test_audit_redacts_secret_text_inside_lists_and_bearer_headers():
    event = {
        "argv": ["--authorization=raw-token", "token=secret-token"],
        "message": "Authorization: Bearer bearer-secret",
    }

    sanitized = _redact_audit_value(event)

    assert "raw-token" not in repr(sanitized)
    assert "secret-token" not in repr(sanitized)
    assert "bearer-secret" not in repr(sanitized)
    assert sanitized["argv"] == ["--authorization=[REDACTED]", "token=[REDACTED]"]
    assert sanitized["message"] == "Authorization=[REDACTED] [REDACTED]"


def test_handle_function_call_records_runtime_tool_call_audit(monkeypatch):
    import model_tools
    from tools.registry import registry as tool_registry

    context = RuntimeContext(mode="agentops", org_id="acme", run_id="run-tool")

    if hasattr(model_tools, "_TOOL_AUDIT_REGISTRY_CACHE"):
        model_tools._TOOL_AUDIT_REGISTRY_CACHE.clear()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        tool_registry,
        "dispatch",
        lambda name, args, **kw: '{"ok": true, "path": "/Users/derek/private.txt", "token": "secret-token"}',
    )

    result = model_tools.handle_function_call(
        "runtime_audit_dummy",
        {"command": "cat /Users/derek/private.txt && echo token=secret-token"},
        task_id="task-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        skip_pre_tool_call_hook=True,
        runtime_context=context,
    )

    audit_registry = model_tools._get_runtime_tool_audit_registry({})
    events = audit_registry.get(BackendCapability.AUDIT, context)._events[
        (
            context.mode,
            context.org_id,
            context.workspace_id,
            context.user_id,
            context.conversation_id,
            context.agent_profile_id,
            context.project_id,
            context.run_id,
            context.job_id,
        )
    ]
    tool_events = [event for event in events if event["action"] == "tool.runtime_audit_dummy"]
    assert result == '{"ok": true, "path": "/Users/derek/private.txt", "token": "secret-token"}'
    assert tool_events[-1]["success"] is True
    assert tool_events[-1]["tool_name"] == "runtime_audit_dummy"
    assert "secret-token" not in repr(tool_events)
    assert "/Users/derek" not in repr(tool_events)


def test_http_audit_backend_count_events_returns_integer_count():
    """count_events sends GET /audit with scope param; returns the integer count."""
    seen: list[tuple[str, str, dict]] = []

    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        seen.append((method, url, headers or {}))
        return 200, {"content-type": "application/json"}, json.dumps({"count": 3}).encode()

    backend = HttpAuditBackend(base_url="https://control.example", request=fake_request)
    result = backend.count_events(_context("derek"))

    assert result == 3
    assert isinstance(result, int) and not isinstance(result, bool)
    assert len(seen) == 1
    method, url, _ = seen[0]
    assert method == "GET"
    assert "/audit" in url
    assert "scope" in url


def test_http_audit_backend_count_events_token_in_header_not_url():
    """Bearer token must appear only in the Authorization header, not in the query URL."""
    seen_calls: list[tuple[str, dict]] = []

    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        seen_calls.append((url, dict(headers or {})))
        return 200, {"content-type": "application/json"}, json.dumps({"count": 0}).encode()

    backend = HttpAuditBackend(
        base_url="https://control.example",
        token="secret-bearer-token",
        request=fake_request,
    )
    backend.count_events(_context("derek"))

    url, hdrs = seen_calls[0]
    assert "secret-bearer-token" not in url
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    assert "secret-bearer-token" in auth


def test_http_audit_backend_count_events_fails_closed_on_bool_count():
    """count_events must raise for a JSON boolean — True/False are not valid integers."""
    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        return 200, {"content-type": "application/json"}, json.dumps({"count": True}).encode()

    backend = HttpAuditBackend(base_url="https://control.example", request=fake_request)
    try:
        backend.count_events(_context("derek"))
    except (ValueError, TypeError, RuntimeError):
        return
    raise AssertionError("expected count_events to raise for a boolean count")


def test_http_audit_backend_count_events_fails_closed_on_string_count():
    """count_events must raise when server returns a string, not silently cast it."""
    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        return 200, {"content-type": "application/json"}, json.dumps({"count": "3"}).encode()

    backend = HttpAuditBackend(base_url="https://control.example", request=fake_request)
    try:
        backend.count_events(_context("derek"))
    except (ValueError, TypeError, RuntimeError):
        return
    raise AssertionError("expected count_events to raise for a string count")


def test_http_audit_backend_count_events_fails_closed_on_malformed_json():
    """count_events must raise when the server body is not valid JSON."""
    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        return 200, {"content-type": "application/json"}, b"not-json!!!"

    backend = HttpAuditBackend(base_url="https://control.example", request=fake_request)
    try:
        backend.count_events(_context("derek"))
    except Exception:
        return
    raise AssertionError("expected count_events to raise for malformed JSON")


def test_http_audit_backend_count_events_malformed_body_error_is_sanitized():
    """Malformed count responses must not retain body, URL, or token material in exceptions."""
    raw_body = b'{"audit_payload":"secret-audit-sentinel",'

    def fake_request(method: str, url: str, *, headers=None, body=None, timeout=None):
        return 200, {"content-type": "application/json"}, raw_body

    backend = HttpAuditBackend(
        base_url="https://control.example/private-audit-path",
        token="secret-audit-token",
        request=fake_request,
    )
    try:
        backend.count_events(_context("derek"))
    except Exception as exc:
        observed = " ".join(
            part
            for part in (
                str(exc),
                repr(exc),
                repr(getattr(exc, "__cause__", None)),
                repr(getattr(exc, "__context__", None)),
                repr(getattr(getattr(exc, "__context__", None), "doc", None)),
                repr(getattr(exc, "doc", None)),
            )
            if part
        )
        assert "secret-audit-sentinel" not in observed
        assert "secret-audit-token" not in observed
        assert "private-audit-path" not in observed
        return
    raise AssertionError("expected count_events to raise for malformed JSON")
