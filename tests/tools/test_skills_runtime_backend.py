"""skills_list / skill_view routing through a scoped backend (M7).

Proves the native read surfaces route through the selected skill backend when a
RuntimeContext picks an AgentOps profile, enforce tenant isolation, keep the
auto-load path read-only, and fall back to the default local filesystem path
when no distributed mode/backend is selected.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.runtime_context import RuntimeContext, use_runtime_context
from agent.runtime_skills import (
    ScopedSkillBackend,
    clear_active_skill_backend,
    set_active_skill_backend,
)
from tools.skills_tool import skill_view, skills_list

_MUTATING = {"create", "edit", "patch", "delete", "write_file", "remove_file"}


@pytest.fixture(autouse=True)
def _reset_active_backend():
    clear_active_skill_backend()
    yield
    clear_active_skill_backend()


def _ctx(user_id, org_id="acme"):
    return RuntimeContext(
        mode="agentops",
        org_id=org_id,
        user_id=user_id,
        conversation_id="thread-1",
        backend_profile="compose-self-hosted",
    )


def _seed():
    backend = ScopedSkillBackend()
    backend.add_skill(name="derek-private", scope="user", org_id="acme", user_id="derek", content="derek body")
    backend.add_skill(name="org-runbook", scope="org", org_id="acme", content="org body")
    return backend


def test_skills_list_routes_through_backend_with_isolation():
    set_active_skill_backend(_seed())
    with use_runtime_context(_ctx("derek")):
        derek = json.loads(skills_list())
    with use_runtime_context(_ctx("alex")):
        alex = json.loads(skills_list())

    derek_names = {s["name"] for s in derek["skills"]}
    alex_names = {s["name"] for s in alex["skills"]}

    assert "derek-private" in derek_names
    assert "org-runbook" in derek_names
    assert "derek-private" not in alex_names
    assert "org-runbook" in alex_names


def test_skill_view_routes_and_blocks_cross_tenant_load():
    set_active_skill_backend(_seed())
    with use_runtime_context(_ctx("derek")):
        ok = json.loads(skill_view("derek-private"))
        assert ok["success"] is True
        assert ok["content"] == "derek body"

    with use_runtime_context(_ctx("alex")):
        blocked = json.loads(skill_view("derek-private"))
        assert blocked["success"] is False
        assert "derek body" not in json.dumps(blocked)


def test_autoloaded_skill_view_is_read_only():
    backend = _seed()
    set_active_skill_backend(backend)
    # The preload/autoload path calls skill_view(preprocess=False); it must
    # resolve through the scoped backend without any mutation occurring.
    with use_runtime_context(_ctx("derek")):
        loaded = json.loads(skill_view("org-runbook", preprocess=False))

    assert loaded["success"] is True
    assert backend.load_count >= 1
    assert all(entry["action"] not in _MUTATING for entry in backend.audit)


def test_local_default_path_preserved_without_context(tmp_path):
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skill_dir = tmp_path / "fs-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fs-skill\ndescription: d.\n---\n\n# fs-skill\n\nBody.\n"
        )
        listing = json.loads(skills_list())
        view = json.loads(skill_view("fs-skill"))

    assert any(s["name"] == "fs-skill" for s in listing["skills"])
    assert "Body." in view["content"]


def test_agentops_without_active_backend_falls_back_to_local(tmp_path):
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skill_dir = tmp_path / "fs-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fs-skill\ndescription: d.\n---\n\n# fs-skill\n\nBody.\n"
        )
        with use_runtime_context(_ctx("derek")):
            listing = json.loads(skills_list())

    assert any(s["name"] == "fs-skill" for s in listing["skills"])
