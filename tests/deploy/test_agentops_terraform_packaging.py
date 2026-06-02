"""Static contract tests for the AgentOps managed-cloud Terraform/OpenTofu packaging (M15).

These tests parse the Terraform/OpenTofu HCL and docs as text rather than asserting
exact snapshots, so the packaging can evolve while preserving the M15 contract:
required resources/services, bring-your-own-network and bring-your-own-managed-resource
variables, honest outputs, and secret handling that keeps raw app/integration secret
values out of the recommended state path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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


def test_gcp_network_docs_describe_module_created_default():
    variables = _read(GCP_DIR / "variables.tf")
    vpc_block = _variable_block(variables, "existing_vpc_id")
    subnet_block = _variable_block(variables, "existing_subnet_ids")

    # The module now CREATES a private network/subnet by default when no
    # existing VPC is provided; the variable docs must say so and drop the old
    # "default egress" / "not done here" parity-gap wording.
    assert "creates" in vpc_block.lower()
    assert "network" in vpc_block.lower()
    assert "creates" in subnet_block.lower()
    assert "subnet" in subnet_block.lower()
    assert "default egress" not in variables.lower()
    assert "not done here" not in variables.lower()


def test_gcp_byo_vpc_requires_subnets_and_validation_is_documented():
    variables = _read(GCP_DIR / "variables.tf")
    readme = _read(GCP_DIR / "README.md")
    subnet_block = _variable_block(variables, "existing_subnet_ids")

    # Direct VPC egress cannot be configured with only a network name; the Cloud
    # Run template also needs at least one subnet self-link. Fail during variable
    # validation instead of allowing an apply to fail later or silently using
    # default egress.
    assert "validation" in subnet_block
    assert "existing_vpc_id" in subnet_block
    assert "length(var.existing_subnet_ids) > 0" in subnet_block
    assert "existing_subnet_ids must be provided when existing_vpc_id is set" in subnet_block

    lowered = readme.lower()
    assert "existing_subnet_ids must be provided when existing_vpc_id is set" in lowered


def test_gcp_cross_variable_validation_requires_terraform_1_9_or_newer():
    main = _read(GCP_DIR / "main.tf")
    variables = _read(GCP_DIR / "variables.tf")

    assert "var.existing_vpc_id" in _variable_block(variables, "existing_subnet_ids")
    assert 'required_version = ">= 1.9"' in main


# --- GCP module-created private network (default, no-BYO path) ---------------


def test_gcp_creates_private_network_and_subnet_by_default():
    # When no existing VPC is provided, the module must create its own private
    # VPC network + regional subnet, gated to the no-BYO-network path.
    main = _read(GCP_DIR / "main.tf")

    net = _resource_block(main, "google_compute_network", "this")
    assert net, "missing module-created google_compute_network.this"
    assert "count" in net and "local.create_vpc" in net, "network not gated on create_vpc"
    assert "auto_create_subnetworks = false" in net, "created VPC must own its subnets explicitly"

    subnet = _resource_block(main, "google_compute_subnetwork", "this")
    assert subnet, "missing module-created google_compute_subnetwork.this"
    assert "count" in subnet and "local.create_vpc" in subnet, "subnet not gated on create_vpc"
    assert "google_compute_network.this" in subnet, "subnet not attached to the created network"
    assert "ip_cidr_range" in subnet, "subnet missing ip_cidr_range"
    assert "var.region" in subnet, "subnet not regional to var.region"


def test_gcp_create_vpc_local_gates_on_empty_existing_vpc():
    main = _read(GCP_DIR / "main.tf")
    create_local = next(
        (ln for ln in main.splitlines() if ln.strip().startswith("create_vpc")),
        "",
    )
    assert create_local, "missing create_vpc local"
    assert 'var.existing_vpc_id == ""' in create_local


def test_gcp_effective_network_resolves_created_or_byo():
    # effective_vpc_id / effective_subnet_ids must resolve to the module-created
    # network/subnet by default, and to the bring-your-own refs when provided.
    main = _read(GCP_DIR / "main.tf")

    vpc_local = next(
        (ln for ln in main.splitlines() if ln.strip().startswith("effective_vpc_id")),
        "",
    )
    assert vpc_local, "missing effective_vpc_id local"
    assert "local.create_vpc" in vpc_local
    assert "google_compute_network.this[0].id" in vpc_local
    assert "var.existing_vpc_id" in vpc_local

    subnet_local = next(
        (ln for ln in main.splitlines() if ln.strip().startswith("effective_subnet_ids")),
        "",
    )
    assert subnet_local, "missing effective_subnet_ids local"
    assert "local.create_vpc" in subnet_local
    assert "google_compute_subnetwork.this[0].id" in subnet_local
    assert "var.existing_subnet_ids" in subnet_local


def test_gcp_cloud_run_vpc_access_uses_effective_refs_for_all_services():
    # Every Cloud Run service must attach to the EFFECTIVE network/subnet (the
    # module-created one by default, BYO when provided), not only on the BYO
    # path. The old BYO-only `byo_network` gate must be gone.
    main = _read(GCP_DIR / "main.tf")
    assert "byo_network" not in main, "stale BYO-only network gate still present"
    for name in GCP_CLOUD_RUN_SERVICES:
        block = _resource_block(main, "google_cloud_run_v2_service", name)
        assert block, f"missing google_cloud_run_v2_service {name}"
        assert "vpc_access" in block, f"{name} has no vpc_access block"
        assert "local.effective_vpc_id" in block, f"{name} vpc_access ignores effective_vpc_id"
        assert (
            "local.effective_subnet_ids" in block
        ), f"{name} vpc_access ignores effective_subnet_ids"


def test_gcp_network_refs_output_indicates_module_created_or_byo():
    outputs = _read(GCP_DIR / "outputs.tf")
    assert 'output "network_refs"' in outputs
    assert "local.effective_vpc_id" in outputs
    assert "local.effective_subnet_ids" in outputs
    # Surface whether module-created networking is in use vs. BYO refs.
    assert "local.create_vpc" in outputs
    assert "module_created" in outputs


def test_gcp_byo_vpc_subnet_validation_is_preserved():
    # Closing the new-network gap must not weaken the existing BYO guard: an
    # existing_vpc_id still requires non-empty existing_subnet_ids.
    variables = _read(GCP_DIR / "variables.tf")
    subnet_block = _variable_block(variables, "existing_subnet_ids")
    assert "validation" in subnet_block
    assert "length(var.existing_subnet_ids) > 0" in subnet_block
    assert "existing_subnet_ids must be provided when existing_vpc_id is set" in subnet_block


def test_gcp_readme_drops_networking_creation_parity_gap():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    # The networking-creation gap is now closed: the module creates a private
    # network/subnet by default, so the old "not yet wired" wording is gone.
    assert "not yet wired" not in lowered, "networking-creation gap still listed"
    assert "creates" in lowered and "network" in lowered
    # Remaining honest gaps stay explicit.
    assert "load balancer" in lowered
    assert "dns" in lowered
    assert "live" in lowered and "smoke" in lowered


def test_gcp_secret_hygiene_preserved_after_network_wiring():
    # The network slice must not introduce secret versions or raw secret inputs.
    main = _read(GCP_DIR / "main.tf")
    variables = _read(GCP_DIR / "variables.tf").lower()
    assert "google_secret_manager_secret_version" not in main
    for token in RAW_SECRET_TOKENS:
        assert f'variable "{token}"' not in variables, token


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


# --- AWS public ingress (ALB + target group + listener) — M15 slice ---------


def test_aws_main_defines_public_alb_target_group_and_listener():
    main = _read(AWS_DIR / "main.tf")
    assert 'resource "aws_lb"' in main, "missing public ALB"
    assert 'resource "aws_lb_target_group"' in main, "missing target group"
    assert 'resource "aws_lb_listener"' in main, "missing listener"


def test_aws_target_group_uses_ip_target_type_in_effective_vpc():
    main = _read(AWS_DIR / "main.tf")
    tg = _resource_block(main, "aws_lb_target_group", "api")
    assert tg, "missing aws_lb_target_group api"
    # Fargate awsvpc tasks register by IP, not by instance.
    assert 'target_type = "ip"' in tg
    # Target group lives in the effective (created/BYO) VPC.
    assert "local.effective_vpc_id" in tg


def test_aws_listener_forwards_to_api_target_group():
    main = _read(AWS_DIR / "main.tf")
    listener = _resource_block(main, "aws_lb_listener", "api")
    assert listener, "missing aws_lb_listener api"
    assert "aws_lb.this.arn" in listener, "listener not bound to the ALB"
    assert "aws_lb_target_group.api.arn" in listener, "listener does not forward to the api target group"


def test_aws_control_plane_service_wires_load_balancer_to_target_group():
    main = _read(AWS_DIR / "main.tf")
    block = _resource_block(main, "aws_ecs_service", "control_plane")
    assert block, "missing control_plane service"
    assert "load_balancer {" in block, "control_plane service has no load_balancer block"
    assert "aws_lb_target_group.api.arn" in block, "load_balancer block does not reference the target group"
    assert "container_name" in block, "load_balancer block does not name the container"
    assert "var.api_container_port" in block, "load_balancer block does not reference the container port"


def test_aws_control_plane_task_def_exposes_api_container_port():
    main = _read(AWS_DIR / "main.tf")
    block = _resource_block(main, "aws_ecs_task_definition", "control_plane")
    assert block, "missing control_plane task definition"
    assert "portMappings" in block, "control_plane container exposes no port for the target group"
    assert "var.api_container_port" in block


def test_aws_api_container_port_is_non_secret_variable_with_default():
    variables = _read(AWS_DIR / "variables.tf")
    block = _variable_block(variables, "api_container_port")
    assert block, "missing api_container_port variable"
    assert "default" in block, "api_container_port has no default"
    lowered = block.lower()
    for token in RAW_SECRET_TOKENS:
        assert token not in lowered, f"{token} leaked into api_container_port"


def test_aws_api_url_outputs_derive_from_alb_dns_not_placeholder():
    outputs = _read(AWS_DIR / "outputs.tf")
    # The reachable API base URL derives from the ALB DNS name (DNS/ACM not yet
    # wired), so api/bootstrap/webhook URLs are real endpoints, not placeholders.
    assert "aws_lb.this.dns_name" in outputs, "api url not derived from the ALB DNS name"
    assert "TODO" not in outputs, "outputs still contain a TODO placeholder"


def test_aws_main_has_no_ingress_todo_placeholder():
    main = _read(AWS_DIR / "main.tf")
    # Targeted: the old ingress placeholder must be gone. A blanket "no TODO
    # anywhere" assertion is brittle (it would flag an unrelated honest TODO),
    # so we only forbid the specific ingress placeholder this slice replaced.
    assert "TODO(ingress)" not in main, "old TODO(ingress) placeholder still present in main.tf"


# --- AWS ALB subnet contract (independent-review blocker fix) ---------------
#
# An ALB needs public/edge subnets distinct from the private service subnets.
# existing_alb_subnet_ids carries those; the ALB wires to a dedicated local that
# falls back to the service subnets only for scaffold/default compatibility.


def test_aws_alb_subnet_ids_variable_validates_at_least_two():
    variables = _read(AWS_DIR / "variables.tf")
    block = _variable_block(variables, "existing_alb_subnet_ids")
    assert block, "missing existing_alb_subnet_ids variable"

    # Public/edge intent is documented (distinct from the private service subnets).
    lowered = block.lower()
    assert "public" in lowered or "edge" in lowered, "alb subnet var does not document public/edge intent"

    # An Application Load Balancer needs at least two subnets; enforce it when
    # the customer provides the list (empty stays allowed for the fallback).
    assert "validation" in block, "existing_alb_subnet_ids has no validation"
    assert "length(var.existing_alb_subnet_ids)" in block
    assert ">= 2" in block, "validation does not require at least two ALB subnets"

    # Not a secret input.
    for token in RAW_SECRET_TOKENS:
        assert token not in lowered, f"{token} leaked into existing_alb_subnet_ids"


def test_aws_lb_uses_dedicated_alb_subnet_local():
    main = _read(AWS_DIR / "main.tf")

    # A dedicated local picks the ALB (edge) subnets, falling back to the
    # service subnets only for scaffold/default compatibility.
    assert "effective_alb_subnet_ids" in main, "missing effective_alb_subnet_ids local"
    assert "var.existing_alb_subnet_ids" in main, "ALB subnet local does not consume the new variable"

    lb = _resource_block(main, "aws_lb", "this")
    assert lb, "missing aws_lb this"
    assert "local.effective_alb_subnet_ids" in lb, "ALB does not wire to the dedicated edge-subnet local"
    assert "local.effective_subnet_ids" not in lb, "ALB still wires directly to the private service subnets"


def test_aws_ecs_services_use_private_service_subnets_not_alb_subnets():
    main = _read(AWS_DIR / "main.tf")
    for name in ("control_plane", "worker", "scheduler"):
        block = _resource_block(main, "aws_ecs_service", name)
        assert block, f"missing aws_ecs_service {name}"
        assert (
            "local.effective_subnet_ids" in block
        ), f"{name} service must stay on the private service subnets"
        assert (
            "local.effective_alb_subnet_ids" not in block
        ), f"{name} service must not run in the public ALB subnets"


def test_aws_outputs_do_not_overstate_reachability():
    outputs = _read(AWS_DIR / "outputs.tf").lower()
    # URLs derive from the ALB DNS name, but reachability is conditional: it
    # requires the ALB subnets to have public routing.
    assert "reachable now" not in outputs, 'outputs still claim the endpoint is "reachable now"'
    assert "public" in outputs and "rout" in outputs, "outputs do not condition reachability on public routing"


def test_aws_readme_conditions_reachability_on_public_alb_subnets():
    readme = _read(AWS_DIR / "README.md")
    lowered = readme.lower()
    # The new public/edge ALB subnet knob is documented.
    assert "existing_alb_subnet_ids" in readme, "README does not document existing_alb_subnet_ids"
    # No unconditional reachability claim; reachability requires public routing.
    assert "reachable now" not in lowered, 'README still claims the endpoint is "reachable now"'
    assert "yields an endpoint reachable" not in lowered, "README still claims apply yields a reachable endpoint unconditionally"
    assert "public" in lowered and "rout" in lowered, "README does not condition reachability on public ALB subnet routing"


def test_aws_main_comment_does_not_claim_unconditional_reachability():
    main = _read(AWS_DIR / "main.tf").lower()
    assert "reachable now" not in main, 'main.tf comment still claims "reachable now"'
    assert "yields a reachable" not in main, "main.tf comment still claims apply yields a reachable endpoint unconditionally"


# --- AWS public ALB subnets get real public routing (review blocker fix) -----
#
# The default path (module-created VPC, no BYO ALB subnets) must create public
# ALB subnets with IGW + public-route wiring rather than reusing the private
# service subnets, so a default apply can actually create an internet-facing ALB.


def test_aws_main_creates_public_alb_subnets_with_igw_and_routing():
    main = _read(AWS_DIR / "main.tf")

    # Internet gateway for the module-created VPC.
    igw = _resource_block(main, "aws_internet_gateway", "this")
    assert igw, "missing aws_internet_gateway for the created VPC"
    assert "aws_vpc.this" in igw, "IGW not attached to the created VPC"

    # Public route table with a default route to the IGW.
    rt = _resource_block(main, "aws_route_table", "alb")
    assert rt, "missing aws_route_table alb for public ALB routing"
    assert "0.0.0.0/0" in rt, "ALB route table has no default route"
    assert "aws_internet_gateway.this" in rt, "ALB default route does not target the IGW"

    # Public ALB subnets, clearly public.
    alb_subnet = _resource_block(main, "aws_subnet", "alb")
    assert alb_subnet, "missing aws_subnet alb (public ALB subnets)"
    assert "map_public_ip_on_launch = true" in alb_subnet, "ALB subnets are not clearly public"

    # Route table association binding ALB subnets to the public route table.
    assoc = _resource_block(main, "aws_route_table_association", "alb")
    assert assoc, "missing aws_route_table_association alb"
    assert "aws_subnet.alb" in assoc, "association does not bind the ALB subnets"
    assert "aws_route_table.alb" in assoc, "association does not bind the public route table"


def test_aws_create_alb_subnets_local_gates_on_created_vpc_and_empty_byo():
    main = _read(AWS_DIR / "main.tf")
    assert "create_alb_subnets" in main, "missing create_alb_subnets local"

    start = main.find("create_alb_subnets = ")
    assert start != -1, "create_alb_subnets is not assigned"
    line = main[start : main.find("\n", start)]
    # Module creates public ALB subnets only when it creates the VPC and no BYO
    # ALB subnets were supplied.
    assert "local.create_vpc" in line, "create_alb_subnets does not gate on creating the VPC"
    assert "length(var.existing_alb_subnet_ids) == 0" in line, "create_alb_subnets does not gate on empty BYO ALB subnets"


def test_aws_effective_alb_subnets_use_created_public_subnets_not_private_service_subnets():
    main = _read(AWS_DIR / "main.tf")
    start = main.find("effective_alb_subnet_ids = ")
    assert start != -1, "missing effective_alb_subnet_ids local assignment"
    line = main[start : main.find("\n", start)]

    # Module-created path uses the public ALB subnets; BYO path uses the public
    # ALB subnet IDs. It must never fall back to the private service subnets.
    assert "aws_subnet.alb[*].id" in line, "ALB subnet local does not use the module-created public subnets"
    assert "var.existing_alb_subnet_ids" in line, "ALB subnet local does not use the BYO public ALB subnets"
    assert "effective_subnet_ids" not in line, "ALB subnet local still falls back to the private service subnets"


def test_aws_lb_consumes_effective_alb_subnet_local():
    main = _read(AWS_DIR / "main.tf")
    lb = _resource_block(main, "aws_lb", "this")
    assert lb, "missing aws_lb this"
    assert "local.effective_alb_subnet_ids" in lb, "ALB does not wire to the effective ALB subnet local"


def test_aws_alb_subnet_ids_validation_requires_two_when_byo_vpc():
    variables = _read(AWS_DIR / "variables.tf")
    block = _variable_block(variables, "existing_alb_subnet_ids")
    assert block, "missing existing_alb_subnet_ids variable"
    assert "validation" in block, "existing_alb_subnet_ids has no validation"

    # BYO VPC requires at least two public ALB subnets — empty is only allowed
    # when the module creates the VPC (and thus its public ALB subnets).
    assert "var.existing_vpc_id" in block, "ALB subnet validation does not gate empty-allowed on creating the VPC"
    assert ">= 2" in block, "ALB subnet validation does not require at least two subnets"


def test_aws_alb_subnet_ids_validation_rejects_mixed_created_vpc_with_byo_alb_subnets():
    variables = _read(AWS_DIR / "variables.tf")
    block = _variable_block(variables, "existing_alb_subnet_ids")
    assert block, "missing existing_alb_subnet_ids variable"

    # Mixed mode would create the ALB security group/target group in the new VPC
    # while attaching the ALB to caller-supplied subnets from another VPC. The
    # valid modes are: fully module-created network, or BYO VPC plus BYO public
    # ALB subnets.
    normalized = " ".join(block.split())
    assert 'var.existing_vpc_id != "" && length(var.existing_alb_subnet_ids) >= 2' in normalized


def test_tfvars_example_documents_existing_alb_subnet_ids():
    tfvars = _read(AWS_DIR / "terraform.tfvars.example")
    assert "existing_alb_subnet_ids" in tfvars, "tfvars example does not surface existing_alb_subnet_ids"

    # The example explains these are public/edge ALB subnets for the BYO path.
    lowered = tfvars.lower()
    assert "public" in lowered or "edge" in lowered, "tfvars does not explain ALB subnets are public/edge"


def test_aws_readme_states_byo_vpc_requires_public_alb_subnets_and_default_is_public():
    readme = _read(AWS_DIR / "README.md")
    lowered = readme.lower()

    assert "existing_alb_subnet_ids" in readme, "README does not document existing_alb_subnet_ids"
    # Default module-created ALB subnets now get public routing.
    assert "public rout" in lowered, "README does not say module-created ALB subnets get public routing"
    # BYO VPC requires public ALB subnets.
    assert "requir" in lowered, "README does not state BYO VPC requires public ALB subnets"
    # No claim that the fallback reuses the private service subnets for the ALB.
    assert (
        "reuse the private service subnets" not in lowered
        and "reuse `existing_subnet_ids`" not in readme
    ), "README still claims the ALB falls back to the private service subnets"


def test_aws_readme_drops_blocking_ingress_caveat_but_keeps_dns_acm_honesty():
    readme = _read(AWS_DIR / "README.md")
    # Ingress is now wired, so the blocking "apply yields nothing reachable"
    # caveat is gone.
    assert "does not yield a working deployment" not in readme
    lowered = readme.lower()
    # Remaining honest gaps are still stated: custom DNS, TLS/ACM (HTTP-only
    # listener), and an applied smoke test.
    assert "dns" in lowered
    assert "acm" in lowered or "tls" in lowered or "https" in lowered
    assert "smoke" in lowered


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


# --- AWS optional custom-domain HTTPS/TLS (M15 slice) -----------------------
#
# The default path stays ALB-DNS HTTP. Optionally a customer supplies an ACM
# certificate ARN (→ HTTPS listener on 443) and/or a Route53 hosted zone (→ alias
# record pointing the custom domain at the ALB). These are non-secret inputs; raw
# app/integration secret values still belong to bootstrap, not Terraform.


def _output_block(text: str, name: str) -> str:
    import re

    match = re.search(
        r'output "' + re.escape(name) + r'".*?(?=\noutput "|\Z)',
        text,
        re.DOTALL,
    )
    return match.group(0) if match else ""


def test_aws_variables_expose_optional_tls_dns_inputs():
    variables = _read(AWS_DIR / "variables.tf")
    for var_name in ("acm_certificate_arn", "route53_zone_id"):
        block = _variable_block(variables, var_name)
        assert block, f"missing optional TLS/DNS variable {var_name}"
        # Optional: empty-string default keeps the default ALB-HTTP path intact.
        assert 'default     = ""' in block or 'default = ""' in block, f"{var_name} is not optional (no empty default)"
        # Not a secret value.
        lowered = block.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} leaked into {var_name}"


def test_aws_https_listener_created_only_when_certificate_provided():
    main = _read(AWS_DIR / "main.tf")
    listener = _resource_block(main, "aws_lb_listener", "api_https")
    assert listener, "missing aws_lb_listener api_https"

    # Gated on a certificate being supplied (default empty → no HTTPS listener).
    assert 'var.acm_certificate_arn != ""' in listener, "HTTPS listener is not gated on the certificate ARN"
    assert "count" in listener, "HTTPS listener is not conditional"

    # HTTPS on 443 using the supplied certificate, reusing the existing ALB.
    assert "443" in listener
    assert '"HTTPS"' in listener
    assert "var.acm_certificate_arn" in listener, "listener does not use the certificate ARN"
    assert "aws_lb.this.arn" in listener, "HTTPS listener not bound to the existing ALB"

    # Forwards to the SAME existing API target group as the HTTP listener.
    assert "aws_lb_target_group.api.arn" in listener, "HTTPS listener does not forward to the api target group"


def test_aws_alb_security_group_allows_https_when_certificate_provided():
    main = _read(AWS_DIR / "main.tf")
    sg = _resource_block(main, "aws_security_group", "alb")
    assert sg, "missing aws_security_group alb"
    # HTTPS ingress (443) is opened only when a certificate is supplied.
    assert "443" in sg, "ALB security group does not allow HTTPS (443)"
    assert 'var.acm_certificate_arn != ""' in sg, "443 ingress is not gated on the certificate ARN"


def test_aws_route53_alias_record_created_only_when_zone_provided():
    main = _read(AWS_DIR / "main.tf")
    record = _resource_block(main, "aws_route53_record", "api")
    assert record, "missing aws_route53_record api"

    # Gated on a hosted zone being supplied (default empty → no DNS record).
    assert 'var.route53_zone_id != ""' in record, "DNS record is not gated on the hosted zone id"
    assert "count" in record, "DNS record is not conditional"

    # Alias record pointing the custom domain at the ALB DNS/zone.
    assert "var.route53_zone_id" in record, "record does not use the hosted zone id"
    assert "var.domain" in record, "record does not point the custom domain"
    assert "alias" in record, "record is not an ALB alias record"
    assert "aws_lb.this.dns_name" in record, "alias does not target the ALB DNS name"
    assert "aws_lb.this.zone_id" in record, "alias does not target the ALB hosted zone id"


def test_aws_http_listener_remains_unconditional_default_path():
    main = _read(AWS_DIR / "main.tf")
    http = _resource_block(main, "aws_lb_listener", "api")
    assert http, "missing default HTTP listener"
    # The default ALB-DNS HTTP path is preserved: the port-80 listener is not
    # made conditional on TLS/DNS being configured.
    assert "80" in http
    assert '"HTTP"' in http
    assert "count" not in http, "the default HTTP listener must stay unconditional"


def test_aws_outputs_expose_both_alb_http_and_effective_api_url():
    outputs = _read(AWS_DIR / "outputs.tf")

    # The raw ALB HTTP URL is always exposed.
    assert 'output "alb_http_url"' in outputs, "missing alb_http_url output"
    assert "aws_lb.this.dns_name" in outputs

    # The effective API URL is the custom HTTPS URL when a certificate is
    # configured, otherwise the ALB HTTP URL.
    assert "var.acm_certificate_arn" in outputs, "effective API URL is not conditioned on the certificate"
    assert "https://${var.domain}" in outputs, "effective API URL is not the custom HTTPS domain URL when configured"

    # agentops_api_url surfaces the effective URL.
    api_url = _output_block(outputs, "agentops_api_url")
    assert "local.api_base_url" in api_url, "agentops_api_url does not surface the effective base URL"


def test_aws_bootstrap_and_webhook_urls_derive_from_effective_api_url():
    outputs = _read(AWS_DIR / "outputs.tf")
    for out in (
        "bootstrap_url",
        "slack_webhook_url",
        "github_webhook_url",
        "linear_webhook_url",
        "jira_webhook_url",
    ):
        block = _output_block(outputs, out)
        assert block, f"missing output {out}"
        assert "local.api_base_url" in block, f"{out} does not derive from the effective API base URL"


def test_aws_smoke_hints_stay_honest_about_optional_dns_tls():
    outputs = _read(AWS_DIR / "outputs.tf")
    hints = _output_block(outputs, "smoke_test_hints").lower()
    assert hints, "missing smoke_test_hints output"
    # DNS/TLS is optional, and a live apply/smoke is still pending.
    assert "optional" in hints, "smoke hints do not flag DNS/TLS as optional"
    assert "https" in hints or "tls" in hints or "acm" in hints
    assert "dns" in hints


def test_aws_readme_documents_optional_tls_dns_path():
    readme = _read(AWS_DIR / "README.md")
    lowered = readme.lower()
    # Both optional knobs are documented by name.
    assert "acm_certificate_arn" in readme, "README does not document acm_certificate_arn"
    assert "route53_zone_id" in readme, "README does not document route53_zone_id"
    # The default path is still ALB-DNS HTTP; HTTPS is opt-in.
    assert "optional" in lowered
    assert "https" in lowered
    # Raw secrets remain a bootstrap concern, not Terraform inputs.
    assert "bootstrap" in lowered
    assert "raw" in lowered


def test_tfvars_example_documents_optional_tls_dns():
    tfvars = _read(AWS_DIR / "terraform.tfvars.example")
    assert "acm_certificate_arn" in tfvars, "tfvars example does not surface acm_certificate_arn"
    assert "route53_zone_id" in tfvars, "tfvars example does not surface route53_zone_id"
    # These stay commented/optional so the default apply is ALB-HTTP.
    lowered = tfvars.lower()
    assert "optional" in lowered or "https" in lowered, "tfvars does not explain the optional HTTPS path"


# --- AWS Route53-only HTTP custom-domain URL contract (M15 slice) -----------
#
# The optional DNS path is honest about its scheme. There are three effective
# customer-facing URL cases:
#   * acm_certificate_arn set                  → https://<domain> (443 listener)
#   * route53_zone_id set, acm cert empty      → http://<domain>  (Route53 alias
#                                                 over the existing HTTP listener)
#   * neither set                              → ALB DNS HTTP URL
# Previously a route53-only deployment aliased <domain> at the ALB but still
# advertised the raw ALB DNS HTTP URL, which is inconsistent with the alias.


def _local_expr(text: str, name: str) -> str:
    # Capture a local's assigned expression: from `<name>` up to the end of the
    # enclosing locals block (a `}` on its own line). The conditional is small.
    start = text.find(name)
    if start == -1:
        return ""
    end = text.find("\n}", start)
    return text[start : end if end != -1 else len(text)]


def test_aws_effective_api_url_three_way_tls_dns_selection():
    outputs = _read(AWS_DIR / "outputs.tf")
    expr = _local_expr(outputs, "api_base_url")
    assert expr, "missing api_base_url local"

    # HTTPS custom domain when an ACM certificate is supplied.
    assert 'var.acm_certificate_arn != ""' in expr, "api_base_url does not branch on the certificate"
    assert "https://${var.domain}" in expr, "api_base_url has no HTTPS custom-domain branch"

    # HTTP custom domain when only a Route53 zone is supplied (alias over HTTP).
    assert 'var.route53_zone_id != ""' in expr, "api_base_url does not branch on the route53 zone"
    assert "http://${var.domain}" in expr, "api_base_url has no HTTP custom-domain branch for route53-only"

    # ALB DNS HTTP URL when neither is supplied.
    assert "local.alb_http_url" in expr, "api_base_url has no ALB-HTTP fallback branch"

    # The certificate (HTTPS) wins over a bare route53 zone (HTTP).
    assert expr.index("acm_certificate_arn") < expr.index("route53_zone_id"), (
        "HTTPS certificate branch must take precedence over the route53-only HTTP branch"
    )


def test_aws_route53_only_zone_yields_http_custom_domain_url():
    outputs = _read(AWS_DIR / "outputs.tf")
    expr = _local_expr(outputs, "api_base_url")
    assert expr, "missing api_base_url local"

    # The route53-only branch produces the HTTP custom domain (not the raw ALB
    # DNS URL): the alias record is the only thing standing the domain up over
    # the existing HTTP listener.
    http_idx = expr.find("http://${var.domain}")
    zone_idx = expr.find('var.route53_zone_id != ""')
    assert http_idx != -1 and zone_idx != -1, "route53-only HTTP branch is missing"
    # The route53 condition is evaluated for the HTTP-domain branch.
    assert zone_idx < http_idx, "http custom-domain URL is not selected by the route53 zone condition"


def test_aws_readme_documents_route53_only_http_custom_domain():
    readme = _read(AWS_DIR / "README.md")
    lowered = readme.lower()
    assert "route53_zone_id" in readme, "README does not document route53_zone_id"
    # Honest about the scheme: a route53-only deployment (no ACM cert) serves the
    # custom domain over HTTP.
    assert "http://<domain>" in lowered or "http://" in lowered, "README does not show the HTTP custom-domain URL"
    # Explicitly ties the route53-only path to the http custom domain, not HTTPS.
    assert "without" in lowered or "only" in lowered, "README does not isolate the route53-only (no cert) case"


def test_aws_tfvars_documents_route53_only_http_custom_domain():
    tfvars = _read(AWS_DIR / "terraform.tfvars.example")
    assert "route53_zone_id" in tfvars, "tfvars example does not surface route53_zone_id"
    lowered = tfvars.lower()
    # The example explains that route53 alone aliases the domain over HTTP.
    assert "http://" in lowered, "tfvars does not document the route53-only HTTP custom-domain URL"


def test_aws_smoke_hints_surface_route53_only_http_custom_domain_selection():
    outputs = _read(AWS_DIR / "outputs.tf")
    hints = _output_block(outputs, "smoke_test_hints").lower()
    assert hints, "missing smoke_test_hints output"
    # Hints surface the route53-only HTTP custom-domain path / effective URL choice.
    assert "route53" in hints, "smoke hints do not mention the route53-only path"
    assert "http://" in hints, "smoke hints do not surface the HTTP custom-domain URL selection"


# --- AWS live-smoke helper (smoke.sh) — M15 slice ---------------------------
#
# A module-local executable helper an operator runs from deploy/terraform/aws-managed
# after configuring tfvars + AWS credentials. It must fail closed BEFORE any side
# effect when the Terraform/OpenTofu CLI is missing or AWS credentials/config are
# unavailable, default to a safe plan-only mode, use module-local commands (no
# root-relative -chdir), and surface post-apply smoke hints/outputs. It must never
# accept or echo raw app/integration secret values.


SMOKE_SCRIPT = AWS_DIR / "smoke.sh"
GCP_SMOKE_SCRIPT = GCP_DIR / "smoke.sh"


def test_aws_smoke_script_exists_is_executable_and_strict():
    assert SMOKE_SCRIPT.is_file(), "missing aws-managed/smoke.sh live-smoke helper"
    mode = SMOKE_SCRIPT.stat().st_mode
    assert mode & 0o111, "smoke.sh is not executable"
    text = _read(SMOKE_SCRIPT)
    assert text.startswith("#!"), "smoke.sh has no shebang"
    # Strict bash so a failed prerequisite check actually aborts the run.
    assert "set -euo pipefail" in text, "smoke.sh is not in strict mode"


def test_aws_smoke_script_requires_a_terraform_or_opentofu_cli():
    text = _read(SMOKE_SCRIPT)
    # Detects an available CLI (terraform or OpenTofu) rather than assuming one.
    assert "command -v" in text, "smoke.sh does not detect a CLI with command -v"
    assert "terraform" in text and "tofu" in text, "smoke.sh does not consider both terraform and tofu"


def test_aws_smoke_script_fails_closed_on_missing_prereqs_before_side_effects():
    text = _read(SMOKE_SCRIPT)
    # Verifies AWS credentials/config before doing anything with side effects.
    assert "get-caller-identity" in text, "smoke.sh does not verify AWS credentials/config"

    # The credential and CLI checks must run BEFORE any side-effecting CLI
    # invocation. Anchor on `-input=false`, which only appears on the real
    # init/plan/apply command lines (never in prose), so the ordering check is
    # not fooled by the words "plan"/"apply" in comments.
    cred_idx = text.find("get-caller-identity")
    cli_idx = text.find("command -v")
    first_side_effect = text.find("-input=false")
    assert cred_idx != -1 and cli_idx != -1
    assert first_side_effect != -1, "smoke.sh never runs a CLI command"
    assert cli_idx < first_side_effect, "CLI check must run before any side effect"
    assert cred_idx < first_side_effect, "credential check must run before any side effect"

    # plan and apply are both reachable commands.
    assert "apply" in text, "smoke.sh never runs apply"
    assert "plan" in text, "smoke.sh never runs plan"

    # Fails closed (non-zero exit) when a prerequisite is missing.
    assert "exit 1" in text, "smoke.sh does not fail closed with a non-zero exit"


def test_aws_smoke_script_uses_module_local_commands_not_root_relative_chdir():
    text = _read(SMOKE_SCRIPT)
    # No root-relative -chdir hack — the helper runs from the module directory.
    assert "-chdir=deploy/terraform" not in text
    for command in ("init", "validate", "plan"):
        assert command in text, f"smoke.sh does not run {command}"


def test_aws_smoke_script_defaults_to_safe_plan_only_mode():
    text = _read(SMOKE_SCRIPT)
    # Apply is opt-in; the default is a side-effect-light plan.
    assert "PLAN_ONLY" in text, "smoke.sh does not expose a PLAN_ONLY mode"


def test_aws_smoke_script_surfaces_post_apply_smoke_hints_and_outputs():
    text = _read(SMOKE_SCRIPT)
    assert "output" in text, "smoke.sh does not surface terraform outputs"
    assert "agentops_api_url" in text, "smoke.sh does not surface agentops_api_url"
    assert "smoke_test_hints" in text, "smoke.sh does not surface smoke_test_hints"


def test_aws_smoke_script_does_not_accept_or_echo_raw_secrets():
    text = _read(SMOKE_SCRIPT).lower()
    for token in RAW_SECRET_TOKENS:
        assert token not in text, f"{token} referenced in smoke.sh"


def test_aws_readme_documents_smoke_script():
    readme = _read(AWS_DIR / "README.md")
    lowered = readme.lower()
    assert "smoke.sh" in readme, "README does not document the smoke.sh helper"
    # Default plan-only safety is documented honestly.
    assert "plan_only" in lowered, "README does not document the PLAN_ONLY default"
    # The helper is run from inside the module directory.
    assert "./smoke.sh" in readme, "README does not show running ./smoke.sh from the module dir"


# --- GCP live-smoke helper parity (M15 slice) -------------------------------
#
# The GCP scaffold should provide the same module-local smoke-helper ergonomics
# as aws-managed while staying honest that the GCP module remains scaffold-level.


def test_gcp_smoke_script_exists_is_executable_and_strict():
    assert GCP_SMOKE_SCRIPT.is_file(), "missing gcp-managed/smoke.sh live-smoke helper"
    mode = GCP_SMOKE_SCRIPT.stat().st_mode
    assert mode & 0o111, "gcp smoke.sh is not executable"
    text = _read(GCP_SMOKE_SCRIPT)
    assert text.startswith("#!"), "gcp smoke.sh has no shebang"
    assert "set -euo pipefail" in text, "gcp smoke.sh is not in strict mode"


def test_gcp_smoke_script_requires_a_terraform_or_opentofu_cli_and_gcloud_auth():
    text = _read(GCP_SMOKE_SCRIPT)
    assert "command -v" in text, "gcp smoke.sh does not detect tools with command -v"
    assert "terraform" in text and "tofu" in text, "gcp smoke.sh does not consider both terraform and tofu"
    assert "gcloud" in text, "gcp smoke.sh does not require gcloud"
    assert "auth" in text and "list" in text, "gcp smoke.sh does not verify gcloud authentication"


def test_gcp_smoke_script_fails_closed_before_side_effects_and_defaults_plan_only():
    text = _read(GCP_SMOKE_SCRIPT)
    cli_idx = text.find("command -v")
    auth_idx = text.find("gcloud auth")
    first_side_effect = text.find("-input=false")
    assert cli_idx != -1 and auth_idx != -1
    assert first_side_effect != -1, "gcp smoke.sh never runs a Terraform/OpenTofu command"
    assert cli_idx < first_side_effect, "CLI checks must run before any Terraform/OpenTofu side effect"
    assert auth_idx < first_side_effect, "gcloud auth check must run before any Terraform/OpenTofu side effect"
    assert "exit 1" in text, "gcp smoke.sh does not fail closed with a non-zero exit"
    assert "PLAN_ONLY" in text, "gcp smoke.sh does not expose a PLAN_ONLY mode"
    assert "apply" in text and "plan" in text, "gcp smoke.sh must expose plan and opt-in apply paths"


def test_gcp_smoke_script_is_module_local_and_surfaces_outputs_without_raw_secrets():
    text = _read(GCP_SMOKE_SCRIPT)
    assert "-chdir=deploy/terraform" not in text
    for command in ("init", "validate", "plan"):
        assert command in text, f"gcp smoke.sh does not run {command}"
    assert "output" in text, "gcp smoke.sh does not surface Terraform/OpenTofu outputs"
    assert "agentops_api_url" in text, "gcp smoke.sh does not surface agentops_api_url"
    assert "smoke_test_hints" in text, "gcp smoke.sh does not surface smoke_test_hints"
    lowered = text.lower()
    for token in RAW_SECRET_TOKENS:
        assert token not in lowered, f"{token} referenced in gcp smoke.sh"


def test_gcp_readme_documents_smoke_script_and_plan_only_default():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    assert "smoke.sh" in readme, "GCP README does not document the smoke.sh helper"
    assert "plan_only" in lowered, "GCP README does not document the PLAN_ONLY default"
    assert "./smoke.sh" in readme, "GCP README does not show running ./smoke.sh from the module dir"
    assert "gcloud" in lowered, "GCP README does not document the gcloud prerequisite"


# --- post-apply API /healthz probe (M15 slice) ------------------------------
#
# On the opt-in apply path (PLAN_ONLY=0) the smoke helpers must PROVE the
# provisioned API endpoint responds before the run is considered successful:
# fetch the bare `agentops_api_url` output (`output -raw`), probe
# `${agentops_api_url}/healthz` with curl, and fail closed (non-zero) on an
# unhealthy/unreachable response. The probe runs only AFTER apply (not before
# prerequisites/plan), curl is required before applying when PLAN_ONLY=0, and the
# plan-only default must not require curl. No raw app/integration secrets.


def _healthz_line(text: str) -> str:
    # The line that actually probes the endpoint (an executable `curl ` call to
    # /healthz), not a header comment that merely mentions it.
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "/healthz" in line and "curl " in line:
            return line
    return ""


def _assert_smoke_health_probe(script_path: Path, plan_guard: str) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    # Fetch the bare API URL via `output -raw agentops_api_url`.
    assert (
        "output -raw agentops_api_url" in text
    ), f"{name}: smoke.sh does not fetch the bare agentops_api_url via output -raw"

    # Probe ${agentops_api_url}/healthz with curl.
    assert "/healthz" in text, f"{name}: smoke.sh does not probe the /healthz endpoint"
    health_line = _healthz_line(text)
    assert "curl" in health_line, f"{name}: /healthz is not probed with curl"
    # curl must fail on unhealthy HTTP responses (e.g. -f/--fail/-fsS), not just
    # on connection errors.
    assert (
        "--fail" in health_line or "-f" in health_line
    ), f"{name}: curl health probe does not fail on unhealthy HTTP status (no -f/--fail)"

    apply_idx = text.find("apply -input=false")
    # Anchor on the executable probe line, not the header comment mention.
    health_idx = text.find(health_line)
    url_idx = text.find("output -raw agentops_api_url")
    curl_req_idx = text.find("command -v curl")
    guard_idx = text.find(plan_guard)
    fail_closed_idx = text.rfind("exit 1")

    assert apply_idx != -1, f"{name}: smoke.sh never runs apply"
    assert guard_idx != -1, f"{name}: smoke.sh has no PLAN_ONLY guard"
    assert curl_req_idx != -1, f"{name}: smoke.sh does not require curl with command -v"

    # The probe runs only AFTER apply (not before prerequisites/plan).
    assert apply_idx < url_idx, f"{name}: API URL fetched before apply"
    assert apply_idx < health_idx, f"{name}: /healthz probed before apply"
    # URL is fetched before it is probed.
    assert url_idx < health_idx, f"{name}: /healthz probed before fetching the API URL"

    # curl is required before applying (fail clearly before the apply side effect)...
    assert curl_req_idx < apply_idx, f"{name}: curl requirement is not checked before apply"
    # ...but only on the apply path — the plan-only default must not require curl.
    assert (
        curl_req_idx > guard_idx
    ), f"{name}: curl is required even on the plan-only path (check precedes the PLAN_ONLY guard)"

    # Fail closed (non-zero) after apply when the probe is unhealthy/unreachable.
    assert (
        fail_closed_idx > health_idx
    ), f"{name}: no fail-closed exit 1 after the /healthz probe"


def test_aws_smoke_script_probes_healthz_after_apply_and_fails_closed():
    _assert_smoke_health_probe(SMOKE_SCRIPT, plan_guard='PLAN_ONLY" != "0"')


def test_gcp_smoke_script_probes_healthz_after_apply_and_fails_closed():
    _assert_smoke_health_probe(GCP_SMOKE_SCRIPT, plan_guard='PLAN_ONLY" != "0"')


def test_smoke_scripts_health_probe_does_not_reference_raw_secrets():
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/smoke.sh"


# --- GCP apply-path public-invoker preflight (M15 slice) --------------------
#
# The gcp-managed public API endpoint is IAM-gated unless enable_public_invoker
# is true (allUsers roles/run.invoker on the control-plane). A PLAN_ONLY=0 live
# smoke run with the default (false) would apply resources and then fail the
# post-apply /healthz probe with 403. The helper must fail closed BEFORE the
# apply side effect when the effective enable_public_invoker input is not true,
# while staying permissive in plan-only mode. This is a non-secret input
# preflight; raw app/integration secret values remain a bootstrap (M16) concern.


def _make_smoke_stub_bin(tmp: Path) -> Path:
    """Build stub terraform/gcloud/curl executables for driving smoke.sh.

    The terraform stub no-ops init/validate/plan, evaluates `terraform console`
    for `var.enable_public_invoker` from the TF_VAR_* env (mirroring real
    default-false / TF_VAR override behavior), returns a non-placeholder image
    for image preflight, and records that `apply` was reached by touching
    $APPLY_MARKER before exiting non-zero (so a reached apply is observable
    without provisioning anything).
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    terraform = bin_dir / "terraform"
    terraform.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  console)\n"
        '    q="$(cat)"\n'
        '    case "$q" in\n'
        "      *enable_public_invoker*) printf '%s\\n' \"${TF_VAR_enable_public_invoker:-false}\" ;;\n"
        "      *image*) printf 'gcr.io/example/app:v1\\n' ;;\n"
        "      *) printf '\\n' ;;\n"
        "    esac\n"
        "    ;;\n"
        "  output)\n"
        '    if [ "${2:-}" = "-raw" ]; then printf \'http://api.example.test\\n\'; else printf \'hint\\n\'; fi\n'
        "    ;;\n"
        "  apply)\n"
        '    : >"$APPLY_MARKER"\n'
        "    exit 1\n"
        "    ;;\n"
        "  *) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    terraform.chmod(0o755)

    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "auth" ] && [ "${2:-}" = "list" ]; then printf \'tester@example.com\\n\'; fi\n',
        encoding="utf-8",
    )
    gcloud.chmod(0o755)

    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    curl.chmod(0o755)

    return bin_dir


