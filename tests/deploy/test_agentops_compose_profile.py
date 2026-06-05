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


def test_deep_memory_extra_exists_in_pyproject_with_lazy_deps_pins():
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from tools.lazy_deps import LAZY_DEPS

    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    optional_deps = pyproject["project"]["optional-dependencies"]

    assert "deep-memory" in optional_deps, (
        "pyproject.toml must declare [project.optional-dependencies.deep-memory]"
    )
    deep_memory_pins = set(optional_deps["deep-memory"])
    lazy_pins = set(LAZY_DEPS["memory.local"])

    assert "chromadb==1.5.9" in deep_memory_pins
    assert "onnxruntime==1.26.0" in deep_memory_pins
    assert deep_memory_pins == lazy_pins, (
        f"deep-memory extra pins {deep_memory_pins} must exactly match "
        f"LAZY_DEPS['memory.local'] {lazy_pins}"
    )


def test_compose_runtime_image_installs_deep_memory_extra():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--extra deep-memory" in dockerfile, (
        "Dockerfile uv sync line must include --extra deep-memory for the runtime image"
    )


def test_api_service_has_artifact_root_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_ARTIFACT_ROOT=/var/lib/agentops/artifacts" in api_env


def test_api_service_mounts_named_artifact_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/artifacts" in v for v in volume_strings)


def test_compose_declares_artifact_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-artifacts" in compose["volumes"]


def test_api_service_has_session_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_SESSION_DB_PATH=/var/lib/agentops/sessions/sessions.db" in api_env


def test_api_service_mounts_sessions_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/sessions" in v for v in volume_strings)


def test_compose_declares_sessions_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-sessions" in compose["volumes"]


# ---------------------------------------------------------------------------
# M12B: Secret store volume and env (local-secrets service)
# ---------------------------------------------------------------------------


def test_local_secrets_service_has_secret_store_path_env():
    services = _compose()["services"]
    ls_env = "\n".join(services["local-secrets"]["environment"])
    assert "AGENTOPS_SECRET_STORE_PATH=" in ls_env


def test_local_secrets_service_mounts_secrets_volume():
    services = _compose()["services"]
    ls = services["local-secrets"]
    assert "volumes" in ls
    volume_strings = [str(v) for v in ls["volumes"]]
    assert any("agentops-secrets" in v for v in volume_strings)


def test_compose_declares_secrets_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-secrets" in compose["volumes"]


# ---------------------------------------------------------------------------
# M12B: Curated memory volume and env (api service)
# ---------------------------------------------------------------------------


def test_api_service_has_curated_memory_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_CURATED_MEMORY_DB_PATH=/var/lib/agentops/curated-memory/curated.db" in api_env


def test_api_service_mounts_curated_memory_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/curated-memory" in v for v in volume_strings)


def test_compose_declares_curated_memory_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-curated-memory" in compose["volumes"]


# ---------------------------------------------------------------------------
# M12B: Skills volume and env (api service)
# ---------------------------------------------------------------------------


def test_api_service_has_skill_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_SKILL_DB_PATH=/var/lib/agentops/skills/skills.db" in api_env


def test_api_service_mounts_skills_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/skills" in v for v in volume_strings)


def test_compose_declares_skills_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-skills" in compose["volumes"]


# ---------------------------------------------------------------------------
# M12B: Queue volume and env (api service)
# ---------------------------------------------------------------------------


def test_api_service_has_queue_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_QUEUE_DB_PATH=/var/lib/agentops/queue/queue.db" in api_env


def test_api_service_mounts_queue_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/queue" in v for v in volume_strings)


def test_compose_declares_queue_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-queue" in compose["volumes"]


# ---------------------------------------------------------------------------
# M12B: Run-lease volume and env (api service only)
# ---------------------------------------------------------------------------


def test_api_service_has_run_lease_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_RUN_LEASE_DB_PATH=/var/lib/agentops/run-leases/run-leases.db" in api_env


