#!/usr/bin/env bash
#
# AgentOps Hermes Runtime — aws-managed image-publishing helper.
#
# Builds, tags, and pushes the three runtime container images
# (control-plane / worker / scheduler) to Amazon ECR and prints the non-secret
# terraform.tfvars lines (control_plane_image / worker_image / scheduler_image)
# to paste before a `PLAN_ONLY=0 ./smoke.sh` apply. This gives operators a path
# to produce REAL image refs that replace the ':replace-me' placeholders without
# ever handing app/integration secrets to Terraform.
#
# Side-effect-safe by default: with DRY_RUN=1 (the default) it only PRINTS the
# docker/aws commands it would run — it never builds, logs in, tags, pushes,
# creates repositories, or modifies Terraform vars. Set DRY_RUN=0 for a live
# publish; the live path requires the aws + docker CLIs and verifies AWS
# credentials before any side effect, failing closed if a prerequisite/config is
# missing.
#
# Inputs (all env, all non-secret):
#   DRY_RUN            1 (default) prints commands; 0 performs the live publish.
#   AWS_REGION         ECR region (falls back to AWS_DEFAULT_REGION). Required on
#                      the live path; a placeholder is used in dry-run.
#   AWS_ACCOUNT_ID     ECR account id. Autodetected via `aws sts` on the live
#                      path when unset; a placeholder is used in dry-run.
#   IMAGE_TAG          Image tag (default: UTC timestamp).
#   CONTROL_PLANE_REPO / WORKER_REPO / SCHEDULER_REPO
#                      ECR repository names (defaults based on the agentops
#                      hermes runtime).
#   CONTROL_PLANE_CONTEXT / WORKER_CONTEXT / SCHEDULER_CONTEXT
#                      docker build contexts. Default: the repo root
#                      ("../../.." from this module dir), where the Dockerfile
#                      lives — this helper cd's into the module dir, so "." would
#                      build the (Dockerfile-less) module directory.
#
# This helper never accepts or echoes raw app/integration secret values.

set -euo pipefail

# Operate from the module directory this script lives in (module-local; no
# root-relative -chdir).
cd "$(dirname "$0")"

DRY_RUN="${DRY_RUN-1}"

# Opt-in, non-secret image tfvars writer. With WRITE_TFVARS=1 on the live path
# (DRY_RUN=0) the three image assignments are also written to a module-local
# tfvars override (default image.auto.tfvars) so operators need not hand-copy
# them before a PLAN_ONLY=0 ./smoke.sh apply. Off by default; never writes in
# dry-run.
WRITE_TFVARS="${WRITE_TFVARS-0}"
IMAGE_TFVARS_PATH="${IMAGE_TFVARS_PATH:-image.auto.tfvars}"

# --- validate boolean-ish safety flags (fail closed before any side effect) --
# DRY_RUN and WRITE_TFVARS must each be exactly 0 or 1. A typo (DRY_RUN=true) must
# not be silently coerced into an unsafe path, so reject anything else with a
# clear non-secret error naming the variable BEFORE any aws/docker side effect or
# live prerequisite command runs.
require_boolean_flag() {
  case "$2" in
    0|1) ;;
    *)
      echo "error: $1 must be 0 or 1" >&2
      exit 1
      ;;
  esac
}
require_boolean_flag DRY_RUN "$DRY_RUN"
require_boolean_flag WRITE_TFVARS "$WRITE_TFVARS"

IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"

CONTROL_PLANE_REPO="${CONTROL_PLANE_REPO:-agentops-hermes-runtime/control-plane}"
WORKER_REPO="${WORKER_REPO:-agentops-hermes-runtime/worker}"
SCHEDULER_REPO="${SCHEDULER_REPO:-agentops-hermes-runtime/scheduler}"

# Build contexts default to the repo root (where the Dockerfile lives), since
# this helper has cd'd into the module dir above. Overridable via env.
CONTROL_PLANE_CONTEXT="${CONTROL_PLANE_CONTEXT:-../../..}"
WORKER_CONTEXT="${WORKER_CONTEXT:-../../..}"
SCHEDULER_CONTEXT="${SCHEDULER_CONTEXT:-../../..}"

