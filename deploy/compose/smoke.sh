#!/usr/bin/env bash
set -euo pipefail

# Run from deploy/compose so the documented commands do not rely on
# root-relative -f paths that break after `cd deploy/compose`.
if [ ! -f "docker-compose.yml" ]; then
  echo "error: run this script from deploy/compose" >&2
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  echo "error: Docker daemon is not available. Start Docker and re-run ./smoke.sh." >&2
  exit 2
fi

SMOKE_WORKERS=${AGENTOPS_SMOKE_WORKERS:-3}
SMOKE_SCHEDULERS=${AGENTOPS_SMOKE_SCHEDULERS:-2}
SMOKE_FLEET_RUN_ID=${AGENTOPS_SMOKE_FLEET_RUN_ID:-smoke-$(date +%s)-$$}
export AGENTOPS_SMOKE_FLEET_RUN_ID=${SMOKE_FLEET_RUN_ID}

cleanup() {
  docker compose down --remove-orphans
}
trap cleanup EXIT

docker compose up --build -d --scale worker=${SMOKE_WORKERS} --scale scheduler=${SMOKE_SCHEDULERS}
docker compose --profile smoke run --rm smoke
docker compose --profile smoke run --rm \
  -e AGENTOPS_SMOKE_EXPECTED_WORKERS=${SMOKE_WORKERS} \
  -e AGENTOPS_SMOKE_FLEET_RUN_ID=${SMOKE_FLEET_RUN_ID} \
  durable-smoke
