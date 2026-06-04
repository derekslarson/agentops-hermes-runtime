"""Tests for the M12B HTTP skill backend adapter.

Covers:
- list_skills() transport and return values; category in body
- load_skill() transport; name and file_path in body only, not URL
- manage_skill() transport; mutation fields transported verbatim
- scope payload includes conversation_id and skill_write_approved from metadata
- scope payload excludes run_id, job_id, delivery_ref
- base URL validation rejects unsafe configs
- token sent only via Authorization header, not in URL/body
- request errors sanitized; no token/name/file_path/content leakage; logical endpoint labels used
- malformed/non-JSON/non-object responses fail closed
- registry integration
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext
from agent.runtime_skill_http import (
    HttpSkillBackend,
    register_http_skill_backend,
)


# ---------------------------------------------------------------------------
# Minimal in-process HTTP server
# ---------------------------------------------------------------------------


class _SkillHandler(BaseHTTPRequestHandler):
    server: "_SkillServer"

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))

        force_status = getattr(self.server, "force_status", None)
        if force_status is not None:
            self.send_error(force_status)
            return

        raw_response = getattr(self.server, "raw_response", None)
        if raw_response is not None:
            raw = raw_response if isinstance(raw_response, bytes) else str(raw_response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        truncated_response = getattr(self.server, "truncated_response", None)
        if truncated_response is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(truncated_response) + 20))
            self.end_headers()
            self.wfile.write(truncated_response)
            return

        path = self.path.strip("/")

        if path == "skills/list":
            skills = getattr(self.server, "skills", [])
            self._json({"skills": skills})
            return

        if path == "skills/view":
            skill_payload = getattr(self.server, "skill_payload", {"success": True, "content": "step 1."})
            self._json(skill_payload)
            return

        if path == "skills/manage":
            manage_result = getattr(self.server, "manage_result", {"success": True})
            self._json(manage_result)
            return

        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _SkillServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None, dict[str, Any]]]
    skills: list[dict[str, Any]]
    skill_payload: dict[str, Any]
    manage_result: dict[str, Any]
    force_status: int | None
    raw_response: str | bytes | None
    truncated_response: bytes | None


@pytest.fixture
def skill_server():
    server = _SkillServer(("127.0.0.1", 0), _SkillHandler)
    server.requests = []
    server.skills = [{"name": "deploy", "description": "Deploy service", "category": "ops"}]
    server.skill_payload = {"success": True, "name": "deploy", "content": "step 1."}
    server.manage_result = {"success": True}
    server.force_status = None
    server.raw_response = None
    server.truncated_response = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _ctx(**overrides: Any) -> RuntimeContext:
    base = dict(
        mode="agentops",
        org_id="acme",
        workspace_id="slack-main",
        workspace_type="slack",
        user_id="derek",
        conversation_id="thread-1",
        agent_profile_id="default",
        project_id="agentops-runtime",
        backend_profile="compose-self-hosted",
        permissions_ref="perms:acme:derek",
        run_id="run-abc",
        job_id="job-xyz",
        delivery_ref="delivery:slack:home",
    )
    base.update(overrides)
    return RuntimeContext(**base)


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------


def test_list_skills_posts_context_and_returns_skills(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}", token="sentinel-token")
    result = backend.list_skills(_ctx())

    assert isinstance(result, list)
    assert result[0]["name"] == "deploy"
    assert len(skill_server.requests) == 1
    method, path, auth, body = skill_server.requests[0]
    assert method == "POST"
    assert path == "/skills/list"
    assert auth == "Bearer sentinel-token"
    assert isinstance(body.get("context"), dict)


def test_list_skills_sends_category_in_body(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx(), category="ops")

    _, _, _, body = skill_server.requests[0]
    assert body.get("category") == "ops"


def test_list_skills_omits_category_when_none(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx(), category=None)

    _, _, _, body = skill_server.requests[0]
    assert "category" not in body


def test_list_skills_returns_empty_list_for_empty_skills(skill_server):
    skill_server.skills = []
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    result = backend.list_skills(_ctx())
    assert result == []


def test_list_skills_accepts_bare_json_list(skill_server):
    skill_server.raw_response = '[{"name": "bare-list-skill", "description": "d"}]'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    result = backend.list_skills(_ctx())

    assert result == [{"name": "bare-list-skill", "description": "d"}]


def test_list_skills_rejects_non_object_skill_entries(skill_server):
    skill_server.raw_response = '["skill-name-sentinel"]'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())

    text = str(exc_info.value)
    assert "skill entry" in text
    assert "skill-name-sentinel" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_list_skills_rejects_entries_missing_string_name(skill_server):
    skill_server.raw_response = '[{"description": "skill-name-sentinel"}]'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())

    text = str(exc_info.value)
    assert "skill entry" in text
    assert "skill-name-sentinel" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_list_skills_raises_on_missing_skills_key(skill_server):
    skill_server.raw_response = "{}"
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError):
        backend.list_skills(_ctx())


def test_list_skills_raises_on_non_list_skills_value(skill_server):
    skill_server.raw_response = '{"skills": "bad-type-sentinel"}'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    text = str(exc_info.value)
    assert "str" in text
    assert "bad-type-sentinel" not in text


# ---------------------------------------------------------------------------
# load_skill
# ---------------------------------------------------------------------------


def test_load_skill_posts_name_in_body_not_url(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}", token="tok-123")
    backend.load_skill(_ctx(), "deploy-skill-sentinel")

    assert len(skill_server.requests) == 1
    method, path, auth, body = skill_server.requests[0]
    assert method == "POST"
    assert path == "/skills/view"
    assert "deploy-skill-sentinel" not in path
    assert body.get("name") == "deploy-skill-sentinel"
    assert isinstance(body.get("context"), dict)


def test_load_skill_sends_file_path_in_body_not_url(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.load_skill(_ctx(), "deploy", file_path="references/api-sentinel.md")

    _, path, _, body = skill_server.requests[0]
    assert "references" not in path
    assert "api-sentinel.md" not in path
    assert body.get("file_path") == "references/api-sentinel.md"


def test_load_skill_omits_file_path_when_none(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.load_skill(_ctx(), "deploy", file_path=None)

    _, _, _, body = skill_server.requests[0]
    assert "file_path" not in body


def test_load_skill_includes_preprocess_in_body(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.load_skill(_ctx(), "deploy", preprocess=False)

    _, _, _, body = skill_server.requests[0]
    assert body.get("preprocess") is False


def test_load_skill_returns_payload_dict(skill_server):
    skill_server.skill_payload = {"success": True, "name": "deploy", "content": "step 1."}
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    result = backend.load_skill(_ctx(), "deploy")
    assert result["success"] is True
    assert result["content"] == "step 1."


def test_load_skill_returns_none_for_remote_null(skill_server):
    skill_server.raw_response = "null"
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    assert backend.load_skill(_ctx(), "deploy") is None


def test_load_skill_rejects_non_string_name(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises((TypeError, ValueError)):
        backend.load_skill(_ctx(), 123)  # type: ignore[arg-type]
    assert skill_server.requests == []


# ---------------------------------------------------------------------------
# manage_skill
# ---------------------------------------------------------------------------


def test_manage_skill_posts_action_and_name_in_body(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}", token="tok-mgmt")
    backend.manage_skill(_ctx(), action="create", name="new-flow-sentinel", scope="user", content="step 1.")

    assert len(skill_server.requests) == 1
    method, path, auth, body = skill_server.requests[0]
    assert method == "POST"
    assert path == "/skills/manage"
    assert auth == "Bearer tok-mgmt"
    assert body.get("action") == "create"
    assert body.get("name") == "new-flow-sentinel"
    assert body.get("scope") == "user"
    assert body.get("content") == "step 1."
    assert isinstance(body.get("context"), dict)


def test_manage_skill_transports_mutation_fields_verbatim(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.manage_skill(
        _ctx(),
        action="edit",
        name="my-skill",
        content="new content",
        old_string="old text",
        new_string="new text",
        file_content="file body",
    )

    _, _, _, body = skill_server.requests[0]
    assert body.get("content") == "new content"
    assert body.get("old_string") == "old text"
    assert body.get("new_string") == "new text"
    assert body.get("file_content") == "file body"


def test_manage_skill_returns_result_dict(skill_server):
    skill_server.manage_result = {"success": True, "message": "created"}
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    result = backend.manage_skill(_ctx(), action="create", name="x", scope="user", content="body")
    assert result["success"] is True
    assert result["message"] == "created"


def test_manage_skill_rejects_non_string_name(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises((TypeError, ValueError)):
        backend.manage_skill(_ctx(), action="create", name=42, scope="user")  # type: ignore[arg-type]
    assert skill_server.requests == []


# ---------------------------------------------------------------------------
# Scope payload
# ---------------------------------------------------------------------------


def test_scope_payload_includes_binding_and_policy_fields(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(
        _ctx(
            mode="agentops",
            org_id="org-1",
            workspace_id="ws-2",
            workspace_type="slack",
            user_id="u-3",
            project_id="proj-4",
            conversation_id="conv-5",
            agent_profile_id="profile-6",
            permissions_ref="perms:org-1:u-3",
            backend_profile="compose-self-hosted",
        )
    )

    _, _, _, body = skill_server.requests[0]
    ctx = body["context"]
    assert ctx["mode"] == "agentops"
    assert ctx["org_id"] == "org-1"
    assert ctx["workspace_id"] == "ws-2"
    assert ctx["workspace_type"] == "slack"
    assert ctx["user_id"] == "u-3"
    assert ctx["project_id"] == "proj-4"
    assert ctx["conversation_id"] == "conv-5"
    assert ctx["agent_profile_id"] == "profile-6"
    assert ctx["permissions_ref"] == "perms:org-1:u-3"
    assert ctx["backend_profile"] == "compose-self-hosted"


def test_scope_payload_includes_skill_write_approved_from_metadata(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx(metadata={"skill_write_approved": True}))

    _, _, _, body = skill_server.requests[0]
    assert body["context"]["skill_write_approved"] is True


def test_scope_payload_omits_skill_write_approved_when_absent_from_metadata(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx())

    _, _, _, body = skill_server.requests[0]
    assert "skill_write_approved" not in body["context"]


def test_scope_payload_includes_conversation_id(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx(conversation_id="conv-runtime-sentinel"))

    _, _, _, body = skill_server.requests[0]
    assert body["context"]["conversation_id"] == "conv-runtime-sentinel"


def test_scope_payload_excludes_run_id_job_id_delivery_ref(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx(run_id="run-123", job_id="job-456", delivery_ref="delivery:slack:home"))

    _, _, _, body = skill_server.requests[0]
    ctx = body["context"]
    assert "run_id" not in ctx
    assert "job_id" not in ctx
    assert "delivery_ref" not in ctx


def test_scope_payload_is_empty_dict_for_none_context(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(None)

    _, _, _, body = skill_server.requests[0]
    assert body["context"] == {}


# ---------------------------------------------------------------------------
# Error sanitization — no name, file_path, content, hostname, token in errors
# ---------------------------------------------------------------------------


def test_skill_name_not_in_http_error_message(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.load_skill(_ctx(), "skill-name-sentinel")
    assert "skill-name-sentinel" not in str(exc_info.value)


def test_file_path_not_in_http_error_message(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.load_skill(_ctx(), "deploy", file_path="linked-file-path-sentinel.md")
    assert "linked-file-path-sentinel.md" not in str(exc_info.value)


def test_content_not_in_http_error_message(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.manage_skill(_ctx(), action="create", name="x", content="content-sentinel-value")
    assert "content-sentinel-value" not in str(exc_info.value)


def test_token_not_in_http_error_message(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}", token="super-secret-bearer-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    assert "super-secret-bearer-sentinel" not in str(exc_info.value)


def test_base_url_not_in_error_message(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    assert "127.0.0.1" not in str(exc_info.value)


def test_error_message_uses_logical_endpoint_label_list(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/skills/list"):
        backend.list_skills(_ctx())


def test_error_message_uses_logical_endpoint_label_load(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/skills/view"):
        backend.load_skill(_ctx(), "x")


def test_error_message_uses_logical_endpoint_label_manage(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError, match=r"/skills/manage"):
        backend.manage_skill(_ctx(), action="create", name="x", scope="user", content="body")


def test_no_raw_exception_chain_retained_on_http_error(skill_server):
    skill_server.force_status = 500
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_no_raw_exception_chain_retained_on_transport_error():
    backend = HttpSkillBackend("http://127.0.0.1:19998")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_timeout_error_sanitized_without_raw_exception_chain(monkeypatch):
    def fail_with_timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("timed out while reading skill-name-sentinel")

    monkeypatch.setattr("urllib.request.urlopen", fail_with_timeout)
    backend = HttpSkillBackend("https://skills.internal", token="token-sentinel")

    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())

    text = str(exc_info.value)
    assert "timed out" not in text
    assert "skill-name-sentinel" not in text
    assert "token-sentinel" not in text
    assert "skills.internal" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_json_response_sanitized(skill_server):
    skill_server.raw_response = "not-json-skill-name-sentinel"
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    text = str(exc_info.value)
    assert "not-json-skill-name-sentinel" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_empty_view_response_fails_closed(skill_server):
    skill_server.raw_response = ""
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    with pytest.raises(RuntimeError, match=r"/skills/view") as exc_info:
        backend.load_skill(_ctx(), "deploy")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_empty_manage_response_fails_closed(skill_server):
    skill_server.raw_response = ""
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")

    with pytest.raises(RuntimeError, match=r"/skills/manage") as exc_info:
        backend.manage_skill(_ctx(), action="create", name="deploy", content="body")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_list_non_object_json_response_fails_closed_with_type_only_error(skill_server):
    skill_server.raw_response = '"skill-name-sentinel"'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    text = str(exc_info.value)
    assert "str" in text
    assert "skill-name-sentinel" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_utf8_response_sanitized(skill_server):
    skill_server.raw_response = b'{"skills":[]}\xff'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    text = str(exc_info.value)
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_truncated_response_sanitized(skill_server):
    skill_server.truncated_response = b'{"skills":[{"name":"skill-name-sentinel"}'
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    with pytest.raises(RuntimeError) as exc_info:
        backend.list_skills(_ctx())
    text = str(exc_info.value)
    assert "skill-name-sentinel" not in text
    assert "127.0.0.1" not in text
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_empty_base_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpSkillBackend("")


def test_non_string_base_url_raises_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend(123)  # type: ignore[arg-type]
    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_http_scheme_raises_value_error():
    with pytest.raises(ValueError):
        HttpSkillBackend("ftp://skills.internal")


def test_credentials_in_url_raises_value_error():
    with pytest.raises(ValueError):
        HttpSkillBackend("https://user:***@skills.internal")


def test_malformed_host_url_raises_sanitized_value_error():
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend("http://[skill-host-sentinel]")
    assert "skill-host-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("url", ["https://skills.internal?foo=bar", "https://skills.internal?"])
def test_query_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpSkillBackend(url)


@pytest.mark.parametrize("url", ["https://skills.internal#section", "https://skills.internal#"])
def test_fragment_in_url_raises_value_error(url):
    with pytest.raises(ValueError):
        HttpSkillBackend(url)


def test_invalid_timeout_raises_value_error():
    with pytest.raises(ValueError):
        HttpSkillBackend("https://skills.internal", timeout=-1)


@pytest.mark.parametrize(
    "url",
    [" https://skills.internal", "https://skills.internal ", "\thttps://skills.internal"],
)
def test_base_url_with_whitespace_rejected(url):
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend(url)
    assert "skills.internal" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_invalid_port_rejected_without_raw_exception_chain():
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend("https://skills.internal:secret-port-sentinel")
    assert "secret-port-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_token_with_control_character_rejected():
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend("https://skills.internal", token="token-secret\nX-Leak: yes")
    assert "token-secret" not in str(exc_info.value)
    assert "X-Leak" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_string_token_rejected():
    with pytest.raises(ValueError) as exc_info:
        HttpSkillBackend("https://skills.internal", token=123)  # type: ignore[arg-type]
    assert "123" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ---------------------------------------------------------------------------
# Token in header only
# ---------------------------------------------------------------------------


def test_token_sent_only_via_authorization_header(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}", token="tok-abc")
    backend.list_skills(_ctx())

    _, _, auth, body = skill_server.requests[0]
    assert auth == "Bearer tok-abc"
    body_str = json.dumps(body)
    assert "tok-abc" not in body_str


def test_no_token_sends_no_authorization_header(skill_server):
    backend = HttpSkillBackend(f"http://127.0.0.1:{skill_server.server_port}")
    backend.list_skills(_ctx())

    _, _, auth, _ = skill_server.requests[0]
    assert auth is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_register_http_skill_backend_registers_skill_capability():
    registry = RuntimeBackendRegistry()
    register_http_skill_backend(registry)

    assert "compose-self-hosted" in registry.registered_profiles(BackendCapability.SKILL)


def test_register_http_skill_backend_factory_creates_http_skill_backend(skill_server):
    registry = RuntimeBackendRegistry()
    register_http_skill_backend(registry)
    registry.set_capability_options(
        BackendCapability.SKILL,
        {"base_url": f"http://127.0.0.1:{skill_server.server_port}", "token": "t"},
        merge=False,
    )
    context = RuntimeContext(mode="agentops", backend_profile="compose-self-hosted")
    backend = registry.get(BackendCapability.SKILL, context)
    assert isinstance(backend, HttpSkillBackend)
