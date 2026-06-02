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
