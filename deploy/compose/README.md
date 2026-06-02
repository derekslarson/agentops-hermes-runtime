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

## Smoke-check the running services

Once the stack is up, run the packaged smoke surface against the live services. It
probes `/healthz` and `/readyz` for `api`, `worker`, and `scheduler`, and `/healthz`
for `local-secrets`, and prints a structured JSON report. It exits non-zero (fails
closed) if any service is unreachable, returns a non-200 status, or reports
`"ok": false`:

```bash
docker compose up --build -d
docker compose exec api \
  python -m agentops_runtime.compose_health_smoke
```

The check runs inside the Compose network, so it reaches `worker` and `scheduler`
by their service DNS names even though those services publish no host ports.
Service URLs default to the in-network Compose names and can be overridden via
`AGENTOPS_API_URL`, `AGENTOPS_WORKER_URL`, `AGENTOPS_SCHEDULER_URL`, and
`AGENTOPS_SECRET_STORE_URL` to run the same check from a sidecar or the host.

## Scale workers

Workers have no fixed `container_name`, so Compose can scale them horizontally:

```bash
docker compose --scale worker=3 up --build
```

Each worker reads `AGENTOPS_WORKER_MAX_CONCURRENT_RUNS` to advertise multiple local execution slots inside one worker task.

## Secret handling

The `.env.example` file contains only local development infrastructure defaults. Do not put raw app/integration secrets in this file. Bootstrap owns model provider, Slack, GitHub, Linear, Jira, and other raw app/integration secrets and stores them directly in the selected secret backend.

## Current scope

This slice provides the Compose topology, backend wiring, distributed-semantics contract smoke tests, and health-checked service entry points:

- `api` / control-plane service
- `worker` fleet service, scalable with `--scale worker=N`
- `scheduler` service
- `postgres` database
- `redis` queue
- `minio` artifact store
- `local-secrets` development secret-store surface

The only remaining M12 closure step is to capture a live green `docker compose up` health transcript plus a passing `python -m agentops_runtime.compose_health_smoke` run on a host with Docker daemon access.