# --- side-effect wrapper -----------------------------------------------------
# Executes its arguments only on the live path (DRY_RUN=0); otherwise prints the
# command it WOULD run. Every build/login/tag/push/repository side effect is
# routed through this, so the default dry-run never touches anything.
run() {
  if [ "$DRY_RUN" = "0" ]; then
    "$@"
  else
    echo "+ $*"
  fi
}

# --- live-path build-context / Dockerfile preflight --------------------------
# On the live path only, verify each docker build context exists and contains a
# Dockerfile BEFORE any cloud/docker side effect (login, repository creation,
# build, push). Dry-run stays permissive and never requires the contexts/
# Dockerfiles to exist. Errors are non-secret and name the offending variable.
preflight_build_contexts() {
  for ctx_var in CONTROL_PLANE_CONTEXT WORKER_CONTEXT SCHEDULER_CONTEXT; do
    ctx_dir="${!ctx_var}"
    if [ ! -d "$ctx_dir" ]; then
      echo "error: ${ctx_var}='${ctx_dir}' is not a directory — point ${ctx_var} at a build-context directory containing a Dockerfile" >&2
      exit 1
    fi
    if [ ! -f "${ctx_dir}/Dockerfile" ]; then
      echo "error: ${ctx_var}='${ctx_dir}' has no Dockerfile — point ${ctx_var} at a build-context directory containing a Dockerfile" >&2
      exit 1
    fi
  done
}

# --- opt-in writer: custom output path must stay module-local ----------------
# When WRITE_TFVARS=1 the generated non-secret image tfvars is written to
# IMAGE_TFVARS_PATH. Reject anything that is not a module-local plain filename
# (absolute path, a '/' path segment, or a '..' traversal) so the writer can
# never clobber files outside this module directory and its output stays covered
# by the deploy/terraform/*-managed/ .gitignore entry. Non-secret check only.
preflight_image_tfvars_path() {
  case "$IMAGE_TFVARS_PATH" in
    /*|*/*|*..*)
      echo "error: IMAGE_TFVARS_PATH='${IMAGE_TFVARS_PATH}' must be a module-local filename (no leading '/', no '/' path segments, no '..') so the generated non-secret image tfvars cannot escape the module directory" >&2
      exit 1
      ;;
  esac
}

# --- live-path prerequisites / config (fail closed before side effects) ------
if [ "$DRY_RUN" = "0" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "error: the 'aws' CLI is required for a live publish (DRY_RUN=0)" >&2
    exit 1
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: 'docker' is required for a live publish (DRY_RUN=0)" >&2
    exit 1
  fi
  if [ -z "$AWS_REGION" ]; then
    echo "error: AWS_REGION (or AWS_DEFAULT_REGION) must be set for a live publish" >&2
    exit 1
  fi
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo "error: AWS credentials/config unavailable — run 'aws configure'/set AWS_PROFILE first" >&2
    exit 1
  fi
  AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
  # Build contexts must be valid before any ECR login / repo create / build.
  preflight_build_contexts
  # A custom image-tfvars output path must stay module-local before we publish.
  if [ "$WRITE_TFVARS" = "1" ]; then
    preflight_image_tfvars_path
  fi
else
  # Dry-run placeholders so the printed commands are concrete and readable.
  AWS_REGION="${AWS_REGION:-<AWS_REGION>}"
  AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-<AWS_ACCOUNT_ID>}"
fi

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

CONTROL_PLANE_IMAGE="${REGISTRY}/${CONTROL_PLANE_REPO}:${IMAGE_TAG}"
WORKER_IMAGE="${REGISTRY}/${WORKER_REPO}:${IMAGE_TAG}"
SCHEDULER_IMAGE="${REGISTRY}/${SCHEDULER_REPO}:${IMAGE_TAG}"

# --- ECR login ---------------------------------------------------------------
run sh -c "aws ecr get-login-password --region '${AWS_REGION}' | docker login --username AWS --password-stdin '${REGISTRY}'"

