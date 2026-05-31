# AgentOps Hermes Runtime

This fork turns Hermes from a single-user, local-profile agent into a distributed, multi-tenant runtime that can still run locally with the existing local backends.

## Product thesis

Hermes already has the valuable kernel: model/provider routing, tool calling, skills, memory behavior, gateway integrations, cron jobs, delegation, and autonomous operator ergonomics. The missing enterprise/business layer is not a dashboard first; it is a runtime substrate where those same native Hermes primitives can be backed by remote, scoped services.

AgentOps Hermes Runtime should let the same Hermes behavior run in either mode:

- **Local mode:** current Hermes behavior, local profile files, local SQLite, local memory files, local skills, local cron, local credentials.
- **AgentOps mode:** runtime context selects remote backends for memory, skills, sessions, cron jobs, credentials, artifacts, audit, and messaging routes.

The goal is not to inject a parallel context system around Hermes. The native Hermes tools should keep their names and semantics. For example, the `memory` tool should still be the `memory` tool, but its backend should become context-scoped and optionally remote.

## Non-negotiables

- Native Hermes memory tool must route through pluggable backends.
- Native Hermes skills loading must support local and remote scoped skill sources.
- Native Hermes cron/autonomous jobs must support local and remote schedulers/stores.
- Credentials must resolve through pluggable, scoped resolvers; raw secrets must not enter prompts/transcripts.
- Runtime must be cloud agnostic: AWS/GCP/local are adapters, not architecture.
- Runtime must be database agnostic: Postgres is an initial adapter, not the core contract.
- Secret storage must be agnostic: local env, AWS Secrets Manager, GCP Secret Manager, Vault, etc. are adapters.
- Local developer/personal Hermes mode must keep working.

## Repository relationship

This repository is a fork of `NousResearch/hermes-agent` created for AgentOps runtime work. Changes should be kept in small, reviewable slices and structured so generally useful backend seams can be upstreamed later if desired.
