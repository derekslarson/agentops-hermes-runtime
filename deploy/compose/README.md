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
./smoke.sh
```

`smoke.sh` starts the stack with scaled workers/schedulers via
`docker compose up --build -d --scale worker=${AGENTOPS_SMOKE_WORKERS:-3}` and
`--scale scheduler=${AGENTOPS_SMOKE_SCHEDULERS:-2}`, runs
`docker compose --profile smoke run --rm smoke` (health-only), then runs
`docker compose --profile smoke run --rm durable-smoke` (durable backend
parity: worker_fleet, queue tenant isolation, conversation routing, secret
roundtrip, native state continuity, artifact/audit roundtrips,
delivery dispatch, skill roundtrip, scheduler claim-once, and optional current-run `worker_fleet_scale`),
and tears the stack down with `docker compose down --remove-orphans`.
If Docker is already running and you want to keep the stack up after the check,
run those commands manually.

The `smoke` and `durable-smoke` one-shot services run inside the Compose
network, so they reach all services by their DNS names even though those
services publish no host ports. The health smoke can also be run manually
from the API container:

```bash
docker compose exec api \
  python -m agentops_runtime.compose_health_smoke
```

The durable smoke (`python -m agentops_runtime.compose_durable_smoke`) can
be run from the API container or any container with access to `AGENTOPS_API_URL`
and `AGENTOPS_SECRET_STORE_URL`. Service URLs default to the in-network Compose
names and can be overridden via `AGENTOPS_API_URL`, `AGENTOPS_WORKER_URL`,
`AGENTOPS_SCHEDULER_URL`, and `AGENTOPS_SECRET_STORE_URL` to run the same
check from a sidecar or the host.

## Scale workers

Workers have no fixed `container_name`, so Compose can scale them horizontally:

```bash
docker compose --scale worker=3 up --build
```

Each worker reads `AGENTOPS_WORKER_MAX_CONCURRENT_RUNS` to advertise multiple local execution slots inside one worker task.

## Scaled live smoke (M12B)

To run the full scaled smoke with explicit worker and scheduler counts:

```bash
cd deploy/compose
AGENTOPS_SMOKE_WORKERS=3 AGENTOPS_SMOKE_SCHEDULERS=2 ./smoke.sh
```

`smoke.sh` starts the stack with `--scale worker=${AGENTOPS_SMOKE_WORKERS:-3}` and
`--scale scheduler=${AGENTOPS_SMOKE_SCHEDULERS:-2}`, exports a per-run
`AGENTOPS_SMOKE_FLEET_RUN_ID`, then runs durable-smoke with both
`AGENTOPS_SMOKE_EXPECTED_WORKERS` and that fleet run id so the `worker_fleet_scale`
step verifies the expected number of distinct fleet workers from the current smoke
run have self-registered (stale rows from previous durable volumes do not count).

**M12B status: Started.** The scaled smoke wiring is in place and verifiable with a
running Docker daemon, but M12B is not Done until a live transcript from `./smoke.sh`
running against real containers is captured, showing all durable-smoke steps green
including `worker_fleet_scale` with `enforced: true` and the expected worker count.

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

The remaining M12 closure evidence is a live green `docker compose up` transcript on a host with Docker daemon access showing both the health smoke (`python -m agentops_runtime.compose_health_smoke`) and durable control-plane smoke (`python -m agentops_runtime.compose_durable_smoke`) passing against the running stack.
