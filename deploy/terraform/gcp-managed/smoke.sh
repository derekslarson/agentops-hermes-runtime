#!/usr/bin/env bash
#
# AgentOps Hermes Runtime — gcp-managed live-smoke helper.
#
# Run this from inside deploy/terraform/gcp-managed AFTER configuring
# terraform.tfvars and gcloud (auth + project/region). It fails closed before any
# side effect when the Terraform/OpenTofu CLI is missing or gcloud is unauthenticated,
# defaults to a safe plan-only run, and surfaces the post-apply smoke hints/outputs.
#
# Honesty: the gcp-managed module is still scaffold-level — no live GCP apply/smoke
# is captured, and bootstrap (M16) owns the raw secret values. This helper gives the
# same ergonomics as aws-managed so a plan can be exercised, but a successful plan is
# not a captured live deployment.
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

# --- prerequisite: gcloud CLI + authenticated account ------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  echo "error: the 'gcloud' CLI is required to verify GCP authentication/config" >&2
  exit 1
fi

# Read-only auth check — lists the active account without any side effect. Fails
# closed if no account is active so we never plan/apply against an unset project.
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q .; then
  echo "error: no active gcloud account — run 'gcloud auth login' and 'gcloud config set project <id>' first" >&2
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
