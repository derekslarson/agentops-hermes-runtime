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
#   PLAN_ONLY=0            init + validate + plan + apply, then probe the live API
#                          (${agentops_api_url}/healthz) and print smoke outputs.
#
# On the apply path the helper proves the provisioned API endpoint responds: it
# fetches the bare agentops_api_url output and curls ${agentops_api_url}/healthz,
# failing closed (non-zero) on an unhealthy/unreachable response.
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

# --- apply-path prerequisite: curl for the post-apply health probe -----------
# Required only on the apply path (plan-only never probes). Fail clearly BEFORE
# the apply side effect if curl is unavailable.
if ! command -v curl >/dev/null 2>&1; then
  echo "error: 'curl' is required to probe the API health endpoint after apply" >&2
  exit 1
fi

"$TF" apply -input=false -auto-approve

# --- post-apply API health probe ---------------------------------------------
# Prove the provisioned API endpoint responds before declaring success.
API_URL="$("$TF" output -raw agentops_api_url)"
echo "Probing ${API_URL}/healthz ..."
if ! curl -fsS --max-time 30 "${API_URL}/healthz" >/dev/null; then
  echo "error: API health probe failed — ${API_URL}/healthz did not respond healthy" >&2
  exit 1
fi
echo "API health probe OK — ${API_URL}/healthz responded healthy"

# --- post-apply smoke hints/outputs ------------------------------------------
echo "Apply complete — smoke hints/outputs:"
"$TF" output agentops_api_url
"$TF" output smoke_test_hints
