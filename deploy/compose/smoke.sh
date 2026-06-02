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

cleanup() {
  docker compose down --remove-orphans
}
trap cleanup EXIT

docker compose up --build -d
docker compose --profile smoke run --rm smoke
