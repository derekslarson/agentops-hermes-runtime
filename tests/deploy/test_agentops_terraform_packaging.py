"""Static contract tests for the AgentOps managed-cloud Terraform/OpenTofu packaging (M15).

These tests parse the Terraform/OpenTofu HCL and docs as text rather than asserting
exact snapshots, so the packaging can evolve while preserving the M15 contract:
required resources/services, bring-your-own-network and bring-your-own-managed-resource
variables, honest outputs, and secret handling that keeps raw app/integration secret
values out of the recommended state path.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DIR = REPO_ROOT / "deploy" / "terraform"
AWS_DIR = TERRAFORM_DIR / "aws-managed"
GCP_DIR = TERRAFORM_DIR / "gcp-managed"

# Raw app/integration secret variable names that must never appear in tfvars
# examples or docs as accepted inputs. Bootstrap (M16) owns these values; the
# Terraform layer only creates secret containers/refs.
RAW_SECRET_TOKENS = (
    "slack_bot_token",
    "slack_signing_secret",
    "github_token",
    "linear_api_key",
    "jira_api_token",
    "model_provider_api_key",
    "openai_api_key",
    "anthropic_api_key",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _module_files(module_dir: Path) -> dict[str, str]:
    return {
        "main": _read(module_dir / "main.tf"),
        "variables": _read(module_dir / "variables.tf"),
        "outputs": _read(module_dir / "outputs.tf"),
        "tfvars": _read(module_dir / "terraform.tfvars.example"),
        "readme": _read(module_dir / "README.md"),
    }


# --- directory / file presence ---------------------------------------------


def test_managed_profiles_provide_required_module_files():
    for module_dir in (AWS_DIR, GCP_DIR):
        assert module_dir.is_dir(), f"missing module dir {module_dir}"
        for name in (
            "main.tf",
            "variables.tf",
            "outputs.tf",
            "terraform.tfvars.example",
            "README.md",
        ):
            assert (module_dir / name).is_file(), f"missing {module_dir / name}"


# --- AWS resources / services ----------------------------------------------


def test_aws_main_provisions_required_managed_resources():
    main = _read(AWS_DIR / "main.tf")

    # API/control-plane + worker + scheduler services (ECS/Fargate).
    assert "aws_ecs_cluster" in main
    assert "aws_ecs_service" in main
    for service_concept in ("control-plane", "worker", "scheduler"):
        assert service_concept in main

    # Managed datastore, queue, artifact store, secrets, logs, IAM, autoscaling.
    assert "aws_db_instance" in main  # RDS/Postgres
    assert "postgres" in main.lower()
    assert "aws_sqs_queue" in main  # queue
    assert "aws_s3_bucket" in main  # artifact store
    assert "aws_secretsmanager_secret" in main  # secret placeholders/refs
    assert "aws_cloudwatch_log_group" in main  # logs
    assert "aws_iam_role" in main  # IAM
    assert "aws_appautoscaling_target" in main  # autoscaling


def test_aws_variables_cover_account_capacity_and_byo_paths():
    variables = _read(AWS_DIR / "variables.tf")

    # Account / region / domain / capacity knobs the customer edits.
    for required in ("region", "domain", "desired_task_count", "max_concurrent_runs"):
        assert f'variable "{required}"' in variables

    # Bring-your-own-network refs.
    assert 'variable "existing_vpc_id"' in variables
    assert 'variable "existing_subnet_ids"' in variables

    # Bring-your-own-managed-resource refs.
    assert 'variable "existing_database_arn"' in variables
    assert 'variable "existing_artifact_bucket"' in variables
    assert 'variable "existing_secret_prefix"' in variables


def test_aws_outputs_expose_urls_refs_and_smoke_hints():
    outputs = _read(AWS_DIR / "outputs.tf")

    for required_output in (
        "agentops_api_url",
        "bootstrap_url",
        "bootstrap_token_secret_ref",
        "slack_webhook_url",
        "github_webhook_url",
        "linear_webhook_url",
        "jira_webhook_url",
        "secret_refs",
        "queue_refs",
        "artifact_refs",
        "worker_service_name",
        "smoke_test_hints",
    ):
        assert f'output "{required_output}"' in outputs, required_output


# --- GCP scaffold + parity --------------------------------------------------


def test_gcp_scaffold_includes_equivalent_resources_and_parity_gaps():
    main = _read(GCP_DIR / "main.tf")
    readme = _read(GCP_DIR / "README.md")

    # Equivalent GCP managed resources (scaffold-level is acceptable).
    assert "google_cloud_run" in main  # API/worker/scheduler services
    assert "google_sql_database_instance" in main  # Postgres
    assert "google_storage_bucket" in main  # artifact store
    assert "google_secret_manager_secret" in main  # secret refs
    assert "google_pubsub" in main or "google_cloud_tasks" in main  # queue

    # Parity gaps must be documented honestly, not hidden.
    lowered = readme.lower()
    assert "parity" in lowered
    assert "aws-managed" in lowered


# --- secret hygiene ---------------------------------------------------------


def test_tfvars_examples_avoid_raw_app_integration_secrets():
    for module_dir in (AWS_DIR, GCP_DIR):
        tfvars = _read(module_dir / "terraform.tfvars.example").lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in tfvars, f"{token} leaked into {module_dir} tfvars"


def test_variables_do_not_accept_raw_app_integration_secret_inputs():
    for module_dir in (AWS_DIR, GCP_DIR):
        variables = _read(module_dir / "variables.tf").lower()
        for token in RAW_SECRET_TOKENS:
            assert f'variable "{token}"' not in variables, token


def test_docs_explain_secret_refs_keep_raw_values_out_of_state():
    aws_readme = _read(AWS_DIR / "README.md").lower()

    # The recommended path creates empty secret containers; bootstrap fills them.
    assert "secret" in aws_readme
    assert "state" in aws_readme
    assert "bootstrap" in aws_readme
    # Be explicit that raw values are not Terraform inputs.
    assert "raw" in aws_readme

    main = _read(AWS_DIR / "main.tf")
    # Secret containers are created, but no secret *version* with a raw value
    # is part of the recommended path.
    assert "aws_secretsmanager_secret" in main
    assert "aws_secretsmanager_secret_version" not in main


# --- bring-your-own refs are actually consumed ------------------------------


def _declared_existing_vars(module_dir: Path) -> set[str]:
    variables = _read(module_dir / "variables.tf")
    import re

    return set(re.findall(r'variable "(existing_[a-z_]+)"', variables))


def test_every_declared_byo_ref_is_consumed_not_dead():
    for module_dir in (AWS_DIR, GCP_DIR):
        consumed = _read(module_dir / "main.tf") + _read(module_dir / "outputs.tf")
        for var_name in _declared_existing_vars(module_dir):
            assert (
                f"var.{var_name}" in consumed
            ), f"{module_dir.name}: declared {var_name} is never consumed (dead variable)"


def test_byo_network_refs_feed_into_main_topology():
    # The VPC/subnet refs must drive real wiring in main.tf, not just sit in
    # outputs, so a bring-your-own-network customer's values take effect.
    for module_dir in (AWS_DIR, GCP_DIR):
        main = _read(module_dir / "main.tf")
        assert "var.existing_vpc_id" in main, f"{module_dir.name}: existing_vpc_id unused in main.tf"
        assert (
            "var.existing_subnet_ids" in main
        ), f"{module_dir.name}: existing_subnet_ids unused in main.tf"


# --- BYO managed-resource refs are surfaced, not just suppressors -----------


def _variable_block(text: str, name: str) -> str:
    import re

    match = re.search(
        r'variable "' + re.escape(name) + r'".*?(?=\nvariable "|\Z)',
        text,
        re.DOTALL,
    )
    return match.group(0) if match else ""


def test_database_refs_output_consumes_byo_database_ref():
    # existing_database_arn must be surfaced as a usable ref, not only used to
    # suppress DB creation — otherwise BYO-database loses the connection ref.
    for module_dir in (AWS_DIR, GCP_DIR):
        outputs = _read(module_dir / "outputs.tf")
        assert 'output "database_refs"' in outputs, module_dir.name
        assert "var.existing_database_arn" in outputs, module_dir.name


def test_aws_subnet_docs_require_subnets_with_byo_vpc():
    variables = _read(AWS_DIR / "variables.tf")
    block = _variable_block(variables, "existing_subnet_ids")

    # The misleading "empty always creates subnets" claim must be gone — subnets
    # are only created when the module also creates the VPC.
    assert "Empty list creates new subnets" not in variables
    # Honest docs: required when a BYO VPC is supplied.
    assert "existing_vpc_id" in block
    assert "requir" in block.lower()
    # Enforced, not just documented.
    assert "validation" in block


def test_gcp_network_docs_match_parity_gap_no_new_network():
    variables = _read(GCP_DIR / "variables.tf")
    vpc_block = _variable_block(variables, "existing_vpc_id")
    subnet_block = _variable_block(variables, "existing_subnet_ids")

    # GCP does NOT create a new private network/subnets (README parity gap);
    # the docs must not claim it does.
    assert "creates a new one" not in vpc_block
    assert "creates new subnets" not in subnet_block
    for block in (vpc_block, subnet_block):
        lowered = block.lower()
        assert "default egress" in lowered or "parity" in lowered


# --- honesty: apply path is not overstated while TODOs remain ---------------


def test_readmes_do_not_overstate_apply_completeness_while_todos_remain():
    for module_dir in (AWS_DIR, GCP_DIR):
        main = _read(module_dir / "main.tf")
        readme = _read(module_dir / "README.md")
        if "TODO" not in main:
            continue
        # If main.tf still has TODO placeholders, the README must say apply does
        # not yield a working deployment yet, and must not claim it does.
        assert "does not yield a working deployment" in readme, module_dir.name
        assert "receive working infrastructure" not in readme, module_dir.name


# --- docs commands run from the module dir ---------------------------------


# --- AWS ECS task definitions + container images (M15 slice) ----------------


AWS_TASK_DEF_NAMES = ("control_plane", "worker", "scheduler")
AWS_IMAGE_VARS = ("control_plane_image", "worker_image", "scheduler_image")
AWS_TODO_TASK_DEF_LITERALS = (
    "TODO-control-plane-task-definition",
    "TODO-worker-task-definition",
    "TODO-scheduler-task-definition",
)


def _resource_block(text: str, resource_type: str, name: str) -> str:
    import re

    match = re.search(
        r'resource "'
        + re.escape(resource_type)
        + r'" "'
        + re.escape(name)
        + r'".*?(?=\nresource "|\ndata "|\noutput "|\Z)',
        text,
        re.DOTALL,
    )
    return match.group(0) if match else ""


def _list_local(text: str, name: str) -> str:
    start = text.find(name + " = [")
    if start == -1:
        start = text.find(name + "= [")
    if start == -1:
        return ""
    bracket_start = text.index("[", start)
    depth = 0
    for i in range(bracket_start, len(text)):
        char = text[i]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[bracket_start : i + 1]
    return ""


def test_aws_defines_ecs_task_definitions_for_all_services():
    main = _read(AWS_DIR / "main.tf")
    for name in AWS_TASK_DEF_NAMES:
        assert (
            f'resource "aws_ecs_task_definition" "{name}"' in main
        ), f"missing aws_ecs_task_definition {name}"


def test_aws_ecs_services_reference_task_definitions_not_todo_placeholders():
    main = _read(AWS_DIR / "main.tf")

    # The literal TODO task-definition placeholders must be gone.
    for todo in AWS_TODO_TASK_DEF_LITERALS:
        assert todo not in main, f"{todo} still present"

    # Each service's task_definition points at its task-definition resource.
    for name in AWS_TASK_DEF_NAMES:
        block = _resource_block(main, "aws_ecs_service", name)
        assert block, f"missing aws_ecs_service {name}"
        assert (
            f"aws_ecs_task_definition.{name}.arn" in block
        ), f"{name} service does not reference its task definition resource"


def test_aws_variables_expose_non_secret_container_image_inputs():
    variables = _read(AWS_DIR / "variables.tf")
    for var_name in AWS_IMAGE_VARS:
        block = _variable_block(variables, var_name)
        assert block, f"missing image variable {var_name}"
        # Placeholder default/example is provided so the contract is usable...
        assert "default" in block, f"{var_name} has no placeholder default"
        # ...but the image input is not a secret value.
        lowered = block.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} leaked into {var_name}"


def test_aws_task_definitions_consume_their_image_variables():
    main = _read(AWS_DIR / "main.tf")
    for name in AWS_TASK_DEF_NAMES:
        block = _resource_block(main, "aws_ecs_task_definition", name)
        assert block, f"missing task definition {name}"
        assert (
            f"var.{name}_image" in block
        ), f"{name} task definition does not consume var.{name}_image"


def test_aws_worker_task_def_sets_max_concurrent_runs_from_var():
    main = _read(AWS_DIR / "main.tf")

    worker = _resource_block(main, "aws_ecs_task_definition", "worker")
    assert worker, "missing worker task definition"
    assert "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS" in worker
    assert "var.max_concurrent_runs" in worker

    # The per-task run-slot bound is worker-specific, not on the other services.
    for other in ("control_plane", "scheduler"):
        block = _resource_block(main, "aws_ecs_task_definition", other)
        assert (
            "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS" not in block
        ), f"{other} should not advertise the worker run-slot bound"


def test_aws_all_task_defs_share_non_secret_runtime_env():
    main = _read(AWS_DIR / "main.tf")
    for name in AWS_TASK_DEF_NAMES:
        block = _resource_block(main, "aws_ecs_task_definition", name)
        assert block, f"missing task definition {name}"
        assert (
            "local.runtime_common_env" in block
        ), f"{name} task definition does not include the shared runtime env"


def test_aws_runtime_common_env_carries_profile_and_backend_refs():
    main = _read(AWS_DIR / "main.tf")
    env = _list_local(main, "runtime_common_env")
    assert env, "missing runtime_common_env local list"

    # AGENTOPS_RUNTIME_PROFILE=aws-managed
    assert "AGENTOPS_RUNTIME_PROFILE" in env
    assert "aws-managed" in env

    # Queue ref wired to the real SQS queue (not a literal).
    assert "AGENTOPS_QUEUE_URL" in env
    assert "aws_sqs_queue.runs" in env

    # Artifact store ref (BYO-aware bucket).
    assert "AGENTOPS_ARTIFACT_BUCKET" in env
    assert "artifact" in env.lower()

    # Secret prefix/refs.
    assert "AGENTOPS_SECRET_PREFIX" in env

    # Database ref points at the database secret container, not a raw value.
    assert "AGENTOPS_DATABASE" in env
    assert 'aws_secretsmanager_secret.containers["database"].arn' in env


# --- IAM: execution + task roles actually grant what the task defs need -----


def test_aws_execution_role_has_ecs_execution_permissions():
    # Task defs use execution_role_arn for awslogs + customer image pulls, so the
    # execution role must carry the managed ECS execution policy (logs + ECR auth),
    # not just an assume-role policy.
    main = _read(AWS_DIR / "main.tf")
    assert "AmazonECSTaskExecutionRolePolicy" in main, "execution role lacks ECS execution policy"

    block = _resource_block(main, "aws_iam_role_policy_attachment", "task_execution")
    assert block, "missing execution-role managed-policy attachment"
    assert "aws_iam_role.task_execution" in block, "attachment not bound to the execution role"
    assert "AmazonECSTaskExecutionRolePolicy" in block


def test_aws_task_role_grants_scoped_backend_access():
    # The task role is wired into the task defs and the runtime env advertises the
    # queue/artifact/secret refs, so the role must actually permit SQS, S3, and
    # Secrets Manager access scoped to the module's resources.
    main = _read(AWS_DIR / "main.tf")
    block = _resource_block(main, "aws_iam_role_policy", "task_runtime_backends")
    assert block, "missing task_runtime_backends inline policy"

    # Bound to the task role, not the execution role.
    assert "aws_iam_role.task.id" in block, "policy not bound to the task role"

    # SQS access scoped to the runs queue ARN.
    assert "sqs:" in block
    assert "aws_sqs_queue.runs.arn" in block

    # S3 access scoped to the effective (created/BYO) artifact bucket + objects.
    assert "s3:" in block
    assert "local.effective_artifact_bucket" in block
    assert "/*" in block, "no object-level S3 ARN"

    # Secrets Manager access scoped to the created secret containers.
    assert "secretsmanager:" in block
    assert "aws_secretsmanager_secret.containers" in block


def test_readmes_use_module_local_commands_not_root_relative_paths():
    for module_dir, name in ((AWS_DIR, "aws-managed"), (GCP_DIR, "gcp-managed")):
        readme = _read(module_dir / "README.md")

        # Customer runs commands from inside the module directory.
        assert f"cd deploy/terraform/{name}" in readme
        assert "terraform init" in readme
        assert "terraform apply" in readme
        # OpenTofu is supported as a drop-in.
        assert "tofu init" in readme

        # No root-relative -chdir hacks that break when run from the module dir.
        assert "-chdir=deploy/terraform" not in readme
