"""Static contract tests for the AgentOps compose-self-hosted MVP profile."""

from __future__ import annotations

from pathlib import Path

import tomllib
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
README = COMPOSE_DIR / "README.md"
SMOKE_SCRIPT = COMPOSE_DIR / "smoke.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _compose() -> dict:
    with COMPOSE_FILE.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_compose_profile_declares_required_distributed_services():
    compose = _compose()

    services = compose["services"]

    assert {"api", "worker", "scheduler", "postgres", "redis", "minio", "local-secrets"}.issubset(
        services
    )
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["scheduler"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_workers_are_scale_safe_and_configure_multiple_slots():
    services = _compose()["services"]
    worker = services["worker"]

    assert "container_name" not in worker
    assert worker["deploy"]["replicas"] == 1
    assert any(str(item).startswith("AGENTOPS_WORKER_MAX_CONCURRENT_RUNS=") for item in worker["environment"])
    assert "${AGENTOPS_WORKER_MAX_CONCURRENT_RUNS:-2}" in "\n".join(worker["environment"])


def test_compose_services_use_agentops_runtime_profile_and_backend_refs():
    services = _compose()["services"]

    for service_name in ("api", "worker", "scheduler"):
        env = "\n".join(services[service_name]["environment"])
        assert "HERMES_RUNTIME_MODE=agentops" in env
        assert "HERMES_BACKEND_PROFILE=compose-self-hosted" in env
        assert "AGENTOPS_DATABASE_URL=" in env
        assert "${AGENTOPS_POSTGRES_PASSWORD" in env
        assert "AGENTOPS_QUEUE_URL=" in env
        assert "AGENTOPS_ARTIFACT_ENDPOINT=" in env
        assert "AGENTOPS_SECRET_STORE_URL=" in env
        assert "AGENTOPS_API_URL=http://api:8710" in env


def test_compose_api_wires_deep_memory_db_to_postgres_without_worker_direct_db():
    services = _compose()["services"]

    worker_env = "\n".join(services["worker"]["environment"])
    scheduler_env = "\n".join(services["scheduler"]["environment"])

    deep_memory_lines = [line for line in services["api"]["environment"] if line.startswith("AGENTOPS_DEEP_MEMORY_DB_URL=")]
    assert deep_memory_lines == [
        "AGENTOPS_DEEP_MEMORY_DB_URL=postgresql://agentops:${AGENTOPS_POSTGRES_PASSWORD:-agentops-dev-password}@postgres:5432/${AGENTOPS_POSTGRES_DB:-agentops}"
    ]
    assert "${AGENTOPS_DATABASE_URL}" not in deep_memory_lines[0]
    assert "AGENTOPS_DEEP_MEMORY_DB_URL=" not in worker_env
    assert "AGENTOPS_DEEP_MEMORY_DB_URL=" not in scheduler_env


def test_compose_postgres_image_includes_pgvector_for_deep_memory_schema():
    postgres = _compose()["services"]["postgres"]

    assert "pgvector" in postgres["image"].lower()


def test_compose_profile_documents_scale_and_smoke_commands_without_raw_secrets():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    assert "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS=" in env_text
    assert "docker compose --scale worker=3" in readme_text
    assert "docker compose up" in readme_text
    assert "docker compose --profile smoke run --rm smoke" in readme_text
    assert "python -m agentops_runtime.compose_health_smoke" in readme_text
    assert "-f deploy/compose/docker-compose.yml" not in readme_text
    assert "raw app/integration secrets" in readme_text
    assert "SLACK_BOT_TOKEN=" not in env_text
    assert "OPENAI_API_KEY=" not in env_text


def test_compose_profile_includes_one_shot_smoke_service():
    services = _compose()["services"]
    smoke = services["smoke"]

    assert smoke["profiles"] == ["smoke"]
    assert smoke["command"] == ["python", "-m", "agentops_runtime.compose_health_smoke"]
    assert "container_name" not in smoke
    for dependency in ("api", "worker", "scheduler", "local-secrets"):
        assert smoke["depends_on"][dependency]["condition"] == "service_healthy"

    env = "\n".join(smoke["environment"])
    assert "AGENTOPS_API_URL=http://api:8710" in env
    assert "AGENTOPS_WORKER_URL=http://worker:8711" in env
    assert "AGENTOPS_SCHEDULER_URL=http://scheduler:8712" in env
    assert "AGENTOPS_SECRET_STORE_URL=http://local-secrets:8713" in env


def test_compose_profile_includes_live_smoke_script_from_compose_dir():
    script = SMOKE_SCRIPT.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    assert "docker compose up --build -d" in script
    assert "docker compose --profile smoke run --rm smoke" in script
    assert "docker compose down" in script
    assert "docker info" in script
    assert "-f deploy/compose/docker-compose.yml" not in script
    assert "./smoke.sh" in readme_text


def test_agentops_runtime_compose_service_is_packaged_in_wheels():
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    packages = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

    assert "agentops_runtime" in packages
    assert "agentops_runtime.*" in packages


def test_compose_runtime_image_installs_postgres_driver_extra():
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    postgres_extra = pyproject["project"]["optional-dependencies"]["postgres"]

    assert any(dep.startswith("psycopg2-binary==") for dep in postgres_extra)
    assert "--extra postgres" in dockerfile
