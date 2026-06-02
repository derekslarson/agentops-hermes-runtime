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

# --- preflight: required non-secret Terraform inputs -------------------------
# Validate the module's required non-secret account/location inputs (region,
# domain) BEFORE any Terraform/OpenTofu side effect (init/validate/plan/apply),
# so an unconfigured run fails closed without creating .terraform/ or a lock file
# (and never reaches a post-init plan-time "missing required variable" error).
# Inputs may be supplied via a tfvars file (terraform.tfvars[.json],
# *.auto.tfvars[.json]) or TF_VAR_* env vars. These are non-secret inputs only;
# raw app/integration secrets remain a bootstrap (M16) concern.
tfvars_file_defines() {
  local file="$1"
  local var_name="$2"
  case "$file" in
    *.json)
      grep -Eq '"'"${var_name}"'"[[:space:]]*:' "$file"
      ;;
    *)
      grep -Eq "^[[:space:]]*${var_name}[[:space:]]*=" "$file"
      ;;
  esac
}

missing=""
for required_var in region domain; do
  env_name="TF_VAR_${required_var}"
  configured=0
  if [ -n "${!env_name:-}" ]; then
    configured=1
  else
    for tfvars_file in terraform.tfvars terraform.tfvars.json *.auto.tfvars *.auto.tfvars.json; do
      if [ -f "$tfvars_file" ] && tfvars_file_defines "$tfvars_file" "$required_var"; then
        configured=1
        break
      fi
    done
  fi
  if [ "$configured" -eq 0 ]; then
    missing="${missing} ${required_var}"
  fi
done

if [ -n "$missing" ]; then
  echo "error: missing required non-secret Terraform input(s):${missing}" >&2
  echo "       supply them via a terraform.tfvars / *.auto.tfvars file that defines each required input, or set TF_VAR_<name> env vars (e.g. TF_VAR_region, TF_VAR_domain) before running ./smoke.sh" >&2
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

# --- apply-path preflight: refuse placeholder ':replace-me' images -----------
# The image variables ship with ':replace-me' placeholder defaults so a plan is
# exercisable, but an apply built from them can never yield a working deployment.
# Inspect each effective (tfvars/defaults) image value independently and fail
# closed BEFORE the apply side effect if any placeholder remains. No raw secret
# values are touched.
for image_var in control_plane_image worker_image scheduler_image; do
  if ! image_value="$(printf 'var.%s\n' "$image_var" | "$TF" console 2>/dev/null)"; then
    echo "error: could not inspect ${image_var} with ${TF} console before PLAN_ONLY=0 apply/smoke" >&2
    exit 1
  fi
  if printf '%s' "$image_value" | grep -q 'replace-me'; then
    echo "error: ${image_var} still contains the ':replace-me' placeholder — set a real image reference in terraform.tfvars before PLAN_ONLY=0 apply/smoke" >&2
    exit 1
  fi
done

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

# --- post-apply non-secret smoke transcript ----------------------------------
# Write a module-local, non-secret transcript an operator can attach to the
# still-pending M15 live-smoke evidence before marking the milestone Done. It
# records the provider/profile, a UTC timestamp, the effective agentops_api_url,
# the /healthz success, and the smoke_test_hints output — all non-secret values
# already surfaced above. It never writes a raw app/integration secret value, but
# review it for secrets before sharing regardless.
TRANSCRIPT="smoke-transcript-$(date -u +%Y%m%dT%H%M%SZ).log"
{
  echo "provider/profile: aws-managed"
  echo "timestamp (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "agentops_api_url: ${API_URL}"
  echo "healthz probe: OK — ${API_URL}/healthz responded healthy"
  echo "smoke_test_hints:"
  "$TF" output smoke_test_hints
} >"$TRANSCRIPT"
echo "Wrote non-secret smoke transcript: ${TRANSCRIPT}"
echo "Attach it to M15 evidence after reviewing it for secrets before sharing."