def _run_gcp_smoke(bin_dir: Path, apply_marker: Path, extra_env: dict[str, str]):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLAN_ONLY": "0",
        "TF_VAR_project": "example-project",
        "TF_VAR_region": "us-central1",
        "APPLY_MARKER": str(apply_marker),
    }
    env.pop("TF_VAR_enable_public_invoker", None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(GCP_SMOKE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_gcp_smoke_script_blocks_apply_unless_public_invoker_enabled(tmp_path):
    assert GCP_SMOKE_SCRIPT.is_file(), "missing gcp-managed/smoke.sh"
    apply_marker = tmp_path / "applied"
    bin_dir = _make_smoke_stub_bin(tmp_path)

    # --- blocked: public invoker NOT enabled → fail closed BEFORE apply -------
    blocked = _run_gcp_smoke(bin_dir, apply_marker, {})
    assert blocked.returncode != 0, "smoke.sh did not fail with public invoker disabled"
    assert not apply_marker.exists(), (
        "smoke.sh reached terraform apply with public invoker disabled "
        f"(stdout={blocked.stdout!r} stderr={blocked.stderr!r})"
    )
    assert "enable_public_invoker" in blocked.stderr, (
        "smoke.sh did not explain the enable_public_invoker requirement "
        f"(stderr={blocked.stderr!r})"
    )

    # --- permitted: enabled via TF_VAR → reaches apply preflight --------------
    if apply_marker.exists():
        apply_marker.unlink()
    permitted = _run_gcp_smoke(
        bin_dir, apply_marker, {"TF_VAR_enable_public_invoker": "true"}
    )
    assert apply_marker.exists(), (
        "smoke.sh did not reach apply when public invoker was enabled "
        f"(stdout={permitted.stdout!r} stderr={permitted.stderr!r})"
    )


def test_gcp_smoke_script_public_invoker_preflight_permits_plan_only(tmp_path):
    # Plan-only (the safe default) must stay permissive: the public-invoker
    # apply-path gate must not block a PLAN_ONLY=1 run even with the default
    # (disabled) public invoker.
    assert GCP_SMOKE_SCRIPT.is_file(), "missing gcp-managed/smoke.sh"
    apply_marker = tmp_path / "applied"
    bin_dir = _make_smoke_stub_bin(tmp_path)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLAN_ONLY": "1",
        "TF_VAR_project": "example-project",
        "TF_VAR_region": "us-central1",
        "APPLY_MARKER": str(apply_marker),
    }
    env.pop("TF_VAR_enable_public_invoker", None)
    result = subprocess.run(
        ["bash", str(GCP_SMOKE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, (
        "plan-only smoke.sh failed even though apply is not requested "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert not apply_marker.exists(), "plan-only run must never reach apply"


def test_readmes_document_post_apply_healthz_probe():
    for module_dir in (AWS_DIR, GCP_DIR):
        readme = _read(module_dir / "README.md")
        lowered = readme.lower()
        assert "/healthz" in readme, f"{module_dir.name}: README does not mention the /healthz probe"
        assert "probe" in lowered, f"{module_dir.name}: README does not describe the post-apply probe"


# --- GCP Cloud Run container images + shared runtime env (M15 slice) ---------
#
# Parity with the AWS task-definition slice: the GCP Cloud Run containers must
# consume customer-supplied non-secret image variables (no TODO literals) and
# carry a shared non-secret runtime env (profile + queue/topic/subscription /
# artifact / secret-prefix / database refs) on all three services, while the
# worker preserves its per-instance run-slot bound.

GCP_SERVICE_NAMES = ("control_plane", "worker", "scheduler")
GCP_IMAGE_VARS = ("control_plane_image", "worker_image", "scheduler_image")
GCP_TODO_IMAGE_LITERALS = (
    "TODO-control-plane-image",
    "TODO-worker-image",
    "TODO-scheduler-image",
)


def test_gcp_variables_expose_non_secret_container_image_inputs():
    variables = _read(GCP_DIR / "variables.tf")
    for var_name in GCP_IMAGE_VARS:
        block = _variable_block(variables, var_name)
        assert block, f"missing image variable {var_name}"
        # Placeholder default/example is provided so the contract is usable...
        assert "default" in block, f"{var_name} has no placeholder default"
        # ...but the image input is not a secret value.
        lowered = block.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} leaked into {var_name}"


def test_gcp_cloud_run_services_consume_image_variables_not_todo():
    main = _read(GCP_DIR / "main.tf")

    # The literal TODO image placeholders must be gone.
    for todo in GCP_TODO_IMAGE_LITERALS:
        assert todo not in main, f"{todo} still present"

    # Each service's container points at its image variable.
    for name in GCP_SERVICE_NAMES:
        block = _resource_block(main, "google_cloud_run_v2_service", name)
        assert block, f"missing google_cloud_run_v2_service {name}"
        assert (
            f"var.{name}_image" in block
        ), f"{name} service does not consume var.{name}_image"


def test_gcp_all_services_share_non_secret_runtime_env():
    main = _read(GCP_DIR / "main.tf")
    for name in GCP_SERVICE_NAMES:
        block = _resource_block(main, "google_cloud_run_v2_service", name)
        assert block, f"missing google_cloud_run_v2_service {name}"
        assert (
            "local.runtime_common_env" in block
        ), f"{name} service does not include the shared runtime env"


def test_gcp_worker_preserves_max_concurrent_runs_env():
    main = _read(GCP_DIR / "main.tf")

    worker = _resource_block(main, "google_cloud_run_v2_service", "worker")
    assert worker, "missing worker service"
    assert "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS" in worker
    assert "var.max_concurrent_runs" in worker

    # The per-instance run-slot bound is worker-specific, not on the other services.
    for other in ("control_plane", "scheduler"):
        block = _resource_block(main, "google_cloud_run_v2_service", other)
        assert (
            "AGENTOPS_WORKER_MAX_CONCURRENT_RUNS" not in block
        ), f"{other} should not advertise the worker run-slot bound"


def test_gcp_runtime_common_env_carries_profile_and_backend_refs():
    main = _read(GCP_DIR / "main.tf")
    env = _list_local(main, "runtime_common_env")
    assert env, "missing runtime_common_env local list"

    # AGENTOPS_RUNTIME_PROFILE=gcp-managed
    assert "AGENTOPS_RUNTIME_PROFILE" in env
    assert "gcp-managed" in env

    # Queue topic + subscription refs wired to the real Pub/Sub resources.
    assert "AGENTOPS_QUEUE" in env
    assert "google_pubsub_topic.runs" in env
    assert "google_pubsub_subscription.runs" in env

    # Artifact store ref (BYO-aware bucket).
    assert "AGENTOPS_ARTIFACT_BUCKET" in env
    assert "artifact" in env.lower()

    # Secret prefix/refs.
    assert "AGENTOPS_SECRET_PREFIX" in env

    # Database ref points at the database secret container, not a raw value, and
    # surfaces the non-secret Cloud SQL connection name.
    assert "AGENTOPS_DATABASE" in env
    assert 'google_secret_manager_secret.containers["database"]' in env
    assert "connection_name" in env.lower()


def test_gcp_tfvars_example_documents_container_image_refs():
    tfvars = _read(GCP_DIR / "terraform.tfvars.example")
    for var_name in GCP_IMAGE_VARS:
        assert var_name in tfvars, f"tfvars example does not surface {var_name}"


def test_gcp_readme_documents_image_refs_and_keeps_honesty():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    # Image refs are documented by name.
    for var_name in GCP_IMAGE_VARS:
        assert var_name in readme, f"README does not document {var_name}"
    # GCP remains scaffold-level; live apply/smoke is still pending.
    assert "scaffold" in lowered
    assert "parity" in lowered
    # Raw integration secrets remain a bootstrap concern, not Terraform inputs.
    assert "bootstrap" in lowered
    assert "raw" in lowered


# --- GCP IAM / service accounts (M15 slice) ---------------------------------
#
# Parity with the aws-managed task/execution IAM roles: the GCP Cloud Run
# services must run as a dedicated runtime service account that is granted
# least-reasonable, scoped access to exactly the backend refs advertised in
# local.runtime_common_env (Pub/Sub queue, GCS artifacts, Secret Manager
# containers, Cloud SQL), never the default Compute SA with broad scopes.

GCP_CLOUD_RUN_SERVICES = ("control_plane", "worker", "scheduler")


def test_gcp_defines_dedicated_runtime_service_account():
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(main, "google_service_account", "runtime")
    assert block, "missing dedicated google_service_account.runtime resource"
    # Account id derives from the name prefix (stable, non-secret).
    assert "account_id" in block
    assert "local.prefix" in block or "var.name_prefix" in block


def test_gcp_cloud_run_services_run_as_runtime_service_account():
    main = _read(GCP_DIR / "main.tf")
    for name in GCP_CLOUD_RUN_SERVICES:
        block = _resource_block(main, "google_cloud_run_v2_service", name)
        assert block, f"missing google_cloud_run_v2_service {name}"
        assert (
            "service_account = google_service_account.runtime.email" in block
        ), f"{name} Cloud Run service does not run as the dedicated runtime SA"


def test_gcp_runtime_sa_gets_scoped_pubsub_access():
    # runtime_common_env advertises the Pub/Sub topic + subscription, so the
    # runtime SA must be able to publish to the topic and consume the
    # subscription, scoped to those exact resources.
    main = _read(GCP_DIR / "main.tf")

    publisher = _resource_block(main, "google_pubsub_topic_iam_member", "runtime_publisher")
    assert publisher, "missing Pub/Sub topic publisher binding for the runtime SA"
    assert "roles/pubsub.publisher" in publisher
    assert "google_pubsub_topic.runs" in publisher
    assert "google_service_account.runtime.email" in publisher

    subscriber = _resource_block(
        main, "google_pubsub_subscription_iam_member", "runtime_subscriber"
    )
    assert subscriber, "missing Pub/Sub subscription subscriber binding for the runtime SA"
    assert "roles/pubsub.subscriber" in subscriber
    assert "google_pubsub_subscription.runs" in subscriber
    assert "google_service_account.runtime.email" in subscriber


def test_gcp_runtime_sa_gets_scoped_artifact_bucket_access():
    # The artifact bucket ref (BYO-aware) is advertised to the runtime; the SA
    # must get object read/write on the *effective* bucket, including the BYO
    # bucket-name path, not a project-wide storage role.
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(main, "google_storage_bucket_iam_member", "runtime_artifacts")
    assert block, "missing GCS artifact bucket binding for the runtime SA"
    assert "roles/storage.object" in block, "binding is not object-scoped"
    assert "local.effective_artifact_bucket" in block, "binding ignores the BYO bucket path"
    assert "google_service_account.runtime.email" in block


def test_gcp_runtime_sa_gets_secret_accessor_on_created_containers():
    # Secret Manager containers carry the runtime/integration secrets bootstrap
    # fills; the runtime SA must read them (accessor, not admin) scoped to the
    # created containers, with no secret VERSION resource added.
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(
        main, "google_secret_manager_secret_iam_member", "runtime_secret_accessor"
    )
    assert block, "missing Secret Manager accessor binding for the runtime SA"
    assert "roles/secretmanager.secretAccessor" in block
    assert "google_secret_manager_secret.containers" in block
    assert "google_service_account.runtime.email" in block
    # Read access only — do not grant admin and do not materialise secret values.
    assert "secretmanager.admin" not in block
    assert "google_secret_manager_secret_version" not in main


def test_gcp_runtime_sa_gets_cloud_sql_client_access():
    # The runtime connects to Cloud SQL (managed or BYO connection name), so the
    # SA must hold the Cloud SQL client role.
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(main, "google_project_iam_member", "runtime_cloudsql")
    assert block, "missing Cloud SQL client binding for the runtime SA"
    assert "roles/cloudsql.client" in block
    assert "google_service_account.runtime.email" in block


def test_gcp_readme_drops_iam_parity_gap_keeps_remaining_gaps():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()

    # The stale IAM/service-accounts "not yet" gap must be gone now that the
    # runtime SA + scoped bindings exist.
    assert "are not yet defined" not in lowered, "README still lists IAM as an unwired gap"
    # ...and the README should describe the now-wired, scoped runtime SA.
    assert "service account" in lowered
    assert "scoped" in lowered or "least" in lowered

    # Remaining honest parity gaps stay explicit. (Networking creation is now
    # wired — see test_gcp_readme_drops_networking_creation_parity_gap — so it
    # is no longer asserted as an open gap here.)
    assert "load balancer" in lowered, "load balancer/DNS gap dropped"
    assert "dns" in lowered, "DNS gap dropped"
    assert "live" in lowered and "smoke" in lowered, "live apply/smoke gap dropped"


def test_gcp_name_prefix_validation_covers_runtime_service_account_id():
    # GCP service-account account_id is stricter than many resource names:
    # `${name_prefix}-runtime` must stay <=30 chars, lowercase/hyphen safe, and
    # start with a letter. The input contract must validate that before apply.
    variables = _read(GCP_DIR / "variables.tf")
    block = _variable_block(variables, "name_prefix")
    assert block, "missing name_prefix variable"
    assert "validation" in block, "name_prefix lacks validation for the runtime service account id"
    assert "length(var.name_prefix) <= 22" in block
    assert "regex" in block and "[a-z" in block.lower()
    assert "service account" in block.lower()


def test_gcp_readme_is_honest_that_cloudsql_iam_is_project_scoped():
    # Most runtime bindings are resource-scoped, but roles/cloudsql.client is a
    # project-level IAM binding. README must not imply every binding is resource
    # scoped.
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    assert "roles/cloudsql.client" in readme
    assert "project-level" in lowered or "project scoped" in lowered or "project-scoped" in lowered


# --- GCP public API endpoint contract (M15 slice) ---------------------------
#
# Parity with the aws-managed ALB-DNS honesty: the gcp-managed control-plane has
# no provisioned custom-domain/load-balancer, so the DEFAULT public API endpoint
# must be the Cloud Run control-plane service URI, not an unprovisioned
# `https://${var.domain}`. Public unauthenticated access is an opt-in toggle
# (allUsers + roles/run.invoker on the control-plane only). A custom domain
# mapping is optional and only created when explicitly configured. All of these
# are non-secret inputs.


def test_gcp_api_url_outputs_derive_from_cloud_run_uri_not_unprovisioned_domain():
    outputs = _read(GCP_DIR / "outputs.tf")
    # The default API endpoint is the Cloud Run control-plane service URI.
    assert (
        "google_cloud_run_v2_service.control_plane.uri" in outputs
    ), "api url not derived from the Cloud Run control-plane service URI"
    # The old unconditional custom-domain URL must be gone.
    assert (
        'api_base_url = "https://${var.domain}"' not in outputs
    ), "api_base_url still hard-codes the unprovisioned custom domain"
    # A custom domain is only the effective endpoint when explicitly configured.
    assert (
        "var.enable_custom_domain" in outputs
    ), "effective API URL is not conditioned on the custom-domain toggle"


def test_gcp_outputs_expose_cloud_run_uri_and_effective_api_url():
    outputs = _read(GCP_DIR / "outputs.tf")
    # The raw Cloud Run service URI is always exposed (analogous to alb_http_url).
    assert 'output "cloud_run_api_url"' in outputs, "missing cloud_run_api_url output"
    assert "google_cloud_run_v2_service.control_plane.uri" in outputs
    # agentops_api_url surfaces the effective base URL.
    api_url = _output_block(outputs, "agentops_api_url")
    assert "local.api_base_url" in api_url, "agentops_api_url does not surface the effective base URL"


def test_gcp_bootstrap_and_webhook_urls_derive_from_effective_api_url():
    outputs = _read(GCP_DIR / "outputs.tf")
    for out in (
        "bootstrap_url",
        "slack_webhook_url",
        "github_webhook_url",
        "linear_webhook_url",
        "jira_webhook_url",
    ):
        block = _output_block(outputs, out)
        assert block, f"missing output {out}"
        assert "local.api_base_url" in block, f"{out} does not derive from the effective API base URL"


def test_gcp_public_invoker_binding_is_variable_gated_alluser_run_invoker():
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(
        main, "google_cloud_run_v2_service_iam_member", "control_plane_public"
    )
    assert block, "missing variable-gated public invoker binding for the control-plane"
    # allUsers + roles/run.invoker on the control-plane Cloud Run service only.
    assert "roles/run.invoker" in block
    assert "allUsers" in block
    assert "google_cloud_run_v2_service.control_plane" in block
    # Opt-in only: gated on the non-secret enable_public_invoker toggle.
    assert "var.enable_public_invoker" in block, "public invoker binding is not gated on the toggle"
    assert "count" in block, "public invoker binding is not conditional"


def test_gcp_public_invoker_not_granted_to_worker_or_scheduler():
    main = _read(GCP_DIR / "main.tf")
    # The public unauthenticated invoker is for the control-plane API only — the
    # worker/scheduler services must never get an allUsers run.invoker binding.
    for other in ("worker", "scheduler"):
        block = _resource_block(
            main, "google_cloud_run_v2_service_iam_member", f"{other}_public"
        )
        assert not block, f"{other} must not have a public invoker binding"


def test_gcp_public_invoker_variable_is_non_secret_bool_default_false():
    variables = _read(GCP_DIR / "variables.tf")
    block = _variable_block(variables, "enable_public_invoker")
    assert block, "missing enable_public_invoker variable"
    assert "bool" in block, "enable_public_invoker is not a bool toggle"
    assert "default     = false" in block or "default = false" in block, "default is not false (safe)"
    lowered = block.lower()
    for token in RAW_SECRET_TOKENS:
        assert token not in lowered, f"{token} leaked into enable_public_invoker"


def test_gcp_optional_custom_domain_mapping_is_gated_and_non_secret():
    main = _read(GCP_DIR / "main.tf")
    block = _resource_block(main, "google_cloud_run_domain_mapping", "control_plane")
    assert block, "missing optional Cloud Run domain mapping for the control-plane"
    # Created only when explicitly configured (toggle + a domain value).
    assert "var.enable_custom_domain" in block, "domain mapping is not gated on the toggle"
    assert "var.domain" in block, "domain mapping does not use the configured domain"
    assert "count" in block, "domain mapping is not conditional"
    # Routes the custom domain at the control-plane service.
    assert (
        "google_cloud_run_v2_service.control_plane.name" in block
    ), "domain mapping does not route to the control-plane service"


def test_gcp_custom_domain_variables_are_optional_and_non_secret():
    variables = _read(GCP_DIR / "variables.tf")

    enable = _variable_block(variables, "enable_custom_domain")
    assert enable, "missing enable_custom_domain variable"
    assert "bool" in enable, "enable_custom_domain is not a bool toggle"
    assert "default     = false" in enable or "default = false" in enable

    domain = _variable_block(variables, "domain")
    assert domain, "missing domain variable"
    # Optional: empty-string default keeps the Cloud Run URI as the endpoint.
    assert 'default     = ""' in domain or 'default = ""' in domain, "domain is not optional (no empty default)"

    for block in (enable, domain):
        lowered = block.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered


def test_gcp_smoke_hints_honest_about_cloud_run_uri_and_public_invoker():
    outputs = _read(GCP_DIR / "outputs.tf")
    hints = _output_block(outputs, "smoke_test_hints").lower()
    assert hints, "missing smoke_test_hints output"
    # The default endpoint is the Cloud Run service URI...
    assert "cloud run" in hints, "smoke hints do not mention the Cloud Run service URI default"
    # ...public access is opt-in via the invoker toggle...
    assert "invoker" in hints, "smoke hints do not flag the public-invoker requirement"
    # ...custom domain is optional...
    assert "optional" in hints, "smoke hints do not flag the custom domain as optional"
    # ...and a live apply/smoke is still pending.
    assert "smoke" in hints or "pending" in hints, "smoke hints do not flag live smoke as pending"


def test_gcp_readme_documents_public_invoker_and_cloud_run_uri_contract():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    # Default endpoint is the Cloud Run service URI.
    assert "cloud run" in lowered and "uri" in lowered, "README does not document the Cloud Run URI default endpoint"
    # Opt-in public invoker documented by name + role + member.
    assert "enable_public_invoker" in readme, "README does not document enable_public_invoker"
    assert "roles/run.invoker" in readme, "README does not document the run.invoker role"
    assert "allusers" in lowered, "README does not document the allUsers public binding"
    # Optional custom domain mapping documented.
    assert "enable_custom_domain" in readme, "README does not document enable_custom_domain"
    # Live apply/smoke still pending.
    assert "live" in lowered and "smoke" in lowered, "README drops the live apply/smoke gap"
    # Raw secrets remain a bootstrap concern.
    assert "bootstrap" in lowered
    assert "raw" in lowered


def test_gcp_tfvars_documents_public_invoker_and_optional_custom_domain():
    tfvars = _read(GCP_DIR / "terraform.tfvars.example")
    assert "enable_public_invoker" in tfvars, "tfvars example does not surface enable_public_invoker"
    assert "enable_custom_domain" in tfvars, "tfvars example does not surface enable_custom_domain"
    lowered = tfvars.lower()
    assert "optional" in lowered or "invoker" in lowered, "tfvars does not explain the optional public-endpoint path"


# --- apply-path preflight: refuse placeholder ':replace-me' images (M15 slice) -
#
# The container-image variables ship with ':replace-me' placeholder defaults so
# `plan` is exercisable, but an apply built from those placeholders can never yield
# a working deployment. On the opt-in apply path (PLAN_ONLY=0) both smoke helpers
# must fail closed BEFORE the apply side effect when any image variable still
# carries the ':replace-me' placeholder. The check is apply-path only (plan-only
# may still plan placeholder images) and must not introduce raw app/integration
# secrets. Both READMEs must document that the apply/smoke path requires replacing
# the placeholder images first.


def _assert_smoke_blocks_placeholder_images(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    # The apply path refuses to proceed while an image variable still carries the
    # ':replace-me' placeholder — applying those can never yield a working deploy.
    assert (
        "replace-me" in text
    ), f"{name}: smoke.sh has no ':replace-me' placeholder-image preflight before apply"

    placeholder_idx = text.find("replace-me")
    guard_idx = text.find('PLAN_ONLY" != "0"')
    apply_idx = text.find("apply -input=false")

    assert guard_idx != -1, f"{name}: smoke.sh has no PLAN_ONLY guard"
    assert apply_idx != -1, f"{name}: smoke.sh never runs apply"

    # Apply-path only: the placeholder preflight runs after the PLAN_ONLY guard so
    # plan-only can still plan placeholder images...
    assert (
        placeholder_idx > guard_idx
    ), f"{name}: placeholder-image preflight is not gated behind the PLAN_ONLY apply guard"
    # ...and BEFORE the apply side effect.
    assert (
        placeholder_idx < apply_idx
    ), f"{name}: placeholder-image preflight does not run before apply"

    # Fails closed (non-zero) before apply when a placeholder image remains.
    fail_idx = text.find("exit 1", placeholder_idx)
    assert (
        fail_idx != -1 and fail_idx < apply_idx
    ), f"{name}: placeholder-image preflight does not fail closed (exit 1) before apply"

    # Terraform/OpenTofu console only returns the final expression when multiple
    # expressions are piped at once; check each image variable independently so an
    # overridden scheduler image cannot mask placeholder control-plane/worker images.
    assert "for image_var in" in text, f"{name}: image preflight does not iterate image variables"
    assert "printf 'var.%s" in text, f"{name}: image preflight does not evaluate one Terraform variable per console call"
    assert "|| true" not in text[placeholder_idx:apply_idx], f"{name}: image preflight can fail open if Terraform console fails"
    assert "if ! image_value=" in text, f"{name}: image preflight does not fail closed on Terraform console errors"
    for image_var in ("control_plane_image", "worker_image", "scheduler_image"):
        assert image_var in text, f"{name}: image preflight does not include {image_var}"


def test_aws_smoke_script_blocks_placeholder_images_before_apply():
    _assert_smoke_blocks_placeholder_images(SMOKE_SCRIPT)


def test_gcp_smoke_script_blocks_placeholder_images_before_apply():
    _assert_smoke_blocks_placeholder_images(GCP_SMOKE_SCRIPT)


def test_smoke_placeholder_preflight_does_not_reference_raw_secrets():
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/smoke.sh"


def _smoke_section(readme: str) -> str:
    # Extract the live-smoke helper section (header contains "smoke.sh`)") up to
    # the next "## " heading, so the placeholder-image requirement is documented
    # against the apply/smoke path specifically.
    start = readme.find("smoke.sh`)")
    if start == -1:
        return ""
    line_start = readme.rfind("\n", 0, start)
    next_heading = readme.find("\n## ", start)
    end = next_heading if next_heading != -1 else len(readme)
    return readme[line_start:end]


def test_readmes_document_apply_requires_replacing_placeholder_images():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        # The apply/smoke path requires replacing the ':replace-me' placeholder
        # images first — documented in the smoke-helper section, not buried far
        # from the apply path.
        assert "placeholder" in lowered or "replace-me" in section, (
            f"{module_dir.name}: smoke-helper section does not mention the placeholder images"
        )
        assert "image" in lowered, (
            f"{module_dir.name}: smoke-helper section does not tie the requirement to the container images"
        )
        assert "replace" in lowered, (
            f"{module_dir.name}: smoke-helper section does not state placeholder images must be replaced before apply/smoke"
        )


# --- post-apply non-secret smoke transcript artifact (M15 slice) ------------
#
# On the opt-in apply path (PLAN_ONLY=0), AFTER the /healthz probe succeeds, both
# smoke helpers write a module-local, non-secret transcript an operator can attach
# to the still-pending M15 live-smoke evidence before marking the milestone Done.
# The transcript records the provider/profile, a UTC timestamp, the effective
# agentops_api_url, the /healthz success, and the smoke_test_hints output — never a
# raw app/integration secret value. The helper prints the transcript path. The
# transcript is apply-path only (it must not run in the plan-only default) and is
# written only after the probe. Both READMEs document the artifact, that it is
# produced only after a PLAN_ONLY=0 apply + healthy /healthz, and that it should be
# reviewed for secrets before sharing.


def _transcript_region(text: str) -> str:
    # Everything from the transcript-filename reference onward — the block that
    # builds and prints the transcript artifact.
    idx = text.find("smoke-transcript-")
    return text[idx:] if idx != -1 else ""


def _assert_smoke_writes_transcript(script_path: Path, profile: str) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    # A module-local, timestamped transcript artifact is written.
    assert (
        "smoke-transcript-" in text
    ), f"{name}: smoke.sh does not write a module-local smoke-transcript artifact"
    assert "date -u" in text, f"{name}: transcript is not UTC-timestamped (no date -u)"

    region = _transcript_region(text)

    # Records provider/profile, the effective API URL, the /healthz success, and
    # the smoke_test_hints output.
    assert profile in region, f"{name}: transcript does not record the {profile} provider/profile"
    assert "API_URL" in region, f"{name}: transcript does not record the effective agentops_api_url"
    assert "healthz" in region.lower(), f"{name}: transcript does not record the /healthz success"
    assert (
        "output smoke_test_hints" in region
    ), f"{name}: transcript does not include the smoke_test_hints output"

    # The transcript path is captured in a variable and printed for the operator.
    assert "TRANSCRIPT=" in text, f"{name}: smoke.sh does not capture the transcript path in a variable"
    printed = any(
        ("echo" in line) and ("TRANSCRIPT" in line) and not line.lstrip().startswith("#")
        for line in region.splitlines()
    )
    assert printed, f"{name}: smoke.sh does not print the transcript path for the operator"

    # Apply-path only and after the probe: the transcript is written after the
    # PLAN_ONLY guard, after apply, and after the executable /healthz probe line.
    transcript_idx = text.find("smoke-transcript-")
    guard_idx = text.find('PLAN_ONLY" != "0"')
    apply_idx = text.find("apply -input=false")
    health_idx = text.find(_healthz_line(text))

    assert guard_idx != -1 and apply_idx != -1
    assert transcript_idx > guard_idx, f"{name}: transcript is not gated behind the PLAN_ONLY apply guard"
    assert transcript_idx > apply_idx, f"{name}: transcript is written before apply"
    assert transcript_idx > health_idx, f"{name}: transcript is written before the /healthz probe"


def test_aws_smoke_script_writes_non_secret_transcript_after_healthz():
    _assert_smoke_writes_transcript(SMOKE_SCRIPT, "aws-managed")


def test_gcp_smoke_script_writes_non_secret_transcript_after_healthz():
    _assert_smoke_writes_transcript(GCP_SMOKE_SCRIPT, "gcp-managed")


def test_smoke_transcript_does_not_reference_raw_secrets():
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        region = _transcript_region(_read(script)).lower()
        assert region, f"{script.parent.name}: smoke.sh has no transcript region"
        for token in RAW_SECRET_TOKENS:
            assert token not in region, f"{token} referenced in {script.parent.name} transcript"


def test_readmes_document_smoke_transcript_artifact():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        # The transcript artifact is documented in the smoke-helper section.
        assert "transcript" in lowered, f"{module_dir.name}: smoke-helper section does not mention the transcript artifact"
        # Produced only on the apply path after a healthy /healthz probe.
        assert "plan_only=0" in lowered or "apply" in lowered, f"{module_dir.name}: transcript not tied to the apply path"
        assert "healthz" in lowered, f"{module_dir.name}: transcript not tied to the healthy /healthz probe"
        # Review for secrets before sharing, even though only non-secret outputs
        # are written.
        assert "secret" in lowered, f"{module_dir.name}: README does not mention reviewing the transcript for secrets"
        assert "review" in lowered, f"{module_dir.name}: README does not say to review the transcript before sharing"
        assert "shar" in lowered, f"{module_dir.name}: README does not say to review before sharing"


# --- smoke transcript is written atomically (M15 slice) ---------------------
#
# The transcript is operator evidence. A direct redirect into
# smoke-transcript-*.log leaves a half-written file if `terraform output` fails
# mid-write. Both helpers must instead build the transcript in a temporary file
# and atomically rename it into place (mv) only once fully written, and must
# remove the temporary file if the build fails — so an operator never attaches a
# partially-written transcript as evidence. Both READMEs say so.


def _assert_smoke_transcript_atomic(script_path: Path) -> None:
    region = _transcript_region(_read(script_path))
    name = script_path.parent.name
    assert region, f"{name}: smoke.sh has no transcript region"

    # The final transcript is produced by an atomic rename, not a direct redirect.
    assert (
        'mv "$TRANSCRIPT_TMP" "$TRANSCRIPT"' in region
    ), f"{name}: transcript is not atomically renamed into place (mv temp -> final)"

    # The block builds into the temporary file, never redirecting straight to the
    # final operator-facing transcript path.
    assert (
        '>"$TRANSCRIPT_TMP"' in region or '> "$TRANSCRIPT_TMP"' in region
    ), f"{name}: transcript block does not write to a temporary file first"
    assert '>"$TRANSCRIPT"' not in region and '> "$TRANSCRIPT"' not in region, (
        f"{name}: transcript block still redirects directly to the final transcript file"
    )

    # A failed build cleans up the temporary file rather than leaving a partial.
    assert (
        'rm -f "$TRANSCRIPT_TMP"' in region
    ), f"{name}: transcript build does not remove the temporary file on failure"


def test_aws_smoke_script_writes_transcript_atomically():
    _assert_smoke_transcript_atomic(SMOKE_SCRIPT)


def test_gcp_smoke_script_writes_transcript_atomically():
    _assert_smoke_transcript_atomic(GCP_SMOKE_SCRIPT)


def test_readmes_document_smoke_transcript_atomic_write():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        assert "atomic" in lowered, (
            f"{module_dir.name}: smoke-helper section does not say the transcript is written atomically"
        )
        assert "partial" in lowered, (
            f"{module_dir.name}: smoke-helper section does not say a partial transcript is not committed"
        )


# --- smoke transcript temp file is cleaned up on interruption (M15 slice) ----
#
# The atomic write removes the temp file when the build COMMAND fails, but a
# signal (INT/TERM/HUP) or an unexpected EXIT in the window between creating
# ${TRANSCRIPT}.tmp and the atomic mv would leave a partial temp file behind.
# Both helpers must install an EXIT/HUP/INT/TERM cleanup for the temp file once
# its path is set (BEFORE the temp file is written), and clear or disable that
# cleanup after the mv succeeds so the final transcript is kept. Both READMEs say
# the partial .tmp transcript is removed on failure or interruption.


def _assert_smoke_transcript_interrupt_cleanup(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name
    region = _transcript_region(text)
    assert region, f"{name}: smoke.sh has no transcript region"

    tmp_idx = region.find('TRANSCRIPT_TMP="${TRANSCRIPT}.tmp"')
    assert tmp_idx != -1, f"{name}: transcript temp path is not set as ${{TRANSCRIPT}}.tmp"

    # A signal/exit trap protecting the temp file is installed.
    trap_idx = region.find("trap ")
    assert (
        trap_idx != -1
    ), f"{name}: no trap installed to clean up the transcript temp file on interruption"

    # Normal exit AND every interruption signal is trapped for cleanup. The traps
    # may be split across multiple `trap` lines (EXIT can return normally while
    # signals must terminate), so collect installs rather than expecting one line.
    trapped = set()
    for line in region.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("trap ") or stripped.startswith("trap -"):
            continue
        for signal in ("EXIT", "HUP", "INT", "TERM"):
            if signal in stripped:
                trapped.add(signal)
    for signal in ("EXIT", "HUP", "INT", "TERM"):
        assert signal in trapped, f"{name}: transcript cleanup trap does not cover {signal}"

    # The cleanup actually removes the temp transcript file.
    assert (
        'rm -f "$TRANSCRIPT_TMP"' in region
    ), f"{name}: transcript cleanup does not rm -f the temp transcript"

    # Cleanup is armed AFTER the temp path is set but BEFORE the temp file is
    # written, so an interrupt mid-write is covered.
    write_idx = region.find('"$TRANSCRIPT_TMP"; then')
    if write_idx == -1:
        write_idx = region.find('>"$TRANSCRIPT_TMP"')
    assert write_idx != -1, f"{name}: transcript is not written to the temp file"
    assert tmp_idx < trap_idx, f"{name}: cleanup trap is installed before the temp path is set"
    assert trap_idx < write_idx, f"{name}: cleanup trap is installed after the temp file is written"

    # After the atomic mv succeeds, the cleanup is cleared/disabled so the final
    # transcript is NOT removed on the exit trap.
    mv_idx = region.find('mv "$TRANSCRIPT_TMP" "$TRANSCRIPT"')
    assert mv_idx != -1, f"{name}: transcript is not atomically renamed (mv temp -> final)"
    after_mv = region[mv_idx:]
    disabled = ("trap -" in after_mv) or ('TRANSCRIPT_TMP=""' in after_mv)
    assert disabled, (
        f"{name}: transcript cleanup is not cleared/disabled after the successful mv "
        "(a stale EXIT trap could remove the final transcript)"
    )


def test_aws_smoke_script_cleans_up_transcript_temp_on_interruption():
    _assert_smoke_transcript_interrupt_cleanup(SMOKE_SCRIPT)


def test_gcp_smoke_script_cleans_up_transcript_temp_on_interruption():
    _assert_smoke_transcript_interrupt_cleanup(GCP_SMOKE_SCRIPT)


# In bash a trapped signal REPLACES the default terminating behavior: after the
# handler returns the script keeps running. So an HUP/INT/TERM cleanup that only
# `rm -f`s the temp and returns is not fail-closed — the interrupted run would
# continue. The signal cleanup must remove the temp AND terminate non-zero with
# the conventional 128+signal code (HUP=129, INT=130, TERM=143), while the EXIT
# handler may clean up and return normally.


def _assert_smoke_signal_cleanup_fails_closed(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name
    region = _transcript_region(text)
    assert region, f"{name}: smoke.sh has no transcript region"

    # The interruption signals are trapped (not folded into an EXIT-only handler
    # that would let the script continue after cleanup).
    for signal in ("HUP", "INT", "TERM"):
        trapped = any(
            line.lstrip().startswith("trap ")
            and not line.lstrip().startswith("trap -")
            and signal in line
            for line in region.splitlines()
        )
        assert trapped, f"{name}: {signal} is not trapped for fail-closed transcript cleanup"

    # An EXIT handler still exists for the normal-exit cleanup path.
    exit_trapped = any(
        line.lstrip().startswith("trap ")
        and not line.lstrip().startswith("trap -")
        and "EXIT" in line
        for line in region.splitlines()
    )
    assert exit_trapped, f"{name}: no EXIT cleanup trap installed"

    # The signal cleanup removes the temp file before terminating.
    assert (
        'rm -f "$TRANSCRIPT_TMP"' in region
    ), f"{name}: signal cleanup does not rm -f the temp transcript"

    # ...and then terminates non-zero with the conventional 128+signal exit codes,
    # so an interrupted run fails closed instead of continuing past the handler.
    for signal, code in (("HUP", "129"), ("INT", "130"), ("TERM", "143")):
        assert (
            f"exit {code}" in region
        ), f"{name}: {signal} cleanup does not terminate non-zero (no exit {code})"


def test_aws_smoke_script_signal_cleanup_fails_closed():
    _assert_smoke_signal_cleanup_fails_closed(SMOKE_SCRIPT)


def test_gcp_smoke_script_signal_cleanup_fails_closed():
    _assert_smoke_signal_cleanup_fails_closed(GCP_SMOKE_SCRIPT)


def test_readmes_document_smoke_transcript_interrupt_cleanup():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        # The partial temp transcript is removed on failure OR interruption.
        assert "interrupt" in lowered, (
            f"{module_dir.name}: smoke-helper section does not say the temp transcript is "
            "removed on interruption"
        )
        assert ".tmp" in lowered, (
            f"{module_dir.name}: smoke-helper section does not name the .tmp temp transcript file"
        )


# --- GCP opt-in External HTTPS Load Balancer + DNS (M15 slice) --------------
#
# Closes the load-balancer/DNS parity gap with a small, OPT-IN External HTTPS
# Load Balancer that fronts the CONTROL-PLANE Cloud Run service only:
#   serverless NEG -> backend service -> URL map -> target HTTPS proxy ->
#   global forwarding rule, plus a Google-managed SSL certificate and an
#   optional Cloud DNS A record.
# The whole front door is gated behind explicit non-secret toggles
# (enable_load_balancer_custom_domain + domain, create_dns_record + managed_zone)
# so the default endpoint stays the Cloud Run service URI. Raw app/integration
# secret values remain a bootstrap (M16) concern, and no live PLAN_ONLY=0
# apply/smoke is claimed.

GCP_LB_RESOURCES = (
    ("google_compute_region_network_endpoint_group", "control_plane"),
    ("google_compute_backend_service", "control_plane"),
    ("google_compute_url_map", "control_plane"),
    ("google_compute_target_https_proxy", "control_plane"),
    ("google_compute_global_forwarding_rule", "control_plane"),
    ("google_compute_managed_ssl_certificate", "control_plane"),
    ("google_compute_global_address", "control_plane"),
)


def test_gcp_create_lb_local_gates_on_toggle_and_domain():
    main = _read(GCP_DIR / "main.tf")
    line = next(
        (ln for ln in main.splitlines() if ln.strip().startswith("create_lb")),
        "",
    )
    assert line, "missing create_lb local"
    assert "var.enable_load_balancer_custom_domain" in line
    assert 'var.domain != ""' in line


def test_gcp_load_balancer_resources_are_opt_in_and_front_control_plane_only():
    main = _read(GCP_DIR / "main.tf")
    for resource_type, name in GCP_LB_RESOURCES:
        block = _resource_block(main, resource_type, name)
        assert block, f"missing {resource_type} {name}"
        # Opt-in only: every LB resource is conditional on the LB toggle.
        assert "count" in block, f"{resource_type} {name} is not conditional"
        assert "local.create_lb" in block, f"{resource_type} {name} not gated on create_lb"

    # The serverless NEG fronts the CONTROL-PLANE Cloud Run service specifically.
    neg = _resource_block(
        main, "google_compute_region_network_endpoint_group", "control_plane"
    )
    assert "SERVERLESS" in neg, "NEG is not a serverless NEG"
    assert "cloud_run" in neg, "serverless NEG has no cloud_run target"
    assert (
        "google_cloud_run_v2_service.control_plane.name" in neg
    ), "serverless NEG does not reference the control-plane Cloud Run service"

    # The worker/scheduler services must never be fronted by a load balancer.
    for other in ("worker", "scheduler"):
        for resource_type, _ in GCP_LB_RESOURCES:
            assert not _resource_block(
                main, resource_type, other
            ), f"{other} must not be fronted by a load balancer ({resource_type})"


def test_gcp_load_balancer_chain_is_wired_neg_to_forwarding_rule():
    main = _read(GCP_DIR / "main.tf")

    backend = _resource_block(main, "google_compute_backend_service", "control_plane")
    assert (
        "google_compute_region_network_endpoint_group.control_plane" in backend
    ), "backend service does not reference the serverless NEG"

    url_map = _resource_block(main, "google_compute_url_map", "control_plane")
    assert (
        "google_compute_backend_service.control_plane" in url_map
    ), "url map does not route to the backend service"

    proxy = _resource_block(main, "google_compute_target_https_proxy", "control_plane")
    assert (
        "google_compute_url_map.control_plane" in proxy
    ), "target HTTPS proxy does not use the URL map"
    assert (
        "google_compute_managed_ssl_certificate.control_plane" in proxy
    ), "target HTTPS proxy does not use the managed SSL certificate"

    rule = _resource_block(
        main, "google_compute_global_forwarding_rule", "control_plane"
    )
    assert (
        "google_compute_target_https_proxy.control_plane" in rule
    ), "forwarding rule does not target the HTTPS proxy"
    assert "443" in rule, "forwarding rule does not serve HTTPS (443)"
    assert (
        "google_compute_global_address.control_plane" in rule
    ), "forwarding rule does not use the reserved global address"


def test_gcp_managed_ssl_certificate_covers_the_custom_domain():
    main = _read(GCP_DIR / "main.tf")
    cert = _resource_block(
        main, "google_compute_managed_ssl_certificate", "control_plane"
    )
    assert cert, "missing google_compute_managed_ssl_certificate control_plane"
    assert "managed" in cert, "certificate is not a Google-managed certificate"
    assert "var.domain" in cert, "managed SSL certificate does not cover var.domain"


def test_gcp_optional_dns_record_is_gated_and_points_at_the_load_balancer():
    main = _read(GCP_DIR / "main.tf")
    record = _resource_block(main, "google_dns_record_set", "control_plane")
    assert record, "missing optional google_dns_record_set control_plane"
    assert "count" in record, "DNS record is not conditional"
    assert "var.create_dns_record" in record, "DNS record not gated on create_dns_record"
    assert "var.managed_zone" in record, "DNS record does not use the managed zone"
    assert "var.domain" in record, "DNS record does not name the custom domain"
    assert '"A"' in record, "DNS record is not an A record"
    assert (
        "google_compute_global_address.control_plane" in record
    ), "DNS record does not point at the load balancer global address"


def test_gcp_load_balancer_variables_are_optional_non_secret_toggles():
    variables = _read(GCP_DIR / "variables.tf")

    enable = _variable_block(variables, "enable_load_balancer_custom_domain")
    assert enable, "missing enable_load_balancer_custom_domain variable"
    assert "bool" in enable, "enable_load_balancer_custom_domain is not a bool toggle"
    assert "default     = false" in enable or "default = false" in enable

    create_dns = _variable_block(variables, "create_dns_record")
    assert create_dns, "missing create_dns_record variable"
    assert "bool" in create_dns, "create_dns_record is not a bool toggle"
    assert "default     = false" in create_dns or "default = false" in create_dns

    managed_zone = _variable_block(variables, "managed_zone")
    assert managed_zone, "missing managed_zone variable"
    assert 'default     = ""' in managed_zone or 'default = ""' in managed_zone

    for block in (enable, create_dns, managed_zone):
        lowered = block.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} leaked into a load-balancer variable"


def test_gcp_custom_domain_front_doors_are_mutually_exclusive():
    variables = _read(GCP_DIR / "variables.tf")
    enable = _variable_block(variables, "enable_load_balancer_custom_domain")
    assert "validation" in enable, "load-balancer custom-domain toggle lacks validation"
    assert "var.enable_custom_domain" in enable, "validation does not consider Cloud Run domain mapping toggle"
    assert "var.enable_load_balancer_custom_domain" in enable
    assert "cannot both be true" in enable.lower() or "mutually exclusive" in enable.lower()


def test_gcp_public_endpoint_docs_are_qualified_by_default_or_opt_in_lb():
    for module_file in ("main.tf", "variables.tf", "outputs.tf"):
        text = _read(GCP_DIR / module_file).lower()
        stale_phrases = (
            "has no provisioned load balancer / dns",
            "there is no provisioned load balancer / dns",
            "no provisioned load balancer / dns",
        )
        for phrase in stale_phrases:
            assert phrase not in text, f"{module_file} still contains stale unconditional wording: {phrase}"
        assert "enable_load_balancer_custom_domain" in text, f"{module_file} does not mention the opt-in LB path"

    readme = _read(GCP_DIR / "README.md").lower()
    for phrase in stale_phrases:
        assert phrase not in readme, f"README.md still contains stale unconditional wording: {phrase}"
    assert "default" in readme and "cloud run service uri" in readme
    assert "enable_load_balancer_custom_domain" in readme


def test_gcp_effective_api_url_prefers_load_balanced_custom_domain_when_opted_in():
    outputs = _read(GCP_DIR / "outputs.tf")
    # The effective API URL accounts for the opt-in load-balanced custom domain...
    assert (
        "enable_load_balancer_custom_domain" in outputs
    ), "effective API URL ignores the load-balancer custom-domain toggle"
    # ...and still falls back to the Cloud Run service URI by default.
    assert "google_cloud_run_v2_service.control_plane.uri" in outputs
    api_url = _output_block(outputs, "agentops_api_url")
    assert "local.api_base_url" in api_url


def test_gcp_outputs_expose_load_balancer_address_when_opted_in():
    outputs = _read(GCP_DIR / "outputs.tf")
    assert 'output "load_balancer_ip"' in outputs, "missing load_balancer_ip output"
    block = _output_block(outputs, "load_balancer_ip")
    assert (
        "google_compute_global_address.control_plane" in block
    ), "load_balancer_ip is not derived from the LB global address"
    # Empty when the load balancer is not opted in (count-gated resource).
    assert "local.create_lb" in block, "load_balancer_ip does not handle the not-opted-in case"


def test_gcp_smoke_hints_mention_optional_load_balancer_and_dns():
    outputs = _read(GCP_DIR / "outputs.tf")
    hints = _output_block(outputs, "smoke_test_hints").lower()
    assert hints, "missing smoke_test_hints output"
    assert (
        "load balancer" in hints or "load-balanced" in hints
    ), "smoke hints do not mention the optional load balancer"
    assert "dns" in hints, "smoke hints do not mention the optional DNS record"
    assert "optional" in hints, "smoke hints do not flag the load balancer/DNS as optional"
    # Live apply/smoke is still pending.
    assert "pending" in hints or "smoke" in hints


def test_gcp_load_balancer_secret_hygiene_preserved():
    # The load-balancer slice must not introduce secret versions or raw inputs.
    main = _read(GCP_DIR / "main.tf")
    variables = _read(GCP_DIR / "variables.tf").lower()
    assert "google_secret_manager_secret_version" not in main
    for token in RAW_SECRET_TOKENS:
        assert f'variable "{token}"' not in variables, token


def test_gcp_readme_documents_opt_in_load_balancer_and_dns_contract():
    readme = _read(GCP_DIR / "README.md")
    lowered = readme.lower()
    # Opt-in LB/DNS documented by name.
    assert "enable_load_balancer_custom_domain" in readme
    assert "create_dns_record" in readme
    assert "managed_zone" in readme
    # The LB chain + managed certificate are described.
    assert "load balancer" in lowered
    assert "managed" in lowered and ("ssl" in lowered or "certificate" in lowered)
    assert "network endpoint group" in lowered or "serverless neg" in lowered
    # Honest: live apply/smoke remains pending.
    assert "live" in lowered and "smoke" in lowered
    # Raw secrets remain a bootstrap concern, not Terraform inputs.
    assert "bootstrap" in lowered
    assert "raw" in lowered


def test_gcp_readme_parity_section_no_longer_lists_load_balancer_as_missing():
    readme = _read(GCP_DIR / "README.md")
    idx = readme.lower().find("parity gaps")
    assert idx != -1, "missing parity gaps section"
    section = readme[idx:].lower()
    # The LB/DNS bullet must no longer claim no front door is provided.
    assert (
        "a load-balancer/cdn front door is not" not in section
    ), "parity section still lists the load-balancer front door as missing"
    assert (
        "no external http(s) load balancer or managed dns fronts the control-plane"
        not in section
    ), "parity section still says no external HTTPS LB/DNS fronts the control-plane"
    # The honest live-smoke gap stays.
    assert "live" in section and "smoke" in section


def test_gcp_tfvars_documents_optional_load_balancer_and_dns():
    tfvars = _read(GCP_DIR / "terraform.tfvars.example")
    assert "enable_load_balancer_custom_domain" in tfvars
    assert "create_dns_record" in tfvars
    assert "managed_zone" in tfvars
    lowered = tfvars.lower()
    for token in RAW_SECRET_TOKENS:
        assert token not in lowered, f"{token} leaked into tfvars example"


# --- smoke transcript artifacts are git-ignored (M15 slice) -----------------
#
# The module-local smoke-transcript-<UTC>.log files are per-operator evidence
# artifacts written on the apply path. They are not source and must never be
# accidentally committed, so the root .gitignore must ignore them.


def test_root_gitignore_ignores_smoke_transcript_logs():
    gitignore = _read(REPO_ROOT / ".gitignore")
    lines = {line.strip() for line in gitignore.splitlines()}
    assert (
        "smoke-transcript-*.log" in lines
    ), "root .gitignore does not ignore smoke-transcript-*.log operator evidence artifacts"


def test_root_gitignore_ignores_smoke_transcript_temp_artifact():
    # The smoke helpers write the transcript atomically: they build it into a
    # `${TRANSCRIPT}.tmp` temp file (smoke-transcript-<UTC>.log.tmp) and only
    # rename it into place once fully written. If a smoke run is interrupted
    # (Ctrl-C / a hung `terraform output`) between the temp write and the `mv`,
    # the partial `.tmp` transcript is left behind. The `smoke-transcript-*.log`
    # ignore does NOT cover the `.log.tmp` suffix, so that partial operator
    # evidence could be accidentally committed — exactly what the atomic write
    # was meant to prevent. The temp artifact must be git-ignored too.
    for module_dir in (AWS_DIR, GCP_DIR):
        # The scripts derive the temp name as "${TRANSCRIPT}.tmp"; assert that
        # invariant so this test stays anchored to the real script behavior.
        script = _read(module_dir / "smoke.sh")
        assert 'TRANSCRIPT_TMP="${TRANSCRIPT}.tmp"' in script, (
            f"{module_dir.name}/smoke.sh no longer writes the transcript via a "
            '"${TRANSCRIPT}.tmp" temp file — update this test'
        )
        temp_path = f"deploy/terraform/{module_dir.name}/smoke-transcript-20240101T000000Z.log.tmp"
        assert _git_check_ignore(temp_path), (
            f"root .gitignore does not ignore the partial smoke transcript temp "
            f"artifact {temp_path}"
        )


# --- image publishing helpers (publish-images.sh) — M15 slice ---------------
#
# A module-local executable helper an operator runs to build/tag/push the three
# runtime container images and emit the non-secret terraform.tfvars image lines
# (control_plane_image / worker_image / scheduler_image) to paste before a
# PLAN_ONLY=0 ./smoke.sh apply. It must be side-effect-safe by default
# (DRY_RUN=1 prints the docker/cloud commands without building, logging in,
# tagging, pushing, creating repositories, or modifying Terraform vars), fail
# closed before any side effect when prerequisites/config are missing, and never
# accept or echo raw app/integration secret values.

AWS_PUBLISH_SCRIPT = AWS_DIR / "publish-images.sh"
GCP_PUBLISH_SCRIPT = GCP_DIR / "publish-images.sh"

IMAGE_TFVARS_VARS = ("control_plane_image", "worker_image", "scheduler_image")


def _executable_lines(text: str) -> list[str]:
    # Non-empty, non-comment lines — the lines that actually run.
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _prints_image_tfvars_lines(text: str) -> None:
    # Each image variable is emitted as an HCL `name = "..."` assignment an
    # operator can paste straight into terraform.tfvars (not as a raw export or
    # bare value), and the printed value is the registry image ref, not a
    # placeholder.
    for var_name in IMAGE_TFVARS_VARS:
        printed = [
            line
            for line in text.splitlines()
            if var_name in line and "=" in line and '"' in line and "echo" in line
        ]
        assert printed, f"publish-images.sh does not print a tfvars '{var_name} = \"...\"' line"


def test_publish_image_helpers_exist_executable_and_strict():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        assert script.is_file(), f"missing {script.parent.name}/publish-images.sh"
        assert script.stat().st_mode & 0o111, f"{script.parent.name}/publish-images.sh is not executable"
        text = _read(script)
        assert text.startswith("#!"), f"{script.parent.name}/publish-images.sh has no shebang"
        assert "set -euo pipefail" in text, f"{script.parent.name}/publish-images.sh is not in strict mode"


def test_publish_image_helpers_dry_run_by_default():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        # DRY_RUN defaults to 1 — side-effect-safe unless explicitly opted out.
        assert 'DRY_RUN="${DRY_RUN:-1}"' in text, (
            f"{script.parent.name}/publish-images.sh does not default DRY_RUN to 1"
        )


def test_publish_image_helpers_gate_side_effects_behind_dry_run():
    # All build/login/tag/push/repository side effects must be routed through a
    # wrapper whose execute branch is guarded by DRY_RUN=0, so the default run
    # only prints commands. No bare docker/push/create invocations may run
    # directly at top level.
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name

        # A run-or-echo wrapper exists and only executes when DRY_RUN=0.
        assert "run()" in text or "run ()" in text, f"{name}: no run() wrapper for side effects"
        run_fn = text[text.find("run()") : text.find("run()") + 220]
        assert 'DRY_RUN" = "0"' in run_fn, (
            f"{name}: run() wrapper does not gate execution on DRY_RUN=0"
        )

        # No side-effecting command runs directly (un-wrapped) at the start of an
        # executable line — every docker/push/create goes through the wrapper.
        for line in _executable_lines(text):
            stripped = line.strip()
            assert not stripped.startswith("docker "), (
                f"{name}: un-wrapped docker invocation runs outside the DRY_RUN wrapper: {stripped}"
            )


def test_publish_image_helpers_do_not_modify_terraform_vars():
    # DRY_RUN must not write Terraform vars; in fact the helper only prints the
    # tfvars lines and never writes terraform.tfvars in either mode.
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name
        for redirect in ("> terraform.tfvars", ">terraform.tfvars", ">> terraform.tfvars", ">>terraform.tfvars"):
            assert redirect not in text, f"{name}: publish-images.sh writes terraform.tfvars ({redirect})"


def test_publish_image_helpers_print_three_tfvars_image_lines():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        _prints_image_tfvars_lines(_read(script))


def test_publish_image_helpers_avoid_raw_app_integration_secrets():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/publish-images.sh"


def test_aws_publish_requires_tools_and_verifies_creds_before_side_effects():
    text = _read(AWS_PUBLISH_SCRIPT)

    # Live path requires aws + docker and verifies credentials.
    assert "command -v aws" in text, "aws publish-images.sh does not require the aws CLI"
    assert "command -v docker" in text, "aws publish-images.sh does not require docker"
    assert "get-caller-identity" in text, "aws publish-images.sh does not verify AWS credentials"

    # The credential/tool checks must precede the first side effect. The first
    # real side effect is a docker build routed through the run wrapper.
    first_side_effect = text.find("run docker build")
    assert first_side_effect != -1, "aws publish-images.sh never builds an image"
    assert text.find("command -v aws") < first_side_effect, "aws CLI check must precede build"
    assert text.find("command -v docker") < first_side_effect, "docker check must precede build"
    assert text.find("get-caller-identity") < first_side_effect, "credential check must precede build"

    # Fails closed on missing prerequisites/config.
    assert "exit 1" in text, "aws publish-images.sh does not fail closed"


def test_aws_publish_supports_ecr_refs_tag_and_repository_defaults():
    text = _read(AWS_PUBLISH_SCRIPT)

    # ECR registry/login + per-service repository refs + a default image tag.
    assert "dkr.ecr" in text, "aws publish-images.sh does not build an ECR registry ref"
    assert "get-login-password" in text, "aws publish-images.sh does not log in to ECR"
    assert "IMAGE_TAG" in text, "aws publish-images.sh has no IMAGE_TAG input"
    assert "AWS_ACCOUNT_ID" in text, "aws publish-images.sh has no AWS_ACCOUNT_ID input"
    assert "AWS_REGION" in text, "aws publish-images.sh has no AWS_REGION input"
    # Repository creation is an ECR side effect that must be gated (printed in
    # dry-run, executed only on the live path through the wrapper).
    assert "create-repository" in text, "aws publish-images.sh cannot ensure ECR repositories"
    assert "run " in text[: text.find("create-repository") + len("create-repository") + 200] or "run sh -c" in text, (
        "aws publish-images.sh does not route repository creation through the DRY_RUN wrapper"
    )
    # Defaults reference the agentops/hermes runtime naming, not a raw secret.
    lowered = text.lower()
    assert "agentops" in lowered and "hermes" in lowered, (
        "aws publish-images.sh repository defaults are not based on agentops/hermes runtime"
    )


def test_aws_publish_is_module_local():
    text = _read(AWS_PUBLISH_SCRIPT)
    assert 'cd "$(dirname "$0")"' in text, "aws publish-images.sh is not module-local"
    assert "-chdir=deploy/terraform" not in text, "aws publish-images.sh uses a root-relative -chdir"


def test_gcp_publish_requires_tools_and_verifies_account_before_side_effects():
    text = _read(GCP_PUBLISH_SCRIPT)

    # Live path requires gcloud + docker and verifies an active account.
    assert "command -v gcloud" in text, "gcp publish-images.sh does not require gcloud"
    assert "command -v docker" in text, "gcp publish-images.sh does not require docker"
    assert "auth" in text and "list" in text, "gcp publish-images.sh does not verify an active gcloud account"

    first_side_effect = text.find("run docker build")
    assert first_side_effect != -1, "gcp publish-images.sh never builds an image"
    assert text.find("command -v gcloud") < first_side_effect, "gcloud check must precede build"
    assert text.find("command -v docker") < first_side_effect, "docker check must precede build"
    assert text.find("gcloud auth") < first_side_effect, "account check must precede build"

    assert "exit 1" in text, "gcp publish-images.sh does not fail closed"


def test_gcp_publish_supports_artifact_registry_refs_and_gated_repo_create():
    text = _read(GCP_PUBLISH_SCRIPT)

    # Artifact Registry repo/location/image refs + a default image tag.
    assert "pkg.dev" in text, "gcp publish-images.sh does not build an Artifact Registry ref"
    assert "IMAGE_TAG" in text, "gcp publish-images.sh has no IMAGE_TAG input"
    assert "AR_LOCATION" in text or "LOCATION" in text, "gcp publish-images.sh has no Artifact Registry location input"
    assert "AR_REPO" in text or "REPO" in text, "gcp publish-images.sh has no Artifact Registry repo input"
    assert "configure-docker" in text, "gcp publish-images.sh does not configure docker auth for Artifact Registry"

    # Repo create/configure happens only behind an explicit flag, or is printed
    # in dry-run — never executed unconditionally on the live path.
    assert "artifacts repositories create" in text, "gcp publish-images.sh cannot create the Artifact Registry repo"
    assert "CREATE_REPO" in text, "gcp publish-images.sh has no explicit repo-create flag"
    create_idx = text.find("artifacts repositories create")
    guard_region = text[:create_idx]
    assert "CREATE_REPO" in guard_region and ("DRY_RUN" in guard_region), (
        "gcp publish-images.sh repo create is not gated on an explicit flag / dry-run"
    )

    lowered = text.lower()
    assert "agentops" in lowered and "hermes" in lowered, (
        "gcp publish-images.sh repository defaults are not based on agentops/hermes runtime"
    )


def test_gcp_publish_is_module_local():
    text = _read(GCP_PUBLISH_SCRIPT)
    assert 'cd "$(dirname "$0")"' in text, "gcp publish-images.sh is not module-local"
    assert "-chdir=deploy/terraform" not in text, "gcp publish-images.sh uses a root-relative -chdir"


def test_readmes_document_publish_image_helpers():
    for module_dir in (AWS_DIR, GCP_DIR):
        readme = _read(module_dir / "README.md")
        lowered = readme.lower()
        name = module_dir.name
        assert "publish-images.sh" in readme, f"{name}: README does not document publish-images.sh"
        # DRY_RUN default is documented honestly.
        assert "dry_run" in lowered, f"{name}: README does not document the DRY_RUN default"
        # Live-run prerequisite checks are documented.
        assert "docker" in lowered, f"{name}: README does not mention the docker prerequisite"
        # Copying the printed image variable lines into terraform.tfvars before
        # the PLAN_ONLY=0 apply is documented.
        assert "terraform.tfvars" in readme, f"{name}: README does not tell operators to copy image lines into terraform.tfvars"
        assert "plan_only=0" in lowered, f"{name}: README does not tie publish-images.sh to PLAN_ONLY=0 ./smoke.sh"


# --- docker build contexts default to the repo root, not the module dir ------
#
# Both helpers `cd "$(dirname "$0")"` into the Terraform module directory before
# building. The Dockerfile lives at the repository ROOT, so defaulting the three
# CONTROL_PLANE_CONTEXT / WORKER_CONTEXT / SCHEDULER_CONTEXT build contexts to
# "." would build the (Dockerfile-less) module directory. From
# deploy/terraform/<module> the repo root is "../../.." — the defaults must
# point there while still honoring explicit env overrides.

PUBLISH_CONTEXT_VARS = ("CONTROL_PLANE_CONTEXT", "WORKER_CONTEXT", "SCHEDULER_CONTEXT")

# deploy/terraform/<module> -> repo root is three levels up.
REPO_ROOT_FROM_MODULE = "../../.."


def _env_default(text: str, var_name: str) -> str:
    import re

    # Matches  VAR="${VAR:-<default>}"  and captures the <default> token.
    match = re.search(
        re.escape(var_name) + r'="\$\{' + re.escape(var_name) + r":-([^}]*)\}\"",
        text,
    )
    return match.group(1) if match else "<no-default-found>"


def test_publish_helpers_default_build_contexts_to_repo_root_not_module_dir():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name

        # The helper changes into the module dir before building, so a "." build
        # context would target the module dir (no Dockerfile) instead of the
        # repo root.
        assert 'cd "$(dirname "$0")"' in text, f"{name}: helper is not module-local"

        for var_name in PUBLISH_CONTEXT_VARS:
            default = _env_default(text, var_name)
            assert default != ".", (
                f"{name}: {var_name} defaults to '.' (the module dir), but the "
                f"Dockerfile lives at the repo root"
            )
            assert default == REPO_ROOT_FROM_MODULE, (
                f"{name}: {var_name} defaults to '{default}', expected the repo "
                f"root '{REPO_ROOT_FROM_MODULE}' relative to the module dir"
            )


def test_publish_helpers_allow_env_override_of_build_contexts():
    # The repo-root default must still be overridable via the env var (the
    # `${VAR:-default}` form), not hard-coded.
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name
        for var_name in PUBLISH_CONTEXT_VARS:
            assert f'"${{{var_name}:-' in text, (
                f"{name}: {var_name} is not overridable via an env default"
            )


def test_publish_helper_readmes_document_repo_root_build_context_default():
    for module_dir in (AWS_DIR, GCP_DIR):
        readme = _read(module_dir / "README.md")
        lowered = readme.lower()
        name = module_dir.name
        # The build contexts are documented as defaulting to the repo root and
        # being overridable.
        assert "_context" in lowered, (
            f"{name}: README does not document the *_CONTEXT build-context overrides"
        )
        assert "repo" in lowered and "root" in lowered, (
            f"{name}: README does not say the build context defaults to the repo root"
        )


# --- required non-secret input preflight (M15 hardening slice) --------------
#
# Running smoke.sh from the module dir with no terraform.tfvars and no required
# account/location inputs must fail closed BEFORE the first Terraform/OpenTofu
# side effect (init/validate/plan/apply), so an unconfigured run never creates
# .terraform/ or a lock file and never reaches a plan-time "missing required
# variable" error after init. The required non-secret inputs may be satisfied by
# a tfvars file (terraform.tfvars[.json], *.auto.tfvars[.json]) or by TF_VAR_*
# env vars. The preflight is behavioral/contract-oriented (not a snapshot) and
# must never reference raw app/integration secret values.
#
#   AWS required non-secret inputs: region, domain.
#   GCP required non-secret inputs: project, region.

AWS_REQUIRED_TFVARS = ("region", "domain")
GCP_REQUIRED_TFVARS = ("project", "region")

# A tfvars file in any of these forms satisfies the preflight.
TFVARS_FILE_FORMS = (
    "terraform.tfvars",
    "terraform.tfvars.json",
    "*.auto.tfvars",
    "*.auto.tfvars.json",
)


def _first_side_effect_index(text: str) -> int:
    # `-input=false` only appears on the real init/plan/apply command lines
    # (never in prose), so it anchors "the first Terraform/OpenTofu side effect".
    return text.find("-input=false")


def _assert_required_input_preflight(script_path: Path, required_vars: tuple[str, ...]) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    first_side_effect = _first_side_effect_index(text)
    assert first_side_effect != -1, f"{name}: smoke.sh never runs a Terraform/OpenTofu command"

    # Each required non-secret input is named in the preflight and is accepted
    # via its TF_VAR_<name> env var, before any side effect.
    for var in required_vars:
        env_name = f"TF_VAR_{var}"
        idx = text.find(env_name)
        assert idx != -1, f"{name}: preflight does not accept the {env_name} env var"
        assert idx < first_side_effect, (
            f"{name}: {env_name} preflight runs after the first Terraform/OpenTofu side effect"
        )

    # A tfvars file (operator-configured inputs) also satisfies the preflight,
    # in every supported form, checked before any side effect.
    for fname in TFVARS_FILE_FORMS:
        idx = text.find(fname)
        assert idx != -1, f"{name}: preflight does not accept a {fname} file"
        assert idx < first_side_effect, (
            f"{name}: the {fname} check runs after the first Terraform/OpenTofu side effect"
        )

    # Fails closed with a clear, non-secret missing-config error BEFORE any side
    # effect — not a post-init plan-time variable error.
    missing_idx = text.lower().find("missing required")
    assert missing_idx != -1, f"{name}: preflight has no clear missing-config error"
    assert missing_idx < first_side_effect, (
        f"{name}: the missing-config error is emitted after a Terraform/OpenTofu side effect"
    )

    fail_idx = text.find("exit 1", missing_idx)
    assert fail_idx != -1 and fail_idx < first_side_effect, (
        f"{name}: preflight does not fail closed (exit 1) before the first side effect"
    )


def test_aws_smoke_script_preflights_required_inputs_before_side_effects():
    _assert_required_input_preflight(SMOKE_SCRIPT, AWS_REQUIRED_TFVARS)


def test_gcp_smoke_script_preflights_required_inputs_before_side_effects():
    _assert_required_input_preflight(GCP_SMOKE_SCRIPT, GCP_REQUIRED_TFVARS)


def test_smoke_input_preflight_does_not_reference_raw_secrets():
    # The required-input preflight deals only with non-secret account/location
    # inputs; raw app/integration secrets remain a bootstrap concern.
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/smoke.sh"


def _run_smoke_with_empty_tfvars(
    tmp_path: Path,
    module_dir: Path,
    cloud_tool: str,
) -> subprocess.CompletedProcess[str]:
    module_copy = tmp_path / module_dir.name
    shutil.copytree(module_dir, module_copy, ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"))
    (module_copy / "terraform.tfvars").write_text("# intentionally missing required inputs\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "terraform-invoked"
    for tf_cli in ("terraform", "tofu"):
        (bin_dir / tf_cli).write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 0\n", encoding="utf-8")
        (bin_dir / tf_cli).chmod(0o755)

    if cloud_tool == "aws":
        (bin_dir / "aws").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    elif cloud_tool == "gcloud":
        (bin_dir / "gcloud").write_text("#!/usr/bin/env bash\necho user@example.com\nexit 0\n", encoding="utf-8")
    else:  # pragma: no cover - helper guard
        raise AssertionError(f"unsupported cloud tool {cloud_tool}")
    (bin_dir / cloud_tool).chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    for key in list(env):
        if key.startswith("TF_VAR_"):
            env.pop(key)
    result = subprocess.run(
        ["bash", "./smoke.sh"],
        cwd=module_copy,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert not marker.exists(), f"{module_dir.name}: Terraform/OpenTofu was invoked before required-input preflight"
    assert not (module_copy / ".terraform").exists(), f"{module_dir.name}: .terraform was created before preflight"
    return result


def test_aws_smoke_script_rejects_incomplete_tfvars_before_init(tmp_path):
    result = _run_smoke_with_empty_tfvars(tmp_path, AWS_DIR, "aws")
    assert result.returncode == 1
    assert "missing required" in result.stderr.lower()
    assert "region" in result.stderr and "domain" in result.stderr


def test_gcp_smoke_script_rejects_incomplete_tfvars_before_init(tmp_path):
    result = _run_smoke_with_empty_tfvars(tmp_path, GCP_DIR, "gcloud")
    assert result.returncode == 1
    assert "missing required" in result.stderr.lower()
    assert "project" in result.stderr and "region" in result.stderr


def test_readmes_document_required_input_preflight():
    for module_dir, required in ((AWS_DIR, AWS_REQUIRED_TFVARS), (GCP_DIR, GCP_REQUIRED_TFVARS)):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        # The preflight requirement is documented against the smoke-helper path.
        assert "required" in lowered, (
            f"{module_dir.name}: smoke-helper section does not mention the required-input preflight"
        )
        # Both satisfaction paths are documented: a tfvars file or TF_VAR_* env vars.
        assert "tfvars" in lowered, (
            f"{module_dir.name}: smoke-helper section does not mention the tfvars-file path"
        )
        assert "tf_var_" in lowered, (
            f"{module_dir.name}: smoke-helper section does not mention the TF_VAR_* env-var path"
        )


# --- live-publish build-context / Dockerfile preflight (M15 hardening slice) --
#
# On the live publish path (DRY_RUN=0) only, each configured docker build context
# (CONTROL_PLANE_CONTEXT / WORKER_CONTEXT / SCHEDULER_CONTEXT) must be verified to
# be an existing directory containing a Dockerfile BEFORE any cloud/docker side
# effect (ECR/Artifact-Registry login, repository creation/config, docker
# build/push). The default dry-run must stay side-effect-free and permissive: it
# may print commands with placeholder contexts and must not require the
# contexts/Dockerfiles to exist. Error messages must be non-secret and name the
# offending *_CONTEXT variable.

PUBLISH_SIDE_EFFECT_MARKERS = (
    "get-login-password",
    "auth configure-docker",
    "create-repository",
    "artifacts repositories create",
    "run docker build",
    "run docker push",
)


def _publish_side_effect_indices(text: str) -> list[int]:
    return [text.find(m) for m in PUBLISH_SIDE_EFFECT_MARKERS if text.find(m) != -1]


def _make_publish_env(tmp_path: Path, module_dir: Path, cloud_tool: str):
    module_copy = tmp_path / module_dir.name
    shutil.copytree(
        module_dir,
        module_copy,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    side_effect = tmp_path / "side-effect"

    docker = bin_dir / "docker"
    docker.write_text(
        f"#!/usr/bin/env bash\ntouch {side_effect}\nexit 0\n", encoding="utf-8"
    )
    docker.chmod(0o755)

    if cloud_tool == "aws":
        aws = bin_dir / "aws"
        aws.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "sts" ]; then echo 123456789012; exit 0; fi\n'
            f"touch {side_effect}\nexit 0\n",
            encoding="utf-8",
        )
        aws.chmod(0o755)
    elif cloud_tool == "gcloud":
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(
            "#!/usr/bin/env bash\n"
            'case "$1 $2" in\n'
            '  "auth list") echo user@example.com; exit 0;;\n'
            '  "config get-value") echo my-project; exit 0;;\n'
            "esac\n"
            f"touch {side_effect}\nexit 0\n",
            encoding="utf-8",
        )
        gcloud.chmod(0o755)
    else:  # pragma: no cover - helper guard
        raise AssertionError(f"unsupported cloud tool {cloud_tool}")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_REGION": "us-east-1",
    }
    return module_copy, env, side_effect


def _run_publish(module_copy: Path, env: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "./publish-images.sh"],
        cwd=module_copy,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _assert_live_context_preflight_missing_dockerfile(
    tmp_path: Path, module_dir: Path, cloud_tool: str
) -> None:
    module_copy, env, side_effect = _make_publish_env(tmp_path, module_dir, cloud_tool)
    ctx = tmp_path / "ctx-without-dockerfile"
    ctx.mkdir()
    env.update(
        {
            "DRY_RUN": "0",
            "CONTROL_PLANE_CONTEXT": str(ctx),
            "WORKER_CONTEXT": str(ctx),
            "SCHEDULER_CONTEXT": str(ctx),
        }
    )
    result = _run_publish(module_copy, env)
    name = module_dir.name
    assert result.returncode == 1, (
        f"{name}: live publish did not fail closed on a Dockerfile-less context\n"
        f"{result.stdout}\n{result.stderr}"
    )
    err = result.stderr.lower()
    assert "dockerfile" in err, f"{name}: error does not mention the missing Dockerfile"
    assert "control_plane_context" in err, (
        f"{name}: error does not name the offending *_CONTEXT variable"
    )
    assert not side_effect.exists(), (
        f"{name}: a cloud/docker side effect ran before the context preflight"
    )


def test_aws_publish_preflights_build_context_dockerfile_on_live_path(tmp_path):
    _assert_live_context_preflight_missing_dockerfile(tmp_path, AWS_DIR, "aws")


def test_gcp_publish_preflights_build_context_dockerfile_on_live_path(tmp_path):
    _assert_live_context_preflight_missing_dockerfile(tmp_path, GCP_DIR, "gcloud")


def test_aws_publish_preflight_rejects_missing_context_directory(tmp_path):
    module_copy, env, side_effect = _make_publish_env(tmp_path, AWS_DIR, "aws")
    missing = tmp_path / "does-not-exist"
    env.update(
        {
            "DRY_RUN": "0",
            "CONTROL_PLANE_CONTEXT": str(missing),
            "WORKER_CONTEXT": str(missing),
            "SCHEDULER_CONTEXT": str(missing),
        }
    )
    result = _run_publish(module_copy, env)
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "control_plane_context" in result.stderr.lower()
    assert not side_effect.exists(), "a side effect ran before the missing-context preflight"


def test_publish_dry_run_is_permissive_about_missing_contexts(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        module_copy, env, side_effect = _make_publish_env(tmp_path / cloud_tool, module_dir, cloud_tool)
        bogus = tmp_path / cloud_tool / "nope"
        env.update(
            {
                "DRY_RUN": "1",
                "CONTROL_PLANE_CONTEXT": str(bogus),
                "WORKER_CONTEXT": str(bogus),
                "SCHEDULER_CONTEXT": str(bogus),
            }
        )
        result = _run_publish(module_copy, env)
        assert result.returncode == 0, (
            f"{module_dir.name}: dry-run is not permissive about missing contexts\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert not side_effect.exists(), f"{module_dir.name}: dry-run performed a side effect"


def test_publish_helpers_preflight_contexts_before_side_effects():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name

        # A Dockerfile-in-context check exists, testing both the directory and the
        # Dockerfile file.
        assert "/Dockerfile" in text, f"{name}: publish-images.sh has no Dockerfile preflight"
        assert "-d " in text, f"{name}: publish-images.sh does not check the context is a directory"

        # Each *_CONTEXT variable is considered by the preflight.
        for var_name in PUBLISH_CONTEXT_VARS:
            assert var_name in text, f"{name}: preflight does not consider {var_name}"

        # The Dockerfile preflight runs before the first cloud/docker side effect.
        dockerfile_idx = text.find("/Dockerfile")
        side_effects = _publish_side_effect_indices(text)
        assert side_effects, f"{name}: no side effects found"
        assert dockerfile_idx < min(side_effects), (
            f"{name}: context/Dockerfile preflight does not run before the first side effect"
        )

        # Fails closed.
        assert "exit 1" in text, f"{name}: context preflight does not fail closed"


def test_publish_context_preflight_does_not_reference_raw_secrets():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/publish-images.sh"


def _publish_section(readme: str) -> str:
    start = readme.find("## Image-publishing helper")
    assert start != -1, "README has no Image-publishing helper section"
    end = readme.find("\n## ", start + 1)
    return readme[start:] if end == -1 else readme[start:end]


def test_publish_helper_readmes_document_context_dockerfile_preflight():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _publish_section(_read(module_dir / "README.md"))
        lowered = section.lower()
        name = module_dir.name
        # The live-publish context/Dockerfile preflight is documented.
        assert "preflight" in lowered, (
            f"{name}: publish section does not document the context/Dockerfile preflight"
        )
        assert "dockerfile" in lowered, (
            f"{name}: publish section does not mention the Dockerfile requirement"
        )
        # *_CONTEXT overrides must point at a directory containing a Dockerfile.
        assert "_context" in lowered, (
            f"{name}: publish section does not mention the *_CONTEXT overrides"
        )


# --- opt-in non-secret image tfvars writer (M15 slice) ----------------------
#
# By default the publish helpers only PRINT the three non-secret image
# assignments, leaving operators to hand-copy them into terraform.tfvars before
# a PLAN_ONLY=0 ./smoke.sh apply. WRITE_TFVARS=1 on the live path (DRY_RUN=0)
# additionally writes exactly those non-secret assignments
# (control_plane_image / worker_image / scheduler_image) to a module-local
# tfvars override (default image.auto.tfvars, overridable via IMAGE_TFVARS_PATH)
# using a safe temp-file + mv. The writer is opt-in, NEVER runs in dry-run (which
# must stay side-effect-free), still prints the three lines to stdout, and never
# emits a raw app/integration secret value. Generated files are git-ignored.

IMAGE_AUTO_TFVARS = "image.auto.tfvars"


def _make_publish_context_with_dockerfile(tmp_path: Path) -> Path:
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return ctx


def test_publish_helpers_default_write_tfvars_off_and_image_path():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name
        # Opt-in: WRITE_TFVARS defaults to 0 (off).
        assert 'WRITE_TFVARS="${WRITE_TFVARS:-0}"' in text, (
            f"{name}: publish-images.sh does not default WRITE_TFVARS to 0"
        )
        # Default output path is the module-local image.auto.tfvars, overridable.
        assert 'IMAGE_TFVARS_PATH="${IMAGE_TFVARS_PATH:-image.auto.tfvars}"' in text, (
            f"{name}: publish-images.sh does not default IMAGE_TFVARS_PATH to image.auto.tfvars"
        )


def test_publish_image_tfvars_writer_uses_safe_temp_file_then_mv():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name
        assert "mktemp" in text, f"{name}: image tfvars writer does not use a temp file"
        assert "mv " in text, f"{name}: image tfvars writer does not mv the temp file into place"


def test_publish_image_tfvars_writer_does_not_reference_raw_secrets():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/publish-images.sh"


def test_publish_write_tfvars_writes_only_non_secret_image_file_on_live_path(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        ctx = _make_publish_context_with_dockerfile(sub)
        env.update(
            {
                "DRY_RUN": "0",
                "WRITE_TFVARS": "1",
                "CONTROL_PLANE_CONTEXT": str(ctx),
                "WORKER_CONTEXT": str(ctx),
                "SCHEDULER_CONTEXT": str(ctx),
            }
        )
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode == 0, (
            f"{name}: live publish with WRITE_TFVARS=1 failed\n{result.stdout}\n{result.stderr}"
        )

        written = module_copy / IMAGE_AUTO_TFVARS
        assert written.is_file(), f"{name}: WRITE_TFVARS=1 did not write {IMAGE_AUTO_TFVARS}"

        body = written.read_text(encoding="utf-8")
        # Exactly the three non-secret image assignments — nothing else.
        assign_lines = [
            line for line in body.splitlines() if "=" in line and not line.lstrip().startswith("#")
        ]
        assert len(assign_lines) == 3, (
            f"{name}: {IMAGE_AUTO_TFVARS} has {len(assign_lines)} assignments, expected only the 3 image vars"
        )
        for var_name in IMAGE_TFVARS_VARS:
            assert var_name in body, f"{name}: {IMAGE_AUTO_TFVARS} missing {var_name}"
        lowered = body.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} leaked into {name}/{IMAGE_AUTO_TFVARS}"

        # The helper still prints the three lines to stdout as before.
        for var_name in IMAGE_TFVARS_VARS:
            assert var_name in result.stdout, f"{name}: stdout no longer prints {var_name}"


def test_publish_dry_run_never_writes_image_tfvars_even_with_write_flag(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        # WRITE_TFVARS=1 must be ignored in the default DRY_RUN=1 mode.
        env.update({"DRY_RUN": "1", "WRITE_TFVARS": "1"})
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode == 0, f"{name}: dry-run failed\n{result.stdout}\n{result.stderr}"
        assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
            f"{name}: dry-run wrote {IMAGE_AUTO_TFVARS} (dry-run must be side-effect-free)"
        )


def test_publish_live_without_write_flag_only_prints_does_not_write(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        ctx = _make_publish_context_with_dockerfile(sub)
        env.update(
            {
                "DRY_RUN": "0",
                "CONTROL_PLANE_CONTEXT": str(ctx),
                "WORKER_CONTEXT": str(ctx),
                "SCHEDULER_CONTEXT": str(ctx),
            }
        )
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode == 0, (
            f"{name}: live publish failed\n{result.stdout}\n{result.stderr}"
        )
        # Writing is opt-in: a live publish without WRITE_TFVARS=1 only prints.
        assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
            f"{name}: writer is not opt-in — wrote {IMAGE_AUTO_TFVARS} without WRITE_TFVARS=1"
        )
        for var_name in IMAGE_TFVARS_VARS:
            assert var_name in result.stdout, f"{name}: stdout no longer prints {var_name}"


def test_publish_write_tfvars_respects_custom_image_tfvars_path(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        ctx = _make_publish_context_with_dockerfile(sub)
        env.update(
            {
                "DRY_RUN": "0",
                "WRITE_TFVARS": "1",
                "IMAGE_TFVARS_PATH": "custom-images.auto.tfvars",
                "CONTROL_PLANE_CONTEXT": str(ctx),
                "WORKER_CONTEXT": str(ctx),
                "SCHEDULER_CONTEXT": str(ctx),
            }
        )
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode == 0, (
            f"{name}: live publish failed\n{result.stdout}\n{result.stderr}"
        )
        assert (module_copy / "custom-images.auto.tfvars").is_file(), (
            f"{name}: IMAGE_TFVARS_PATH override not honored"
        )
        assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
            f"{name}: default path written despite IMAGE_TFVARS_PATH override"
        )


def test_root_gitignore_ignores_generated_image_tfvars():
    gitignore = _read(REPO_ROOT / ".gitignore")
    lines = {line.strip() for line in gitignore.splitlines()}
    # The generated module-local image tfvars override must be ignored so an
    # operator's WRITE_TFVARS=1 output is never accidentally committed. The
    # helpers permit custom module-local filenames such as
    # custom-images.auto.tfvars, so the ignore coverage must match any allowed
    # module-local *.auto.tfvars override in a managed Terraform module.
    assert "deploy/terraform/*-managed/*.auto.tfvars" in lines, (
        "root .gitignore does not ignore all module-local *.auto.tfvars operator files"
    )


def test_root_gitignore_ignores_image_tfvars_temp_artifact():
    # The opt-in writer builds the image tfvars override atomically: it writes
    # into a `mktemp "${dest}.XXXXXX"` temp file and only `mv`s it into place once
    # fully written. If a WRITE_TFVARS=1 publish is interrupted between the temp
    # write and the rename, a partial `<name>.auto.tfvars.XXXXXX` artifact is left
    # behind. The `deploy/terraform/*-managed/*.auto.tfvars` ignore does NOT cover
    # the trailing `.XXXXXX` suffix, so that partial operator-generated input could
    # be accidentally committed — exactly what the atomic write was meant to
    # prevent. The temp artifact must be git-ignored too, for both the default
    # `image.auto.tfvars` and a custom module-local override name.
    for module_dir in (AWS_DIR, GCP_DIR):
        # The scripts derive the temp name as `mktemp "${dest}.XXXXXX"`; assert
        # that invariant so this test stays anchored to the real script behavior.
        script = _read(module_dir / "publish-images.sh")
        assert 'mktemp "${dest}.XXXXXX"' in script, (
            f"{module_dir.name}/publish-images.sh no longer writes the image tfvars "
            'via a `mktemp "${dest}.XXXXXX"` temp file — update this test'
        )
        for temp_name in (
            "image.auto.tfvars.ABCDEF",
            "custom-images.auto.tfvars.XYZ123",
        ):
            temp_path = f"deploy/terraform/{module_dir.name}/{temp_name}"
            assert _git_check_ignore(temp_path), (
                f"root .gitignore does not ignore the partial image tfvars writer "
                f"temp artifact {temp_path}"
            )


def test_publish_helper_readmes_document_write_tfvars_option():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _publish_section(_read(module_dir / "README.md"))
        lowered = section.lower()
        name = module_dir.name
        assert "write_tfvars" in lowered, (
            f"{name}: publish section does not document the WRITE_TFVARS opt-in writer"
        )
        assert "image.auto.tfvars" in lowered, (
            f"{name}: publish section does not document the image.auto.tfvars output"
        )
        assert "image_tfvars_path" in lowered, (
            f"{name}: publish section does not document the IMAGE_TFVARS_PATH override"
        )


# --- image tfvars temp file is cleaned up on interruption (M15 slice) --------
#
# The opt-in WRITE_TFVARS=1 writer builds the image tfvars override atomically:
# it creates a `mktemp "${dest}.XXXXXX"` temp file, writes the three non-secret
# image assignments into it, and `mv`s it into place. The atomic mv removes a
# partial file when a WRITE COMMAND fails, but a signal (HUP/INT/TERM) or an
# unexpected EXIT in the window between `mktemp` and the `mv` would leave a
# partial `*.auto.tfvars.XXXXXX` temp file behind. Mirroring the smoke transcript
# helper, the writer must track the temp path in a script-level variable
# (IMAGE_TFVARS_TMP) — a function-local `tmp` cannot be trapped — arm an
# EXIT/HUP/INT/TERM cleanup once the temp path is set (before the temp file is
# written), and clear/disable that cleanup after the mv succeeds so the final
# generated tfvars file is preserved. Both READMEs say the partial temp file is
# removed on interruption and the interrupted write fails closed.


def _assert_publish_image_tfvars_interrupt_cleanup(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    # The temp path is tracked in a script-level variable (trappable), not only a
    # function-local `tmp` that an EXIT/signal handler could never see.
    assert "IMAGE_TFVARS_TMP" in text, (
        f"{name}: image tfvars writer does not track the temp path in IMAGE_TFVARS_TMP"
    )
    # The mktemp result is assigned to that variable (keeping the existing
    # `mktemp "${dest}.XXXXXX"` invariant the .gitignore coverage depends on).
    assert 'IMAGE_TFVARS_TMP="$(mktemp "${dest}.XXXXXX")"' in text, (
        f"{name}: mktemp result is not assigned to IMAGE_TFVARS_TMP"
    )

    # Normal exit AND every interruption signal is trapped for cleanup. The traps
    # may be split across multiple `trap` lines (EXIT can return normally while
    # signals must terminate), so collect installs rather than expecting one line.
    trapped = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("trap ") or stripped.startswith("trap -"):
            continue
        for signal in ("EXIT", "HUP", "INT", "TERM"):
            if signal in stripped:
                trapped.add(signal)
    for signal in ("EXIT", "HUP", "INT", "TERM"):
        assert signal in trapped, (
            f"{name}: image tfvars cleanup trap does not cover {signal}"
        )

    # The cleanup actually removes the temp file.
    assert 'rm -f "$IMAGE_TFVARS_TMP"' in text, (
        f"{name}: image tfvars cleanup does not rm -f the temp file"
    )

    # Cleanup is armed AFTER the temp path is set but BEFORE the temp file is
    # written, so an interrupt mid-write is covered.
    tmp_idx = text.find('IMAGE_TFVARS_TMP="$(mktemp "${dest}.XXXXXX")"')
    trap_idx = text.find("trap cleanup_image_tfvars_tmp EXIT")
    assert trap_idx != -1, (
        f"{name}: no EXIT cleanup trap armed for the image tfvars temp file"
    )
    write_idx = text.find('>"$IMAGE_TFVARS_TMP"')
    assert write_idx != -1, f"{name}: image tfvars is not written to IMAGE_TFVARS_TMP"
    assert tmp_idx < trap_idx, (
        f"{name}: cleanup trap is armed before the temp path is set"
    )
    assert trap_idx < write_idx, (
        f"{name}: cleanup trap is armed after the temp file is written"
    )

    # After the atomic mv succeeds, the cleanup is cleared/disabled so the final
    # generated tfvars file is NOT removed by the EXIT trap.
    mv_idx = text.find('mv "$IMAGE_TFVARS_TMP" "$dest"')
    assert mv_idx != -1, f"{name}: image tfvars is not atomically renamed (mv temp -> dest)"
    after_mv = text[mv_idx:]
    assert 'IMAGE_TFVARS_TMP=""' in after_mv and "trap -" in after_mv, (
        f"{name}: image tfvars cleanup is not cleared/disabled after the successful mv "
        "(a stale EXIT trap could remove the generated tfvars file)"
    )


def test_aws_publish_image_tfvars_cleans_up_temp_on_interruption():
    _assert_publish_image_tfvars_interrupt_cleanup(AWS_PUBLISH_SCRIPT)


def test_gcp_publish_image_tfvars_cleans_up_temp_on_interruption():
    _assert_publish_image_tfvars_interrupt_cleanup(GCP_PUBLISH_SCRIPT)


# In bash a trapped signal REPLACES the default terminating behavior: after the
# handler returns the script keeps running. So an HUP/INT/TERM cleanup that only
# `rm -f`s the temp and returns is not fail-closed. The signal cleanup must
# remove the temp AND terminate non-zero with the conventional 128+signal code
# (HUP=129, INT=130, TERM=143), while the EXIT handler may clean up and return.


def _assert_publish_image_tfvars_signal_fails_closed(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    for signal in ("HUP", "INT", "TERM"):
        trapped = any(
            line.lstrip().startswith("trap ")
            and not line.lstrip().startswith("trap -")
            and signal in line
            for line in text.splitlines()
        )
        assert trapped, f"{name}: {signal} is not trapped for fail-closed image tfvars cleanup"

    exit_trapped = any(
        line.lstrip().startswith("trap ")
        and not line.lstrip().startswith("trap -")
        and "EXIT" in line
        for line in text.splitlines()
    )
    assert exit_trapped, f"{name}: no EXIT cleanup trap installed for the image tfvars temp file"

    assert 'rm -f "$IMAGE_TFVARS_TMP"' in text, (
        f"{name}: signal cleanup does not rm -f the image tfvars temp file"
    )
    for signal, code in (("HUP", "129"), ("INT", "130"), ("TERM", "143")):
        assert f"exit {code}" in text, (
            f"{name}: {signal} cleanup does not terminate non-zero (no exit {code})"
        )


def test_aws_publish_image_tfvars_signal_cleanup_fails_closed():
    _assert_publish_image_tfvars_signal_fails_closed(AWS_PUBLISH_SCRIPT)


def test_gcp_publish_image_tfvars_signal_cleanup_fails_closed():
    _assert_publish_image_tfvars_signal_fails_closed(GCP_PUBLISH_SCRIPT)


def _glob_image_tfvars_temp_artifacts(module_copy: Path) -> list[Path]:
    return list(module_copy.glob(f"{IMAGE_AUTO_TFVARS}.*"))


def test_publish_image_tfvars_exit_cleanup_removes_temp_when_mv_fails(tmp_path):
    # Behavioral: drive a live WRITE_TFVARS=1 publish but make `mv` fail. With
    # `set -e` the failed mv exits the script, and the armed EXIT trap must remove
    # the in-progress temp file so no partial `image.auto.tfvars.XXXXXX` remains.
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        ctx = _make_publish_context_with_dockerfile(sub)
        # A fake `mv` that refuses to move (simulating an mv failure) so the EXIT
        # trap is exercised on the temp file the writer already created.
        fake_mv = sub / "bin" / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            'echo "fake mv refusing to move" >&2\n'
            "exit 1\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)
        env.update(
            {
                "DRY_RUN": "0",
                "WRITE_TFVARS": "1",
                "CONTROL_PLANE_CONTEXT": str(ctx),
                "WORKER_CONTEXT": str(ctx),
                "SCHEDULER_CONTEXT": str(ctx),
            }
        )
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode != 0, (
            f"{name}: publish did not fail when mv failed\n{result.stdout}\n{result.stderr}"
        )
        leftovers = _glob_image_tfvars_temp_artifacts(module_copy)
        assert not leftovers, (
            f"{name}: EXIT cleanup left partial image tfvars temp file(s): {leftovers}"
        )
        # And the final generated file was never put in place.
        assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
            f"{name}: dest tfvars exists despite the mv failure"
        )


def test_publish_image_tfvars_signal_cleanup_removes_temp_and_fails_closed(tmp_path):
    # Behavioral: a fake `mv` that signals the running script (SIGTERM) before the
    # rename completes. The TERM trap must remove the in-progress temp file and
    # terminate the script non-zero with the conventional 143 (128+15) code.
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        sub = tmp_path / cloud_tool
        sub.mkdir()
        module_copy, env, _ = _make_publish_env(sub, module_dir, cloud_tool)
        ctx = _make_publish_context_with_dockerfile(sub)
        fake_mv = sub / "bin" / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            "# Simulate an interruption arriving while the writer is renaming.\n"
            'kill -TERM "$PPID"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)
        env.update(
            {
                "DRY_RUN": "0",
                "WRITE_TFVARS": "1",
                "CONTROL_PLANE_CONTEXT": str(ctx),
                "WORKER_CONTEXT": str(ctx),
                "SCHEDULER_CONTEXT": str(ctx),
            }
        )
        result = _run_publish(module_copy, env)
        name = module_dir.name
        assert result.returncode == 143, (
            f"{name}: TERM cleanup did not fail closed with 143 (got {result.returncode})\n"
            f"{result.stdout}\n{result.stderr}"
        )
        leftovers = _glob_image_tfvars_temp_artifacts(module_copy)
        assert not leftovers, (
            f"{name}: signal cleanup left partial image tfvars temp file(s): {leftovers}"
        )
        assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
            f"{name}: dest tfvars exists despite the simulated interruption"
        )


def test_publish_helper_readmes_document_image_tfvars_interrupt_cleanup():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _publish_section(_read(module_dir / "README.md"))
        name = module_dir.name
        # The doc must describe interrupt-time cleanup: an interrupted
        # WRITE_TFVARS=1 write removes the partial temp file and fails closed.
        # Require the three concepts to co-occur within one passage (a window
        # anchored on "interrupt") so the section actually documents the new
        # behavior rather than incidentally reusing these words elsewhere.
        flat = section.replace("\n", " ").lower()
        idx = flat.find("interrupt")
        assert idx != -1, (
            f"{name}: publish section does not mention interrupted writes"
        )
        window = flat[idx : idx + 320]
        assert "remov" in window, (
            f"{name}: publish section does not say an interrupted WRITE_TFVARS write "
            "removes the partial temp file"
        )
        assert "fail closed" in window or "fails closed" in window, (
            f"{name}: publish section does not say an interrupted WRITE_TFVARS write fails closed"
        )


# --- custom IMAGE_TFVARS_PATH must stay module-local (M15 hardening slice) ----
#
# The opt-in writer (WRITE_TFVARS=1) writes the generated non-secret image tfvars
# to IMAGE_TFVARS_PATH via a temp file + mv. A custom path must stay a
# module-local plain filename so the writer can never clobber files outside the
# module directory and so its output stays covered by the
# `deploy/terraform/*-managed/...` .gitignore entry. On the live write path the
# helpers must fail closed BEFORE any cloud/docker side effect when
# IMAGE_TFVARS_PATH is absolute, contains a '/' path segment, or contains '..'.
# The error must name IMAGE_TFVARS_PATH and never reference a raw secret.

NON_MODULE_LOCAL_IMAGE_PATHS = (
    "/tmp/escape.auto.tfvars",
    "../escape.auto.tfvars",
    "sub/dir.auto.tfvars",
)


def test_publish_write_tfvars_rejects_non_module_local_image_path(tmp_path):
    for module_dir, cloud_tool in ((AWS_DIR, "aws"), (GCP_DIR, "gcloud")):
        for i, bad_path in enumerate(NON_MODULE_LOCAL_IMAGE_PATHS):
            sub = tmp_path / f"{cloud_tool}-{i}"
            sub.mkdir()
            module_copy, env, side_effect = _make_publish_env(sub, module_dir, cloud_tool)
            ctx = _make_publish_context_with_dockerfile(sub)
            env.update(
                {
                    "DRY_RUN": "0",
                    "WRITE_TFVARS": "1",
                    "IMAGE_TFVARS_PATH": bad_path,
                    "CONTROL_PLANE_CONTEXT": str(ctx),
                    "WORKER_CONTEXT": str(ctx),
                    "SCHEDULER_CONTEXT": str(ctx),
                }
            )
            result = _run_publish(module_copy, env)
            name = module_dir.name

            assert result.returncode == 1, (
                f"{name}: live publish did not fail closed on non-module-local "
                f"IMAGE_TFVARS_PATH={bad_path}\n{result.stdout}\n{result.stderr}"
            )
            assert "image_tfvars_path" in result.stderr.lower(), (
                f"{name}: error does not name IMAGE_TFVARS_PATH for {bad_path}\n{result.stderr}"
            )
            # Fail closed BEFORE any cloud/docker side effect.
            assert not side_effect.exists(), (
                f"{name}: a side effect ran before the IMAGE_TFVARS_PATH validation for {bad_path}"
            )
            # Nothing written at the default module-local path either.
            assert not (module_copy / IMAGE_AUTO_TFVARS).exists(), (
                f"{name}: wrote {IMAGE_AUTO_TFVARS} despite rejecting {bad_path}"
            )


def test_publish_image_tfvars_path_validation_does_not_reference_raw_secrets():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        lowered = _read(script).lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {script.parent.name}/publish-images.sh"


def test_publish_validates_image_tfvars_path_is_module_local_before_side_effects():
    for script in (AWS_PUBLISH_SCRIPT, GCP_PUBLISH_SCRIPT):
        text = _read(script)
        name = script.parent.name

        # A module-local validation of the custom output path exists, naming the
        # IMAGE_TFVARS_PATH variable and failing closed.
        assert "module-local filename" in text, (
            f"{name}: no IMAGE_TFVARS_PATH module-local validation"
        )
        assert "IMAGE_TFVARS_PATH" in text

        # It runs before the first cloud/docker side effect so a misconfigured
        # output path never reaches a build/push.
        guard_idx = text.find("module-local filename")
        side_effects = _publish_side_effect_indices(text)
        assert side_effects, f"{name}: no side effects found"
        assert guard_idx < min(side_effects), (
            f"{name}: IMAGE_TFVARS_PATH validation runs after a side effect"
        )


# --- Terraform/OpenTofu local working & state artifacts are git-ignored (M15) ---
#
# The live smoke/publish helpers now run module-local terraform/tofu commands in
# deploy/terraform/*-managed. An operator run leaves a `.terraform/` working dir
# and may leave state/backup/plan/crash artifacts (*.tfstate, *.tfstate.*,
# *.tfplan, crash.log, crash.*.log) that can carry infrastructure details or
# sensitive values. These are operator-local and must never be accidentally
# committed, so the root .gitignore must ignore them. git check-ignore tests the
# real ignore behavior (the patterns can evolve) rather than exact gitignore lines.


def _git_check_ignore(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def test_root_gitignore_ignores_managed_terraform_working_dir():
    # The `.terraform/` plugin/module working dir a local init creates under each
    # managed module must be ignored (contents and the directory itself).
    for path in (
        "deploy/terraform/aws-managed/.terraform/providers/registry/x",
        "deploy/terraform/gcp-managed/.terraform/modules/y",
    ):
        assert _git_check_ignore(path), (
            f"root .gitignore does not ignore the Terraform working dir {path}"
        )


def test_root_gitignore_ignores_terraform_state_plan_and_crash_artifacts():
    # State/backup/plan/crash artifacts a local apply/plan can leave behind may
    # contain infrastructure details or sensitive values.
    for path in (
        "deploy/terraform/aws-managed/terraform.tfstate",
        "deploy/terraform/gcp-managed/terraform.tfstate.backup",
        "deploy/terraform/aws-managed/terraform.tfstate.1700000000",
        "deploy/terraform/gcp-managed/run.tfplan",
        "deploy/terraform/aws-managed/crash.log",
        "deploy/terraform/gcp-managed/crash.2024-01-01.log",
    ):
        assert _git_check_ignore(path), (
            f"root .gitignore does not ignore the Terraform artifact {path}"
        )


def test_root_gitignore_still_ignores_existing_managed_operator_artifacts():
    # The new Terraform-artifact coverage must not regress the existing operator
    # artifact ignores: per-module smoke transcripts and generated image tfvars.
    for path in (
        "deploy/terraform/aws-managed/smoke-transcript-20240101T000000Z.log",
        "deploy/terraform/gcp-managed/smoke-transcript-20240101T000000Z.log",
        "deploy/terraform/aws-managed/image.auto.tfvars",
        "deploy/terraform/gcp-managed/custom-images.auto.tfvars",
    ):
        assert _git_check_ignore(path), (
            f"root .gitignore regressed the existing operator artifact ignore for {path}"
        )


def test_live_smoke_docs_note_terraform_artifacts_are_operator_local_not_committed():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        lowered = section.lower()
        # The working dir / state / plan artifacts a smoke run leaves behind are
        # operator-local and must not be committed.
        assert "state" in lowered, (
            f"{module_dir.name}: live-smoke section does not mention local Terraform state artifacts"
        )
        assert "commit" in lowered, (
            f"{module_dir.name}: live-smoke section does not say the local Terraform artifacts must not be committed"
        )


# --- optional DESTROY_ON_FAILURE cleanup guard on the apply path (M15 slice) -
#
# Both managed-cloud live-smoke helpers must support an OPTIONAL, side-effect-safe
# DESTROY_ON_FAILURE cleanup guard. The default preserves existing behavior (no
# destroy). When PLAN_ONLY=0 and DESTROY_ON_FAILURE=1, if `apply` SUCCEEDS but the
# subsequent /healthz probe FAILS, the helper attempts `terraform/tofu destroy
# -auto-approve -input=false` and then exits non-zero. It must never destroy on
# plan-only runs, never destroy before a successful apply, never destroy on
# placeholder-image/public-invoker preflight failures, and never echo/require raw
# app/integration secret values. Module-local, provider-agnostic (CLI only).

AWS_DESTROY_ENV = {"TF_VAR_region": "us-east-1", "TF_VAR_domain": "agentops.example"}
GCP_DESTROY_ENV = {
    "TF_VAR_project": "example-project",
    "TF_VAR_region": "us-central1",
    "TF_VAR_enable_public_invoker": "true",
}


def _make_destroy_smoke_stub_bin(tmp: Path) -> Path:
    """Stub terraform/aws/gcloud/curl for driving the DESTROY_ON_FAILURE path.

    terraform: no-ops init/validate/plan; `console` returns a non-placeholder
    image and the TF_VAR-driven enable_public_invoker (so the image/public-invoker
    preflights pass); `output -raw` returns a URL; `apply` touches $APPLY_MARKER
    and exits ${APPLY_EXIT:-0}; `destroy` touches $DESTROY_MARKER and exits 0.
    curl exits ${CURL_EXIT:-1} so the post-apply /healthz probe fails by default —
    the exact scenario an opted-in DESTROY_ON_FAILURE run must clean up after.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    terraform = bin_dir / "terraform"
    terraform.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  console)\n"
        '    q="$(cat)"\n'
        '    case "$q" in\n'
        "      *enable_public_invoker*) printf '%s\\n' \"${TF_VAR_enable_public_invoker:-false}\" ;;\n"
        "      *image*) printf 'gcr.io/example/app:v1\\n' ;;\n"
        "      *) printf '\\n' ;;\n"
        "    esac\n"
        "    ;;\n"
        "  output)\n"
        '    if [ "${2:-}" = "-raw" ]; then printf \'http://api.example.test\\n\'; else printf \'hint\\n\'; fi\n'
        "    ;;\n"
        "  apply)\n"
        '    : >"$APPLY_MARKER"\n'
        '    exit "${APPLY_EXIT:-0}"\n'
        "    ;;\n"
        "  destroy)\n"
        '    : >"$DESTROY_MARKER"\n'
        "    exit 0\n"
        "    ;;\n"
        "  *) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    terraform.chmod(0o755)

    aws = bin_dir / "aws"
    aws.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    aws.chmod(0o755)

    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "auth" ] && [ "${2:-}" = "list" ]; then printf \'tester@example.com\\n\'; fi\n',
        encoding="utf-8",
    )
    gcloud.chmod(0o755)

    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit ${CURL_EXIT:-1}\n", encoding="utf-8")
    curl.chmod(0o755)

    return bin_dir


def _run_destroy_smoke(
    script: Path,
    bin_dir: Path,
    *,
    plan_only: str,
    destroy_on_failure: str | None,
    apply_marker: Path,
    destroy_marker: Path,
    extra_env: dict[str, str],
    apply_exit: str = "0",
    curl_exit: str = "1",
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLAN_ONLY": plan_only,
        "APPLY_MARKER": str(apply_marker),
        "DESTROY_MARKER": str(destroy_marker),
        "APPLY_EXIT": apply_exit,
        "CURL_EXIT": curl_exit,
    }
    env.pop("DESTROY_ON_FAILURE", None)
    if destroy_on_failure is not None:
        env["DESTROY_ON_FAILURE"] = destroy_on_failure
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _assert_destroys_on_opt_in(script: Path, extra_env: dict[str, str], tmp_path: Path) -> None:
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        script,
        bin_dir,
        plan_only="0",
        destroy_on_failure="1",
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=extra_env,
    )
    assert apply_marker.exists(), (
        f"{script.parent.name}: apply not reached "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert destroy_marker.exists(), (
        f"{script.parent.name}: DESTROY_ON_FAILURE=1 did not destroy after a failed "
        f"/healthz probe (stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert result.returncode != 0, (
        f"{script.parent.name}: smoke.sh must still exit non-zero after destroying on failure"
    )


def _assert_no_destroy_by_default(script: Path, extra_env: dict[str, str], tmp_path: Path) -> None:
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        script,
        bin_dir,
        plan_only="0",
        destroy_on_failure=None,
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=extra_env,
    )
    assert apply_marker.exists(), f"{script.parent.name}: apply not reached"
    assert not destroy_marker.exists(), (
        f"{script.parent.name}: default run destroyed on probe failure "
        f"(DESTROY_ON_FAILURE must default off; stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert result.returncode != 0, (
        f"{script.parent.name}: probe failure must still exit non-zero by default"
    )


def test_aws_smoke_script_destroys_on_failure_when_opted_in(tmp_path):
    _assert_destroys_on_opt_in(SMOKE_SCRIPT, AWS_DESTROY_ENV, tmp_path)


def test_gcp_smoke_script_destroys_on_failure_when_opted_in(tmp_path):
    _assert_destroys_on_opt_in(GCP_SMOKE_SCRIPT, GCP_DESTROY_ENV, tmp_path)


def test_aws_smoke_script_does_not_destroy_by_default_on_probe_failure(tmp_path):
    _assert_no_destroy_by_default(SMOKE_SCRIPT, AWS_DESTROY_ENV, tmp_path)


def test_gcp_smoke_script_does_not_destroy_by_default_on_probe_failure(tmp_path):
    _assert_no_destroy_by_default(GCP_SMOKE_SCRIPT, GCP_DESTROY_ENV, tmp_path)


def test_aws_smoke_script_plan_only_never_destroys_even_when_opted_in(tmp_path):
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        SMOKE_SCRIPT,
        bin_dir,
        plan_only="1",
        destroy_on_failure="1",
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=AWS_DESTROY_ENV,
    )
    assert result.returncode == 0, (
        f"plan-only run failed (stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert not apply_marker.exists(), "plan-only run must never apply"
    assert not destroy_marker.exists(), "plan-only run must never destroy"


def test_gcp_smoke_script_plan_only_never_destroys_even_when_opted_in(tmp_path):
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        GCP_SMOKE_SCRIPT,
        bin_dir,
        plan_only="1",
        destroy_on_failure="1",
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=GCP_DESTROY_ENV,
    )
    assert result.returncode == 0, (
        f"plan-only run failed (stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert not apply_marker.exists(), "plan-only run must never apply"
    assert not destroy_marker.exists(), "plan-only run must never destroy"


def test_aws_smoke_script_does_not_destroy_before_successful_apply(tmp_path):
    # When apply itself FAILS, the helper must exit (set -e) before the probe and
    # must never run destroy — it only destroys to clean up an apply that
    # succeeded but failed its /healthz probe.
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        SMOKE_SCRIPT,
        bin_dir,
        plan_only="0",
        destroy_on_failure="1",
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=AWS_DESTROY_ENV,
        apply_exit="1",
    )
    assert result.returncode != 0, "a failed apply must exit non-zero"
    assert not destroy_marker.exists(), (
        f"destroy ran even though apply itself failed "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_gcp_smoke_script_does_not_destroy_before_successful_apply(tmp_path):
    apply_marker = tmp_path / "applied"
    destroy_marker = tmp_path / "destroyed"
    bin_dir = _make_destroy_smoke_stub_bin(tmp_path)
    result = _run_destroy_smoke(
        GCP_SMOKE_SCRIPT,
        bin_dir,
        plan_only="0",
        destroy_on_failure="1",
        apply_marker=apply_marker,
        destroy_marker=destroy_marker,
        extra_env=GCP_DESTROY_ENV,
        apply_exit="1",
    )
    assert result.returncode != 0, "a failed apply must exit non-zero"
    assert not destroy_marker.exists(), (
        f"destroy ran even though apply itself failed "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_smoke_scripts_destroy_guard_uses_provider_agnostic_cli_and_no_raw_secrets():
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        text = _read(script)
        name = script.parent.name
        assert "DESTROY_ON_FAILURE" in text, f"{name}/smoke.sh missing DESTROY_ON_FAILURE guard"
        assert "destroy -auto-approve -input=false" in text, (
            f"{name}/smoke.sh does not run a CLI `destroy -auto-approve -input=false`"
        )
        # Provider-agnostic: destroy goes through the detected terraform/tofu CLI
        # ("$TF"), not a cloud SDK.
        assert '"$TF" destroy' in text, (
            f"{name}/smoke.sh destroy is not invoked via the detected $TF CLI"
        )
        lowered = text.lower()
        for token in RAW_SECRET_TOKENS:
            assert token not in lowered, f"{token} referenced in {name}/smoke.sh"


def test_smoke_scripts_document_destroy_on_failure_default_off():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        assert "DESTROY_ON_FAILURE" in section, (
            f"{module_dir.name}: live-smoke section does not document DESTROY_ON_FAILURE"
        )
        lowered = section.lower()
        assert "destroy" in lowered, (
            f"{module_dir.name}: live-smoke section does not describe the destroy cleanup"
        )
        assert "default" in lowered, (
            f"{module_dir.name}: live-smoke section does not state the safe default (no destroy)"
        )


# --- opt-in local Terraform/OpenTofu artifact cleanup (CLEAN_TERRAFORM_ARTIFACTS) — M15 slice ---
#
# A smoke/plan run leaves module-local Terraform/OpenTofu working artifacts
# (.terraform/, .terraform.lock.hcl, *.tfstate[.*], *.tfplan, crash[.*].log). They
# are git-ignored, but an operator who runs only a plan-only smoke may want the
# helper to clean up its own scratch afterwards. CLEAN_TERRAFORM_ARTIFACTS=1
# (opt-in; default off) removes ONLY those working artifacts after a successful
# plan-only run — never the source (.tf), inputs (terraform.tfvars / generated
# *.auto.tfvars), README, or smoke-transcript evidence — and never runs after a
# failed preflight (before Terraform is invoked) or on the apply path (apply keeps
# its state and transcript). No raw app/integration secrets are involved.


def _cleanup_region(text: str) -> str:
    start = text.find("clean_terraform_artifacts() {")
    if start == -1:
        return ""
    end = text.find("\n}", start)
    return text[start : end + 2] if end != -1 else text[start:]


def _assert_smoke_artifact_cleanup_static(script_path: Path) -> None:
    text = _read(script_path)
    name = script_path.parent.name

    # Opt-in via CLEAN_TERRAFORM_ARTIFACTS, defaulting OFF.
    assert (
        'CLEAN_TERRAFORM_ARTIFACTS="${CLEAN_TERRAFORM_ARTIFACTS:-0}"' in text
    ), f"{name}: smoke.sh does not default CLEAN_TERRAFORM_ARTIFACTS off"

    region = _cleanup_region(text)
    assert region, f"{name}: smoke.sh has no clean_terraform_artifacts cleanup region"

    # Removes exactly the Terraform/OpenTofu working artifacts.
    assert "rm -rf .terraform" in region, f"{name}: cleanup does not remove the .terraform/ working dir"
    assert ".terraform.lock.hcl" in region, f"{name}: cleanup does not remove the lock file"
    assert "*.tfstate" in region, f"{name}: cleanup does not remove tfstate files"
    assert "*.tfstate.*" in region, f"{name}: cleanup does not remove tfstate backups"
    assert "*.tfplan" in region, f"{name}: cleanup does not remove tfplan files"
    assert "crash.log" in region, f"{name}: cleanup does not remove crash.log"
    assert "crash.*.log" in region, f"{name}: cleanup does not remove crash.*.log"

    # Never removes source / inputs / evidence.
    assert "terraform.tfvars" not in region, f"{name}: cleanup must not touch terraform.tfvars"
    assert "auto.tfvars" not in region, f"{name}: cleanup must not touch generated image tfvars"
    assert "README" not in region, f"{name}: cleanup must not touch the README"
    assert "smoke-transcript" not in region, f"{name}: cleanup must not touch smoke transcripts"
    # No bare source-.tf removal (only the *.tfstate/*.tfplan globs are allowed).
    for forbidden in ("*.tf\n", "*.tf ", '*.tf"', "*.tf'", "main.tf", "variables.tf"):
        assert forbidden not in region, f"{name}: cleanup must not remove source .tf files ({forbidden!r})"


def test_aws_smoke_script_artifact_cleanup_is_opt_in_and_scoped():
    _assert_smoke_artifact_cleanup_static(SMOKE_SCRIPT)


def test_gcp_smoke_script_artifact_cleanup_is_opt_in_and_scoped():
    _assert_smoke_artifact_cleanup_static(GCP_SMOKE_SCRIPT)


def test_smoke_artifact_cleanup_does_not_reference_raw_secrets():
    for script in (SMOKE_SCRIPT, GCP_SMOKE_SCRIPT):
        region = _cleanup_region(_read(script)).lower()
        assert region, f"{script.parent.name}: smoke.sh has no cleanup region"
        for token in RAW_SECRET_TOKENS:
            assert token not in region, f"{token} referenced in {script.parent.name} cleanup"


# Working artifacts that the opt-in cleanup must remove, and source/input/evidence
# files it must always preserve.
CLEANUP_WORKING_ARTIFACTS = (
    ".terraform.lock.hcl",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "run.tfplan",
    "crash.log",
    "crash.2026.log",
)
CLEANUP_PRESERVED_FILES = (
    "terraform.tfvars",
    "image.auto.tfvars",
    "README.md",
    "main.tf",
    "smoke-transcript-20260101T000000Z.log",
)
CLEANUP_REQUIRED_INPUT_ENV = {
    "aws-managed": {"TF_VAR_region": "us-east-1", "TF_VAR_domain": "example.test"},
    "gcp-managed": {"TF_VAR_project": "example-project", "TF_VAR_region": "us-central1"},
}


def _make_artifact_cleanup_stub_bin(tmp: Path) -> Path:
    """Stub terraform/tofu/aws/gcloud so a plan-only smoke run succeeds with no
    real side effects. terraform/tofu no-op init/validate/plan; aws and gcloud
    satisfy the credential/auth preflights."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for tool in ("terraform", "tofu"):
        path = bin_dir / tool
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    aws = bin_dir / "aws"
    aws.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    aws.chmod(0o755)

    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "auth" ] && [ "${2:-}" = "list" ]; then printf \'tester@example.com\\n\'; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    gcloud.chmod(0o755)
    return bin_dir


def _setup_cleanup_module(tmp_path: Path, script_path: Path) -> Path:
    mod = tmp_path / "module"
    mod.mkdir()
    shutil.copy(script_path, mod / "smoke.sh")
    (mod / "smoke.sh").chmod(0o755)

    art_dir = mod / ".terraform"
    art_dir.mkdir()
    (art_dir / "providers").write_text("x", encoding="utf-8")
    for fname in CLEANUP_WORKING_ARTIFACTS:
        (mod / fname).write_text("artifact", encoding="utf-8")
    # Preserved files must NOT define the required inputs, so the preflight outcome
    # is controlled solely by the TF_VAR_* env supplied per run.
    for fname in CLEANUP_PRESERVED_FILES:
        (mod / fname).write_text("desired_task_count = 2\n", encoding="utf-8")
    return mod


def _run_cleanup_smoke(mod: Path, bin_dir: Path, script_path: Path, clean, with_required_inputs: bool):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLAN_ONLY": "1",
    }
    env.pop("CLEAN_TERRAFORM_ARTIFACTS", None)
    if clean is not None:
        env["CLEAN_TERRAFORM_ARTIFACTS"] = clean
    for var in ("TF_VAR_region", "TF_VAR_domain", "TF_VAR_project"):
        env.pop(var, None)
    if with_required_inputs:
        env.update(CLEANUP_REQUIRED_INPUT_ENV[script_path.parent.name])
    return subprocess.run(
        ["bash", str(mod / "smoke.sh")],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _assert_cleanup_removes_only_working_artifacts(tmp_path: Path, script_path: Path) -> None:
    mod = _setup_cleanup_module(tmp_path, script_path)
    bin_dir = _make_artifact_cleanup_stub_bin(tmp_path)
    result = _run_cleanup_smoke(mod, bin_dir, script_path, clean="1", with_required_inputs=True)
    assert result.returncode == 0, (
        f"plan-only smoke with CLEAN_TERRAFORM_ARTIFACTS=1 failed "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert not (mod / ".terraform").exists(), "cleanup did not remove the .terraform/ working dir"
    for fname in CLEANUP_WORKING_ARTIFACTS:
        assert not (mod / fname).exists(), f"cleanup did not remove working artifact {fname}"
    for fname in CLEANUP_PRESERVED_FILES:
        assert (mod / fname).exists(), f"cleanup wrongly removed preserved file {fname}"


def test_aws_smoke_cleanup_removes_only_working_artifacts_after_plan_only(tmp_path):
    _assert_cleanup_removes_only_working_artifacts(tmp_path, SMOKE_SCRIPT)


def test_gcp_smoke_cleanup_removes_only_working_artifacts_after_plan_only(tmp_path):
    _assert_cleanup_removes_only_working_artifacts(tmp_path, GCP_SMOKE_SCRIPT)


def _assert_cleanup_default_off_preserves_artifacts(tmp_path: Path, script_path: Path) -> None:
    mod = _setup_cleanup_module(tmp_path, script_path)
    bin_dir = _make_artifact_cleanup_stub_bin(tmp_path)
    result = _run_cleanup_smoke(mod, bin_dir, script_path, clean=None, with_required_inputs=True)
    assert result.returncode == 0, (
        f"plan-only smoke (default cleanup off) failed "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert (mod / ".terraform").exists(), "default-off run wrongly removed .terraform/"
    for fname in CLEANUP_WORKING_ARTIFACTS:
        assert (mod / fname).exists(), f"default-off run wrongly removed {fname}"


def test_aws_smoke_cleanup_defaults_off(tmp_path):
    _assert_cleanup_default_off_preserves_artifacts(tmp_path, SMOKE_SCRIPT)


def test_gcp_smoke_cleanup_defaults_off(tmp_path):
    _assert_cleanup_default_off_preserves_artifacts(tmp_path, GCP_SMOKE_SCRIPT)


def _assert_cleanup_skipped_on_preflight_failure(tmp_path: Path, script_path: Path) -> None:
    mod = _setup_cleanup_module(tmp_path, script_path)
    bin_dir = _make_artifact_cleanup_stub_bin(tmp_path)
    # No required inputs → the preflight fails closed BEFORE Terraform is invoked,
    # even with cleanup opted in. Nothing must be cleaned.
    result = _run_cleanup_smoke(mod, bin_dir, script_path, clean="1", with_required_inputs=False)
    assert result.returncode != 0, "missing required inputs must fail closed before plan"
    assert (mod / ".terraform").exists(), "cleanup ran after a failed preflight (before Terraform was invoked)"
    for fname in CLEANUP_WORKING_ARTIFACTS:
        assert (mod / fname).exists(), f"cleanup removed {fname} after a failed preflight"


def test_aws_smoke_cleanup_skipped_on_failed_preflight(tmp_path):
    _assert_cleanup_skipped_on_preflight_failure(tmp_path, SMOKE_SCRIPT)


def test_gcp_smoke_cleanup_skipped_on_failed_preflight(tmp_path):
    _assert_cleanup_skipped_on_preflight_failure(tmp_path, GCP_SMOKE_SCRIPT)


def test_readmes_document_artifact_cleanup_option_default_off():
    for module_dir in (AWS_DIR, GCP_DIR):
        section = _smoke_section(_read(module_dir / "README.md"))
        assert section, f"{module_dir.name}: README has no live-smoke helper section"
        assert "CLEAN_TERRAFORM_ARTIFACTS" in section, (
            f"{module_dir.name}: live-smoke section does not document CLEAN_TERRAFORM_ARTIFACTS"
        )
        lowered = section.lower()
        # Default off and scoped to the plan-only working artifacts.
        assert "default" in lowered, (
            f"{module_dir.name}: live-smoke section does not state the safe default (off)"
        )
        assert "artifact" in lowered, (
            f"{module_dir.name}: live-smoke section does not describe the working-artifact cleanup"
        )
        assert "plan-only" in lowered or "plan only" in lowered, (
            f"{module_dir.name}: live-smoke section does not tie the cleanup to the plan-only path"
        )
