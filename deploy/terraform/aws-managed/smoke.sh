#!/usr/bin/env bash
#
# AgentOps Hermes Runtime — aws-managed live-smoke helper.
#
# Run this from inside deploy/terraform/aws-managed AFTER configuring
# terraform.tfvars and AWS credentials/config. It fails closed before any side
# effect when the Terraform/OpenTofu CLI is missing or AWS credentials/config are
# unavailable, defaults to a safe plan-only run, and surfaces the post-apply smoke
# hints/outputs.
#
# Modes:
#   PLAN_ONLY=1 (default)  init + validate + plan only — no apply.
#   PLAN_ONLY=0            init + validate + plan + apply, then print smoke outputs.
#
# This helper never accepts or echoes raw app/integration secret values; bootstrap
# (M16) owns those and writes them into the secret backend after apply.

set -euo pipefail

PLAN_ONLY="${PLAN_ONLY:-1}"

# Operate on the module directory this script lives in (module-local commands;
# no root-relative -chdir hacks).
cd "$(dirname "$0")"

# --- prerequisite: a Terraform or OpenTofu CLI -------------------------------
if command -v terraform >/dev/null 2>&1; then
  TF="terraform"
elif command -v tofu >/dev/null 2>&1; then
  TF="tofu"
else
  echo "error: neither 'terraform' nor 'tofu' (OpenTofu) is installed/on PATH" >&2
  exit 1
fi

# --- prerequisite: AWS credentials/config ------------------------------------
if ! command -v aws >/dev/null 2>&1; then
  echo "error: the 'aws' CLI is required to verify credentials/config" >&2
  exit 1
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "error: AWS credentials/config unavailable — run 'aws configure'/set AWS_PROFILE first" >&2
  exit 1
fi

# --- side effects only after prerequisites pass ------------------------------
"$TF" init -input=false
"$TF" validate
"$TF" plan -input=false

if [ "$PLAN_ONLY" != "0" ]; then
  echo "PLAN_ONLY=$PLAN_ONLY — stopping after plan. Re-run with PLAN_ONLY=0 to apply." >&2
  exit 0
fi

"$TF" apply -input=false -auto-approve

# --- post-apply smoke hints/outputs ------------------------------------------
echo "Apply complete — smoke hints/outputs:"
"$TF" output agentops_api_url
"$TF" output smoke_test_hints
