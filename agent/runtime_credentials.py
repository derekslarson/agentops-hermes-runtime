"""RuntimeContext-scoped credential resolution helpers.

This module is the M9 bridge between Hermes code that needs a secret and the
pluggable ``CredentialResolver``/``SecretStore`` backend contracts. Callers ask
for a capability/ref pair (for example ``model:openrouter`` + ``default``), the
credential resolver maps that logical request to an opaque secret reference, and
only the narrow process/tool path receives the revealed value.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext


def _context_key(context: RuntimeContext | None) -> tuple[Any, ...] | None:
    if context is None:
        return None
    return (
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.agent_profile_id,
        context.project_id,
        context.permissions_ref,
        context.backend_profile,
    )


_ACTIVE_CREDENTIAL_BROKERS: dict[tuple[Any, ...] | None, "RuntimeCredentialBroker"] = {}


def set_active_credential_broker(
    broker: "RuntimeCredentialBroker | None",
    *,
    context: RuntimeContext | None = None,
) -> None:
    """Bind a RuntimeCredentialBroker for native credential call paths."""

    key = _context_key(context)
    if broker is None:
        _ACTIVE_CREDENTIAL_BROKERS.pop(key, None)
    else:
        _ACTIVE_CREDENTIAL_BROKERS[key] = broker


def get_active_credential_broker(context: RuntimeContext | None = None) -> "RuntimeCredentialBroker | None":
    return _ACTIVE_CREDENTIAL_BROKERS.get(_context_key(context)) or _ACTIVE_CREDENTIAL_BROKERS.get(None)


class CredentialResolutionError(RuntimeError):
    """Raised when a scoped credential cannot be resolved safely."""


@dataclass(frozen=True)
class CredentialHandle:
    """A resolved credential with redacted string/repr behavior.

    The secret value is intentionally accessible only through ``reveal()`` so
    casual logging, transcript serialization, and assertion diffs do not include
    the raw secret by default.
    """

    capability: str
    ref: str
    secret_ref: str
    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - exercised through tests via repr()
        return (
            "CredentialHandle("
            f"capability={self.capability!r}, ref={self.ref!r}, "
            f"secret_ref={self.secret_ref!r}, value='[REDACTED]')"
        )

    __str__ = __repr__


class RuntimeCredentialBroker:
    """Resolve credential refs through the active runtime backend registry."""

    def __init__(self, registry: RuntimeBackendRegistry | None = None) -> None:
        self._registry = registry or RuntimeBackendRegistry()

    @staticmethod
    def _request_key(capability: str, ref: str) -> str:
        cap = (capability or "").strip()
        name = (ref or "").strip()
        if not cap or not name:
            raise CredentialResolutionError("credential capability and ref are required")
        return f"{cap}/{name}"

    def _audit(
        self,
        context: RuntimeContext | None,
        *,
        capability: str,
        ref: str,
        secret_ref: str | None,
        success: bool,
        error: str | None = None,
    ) -> None:
        event: dict[str, object] = {
            "action": "credential.resolve",
            "capability": capability,
            "ref": ref,
            # Opaque backend refs are not raw secrets; audit keeps them so usage
            # can be traced without storing the revealed value.
            "resolved_ref": secret_ref,
            "success": success,
        }
        if error:
            event["error"] = error
        try:
            audit = self._registry.get(BackendCapability.AUDIT, context)
            audit.record(context, event)
        except Exception:
            # Audit must never make credential resolution leak or crash a caller
            # that already has a deterministic success/failure outcome.
            return

    def resolve(self, context: RuntimeContext | None, *, capability: str, ref: str) -> CredentialHandle:
        request_key = self._request_key(capability, ref)
        credential_resolver = self._registry.get(BackendCapability.CREDENTIAL, context)
        secret_store = self._registry.get(BackendCapability.SECRET, context)

        secret_ref = credential_resolver.resolve(context, request_key)
        if not secret_ref:
            message = "credential ref not available"
            self._audit(
                context,
                capability=capability,
                ref=ref,
                secret_ref=None,
                success=False,
                error=message,
            )
            raise CredentialResolutionError(message)

        value = secret_store.get_secret(context, secret_ref)
        if value is None:
            message = "credential secret not available"
            self._audit(
                context,
                capability=capability,
                ref=ref,
                secret_ref=secret_ref,
                success=False,
                error=message,
            )
            raise CredentialResolutionError(message)

        self._audit(context, capability=capability, ref=ref, secret_ref=secret_ref, success=True)
        return CredentialHandle(capability=capability, ref=ref, secret_ref=secret_ref, _value=value)

    @contextmanager
    def scoped_env(
        self,
        context: RuntimeContext | None,
        env_credentials: Mapping[str, tuple[str, str]],
    ) -> Iterator[dict[str, CredentialHandle]]:
        """Expose resolved secrets as environment variables for one block only.

        This preserves existing provider/tool code that reads conventional env
        vars while keeping the raw values out of ambient process state before
        and after the narrow execution path.
        """

        handles: dict[str, CredentialHandle] = {}
        previous: dict[str, str | None] = {}
        try:
            for env_var, (capability, ref) in env_credentials.items():
                handle = self.resolve(context, capability=capability, ref=ref)
                handles[env_var] = handle
                previous[env_var] = os.environ.get(env_var)
                os.environ[env_var] = handle.reveal()
            yield handles
        finally:
            for env_var, old_value in previous.items():
                if old_value is None:
                    os.environ.pop(env_var, None)
                else:
                    os.environ[env_var] = old_value