def test_api_service_mounts_run_leases_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/run-leases" in v for v in volume_strings)


def test_compose_declares_run_leases_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-run-leases" in compose["volumes"]


def test_worker_does_not_have_run_lease_db_path_env():
    services = _compose()["services"]
    worker_env = "\n".join(services["worker"]["environment"])
    assert "AGENTOPS_RUN_LEASE_DB_PATH" not in worker_env


def test_scheduler_does_not_have_run_lease_db_path_env():
    services = _compose()["services"]
    scheduler_env = "\n".join(services["scheduler"]["environment"])
    assert "AGENTOPS_RUN_LEASE_DB_PATH" not in scheduler_env


# ---------------------------------------------------------------------------
# M12B: Cron volume and env (api service only)
# ---------------------------------------------------------------------------


def test_api_service_has_cron_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_CRON_DB_PATH=/var/lib/agentops/cron/cron.db" in api_env


def test_api_service_mounts_cron_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/cron" in v for v in volume_strings)


def test_compose_declares_cron_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-cron" in compose["volumes"]


def test_worker_does_not_have_cron_db_path_env():
    services = _compose()["services"]
    worker_env = "\n".join(services["worker"]["environment"])
    assert "AGENTOPS_CRON_DB_PATH" not in worker_env


def test_scheduler_does_not_have_cron_db_path_env():
    services = _compose()["services"]
    scheduler_env = "\n".join(services["scheduler"]["environment"])
    assert "AGENTOPS_CRON_DB_PATH" not in scheduler_env


# ---------------------------------------------------------------------------
# M12B: Conversation-router volume and env (api service only)
# ---------------------------------------------------------------------------


def test_api_service_has_conversation_router_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_CONVERSATION_ROUTER_DB_PATH=/var/lib/agentops/conversations/conversations.db" in api_env


def test_api_service_mounts_conversations_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/conversations" in v for v in volume_strings)


def test_compose_declares_conversations_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-conversations" in compose["volumes"]


def test_worker_does_not_have_conversation_router_db_path_env():
    services = _compose()["services"]
    worker_env = "\n".join(services["worker"]["environment"])
    assert "AGENTOPS_CONVERSATION_ROUTER_DB_PATH" not in worker_env


def test_scheduler_does_not_have_conversation_router_db_path_env():
    services = _compose()["services"]
    scheduler_env = "\n".join(services["scheduler"]["environment"])
    assert "AGENTOPS_CONVERSATION_ROUTER_DB_PATH" not in scheduler_env


# ---------------------------------------------------------------------------
# M12B: Worker-registry volume and env (api service only)
# ---------------------------------------------------------------------------


def test_api_service_has_worker_registry_db_path_env():
    services = _compose()["services"]
    api_env = "\n".join(services["api"]["environment"])
    assert "AGENTOPS_WORKER_REGISTRY_DB_PATH=/var/lib/agentops/worker-registry/workers.db" in api_env


def test_api_service_mounts_worker_registry_volume():
    services = _compose()["services"]
    api = services["api"]
    assert "volumes" in api
    volume_strings = [str(v) for v in api["volumes"]]
    assert any("/var/lib/agentops/worker-registry" in v for v in volume_strings)


def test_compose_declares_worker_registry_volume():
    compose = _compose()
    assert "volumes" in compose
    assert "agentops-worker-registry" in compose["volumes"]


def test_worker_does_not_have_worker_registry_db_path_env():
    services = _compose()["services"]
    worker_env = "\n".join(services["worker"]["environment"])
    assert "AGENTOPS_WORKER_REGISTRY_DB_PATH" not in worker_env


def test_scheduler_does_not_have_worker_registry_db_path_env():
    services = _compose()["services"]
    scheduler_env = "\n".join(services["scheduler"]["environment"])
    assert "AGENTOPS_WORKER_REGISTRY_DB_PATH" not in scheduler_env
