"""Tests for the M7 skill backend contract.

These prove the extended ``SkillBackend`` surface, the in-memory multi-tenant
``ScopedSkillBackend`` (the reference remote backend), and that the filesystem
``LocalSkillBackend`` keeps wrapping the native skill discovery/loading code.

Covered acceptance criteria:
* user A cannot list/load user B's private skill; org skills are shared
* deterministic precedence across bundled/org/project/user/runtime scopes
* linked-file reads through the scoped backend
* mutation policy: user-private allowed, shared org/project require approval
* pinned-delete guard + absorbed_into semantics on the scoped backend
* error/audit DTOs never leak another tenant's content or private local paths
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.runtime_backends import (
    BackendCapability,
    LocalSkillBackend,
    RuntimeBackendRegistry,
    SkillBackend,
)
from agent.runtime_context import RuntimeContext, use_runtime_context
from agent.runtime_skills import (
    SCOPE_PRECEDENCE,
    ScopedSkillBackend,
    SkillMutationNotAllowed,
    clear_active_skill_backend,
    get_active_skill_backend,
    set_active_skill_backend,
)


def _ctx(*, user_id=None, org_id="acme", project_id=None, conversation_id=None, **extra):
    return RuntimeContext(
        mode="agentops",
        org_id=org_id,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        backend_profile="compose-self-hosted",
        **extra,
    )


# ---------------------------------------------------------------------------
# Contract / registry
# ---------------------------------------------------------------------------


def test_scoped_backend_satisfies_skill_backend_protocol():
    backend = ScopedSkillBackend()
    assert isinstance(backend, SkillBackend)


def test_local_skill_backend_is_registry_default_and_protocol():
    registry = RuntimeBackendRegistry()
    backend = registry.get(BackendCapability.SKILL)
    assert isinstance(backend, LocalSkillBackend)
    assert isinstance(backend, SkillBackend)


def test_agentops_skill_backend_resolution_failure_binds_fail_closed_backend():
    from agent.agent_init import _bind_agentops_skill_backend

    clear_active_skill_backend()
    fallback = ScopedSkillBackend()
    set_active_skill_backend(fallback)
    ctx = _ctx(user_id="derek", org_id="acme")
    agent = SimpleNamespace(runtime_context=ctx)
    config = {"backends": {"capabilities": {"skill": "missing-remote"}}}

    _bind_agentops_skill_backend(agent, config)

    backend = get_active_skill_backend(ctx)
    assert backend is not None
    with pytest.raises(RuntimeError, match="skill backend unavailable"):
        backend.list_skills(ctx)


def test_compose_profile_binds_http_skill_backend():
    from agent.agent_init import _bind_agentops_skill_backend
    from agent.runtime_skill_http import HttpSkillBackend

    clear_active_skill_backend()
    ctx = _ctx(user_id="derek", org_id="acme")
    agent = SimpleNamespace(runtime_context=ctx)
    config = {
        "backends": {
            "options": {"skill": {"base_url": "https://skills.internal"}},
        }
    }

    _bind_agentops_skill_backend(agent, config)

    backend = get_active_skill_backend(ctx)
    assert isinstance(backend, HttpSkillBackend)
    clear_active_skill_backend()


def test_missing_remote_skill_backend_fails_closed_not_local_filesystem(tmp_path):
    from agent.agent_init import _bind_agentops_skill_backend
    from agent.runtime_backends import LocalSkillBackend
    from tools.skills_tool import skills_list

    clear_active_skill_backend()
    ctx = _ctx(user_id="derek", org_id="acme")
    agent = SimpleNamespace(runtime_context=ctx)
    # compose profile but no base_url configured — should fail-closed, not fall back to local
    config = {"backends": {"capabilities": {"skill": "compose-self-hosted"}}}

    _bind_agentops_skill_backend(agent, config)

    backend = get_active_skill_backend(ctx)
    assert not isinstance(backend, LocalSkillBackend)
    assert backend is not None

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        fs_skill = tmp_path / "fs-skill"
        fs_skill.mkdir(parents=True)
        (fs_skill / "SKILL.md").write_text(
            "---\nname: fs-skill\ndescription: d.\n---\n\n# fs-skill\n\nBody.\n"
        )
        with use_runtime_context(ctx):
            result = json.loads(skills_list())

    assert result["success"] is False
    assert "fs-skill" not in json.dumps(result)
    assert "skill backend unavailable" in result["error"]
    clear_active_skill_backend()


def test_agentops_without_explicit_skill_profile_fails_closed_not_local_filesystem(tmp_path):
    from agent.agent_init import _bind_agentops_skill_backend
    from agent.runtime_backends import LocalSkillBackend
    from tools.skills_tool import skill_view

    clear_active_skill_backend()
    ctx = RuntimeContext(mode="agentops", user_id="derek", org_id="acme")
    agent = SimpleNamespace(runtime_context=ctx)

    _bind_agentops_skill_backend(agent, {})

    backend = get_active_skill_backend(ctx)
    assert backend is not None
    assert not isinstance(backend, LocalSkillBackend)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        fs_skill = tmp_path / "fs-skill"
        fs_skill.mkdir(parents=True)
        (fs_skill / "SKILL.md").write_text(
            "---\nname: fs-skill\ndescription: d.\n---\n\n# fs-skill\n\nBody.\n"
        )
        with use_runtime_context(ctx):
            result = json.loads(skill_view("fs-skill"))

    assert result["success"] is False
    assert "Body." not in json.dumps(result)
    assert "skill backend unavailable" in result["error"]
    clear_active_skill_backend()


def test_scope_precedence_is_deterministic_and_documented():
    # Most specific / most ephemeral wins, bundled is the base layer.
    assert SCOPE_PRECEDENCE == ("runtime", "user", "project", "org", "bundled")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_user_a_cannot_list_or_load_user_b_private_skill():
    backend = ScopedSkillBackend()
    backend.add_skill(name="derek-secret", scope="user", org_id="acme", user_id="derek", content="A only")
    backend.add_skill(name="alex-secret", scope="user", org_id="acme", user_id="alex", content="B only")

    derek = _ctx(user_id="derek")
    alex = _ctx(user_id="alex")

    derek_names = {s["name"] for s in backend.list_skills(derek)}
    alex_names = {s["name"] for s in backend.list_skills(alex)}

    assert "derek-secret" in derek_names
    assert "derek-secret" not in alex_names
    assert "alex-secret" in alex_names
    assert "alex-secret" not in derek_names

    # A cross-tenant load fails closed and does not echo the other user's content.
    blocked = backend.load_skill(alex, "derek-secret")
    assert blocked["success"] is False
    assert "A only" not in json.dumps(blocked)


def test_org_skill_is_shared_within_org_but_not_across_orgs():
    backend = ScopedSkillBackend()
    backend.add_skill(name="org-runbook", scope="org", org_id="acme", content="shared runbook")

    acme_a = _ctx(user_id="derek", org_id="acme")
    acme_b = _ctx(user_id="alex", org_id="acme")
    other_org = _ctx(user_id="mallory", org_id="globex")

    assert backend.load_skill(acme_a, "org-runbook")["content"] == "shared runbook"
    assert backend.load_skill(acme_b, "org-runbook")["content"] == "shared runbook"
    assert backend.load_skill(other_org, "org-runbook")["success"] is False


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_user_private_overrides_org_overrides_bundled_for_same_name():
    backend = ScopedSkillBackend()
    backend.add_skill(name="deploy", scope="bundled", content="bundled deploy")
    backend.add_skill(name="deploy", scope="org", org_id="acme", content="org deploy")
    backend.add_skill(name="deploy", scope="user", org_id="acme", user_id="derek", content="derek deploy")

    derek = _ctx(user_id="derek", org_id="acme")
    other_acme = _ctx(user_id="alex", org_id="acme")
    outsider = _ctx(user_id="mallory", org_id="globex")

    assert backend.load_skill(derek, "deploy")["content"] == "derek deploy"
    assert backend.load_skill(other_acme, "deploy")["content"] == "org deploy"
    assert backend.load_skill(outsider, "deploy")["content"] == "bundled deploy"

    # A listing collapses to one entry per name at the winning scope.
    derek_deploy = [s for s in backend.list_skills(derek) if s["name"] == "deploy"]
    assert len(derek_deploy) == 1
    assert derek_deploy[0]["scope"] == "user"


def test_mutation_target_uses_scope_precedence_not_insertion_order():
    backend = ScopedSkillBackend(default_shared_write_approved=True)
    # Lower-precedence org record inserted before higher-precedence user record.
    backend.add_skill(name="deploy", scope="org", org_id="acme", content="org deploy")
    backend.add_skill(name="deploy", scope="user", org_id="acme", user_id="derek", content="user deploy")
    ctx = _ctx(user_id="derek", org_id="acme")

    result = backend.manage_skill(ctx, action="edit", name="deploy", content="edited user")

    assert result["success"] is True
    assert backend.load_skill(ctx, "deploy")["content"] == "edited user"
    org_only = _ctx(user_id="alex", org_id="acme")
    assert backend.load_skill(org_only, "deploy")["content"] == "org deploy"


def test_runtime_scope_requires_tenant_identity_beyond_conversation_id():
    backend = ScopedSkillBackend()
    backend.add_skill(
        name="ephemeral",
        scope="runtime",
        org_id="acme",
        user_id="derek",
        conversation_id="same-thread",
        content="acme runtime",
    )

    same_tenant = _ctx(user_id="derek", org_id="acme", conversation_id="same-thread")
    cross_org_collision = _ctx(user_id="derek", org_id="globex", conversation_id="same-thread")
    cross_user_collision = _ctx(user_id="alex", org_id="acme", conversation_id="same-thread")
    incomplete_identity = RuntimeContext(mode="agentops", conversation_id="same-thread")

    assert backend.load_skill(same_tenant, "ephemeral")["content"] == "acme runtime"
    assert backend.load_skill(cross_org_collision, "ephemeral")["success"] is False
    assert backend.load_skill(cross_user_collision, "ephemeral")["success"] is False
    assert backend.load_skill(incomplete_identity, "ephemeral")["success"] is False


def test_active_skill_backend_is_context_keyed_without_sequential_leakage():
    clear_active_skill_backend()
    first_backend = ScopedSkillBackend()
    second_backend = ScopedSkillBackend()
    first_ctx = _ctx(user_id="derek", org_id="acme", conversation_id="thread-a")
    second_ctx = _ctx(user_id="alex", org_id="globex", conversation_id="thread-b")

    set_active_skill_backend(first_backend, context=first_ctx)
    set_active_skill_backend(second_backend, context=second_ctx)

    assert get_active_skill_backend(first_ctx) is first_backend
    assert get_active_skill_backend(second_ctx) is second_backend

    clear_active_skill_backend()


def test_active_skill_backend_is_context_keyed_without_concurrent_leakage():
    from agent.runtime_context import use_runtime_context

    clear_active_skill_backend()
    first_backend = ScopedSkillBackend()
    second_backend = ScopedSkillBackend()
    first_ctx = _ctx(user_id="derek", org_id="acme", conversation_id="thread-a")
    second_ctx = _ctx(user_id="alex", org_id="globex", conversation_id="thread-b")
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker(ctx, backend):
        with use_runtime_context(ctx):
            set_active_skill_backend(backend)
            barrier.wait(timeout=5)
            results.append(get_active_skill_backend() is backend)

    threads = [
        threading.Thread(target=worker, args=(first_ctx, first_backend)),
        threading.Thread(target=worker, args=(second_ctx, second_backend)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [True, True]
    clear_active_skill_backend()


# ---------------------------------------------------------------------------
# Linked files / readiness metadata
# ---------------------------------------------------------------------------


def test_linked_file_load_through_scoped_backend():
    backend = ScopedSkillBackend()
    backend.add_skill(
        name="axolotl",
        scope="org",
        org_id="acme",
        content="main",
        files={"references/api.md": "API DOCS"},
        readiness_status="setup_needed",
        setup_needed=True,
    )
    ctx = _ctx(user_id="derek", org_id="acme")

    main = backend.load_skill(ctx, "axolotl")
    assert main["linked_files"]["references"] == ["references/api.md"]
    assert main["readiness_status"] == "setup_needed"
    assert main["setup_needed"] is True

    linked = backend.load_skill(ctx, "axolotl", file_path="references/api.md")
    assert linked["success"] is True
    assert linked["content"] == "API DOCS"


# ---------------------------------------------------------------------------
# Mutation policy
# ---------------------------------------------------------------------------


def test_user_private_mutation_is_allowed():
    backend = ScopedSkillBackend()
    ctx = _ctx(user_id="derek", org_id="acme")

    result = backend.manage_skill(
        ctx, action="create", name="my-flow", scope="user", content="private steps"
    )
    assert result["success"] is True
    assert backend.load_skill(ctx, "my-flow")["content"] == "private steps"


def test_shared_scope_mutation_requires_approval_and_fails_closed():
    backend = ScopedSkillBackend()
    ctx = _ctx(user_id="derek", org_id="acme")

    result = backend.manage_skill(
        ctx, action="create", name="org-policy", scope="org", content="org steps"
    )
    assert result["success"] is False
    # No side effect: the skill was never written.
    assert backend.load_skill(ctx, "org-policy")["success"] is False
    assert any(a["action"] == "create" and not a["allowed"] for a in backend.audit)


def test_shared_scope_mutation_allowed_with_metadata_approval_flag():
    backend = ScopedSkillBackend()
    ctx = _ctx(user_id="derek", org_id="acme", metadata={"skill_write_approved": True})

    result = backend.manage_skill(
        ctx, action="create", name="org-policy", scope="org", content="org steps"
    )
    assert result["success"] is True
    other = _ctx(user_id="alex", org_id="acme")
    assert backend.load_skill(other, "org-policy")["content"] == "org steps"


def test_shared_scope_mutation_allowed_with_explicit_flag():
    backend = ScopedSkillBackend()
    ctx = _ctx(user_id="derek", org_id="acme")

    result = backend.manage_skill(
        ctx,
        action="create",
        name="proj-flow",
        scope="project",
        project_id="runtime-mvp",
        content="proj steps",
        allow_shared_write=True,
    )
    assert result["success"] is True


def test_bundled_scope_is_read_only():
    backend = ScopedSkillBackend()
    backend.add_skill(name="bundled-skill", scope="bundled", content="base")
    ctx = _ctx(user_id="derek", org_id="acme", metadata={"skill_write_approved": True})

    result = backend.manage_skill(
        ctx, action="edit", name="bundled-skill", scope="bundled", content="hacked"
    )
    assert result["success"] is False
    assert backend.load_skill(ctx, "bundled-skill")["content"] == "base"


def test_pinned_skill_delete_guard():
    backend = ScopedSkillBackend()
    backend.add_skill(name="keep-me", scope="user", org_id="acme", user_id="derek", content="x", pinned=True)
    ctx = _ctx(user_id="derek", org_id="acme")

    result = backend.manage_skill(ctx, action="delete", name="keep-me", scope="user")
    assert result["success"] is False
    assert "pinned" in result["error"].lower()
    assert backend.load_skill(ctx, "keep-me")["success"] is True


def test_absorbed_into_requires_existing_visible_target():
    backend = ScopedSkillBackend()
    backend.add_skill(name="old", scope="user", org_id="acme", user_id="derek", content="x")
    ctx = _ctx(user_id="derek", org_id="acme")

    missing = backend.manage_skill(
        ctx, action="delete", name="old", scope="user", absorbed_into="does-not-exist"
    )
    assert missing["success"] is False

    backend.add_skill(name="umbrella", scope="user", org_id="acme", user_id="derek", content="y")
    ok = backend.manage_skill(
        ctx, action="delete", name="old", scope="user", absorbed_into="umbrella"
    )
    assert ok["success"] is True


def test_mutation_raises_typed_error_when_requested():
    backend = ScopedSkillBackend(raise_on_denied=True)
    ctx = _ctx(user_id="derek", org_id="acme")
    with pytest.raises(SkillMutationNotAllowed):
        backend.manage_skill(ctx, action="create", name="org-x", scope="org", content="x")


def test_errors_do_not_leak_private_paths_or_secret_setup_values():
    backend = ScopedSkillBackend()
    backend.add_skill(
        name="secret-skill",
        scope="user",
        org_id="acme",
        user_id="derek",
        content="SENTINEL_PRIVATE_BODY",
        files={"references/key.md": "SENTINEL_PRIVATE_BODY"},
    )
    intruder = _ctx(user_id="alex", org_id="acme")

    payload = json.dumps(backend.load_skill(intruder, "secret-skill"))
    assert "SENTINEL_PRIVATE_BODY" not in payload


# ---------------------------------------------------------------------------
# LocalSkillBackend filesystem wrapping
# ---------------------------------------------------------------------------


def _make_skill(skills_dir, name, body="Step 1.", extra_files=None):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Desc for {name}.\n---\n\n# {name}\n\n{body}\n"
    )
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return skill_dir


def test_local_skill_backend_wraps_filesystem_discovery(tmp_path):
    backend = LocalSkillBackend()
    ctx = _ctx(user_id="derek", org_id="acme")
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "fs-skill", extra_files={"references/api.md": "REF BODY"})

        names = {s["name"] for s in backend.list_skills(ctx)}
        assert "fs-skill" in names

        main = backend.load_skill(ctx, "fs-skill")
        assert main["success"] is True
        assert "Step 1." in main["content"]

        linked = backend.load_skill(ctx, "fs-skill", file_path="references/api.md")
        assert linked["content"] == "REF BODY"
