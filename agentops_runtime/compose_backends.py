"""Compose self-hosted runtime backend wiring (M12).

Registers the existing provider-neutral HTTP adapters (memory, cron, artifact,
audit) for the ``compose-self-hosted`` deployment profile, deriving connection
settings from AgentOps compose env/config. The wiring is intentionally narrow:

* It reuses the adapters' own ``register_http_*`` helpers, so the registry keeps
  the single backend-selection seam and the adapters keep owning their request
  contracts.
* Connection URLs come from a control-plane API URL (``AGENTOPS_API_URL``) with
  optional per-capability overrides. No cloud/database/provider names leak into
  this module.
* It fails closed: a missing URL, or one carrying credentials, query, or
  fragment, raises before any backend is registered.
* Only a control-plane bearer token (``AGENTOPS_RUNTIME_TOKEN``) is accepted, and
  only as an option/header token for the HTTP adapters — never embedded in a URL.
  App/integration secrets present in the environment are never copied into
  registry options or payloads.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Mapping

from agent.runtime_artifacts_audit import (
    register_http_artifact_backend,
    register_http_audit_backend,
)
from agent.runtime_backends import BackendCapability, REQUIRED_CAPABILITIES, RuntimeBackendRegistry
from agent.runtime_cron_http import register_http_cron_backend
from agent.runtime_memory_http import register_http_memory_backend
from agent.runtime_memory_record_http import register_http_memory_record_backend

_COMPOSE_PROFILE = "compose-self-hosted"
_API_URL_ENV = "AGENTOPS_API_URL"
_TOKEN_ENV = "AGENTOPS_RUNTIME_TOKEN"
_CAPABILITY_URL_ENV: dict[BackendCapability, str] = {
    BackendCapability.MEMORY: "AGENTOPS_MEMORY_URL",
    BackendCapability.DEEP_MEMORY: "AGENTOPS_DEEP_MEMORY_URL",
    BackendCapability.CRON: "AGENTOPS_CRON_URL",
    BackendCapability.ARTIFACT: "AGENTOPS_ARTIFACT_URL",
    BackendCapability.AUDIT: "AGENTOPS_AUDIT_URL",
}
_CAPABILITY_CONFIG_KEY: dict[BackendCapability, str] = {
    BackendCapability.MEMORY: "memory_url",
    BackendCapability.DEEP_MEMORY: "deep_memory_url",
    BackendCapability.CRON: "cron_url",
    BackendCapability.ARTIFACT: "artifact_url",
    BackendCapability.AUDIT: "audit_url",
}


def configure_compose_runtime_backends(
    registry: RuntimeBackendRegistry,
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    profile: str = _COMPOSE_PROFILE,
) -> None:
    """Register the HTTP backends for ``profile`` from compose env/config.

    Resolves a validated, credential-free base URL per capability and the
    control-plane bearer token, writes them into the registry's capability
    options, then registers the existing HTTP adapter factories. Raises
    ``ValueError`` (fail closed) before registering anything if a URL is missing
    or carries credentials/query/fragment.
    """

    env = environ if environ is not None else os.environ
    agentops_cfg = _agentops_config(config)
    token = _resolve_token(agentops_cfg, env)

    options_by_capability: dict[BackendCapability, dict[str, Any]] = {}
    for capability in _CAPABILITY_URL_ENV:
        base_url = _validate_base_url(_resolve_url(capability, agentops_cfg, env))
        options: dict[str, Any] = {"base_url": base_url}
        if token:
            options["token"] = token
        options_by_capability[capability] = options

    for capability, options in options_by_capability.items():
        registry.set_capability_options(capability, options, merge=False)

    register_http_memory_backend(registry, profile)
    register_http_memory_record_backend(registry, profile)
    register_http_cron_backend(registry, profile)
    register_http_artifact_backend(registry, profile=profile)
    register_http_audit_backend(registry, profile=profile)


def _agentops_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    section = config.get("agentops")
    return section if isinstance(section, Mapping) else {}


def _resolve_url(
    capability: BackendCapability,
    agentops_cfg: Mapping[str, Any],
    env: Mapping[str, str],
) -> str:
    config_key = _CAPABILITY_CONFIG_KEY[capability]
    env_key = _CAPABILITY_URL_ENV[capability]
    candidates = (
        agentops_cfg.get(config_key),
        env.get(env_key),
        agentops_cfg.get("api_url"),
        env.get(_API_URL_ENV),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def _resolve_token(agentops_cfg: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    for candidate in (agentops_cfg.get("runtime_token"), env.get(_TOKEN_ENV)):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _validate_base_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("compose backend wiring requires a control-plane base URL")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("compose backend base URL must be an absolute http(s) URL")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("compose backend base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("compose backend base URL must not contain query or fragment")
    return cleaned.rstrip("/")


COMPOSE_REQUIRED_CAPABILITIES: frozenset[BackendCapability] = REQUIRED_CAPABILITIES | {BackendCapability.DEEP_MEMORY}


def missing_compose_capabilities(
    registry: RuntimeBackendRegistry,
    profile: str = _COMPOSE_PROFILE,
) -> tuple[BackendCapability, ...]:
    """Return capabilities in COMPOSE_REQUIRED_CAPABILITIES lacking a registered factory for ``profile``."""
    return tuple(
        cap for cap in sorted(COMPOSE_REQUIRED_CAPABILITIES, key=lambda item: item.value)
        if profile not in registry.registered_profiles(cap)
    )


def validate_compose_backend_registration(
    registry: RuntimeBackendRegistry,
    profile: str = _COMPOSE_PROFILE,
) -> None:
    """Raise ValueError if any COMPOSE_REQUIRED_CAPABILITIES lack a factory under ``profile``.

    Error message contains only capability names — no DSNs, tokens, env values,
    or local paths.
    """
    missing = missing_compose_capabilities(registry, profile=profile)
    if missing:
        names = ", ".join(sorted(cap.value for cap in missing))
        raise ValueError(f"compose backend registration incomplete; missing capabilities: {names}")


__all__ = [
    "COMPOSE_REQUIRED_CAPABILITIES",
    "configure_compose_runtime_backends",
    "missing_compose_capabilities",
    "validate_compose_backend_registration",
]
