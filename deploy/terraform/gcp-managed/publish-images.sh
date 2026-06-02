#!/usr/bin/env bash
#
# AgentOps Hermes Runtime — gcp-managed image-publishing helper.
#
# Builds, tags, and pushes the three runtime container images
# (control-plane / worker / scheduler) to Google Artifact Registry and prints
# the non-secret terraform.tfvars lines (control_plane_image / worker_image /
# scheduler_image) to paste before a `PLAN_ONLY=0 ./smoke.sh` apply. This gives
# operators a path to produce REAL image refs that replace the ':replace-me'
# placeholders without ever handing app/integration secrets to Terraform.
#
# Side-effect-safe by default: with DRY_RUN=1 (the default) it only PRINTS the
# docker/gcloud commands it would run — it never builds, logs in, tags, pushes,
# creates/configures repositories, or modifies Terraform vars. Set DRY_RUN=0 for
# a live publish; the live path requires the gcloud + docker CLIs and verifies an
# active gcloud account before any side effect, failing closed if a
# prerequisite/config is missing.
#
# Inputs (all env, all non-secret):
#   DRY_RUN          1 (default) prints commands; 0 performs the live publish.
#   PROJECT          GCP project id. Autodetected via `gcloud config` on the live
#                    path when unset; a placeholder is used in dry-run.
#   AR_LOCATION      Artifact Registry location (default: us-central1).
#   AR_REPO          Artifact Registry repository (default: agentops-hermes-runtime).
#   IMAGE_TAG        Image tag (default: UTC timestamp).
#   CREATE_REPO      1 to also create/configure the Artifact Registry repo on the
#                    live path (default 0). Dry-run always prints the command.
#   CONTROL_PLANE_IMAGE_NAME / WORKER_IMAGE_NAME / SCHEDULER_IMAGE_NAME
#                    image names within the repo (defaults based on the agentops
#                    hermes runtime).
#   CONTROL_PLANE_CONTEXT / WORKER_CONTEXT / SCHEDULER_CONTEXT
#                    docker build contexts. Default: the repo root ("../../.."
#                    from this module dir), where the Dockerfile lives — this
#                    helper cd's into the module dir, so "." would build the
#                    (Dockerfile-less) module directory.
#
# This helper never accepts or echoes raw app/integration secret values.

set -euo pipefail

# Operate from the module directory this script lives in (module-local; no
# root-relative -chdir).
cd "$(dirname "$0")"

DRY_RUN="${DRY_RUN:-1}"

# Opt-in, non-secret image tfvars writer. With WRITE_TFVARS=1 on the live path
# (DRY_RUN=0) the three image assignments are also written to a module-local
# tfvars override (default image.auto.tfvars) so operators need not hand-copy
# them before a PLAN_ONLY=0 ./smoke.sh apply. Off by default; never writes in
# dry-run.
WRITE_TFVARS="${WRITE_TFVARS:-0}"
IMAGE_TFVARS_PATH="${IMAGE_TFVARS_PATH:-image.auto.tfvars}"

IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
AR_LOCATION="${AR_LOCATION:-us-central1}"
AR_REPO="${AR_REPO:-agentops-hermes-runtime}"
CREATE_REPO="${CREATE_REPO:-0}"

CONTROL_PLANE_IMAGE_NAME="${CONTROL_PLANE_IMAGE_NAME:-hermes-control-plane}"
WORKER_IMAGE_NAME="${WORKER_IMAGE_NAME:-hermes-worker}"
SCHEDULER_IMAGE_NAME="${SCHEDULER_IMAGE_NAME:-hermes-scheduler}"

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
# Dockerfile BEFORE any cloud/docker side effect (repo create/configure-docker,
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
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: the 'gcloud' CLI is required for a live publish (DRY_RUN=0)" >&2
    exit 1
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: 'docker' is required for a live publish (DRY_RUN=0)" >&2
    exit 1
  fi
  if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q .; then
    echo "error: no active gcloud account — run 'gcloud auth login' first" >&2
    exit 1
  fi
  PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
  if [ -z "${PROJECT}" ] || [ "${PROJECT}" = "(unset)" ]; then
    echo "error: PROJECT must be set (or 'gcloud config set project <id>') for a live publish" >&2
    exit 1
  fi
  # Build contexts must be valid before any repo create / docker auth / build.
  preflight_build_contexts
  # A custom image-tfvars output path must stay module-local before we publish.
  if [ "$WRITE_TFVARS" = "1" ]; then
    preflight_image_tfvars_path
  fi
else
  # Dry-run placeholder so the printed commands are concrete and readable.
  PROJECT="${PROJECT:-<GCP_PROJECT>}"
fi

REGISTRY="${AR_LOCATION}-docker.pkg.dev/${PROJECT}/${AR_REPO}"

CONTROL_PLANE_IMAGE="${REGISTRY}/${CONTROL_PLANE_IMAGE_NAME}:${IMAGE_TAG}"
WORKER_IMAGE="${REGISTRY}/${WORKER_IMAGE_NAME}:${IMAGE_TAG}"
SCHEDULER_IMAGE="${REGISTRY}/${SCHEDULER_IMAGE_NAME}:${IMAGE_TAG}"

# --- optional repo create + docker auth config -------------------------------
# Repository creation is only performed on the live path when CREATE_REPO=1, and
# is always printed in dry-run so operators can see the exact command.
if [ "${CREATE_REPO}" = "1" ] || [ "$DRY_RUN" != "0" ]; then
  run gcloud artifacts repositories create "${AR_REPO}" --repository-format=docker --location="${AR_LOCATION}" --project="${PROJECT}"
fi
run gcloud auth configure-docker "${AR_LOCATION}-docker.pkg.dev" --quiet

# --- per-service: build, tag, push -------------------------------------------
publish_image() {
  context="$1"
  image="$2"
  run docker build -t "${image}" "${context}"
  run docker push "${image}"
}

publish_image "${CONTROL_PLANE_CONTEXT}" "${CONTROL_PLANE_IMAGE}"
publish_image "${WORKER_CONTEXT}" "${WORKER_IMAGE}"
publish_image "${SCHEDULER_CONTEXT}" "${SCHEDULER_IMAGE}"

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
