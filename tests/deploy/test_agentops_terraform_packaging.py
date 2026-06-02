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

    # Remaining honest parity gaps stay explicit.
    assert "private network" in lowered, "networking-creation gap dropped"
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
