"""skill_manage mutation permissions through a scoped backend (M7).

Proves that in AgentOps mode the native skill_manage surface routes through the
selected backend with explicit mutation policy: user-private writes are allowed,
shared org/project writes require an approval flag and fail closed before any
side effect, the pinned-delete guard holds, and a local-mode context never
routes to the scoped backend (default local behavior preserved).
"""

from __future__ import annotations

import json

import pytest

from agent.runtime_context import RuntimeContext, use_runtime_context
from agent.runtime_skills import (
    ScopedSkillBackend,
    clear_active_skill_backend,
    set_active_skill_backend,
)
from tools.skill_manager_tool import skill_manage
from tools.skills_tool import skill_view


@pytest.fixture(autouse=True)
def _reset_active_backend():
    clear_active_skill_backend()
    yield
    clear_active_skill_backend()


def _ctx(user_id="derek", org_id="acme", project_id=None, metadata=None):
    return RuntimeContext(
        mode="agentops",
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        conversation_id="thread-1",
        backend_profile="compose-self-hosted",
        metadata=metadata or {},
    )


def _seed_org():
    backend = ScopedSkillBackend()
    backend.add_skill(name="org-runbook", scope="org", org_id="acme", content="orig")
    return backend


def test_user_private_create_and_edit_allowed():
    set_active_skill_backend(ScopedSkillBackend())
    with use_runtime_context(_ctx()):
        created = json.loads(skill_manage(action="create", name="mine", content="x"))
        edited = json.loads(skill_manage(action="edit", name="mine", content="y"))
    assert created["success"] is True
    assert edited["success"] is True


def test_shared_org_edit_blocked_without_approval_no_side_effect():
    backend = _seed_org()
    set_active_skill_backend(backend)
    with use_runtime_context(_ctx()):
        result = json.loads(skill_manage(action="edit", name="org-runbook", content="hacked"))
        assert result["success"] is False
        assert result.get("policy") == "shared_write_requires_approval"
        # Fail closed: the shared skill content is untouched.
        assert json.loads(skill_view("org-runbook"))["content"] == "orig"


def test_agentops_mutation_without_active_backend_fails_closed():
    with use_runtime_context(_ctx()):
        result = json.loads(skill_manage(action="create", name="failopen", content="x"))
    assert result["success"] is False
    assert result.get("policy") == "agentops_skill_backend_required"


def test_shared_org_edit_allowed_with_approval_flag():
    backend = _seed_org()
    set_active_skill_backend(backend)
    with use_runtime_context(_ctx(metadata={"skill_write_approved": True})):
        result = json.loads(skill_manage(action="edit", name="org-runbook", content="approved"))
        assert result["success"] is True
        assert json.loads(skill_view("org-runbook"))["content"] == "approved"


def test_native_shared_project_create_and_edit_with_explicit_approval_params():
    backend = ScopedSkillBackend()
    set_active_skill_backend(backend)
    with use_runtime_context(_ctx(project_id="proj-a")):
        created = json.loads(
            skill_manage(
                action="create",
                name="project-runbook",
                scope="project",
                project_id="proj-a",
                allow_shared_write=True,
                content="project v1",
            )
        )
        edited = json.loads(
            skill_manage(
                action="edit",
                name="project-runbook",
                scope="project",
                project_id="proj-a",
                allow_shared_write=True,
                content="project v2",
            )
        )
        viewed = json.loads(skill_view("project-runbook"))

    assert created["success"] is True
    assert edited["success"] is True
    assert viewed["content"] == "project v2"


def test_pinned_delete_blocked_through_tool():
    backend = ScopedSkillBackend()
    backend.add_skill(
        name="keep", scope="user", org_id="acme", user_id="derek", content="x", pinned=True
    )
    set_active_skill_backend(backend)
    with use_runtime_context(_ctx()):
        result = json.loads(skill_manage(action="delete", name="keep"))
    assert result["success"] is False
    assert "pinned" in result["error"].lower()


def test_local_mode_context_does_not_route_to_backend():
    backend = ScopedSkillBackend()
    set_active_skill_backend(backend)
    # A local-mode context must use the default filesystem path, never the
    # scoped backend, even when one is bound for the process.
    with use_runtime_context(RuntimeContext(mode="local")):
        json.loads(skill_manage(action="create", name="x", content="not valid frontmatter"))
    assert backend.audit == []
