"""Tests for /skills control-plane API handler (M12B)."""

from __future__ import annotations

import json

import pytest


_ORG_CTX = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "user_id": "alice",
    "project_id": "proj1",
    "conversation_id": "conv1",
}

_OTHER_USER_CTX = {
    "mode": "agentops",
    "org_id": "org1",
    "workspace_id": "ws1",
    "user_id": "bob",
    "project_id": "proj1",
    "conversation_id": "conv2",
}

_OTHER_ORG_CTX = {
    "mode": "agentops",
    "org_id": "org2",
    "workspace_id": "ws2",
    "user_id": "carol",
    "project_id": "proj2",
    "conversation_id": "conv3",
}


@pytest.fixture
def skill_store(tmp_path):
    from agentops_runtime.skills_api import SQLiteScopedSkillStore

    return SQLiteScopedSkillStore(str(tmp_path / "skills.db"))


def _post(path, body, backend, *, token=None, auth_header=None):
    from agentops_runtime.skills_api import handle_skill_request

    return handle_skill_request(
        method="POST",
        path=path,
        query_string="",
        body_bytes=json.dumps(body).encode(),
        auth_header=auth_header,
        token=token,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_skills_api_can_be_imported():
    from agentops_runtime import skills_api

    assert hasattr(skills_api, "SQLiteScopedSkillStore")
    assert hasattr(skills_api, "handle_skill_request")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_empty_on_no_skills(skill_store):
    status, resp = _post("/skills/list", {"context": _ORG_CTX}, skill_store)
    assert status == 200
    assert resp["skills"] == []


def test_list_returns_bundled_skill_without_context(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(name="setup", scope="bundled", content="setup content")
    skill_store.seed_records(seed.export_records())

    status, resp = _post("/skills/list", {"context": {}}, skill_store)
    assert status == 200
    names = [s["name"] for s in resp["skills"]]
    assert "setup" in names


def test_list_with_category_filter(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(name="dbops", scope="bundled", content="x", category="database")
    seed.add_skill(name="webops", scope="bundled", content="y", category="web")
    skill_store.seed_records(seed.export_records())

    status, resp = _post("/skills/list", {"context": {}, "category": "database"}, skill_store)
    assert status == 200
    names = [s["name"] for s in resp["skills"]]
    assert "dbops" in names
    assert "webops" not in names


def test_list_null_context_returns_bundled(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(name="bundled-one", scope="bundled", content="c")
    skill_store.seed_records(seed.export_records())

    status, resp = _post("/skills/list", {"context": None}, skill_store)
    assert status == 200
    names = [s["name"] for s in resp["skills"]]
    assert "bundled-one" in names


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


def test_view_returns_not_found_for_missing_skill(skill_store):
    status, resp = _post("/skills/view", {"context": _ORG_CTX, "name": "nonexistent"}, skill_store)
    assert status == 200
    assert resp["success"] is False


def test_view_missing_skill_error_does_not_echo_name(skill_store):
    sentinel_name = "secret-skill-sentinel"
    status, resp = _post("/skills/view", {"context": _ORG_CTX, "name": sentinel_name}, skill_store)
    assert status == 200
    assert resp["success"] is False
    assert sentinel_name not in json.dumps(resp)


def test_view_missing_linked_file_error_does_not_echo_path_or_available_files(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    sentinel_name = "private-secret-name"
    sentinel_path = "references/missing-secret-file.md"
    available_path = "references/secret-file.md"
    seed = ScopedSkillBackend()
    seed.add_skill(
        name=sentinel_name,
        scope="bundled",
        content="main",
        files={available_path: "ref content"},
    )
    skill_store.seed_records(seed.export_records())

    status, resp = _post(
        "/skills/view",
        {"context": _ORG_CTX, "name": sentinel_name, "file_path": sentinel_path},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is False
    serialized = json.dumps(resp)
    assert sentinel_name not in serialized
    assert sentinel_path not in serialized
    assert available_path not in serialized


def test_view_returns_skill_content(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(name="helper", scope="bundled", content="helper content", description="desc")
    skill_store.seed_records(seed.export_records())

    status, resp = _post("/skills/view", {"context": {}, "name": "helper"}, skill_store)
    assert status == 200
    assert resp["success"] is True
    assert resp["content"] == "helper content"
    assert resp["name"] == "helper"


def test_view_linked_file(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(
        name="docs-skill",
        scope="bundled",
        content="main",
        files={"references/guide.md": "ref content"},
    )
    skill_store.seed_records(seed.export_records())

    status, resp = _post(
        "/skills/view",
        {"context": {}, "name": "docs-skill", "file_path": "references/guide.md"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is True
    assert resp["content"] == "ref content"


def test_view_requires_name(skill_store):
    status, resp = _post("/skills/view", {"context": _ORG_CTX}, skill_store)
    assert status == 400


# ---------------------------------------------------------------------------
# manage
# ---------------------------------------------------------------------------


def test_manage_create_and_view(skill_store):
    status, resp = _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "create", "name": "my-skill", "content": "skill body", "scope": "user"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is True

    status2, resp2 = _post("/skills/view", {"context": _ORG_CTX, "name": "my-skill"}, skill_store)
    assert status2 == 200
    assert resp2["success"] is True
    assert resp2["content"] == "skill body"


def test_manage_edit_skill(skill_store):
    _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "create", "name": "editme", "content": "v1", "scope": "user"},
        skill_store,
    )
    _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "edit", "name": "editme", "content": "v2"},
        skill_store,
    )

    status, resp = _post("/skills/view", {"context": _ORG_CTX, "name": "editme"}, skill_store)
    assert status == 200
    assert resp["content"] == "v2"


def test_manage_requires_action(skill_store):
    status, resp = _post("/skills/manage", {"context": _ORG_CTX, "name": "x"}, skill_store)
    assert status == 400


def test_manage_requires_name(skill_store):
    status, resp = _post("/skills/manage", {"context": _ORG_CTX, "action": "create"}, skill_store)
    assert status == 400


# ---------------------------------------------------------------------------
# SQLite durability across instances
# ---------------------------------------------------------------------------


def test_sqlite_durability_across_instances(tmp_path):
    from agentops_runtime.skills_api import SQLiteScopedSkillStore

    db_path = str(tmp_path / "durable.db")
    store1 = SQLiteScopedSkillStore(db_path)
    _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "create", "name": "durable-skill", "content": "persisted", "scope": "user"},
        store1,
    )

    store2 = SQLiteScopedSkillStore(db_path)
    status, resp = _post("/skills/view", {"context": _ORG_CTX, "name": "durable-skill"}, store2)
    assert status == 200
    assert resp["success"] is True
    assert resp["content"] == "persisted"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_isolation_user_skills(skill_store):
    _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "create", "name": "private", "content": "secret", "scope": "user"},
        skill_store,
    )

    status, resp = _post("/skills/view", {"context": _OTHER_USER_CTX, "name": "private"}, skill_store)
    assert status == 200
    assert resp["success"] is False


def test_tenant_isolation_org_skills_across_orgs(skill_store):
    ctx_with_approval = {**_ORG_CTX, "skill_write_approved": True}
    _post(
        "/skills/manage",
        {"context": ctx_with_approval, "action": "create", "name": "org-skill", "scope": "org"},
        skill_store,
    )

    status, resp = _post("/skills/view", {"context": _OTHER_ORG_CTX, "name": "org-skill"}, skill_store)
    assert status == 200
    assert resp["success"] is False


# ---------------------------------------------------------------------------
# Project/org shared skill visibility
# ---------------------------------------------------------------------------


def test_project_skill_visible_to_same_project(skill_store):
    ctx_with_approval = {**_ORG_CTX, "skill_write_approved": True}
    _post(
        "/skills/manage",
        {"context": ctx_with_approval, "action": "create", "name": "proj-shared", "scope": "project"},
        skill_store,
    )

    peer_ctx = {**_OTHER_USER_CTX, "project_id": "proj1"}
    status, resp = _post("/skills/view", {"context": peer_ctx, "name": "proj-shared"}, skill_store)
    assert status == 200
    assert resp["success"] is True


def test_org_skill_visible_to_same_org(skill_store):
    ctx_with_approval = {**_ORG_CTX, "skill_write_approved": True}
    _post(
        "/skills/manage",
        {"context": ctx_with_approval, "action": "create", "name": "org-shared", "scope": "org"},
        skill_store,
    )

    peer_ctx = {**_OTHER_USER_CTX}
    status, resp = _post("/skills/view", {"context": peer_ctx, "name": "org-shared"}, skill_store)
    assert status == 200
    assert resp["success"] is True


# ---------------------------------------------------------------------------
# skill_write_approved metadata mapping
# ---------------------------------------------------------------------------


def test_shared_write_requires_approval(skill_store):
    status, resp = _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "create", "name": "shared-skill", "scope": "project"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is False
    assert "policy" in resp


def test_shared_write_with_skill_write_approved_in_scope(skill_store):
    ctx_with_approval = {**_ORG_CTX, "skill_write_approved": True}
    status, resp = _post(
        "/skills/manage",
        {"context": ctx_with_approval, "action": "create", "name": "shared-approved", "scope": "project"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is True


def test_shared_write_rejects_truthy_non_boolean_skill_write_approval(skill_store):
    ctx_with_string = {**_ORG_CTX, "skill_write_approved": "true"}
    status, resp = _post(
        "/skills/manage",
        {"context": ctx_with_string, "action": "create", "name": "string-approved", "scope": "project"},
        skill_store,
    )
    assert status == 400
    assert resp["error"] == "skill_write_approved must be a boolean"


def test_shared_write_rejects_truthy_nested_metadata_approval(skill_store):
    ctx_with_string = {**_ORG_CTX, "metadata": {"skill_write_approved": "true"}}
    status, resp = _post(
        "/skills/manage",
        {"context": ctx_with_string, "action": "create", "name": "metadata-string-approved", "scope": "project"},
        skill_store,
    )
    assert status == 400
    assert resp["error"] == "skill_write_approved must be a boolean"


def test_shared_write_rejects_truthy_non_boolean_allow_shared_write_field(skill_store):
    status, resp = _post(
        "/skills/manage",
        {
            "context": _ORG_CTX,
            "action": "create",
            "name": "allow-shared-string-approved",
            "scope": "project",
            "allow_shared_write": "false",
        },
        skill_store,
    )
    assert status == 400
    assert resp["error"] == "allow_shared_write must be a boolean"


def test_manage_rejects_null_allow_shared_write_field(skill_store):
    status, resp = _post(
        "/skills/manage",
        {
            "context": _ORG_CTX,
            "action": "create",
            "name": "allow-shared-null-approved",
            "scope": "project",
            "allow_shared_write": None,
        },
        skill_store,
    )
    assert status == 400
    assert resp["error"] == "allow_shared_write must be a boolean"


def test_manage_missing_skill_error_does_not_echo_name(skill_store):
    sentinel_name = "secret-manage-sentinel"
    status, resp = _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "edit", "name": sentinel_name, "content": "replacement"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is False
    assert sentinel_name not in json.dumps(resp)


def test_skill_write_approved_false_still_blocked(skill_store):
    ctx_with_false = {**_ORG_CTX, "skill_write_approved": False}
    status, resp = _post(
        "/skills/manage",
        {"context": ctx_with_false, "action": "create", "name": "blocked", "scope": "project"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is False
    assert "policy" in resp


# ---------------------------------------------------------------------------
# Bundled read-only policy
# ---------------------------------------------------------------------------


def test_bundled_skill_read_only_policy(skill_store):
    from agent.runtime_skills import ScopedSkillBackend

    seed = ScopedSkillBackend()
    seed.add_skill(name="readonly", scope="bundled", content="system content")
    skill_store.seed_records(seed.export_records())

    status, resp = _post(
        "/skills/manage",
        {"context": _ORG_CTX, "action": "edit", "name": "readonly", "content": "hacked"},
        skill_store,
    )
    assert status == 200
    assert resp["success"] is False
    assert resp.get("policy") == "bundled_read_only"


# ---------------------------------------------------------------------------
# Invalid body/context errors
# ---------------------------------------------------------------------------


def test_empty_body_returns_400(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=skill_store,
    )
    assert status == 400


def test_invalid_json_body_returns_400(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=b"not-json",
        auth_header=None,
        token=None,
        backend=skill_store,
    )
    assert status == 400


def test_non_object_body_returns_400(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=b'["list not object"]',
        auth_header=None,
        token=None,
        backend=skill_store,
    )
    assert status == 400


def test_invalid_context_type_returns_400(skill_store):
    status, resp = _post("/skills/list", {"context": "not-a-dict"}, skill_store)
    assert status == 400


# ---------------------------------------------------------------------------
# Auth rejection before backend use
# ---------------------------------------------------------------------------


def test_auth_rejection_returns_401(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=json.dumps({"context": {}}).encode(),
        auth_header="Bearer wrong-token",
        token="correct-token",
        backend=skill_store,
    )
    assert status == 401
    assert "error" in resp


def test_auth_rejected_even_with_invalid_body(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=b"not-json",
        auth_header="Bearer wrong-token",
        token="correct-token",
        backend=skill_store,
    )
    assert status == 401


def test_no_token_configured_allows_any_auth_header(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/list",
        query_string="",
        body_bytes=json.dumps({"context": {}}).encode(),
        auth_header="Bearer some-token",
        token=None,
        backend=skill_store,
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Method/path behavior
# ---------------------------------------------------------------------------


def test_get_method_returns_405(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="GET",
        path="/skills/list",
        query_string="",
        body_bytes=b"",
        auth_header=None,
        token=None,
        backend=skill_store,
    )
    assert status == 405


def test_unknown_path_returns_404(skill_store):
    from agentops_runtime.skills_api import handle_skill_request

    status, resp = handle_skill_request(
        method="POST",
        path="/skills/unknown",
        query_string="",
        body_bytes=json.dumps({"context": {}}).encode(),
        auth_header=None,
        token=None,
        backend=skill_store,
    )
    assert status == 404


# ---------------------------------------------------------------------------
# Sanitized backend errors
# ---------------------------------------------------------------------------


def test_backend_list_exception_returns_503_sanitized():
    class _ExplodingBackend:
        def list_skills(self, context, *, category=None):
            raise RuntimeError("internal db path=/var/secret/db token=secret-abc123")

    status, resp = _post("/skills/list", {"context": {}}, _ExplodingBackend())
    assert status == 503
    assert "error" in resp
    assert "secret-abc123" not in resp["error"]
    assert "db path" not in resp["error"]


def test_backend_view_exception_returns_503_sanitized():
    class _ExplodingBackend:
        def load_skill(self, context, name, *, file_path=None, preprocess=True):
            raise RuntimeError("org_id=org1 token=abc path=/secret/db")

    status, resp = _post("/skills/view", {"context": {}, "name": "x"}, _ExplodingBackend())
    assert status == 503
    assert "org_id" not in resp["error"]
    assert "abc" not in resp["error"]


def test_backend_manage_exception_returns_503_sanitized():
    class _ExplodingBackend:
        def manage_skill(self, context, *, action, name, **fields):
            raise RuntimeError("skill name=secret-name tenant=org1")

    status, resp = _post(
        "/skills/manage",
        {"context": {}, "action": "create", "name": "x"},
        _ExplodingBackend(),
    )
    assert status == 503
    assert "secret-name" not in resp["error"]
