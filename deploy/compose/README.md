# AgentOps compose-self-hosted profile

This directory is the first Docker Compose packaging slice for the AgentOps Hermes Runtime MVP. It brings up the distributed topology required by M12 while preserving the native Hermes runtime surfaces behind the backend contracts.

## Start

```bash
cd deploy/compose
cp .env.example .env
# Edit infrastructure-only settings if needed.
docker compose up --build
```

The API health endpoint is published on `http://127.0.0.1:${AGENTOPS_API_PORT:-8710}/healthz`.

## Scale workers

Workers have no fixed `container_name`, so Compose can scale them horizontally:

```bash
docker compose --scale worker=3 up --build
```

Each worker reads `AGENTOPS_WORKER_MAX_CONCURRENT_RUNS` to advertise multiple local execution slots inside one worker task.

## Secret handling

The `.env.example` file contains only local development infrastructure defaults. Do not put raw app/integration secrets in this file. Bootstrap owns model provider, Slack, GitHub, Linear, Jira, and other raw app/integration secrets and stores them directly in the selected secret backend.

## Current scope

This slice provides the Compose topology and health-checked service entry points for follow-up M12 work:

- `api` / control-plane service
- `worker` fleet service, scalable with `--scale worker=N`
- `scheduler` service
- `postgres` database
- `redis` queue
- `minio` artifact store
- `local-secrets` development secret-store surface

Follow-up M12 slices still need to wire the durable Compose adapters through the existing RuntimeBackendRegistry contracts and run the full distributed isolation/restart/lease proof.
