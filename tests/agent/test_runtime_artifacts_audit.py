from __future__ import annotations

import json
import os

from agent.runtime_artifacts_audit import (
    LocalFileArtifactBackend,
    LocalFileAuditBackend,
    SanitizedAuditEvent,
    register_http_artifact_backend,
    register_http_audit_backend,
)
from agent.runtime_backends import BackendCapability, BackendSelectionError, RuntimeBackendRegistry
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
