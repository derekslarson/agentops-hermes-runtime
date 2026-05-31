"""Scoped skill backend for AgentOps runtime contexts (M7).

This module provides:

* :class:`ScopedSkillBackend` — an in-memory, multi-tenant reference
  implementation of :class:`~agent.runtime_backends.SkillBackend`. It models the
  scope ladder (bundled / org / project / user-private / runtime), deterministic
  precedence, linked-file reads, readiness metadata, and mutation policy. It is
  the "remote" backend used in tests and is suitable as the contract that real
  remote control-plane adapters satisfy.
* A process-level holder (:func:`set_active_skill_backend` /
  :func:`get_active_skill_backend`) that the native skill surfaces consult so
  ``skills_list``/``skill_view``/``skill_manage`` route through the selected
  backend when a :class:`RuntimeContext` selects an AgentOps profile, while the
  default single-user local path is preserved when no distributed mode is set.

No cloud/provider-specific names appear here; the backing store is opaque.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Mapping

from agent.runtime_backends import BackendCapability

if TYPE_CHECKING:
    from agent.runtime_backends import RuntimeBackendRegistry, SkillBackend
    from agent.runtime_context import RuntimeContext

# Most specific / most ephemeral wins; bundled is the shared base layer. This
# order is the single source of truth for cross-source skill resolution and is
# asserted by tests so auto-loaded channel/topic skills resolve deterministically.
SCOPE_PRECEDENCE: tuple[str, ...] = ("runtime", "user", "project", "org", "bundled")
SHARED_SCOPES = frozenset({"org", "project"})
_LINKED_SUBDIRS = ("references", "templates", "scripts", "assets")
_APPROVAL_METADATA_KEY = "skill_write_approved"


class SkillMutationNotAllowed(PermissionError):
    """Raised (when configured) if a shared-scope mutation lacks approval."""


def _precedence_index(scope: str) -> int:
    try:
        return SCOPE_PRECEDENCE.index(scope)
    except ValueError:
        return len(SCOPE_PRECEDENCE)


class ScopedSkillBackend:
    """In-memory multi-tenant skill backend with scope-aware policy.

    Each skill record carries a ``scope`` and the identity fields that define
    who may see it. Visibility and precedence are derived purely from the active
    :class:`RuntimeContext`, so one user can never list or load another user's
    private skill, and shared org/project skills are visible to peers in the
    same org/project. Error payloads never echo another tenant's content.
    """

    def __init__(
        self,
        *,
        default_shared_write_approved: bool = False,
        raise_on_denied: bool = False,
    ) -> None:
        self._records: list[dict[str, Any]] = []
        self._default_shared_write_approved = default_shared_write_approved
        self._raise_on_denied = raise_on_denied
        self._lock = threading.RLock()
        self.audit: list[dict[str, Any]] = []
        self.load_count = 0

    # -- seeding ---------------------------------------------------------------

    def add_skill(
        self,
        *,
        name: str,
        scope: str,
        content: str = "",
        description: str = "",
        category: str | None = None,
        files: Mapping[str, str] | None = None,
        org_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        pinned: bool = False,
        readiness_status: str = "available",
        setup_needed: bool = False,
        tags: list[str] | None = None,
        required_environment_variables: list[Mapping[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            self._records.append(
                {
                    "name": name,
                    "scope": scope,
                    "content": content,
                    "description": description,
                    "category": category,
                    "files": dict(files or {}),
                    "org_id": org_id,
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "pinned": pinned,
                    "readiness_status": readiness_status,
                    "setup_needed": setup_needed,
                    "tags": list(tags or []),
                    "required_environment_variables": list(required_environment_variables or []),
                }
            )

    # -- visibility / precedence ----------------------------------------------

    @staticmethod
    def _visible(record: Mapping[str, Any], context: "RuntimeContext | None") -> bool:
        scope = record["scope"]
        if scope == "bundled":
            return True
        org = getattr(context, "org_id", None) if context else None
        if scope == "org":
            return org is not None and record["org_id"] == org
        if scope == "project":
            project = getattr(context, "project_id", None) if context else None
            return (
                org is not None
                and project is not None
                and record["org_id"] == org
                and record["project_id"] == project
            )
        if scope == "user":
            user = getattr(context, "user_id", None) if context else None
            return (
                org is not None
                and user is not None
                and record["org_id"] == org
                and record["user_id"] == user
            )
        if scope == "runtime":
            conversation = getattr(context, "conversation_id", None) if context else None
            if conversation is None or record["conversation_id"] != conversation:
                return False
            for field in ("org_id", "workspace_id", "project_id", "user_id"):
                context_value = getattr(context, field, None) if context else None
                record_value = record.get(field)
                # Runtime-scope records are ephemeral but still tenant-scoped:
                # missing identity must not broaden access to a record that
                # carries org/user/project/workspace identity, and broad records
                # must not silently shadow a more specific runtime context.
                if context_value != record_value:
                    return False
            return True
        return False

    def _visible_records(self, context: "RuntimeContext | None") -> list[dict[str, Any]]:
        return [record for record in self._records if self._visible(record, context)]

    def _resolve(self, context: "RuntimeContext | None", name: str) -> dict[str, Any] | None:
        candidates = [r for r in self._visible_records(context) if r["name"] == name]
        if not candidates:
            return None
        return min(candidates, key=lambda r: _precedence_index(r["scope"]))

    # -- read surface ----------------------------------------------------------

    def list_skills(
        self,
        context: "RuntimeContext | None",
        *,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            winners: dict[str, dict[str, Any]] = {}
            for record in self._visible_records(context):
                name = record["name"]
                current = winners.get(name)
                if current is None or _precedence_index(record["scope"]) < _precedence_index(
                    current["scope"]
                ):
                    winners[name] = record
            entries = [
                {
                    "name": record["name"],
                    "description": record["description"],
                    "category": record["category"],
                    "scope": record["scope"],
                }
                for record in winners.values()
                if category is None or record["category"] == category
            ]
        return sorted(entries, key=lambda entry: (entry["category"] or "", entry["name"]))

    def load_skill(
        self,
        context: "RuntimeContext | None",
        name: str,
        *,
        file_path: str | None = None,
        preprocess: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self.load_count += 1
            record = self._resolve(context, name)
            if record is None:
                # Fail closed without confirming whether the name exists for
                # another tenant, and never echo private content/paths.
                return {"success": False, "error": f"Skill '{name}' not found."}
            if file_path is not None:
                return self._load_linked_file(record, name, file_path)
            return self._main_payload(record)

    @staticmethod
    def _linked_files_summary(files: Mapping[str, str]) -> dict[str, list[str]]:
        summary: dict[str, list[str]] = {}
        for rel in sorted(files):
            top = rel.split("/", 1)[0]
            bucket = top if top in _LINKED_SUBDIRS else "other"
            summary.setdefault(bucket, []).append(rel)
        return summary

    def _main_payload(self, record: Mapping[str, Any]) -> dict[str, Any]:
        summary = self._linked_files_summary(record["files"])
        return {
            "success": True,
            "name": record["name"],
            "scope": record["scope"],
            "description": record["description"],
            "category": record["category"],
            "content": record["content"],
            "tags": list(record["tags"]),
            "linked_files": summary or None,
            "required_environment_variables": list(record["required_environment_variables"]),
            "missing_required_environment_variables": [],
            "setup_needed": record["setup_needed"],
            "readiness_status": record["readiness_status"],
        }

    @staticmethod
    def _load_linked_file(record: Mapping[str, Any], name: str, file_path: str) -> dict[str, Any]:
        files = record["files"]
        if file_path not in files:
            return {
                "success": False,
                "error": f"File '{file_path}' not found in skill '{name}'.",
                "available_files": sorted(files),
            }
        return {
            "success": True,
            "name": name,
            "file": file_path,
            "content": files[file_path],
        }

    # -- mutation surface ------------------------------------------------------

    def manage_skill(
        self,
        context: "RuntimeContext | None",
        *,
        action: str,
        name: str,
        scope: str | None = None,
        content: str | None = None,
        category: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        absorbed_into: str | None = None,
        project_id: str | None = None,
        allow_shared_write: bool = False,
        **_extra: Any,
    ) -> dict[str, Any]:
        with self._lock:
            candidates = [
                r
                for r in self._visible_records(context)
                if r["name"] == name and (scope is None or r["scope"] == scope)
            ]
            existing = min(candidates, key=lambda r: _precedence_index(r["scope"])) if candidates else None
            target_scope = scope or (existing["scope"] if existing else "user")

            denial = self._authorize(context, target_scope, allow_shared_write)
            self.audit.append(
                {"action": action, "name": name, "scope": target_scope, "allowed": denial is None}
            )
            if denial is not None:
                if self._raise_on_denied:
                    raise SkillMutationNotAllowed(denial["error"])
                return denial

            return self._apply_mutation(
                context,
                action=action,
                name=name,
                target_scope=target_scope,
                existing=existing,
                content=content,
                category=category,
                file_path=file_path,
                file_content=file_content,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
                absorbed_into=absorbed_into,
                project_id=project_id,
            )

    def _authorize(
        self,
        context: "RuntimeContext | None",
        scope: str,
        allow_shared_write: bool,
    ) -> dict[str, Any] | None:
        """Return a denial payload, or None when the mutation is permitted."""
        if scope == "bundled":
            return {
                "success": False,
                "error": "Bundled skills are read-only and cannot be mutated.",
                "policy": "bundled_read_only",
            }
        if scope in SHARED_SCOPES:
            if allow_shared_write or self._default_shared_write_approved or self._metadata_approved(context):
                return None
            return {
                "success": False,
                "error": (
                    f"Writing a shared '{scope}' skill requires approval. "
                    "Set RuntimeContext.metadata['skill_write_approved'] or pass "
                    "allow_shared_write=True."
                ),
                "policy": "shared_write_requires_approval",
            }
        return None

    @staticmethod
    def _metadata_approved(context: "RuntimeContext | None") -> bool:
        metadata = getattr(context, "metadata", None) if context else None
        if not isinstance(metadata, Mapping):
            return False
        return bool(metadata.get(_APPROVAL_METADATA_KEY))

    def _apply_mutation(
        self,
        context: "RuntimeContext | None",
        *,
        action: str,
        name: str,
        target_scope: str,
        existing: dict[str, Any] | None,
        content: str | None,
        category: str | None,
        file_path: str | None,
        file_content: str | None,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool,
        absorbed_into: str | None,
        project_id: str | None,
    ) -> dict[str, Any]:
        if action == "create":
            if existing is not None:
                return {"success": False, "error": f"A skill named '{name}' already exists."}
            self.add_skill(
                name=name,
                scope=target_scope,
                content=content or "",
                category=category,
                org_id=getattr(context, "org_id", None) if context else None,
                workspace_id=getattr(context, "workspace_id", None) if context else None,
                project_id=project_id or (getattr(context, "project_id", None) if context else None),
                user_id=getattr(context, "user_id", None) if context else None,
                conversation_id=getattr(context, "conversation_id", None) if context else None,
            )
            return {"success": True, "message": f"Skill '{name}' created.", "scope": target_scope}

        if existing is None:
            return {"success": False, "error": f"Skill '{name}' not found."}

        if action == "edit":
            existing["content"] = content or ""
            return {"success": True, "message": f"Skill '{name}' updated."}

        if action == "patch":
            if not old_string or old_string not in existing["content"]:
                return {"success": False, "error": "old_string not found in skill content."}
            count = existing["content"].count(old_string) if replace_all else 1
            existing["content"] = existing["content"].replace(
                old_string, new_string or "", -1 if replace_all else 1
            )
            return {"success": True, "message": f"Patched skill '{name}' ({count} replacement(s))."}

        if action == "write_file":
            if not file_path:
                return {"success": False, "error": "file_path is required for write_file."}
            existing["files"][file_path] = file_content or ""
            return {"success": True, "message": f"File '{file_path}' written to skill '{name}'."}

        if action == "remove_file":
            if not file_path or file_path not in existing["files"]:
                return {"success": False, "error": f"File '{file_path}' not found in skill '{name}'."}
            del existing["files"][file_path]
            return {"success": True, "message": f"File '{file_path}' removed from skill '{name}'."}

        if action == "delete":
            if existing.get("pinned"):
                return {
                    "success": False,
                    "error": f"Skill '{name}' is pinned and cannot be deleted.",
                }
            if absorbed_into and self._resolve(context, absorbed_into) is None:
                return {
                    "success": False,
                    "error": f"absorbed_into='{absorbed_into}' does not exist.",
                }
            self._records.remove(existing)
            message = f"Skill '{name}' deleted."
            if absorbed_into:
                message += f" Content absorbed into '{absorbed_into}'."
            return {"success": True, "message": message}

        return {"success": False, "error": f"Unknown action '{action}'."}


# ---------------------------------------------------------------------------
# RuntimeContext-safe active backend holder (consulted by native skill surfaces)
# ---------------------------------------------------------------------------

_active_lock = threading.RLock()
_active_backend_default: ContextVar["SkillBackend | None"] = ContextVar(
    "hermes_active_skill_backend_default",
    default=None,
)
_active_backends_by_context: dict[tuple[Any, ...], "SkillBackend | None"] = {}


def _active_context_key(context: "RuntimeContext | None") -> tuple[Any, ...] | None:
    if context is None:
        return None
    return (
        getattr(context, "mode", None),
        getattr(context, "org_id", None),
        getattr(context, "workspace_id", None),
        getattr(context, "project_id", None),
        getattr(context, "user_id", None),
        getattr(context, "conversation_id", None),
        getattr(context, "agent_profile_id", None),
        getattr(context, "run_id", None),
        getattr(context, "job_id", None),
        getattr(context, "backend_profile", None),
    )


def set_active_skill_backend(
    backend: "SkillBackend | None",
    context: "RuntimeContext | None" = None,
) -> None:
    """Bind the skill backend native surfaces route through in AgentOps mode.

    When a RuntimeContext is supplied (or currently active), the binding is keyed
    to that context's tenant/run identity. A no-context binding remains as a
    compatibility fallback for local tests and single-context callers, but
    context-specific bindings always win and do not leak across concurrent runs.
    """
    if context is None:
        from agent.runtime_context import get_runtime_context_for_surface

        context = get_runtime_context_for_surface("skills")
    key = _active_context_key(context)
    if key is None:
        _active_backend_default.set(backend)
        return
    with _active_lock:
        _active_backends_by_context[key] = backend


def get_active_skill_backend(context: "RuntimeContext | None" = None) -> "SkillBackend | None":
    if context is None:
        from agent.runtime_context import get_runtime_context_for_surface

        context = get_runtime_context_for_surface("skills")
    key = _active_context_key(context)
    if key is None:
        return _active_backend_default.get()
    with _active_lock:
        if key in _active_backends_by_context:
            return _active_backends_by_context[key]
    return _active_backend_default.get()


def clear_active_skill_backend() -> None:
    _active_backend_default.set(None)
    with _active_lock:
        _active_backends_by_context.clear()


def register_scoped_skill_backend(
    registry: "RuntimeBackendRegistry",
    *,
    profile: str,
    backend: "ScopedSkillBackend | None" = None,
) -> ScopedSkillBackend:
    """Register a :class:`ScopedSkillBackend` factory for ``profile``.

    A single shared backend instance is returned and reused by the factory so
    seeded skills survive registry instance caching.
    """
    shared = backend if backend is not None else ScopedSkillBackend()

    def factory(options: Mapping[str, Any]) -> ScopedSkillBackend:
        return shared

    registry.register(BackendCapability.SKILL, factory, profile=profile)
    return shared