# --- per-service: ensure repo, build, tag, push ------------------------------
publish_image() {
  repo="$1"
  context="$2"
  image="$3"
  # Ensure the ECR repository exists (idempotent: describe, else create).
  run sh -c "aws ecr describe-repositories --repository-names '${repo}' --region '${AWS_REGION}' >/dev/null 2>&1 || aws ecr create-repository --repository-name '${repo}' --region '${AWS_REGION}' >/dev/null"
  run docker build -t "${image}" "${context}"
  run docker push "${image}"
}

publish_image "${CONTROL_PLANE_REPO}" "${CONTROL_PLANE_CONTEXT}" "${CONTROL_PLANE_IMAGE}"
publish_image "${WORKER_REPO}" "${WORKER_CONTEXT}" "${WORKER_IMAGE}"
publish_image "${SCHEDULER_REPO}" "${SCHEDULER_CONTEXT}" "${SCHEDULER_IMAGE}"

# --- non-secret terraform.tfvars image lines ---------------------------------
# Print (never write) the three image variable assignments to paste into
# terraform.tfvars before a PLAN_ONLY=0 ./smoke.sh apply. No secret values here.
if [ "$DRY_RUN" = "0" ]; then
  echo "Pushed images — paste these non-secret lines into terraform.tfvars:"
else
  echo "DRY_RUN=1 — these are the non-secret terraform.tfvars lines a live publish would print:"
fi
echo "control_plane_image = \"${CONTROL_PLANE_IMAGE}\""
echo "worker_image        = \"${WORKER_IMAGE}\""
echo "scheduler_image     = \"${SCHEDULER_IMAGE}\""

# --- opt-in: write the non-secret image tfvars override ----------------------
# Only on the live path (DRY_RUN=0) and only when WRITE_TFVARS=1. Writes solely
# the three non-secret image assignments via a temp file + mv (atomic, no
# partial file on failure). Dry-run never writes anything.
#
# The temp path is tracked in a script-level IMAGE_TFVARS_TMP so an EXIT/signal
# handler can remove a partial `*.auto.tfvars.XXXXXX` left behind if the script
# is interrupted or exits in the window between `mktemp` and the atomic `mv` (a
# function-local `tmp` could never be trapped). EXIT cleanup returns normally; a
# trapped signal otherwise REPLACES bash's default terminating behavior, so the
# HUP/INT/TERM handler must also terminate non-zero (128+signal) to fail closed.
IMAGE_TFVARS_TMP=""
cleanup_image_tfvars_tmp() {
  if [ -n "${IMAGE_TFVARS_TMP:-}" ]; then
    rm -f "$IMAGE_TFVARS_TMP"
  fi
}
on_signal_cleanup_image_tfvars() {
  cleanup_image_tfvars_tmp
  trap - EXIT HUP INT TERM
  case "$1" in
    HUP) exit 129 ;;
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 1 ;;
  esac
}
write_image_tfvars() {
  dest="$1"
  IMAGE_TFVARS_TMP="$(mktemp "${dest}.XXXXXX")"
  trap cleanup_image_tfvars_tmp EXIT
  trap 'on_signal_cleanup_image_tfvars HUP' HUP
  trap 'on_signal_cleanup_image_tfvars INT' INT
  trap 'on_signal_cleanup_image_tfvars TERM' TERM
  {
    echo "control_plane_image = \"${CONTROL_PLANE_IMAGE}\""
    echo "worker_image        = \"${WORKER_IMAGE}\""
    echo "scheduler_image     = \"${SCHEDULER_IMAGE}\""
  } >"$IMAGE_TFVARS_TMP"
  mv "$IMAGE_TFVARS_TMP" "$dest"
  # The generated tfvars file is in place; clear the temp path and disable the
  # cleanup traps so an EXIT handler can never remove the operator-facing file.
  IMAGE_TFVARS_TMP=""
  trap - EXIT HUP INT TERM
}

if [ "$DRY_RUN" = "0" ] && [ "$WRITE_TFVARS" = "1" ]; then
  write_image_tfvars "${IMAGE_TFVARS_PATH}"
  echo "Wrote non-secret image vars to ${IMAGE_TFVARS_PATH}"
fi
