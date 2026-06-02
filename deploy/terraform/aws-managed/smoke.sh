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
# Optional cleanup guard:
#   DESTROY_ON_FAILURE=1   On the apply path only, if apply SUCCEEDS but the
#                          subsequent /healthz probe FAILS, attempt a
#                          non-interactive auto-approved Terraform/OpenTofu destroy
#                          before exiting non-zero, so a failed live smoke does
#                          not leave a half-provisioned stack behind. Defaults to
#                          0 (no destroy), preserving existing behavior. It never
#                          destroys on plan-only runs, never destroys before a
#                          successful apply, and never destroys on a preflight
#                          failure. Provider-agnostic — destroy goes through the
#                          detected Terraform/OpenTofu CLI, never a cloud SDK.
#
# This helper never accepts or echoes raw app/integration secret values; bootstrap
# (M16) owns those and writes them into the secret backend after apply.

set -euo pipefail

PLAN_ONLY="${PLAN_ONLY:-1}"
DESTROY_ON_FAILURE="${DESTROY_ON_FAILURE:-0}"
CLEAN_TERRAFORM_ARTIFACTS="${CLEAN_TERRAFORM_ARTIFACTS:-0}"

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

# --- optional: clean local Terraform/OpenTofu working artifacts --------------
# CLEAN_TERRAFORM_ARTIFACTS=1 (opt-in; default 0) removes the local
# Terraform/OpenTofu working artifacts this helper's init/validate/plan create
# (.terraform/, .terraform.lock.hcl, state/plan/crash files) after a SUCCESSFUL
# plan-only run, without touching source (.tf), inputs (terraform.tfvars,
# generated *.auto.tfvars), the README, or smoke-transcript evidence. set -e
# aborts earlier on a failed preflight or plan, so a failed run never cleans, and
# it is only reached on the plan-only path (the apply path keeps its state and
# transcript). No raw app/integration secret values are involved.
clean_terraform_artifacts() {
  rm -rf .terraform
  rm -f .terraform.lock.hcl ./*.tfstate ./*.tfstate.* ./*.tfplan crash.log crash.*.log
  echo "CLEAN_TERRAFORM_ARTIFACTS=1 — removed local Terraform/OpenTofu working artifacts (.terraform/, lock, state/plan/crash)." >&2
}

if [ "$PLAN_ONLY" != "0" ]; then
  echo "PLAN_ONLY=$PLAN_ONLY — stopping after plan. Re-run with PLAN_ONLY=0 to apply." >&2
  if [ "$CLEAN_TERRAFORM_ARTIFACTS" = "1" ]; then
    clean_terraform_artifacts
  fi
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
  # Optional cleanup: apply succeeded but the endpoint is unhealthy. Only when
  # explicitly opted in (DESTROY_ON_FAILURE=1) do we tear the stack back down via
  # the detected Terraform/OpenTofu CLI (no cloud SDK) before failing closed.
  if [ "$DESTROY_ON_FAILURE" = "1" ]; then
    echo "DESTROY_ON_FAILURE=1 — apply succeeded but /healthz failed; destroying provisioned resources ..." >&2
    "$TF" destroy -auto-approve -input=false
  fi
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
# Write atomically: build the transcript into a temporary file and only rename it
# into place once fully written, removing the temporary file if any line fails.
# This keeps operator evidence from being partially committed if `terraform
# output` errors mid-write.
TRANSCRIPT_TMP="${TRANSCRIPT}.tmp"
# Remove the partial temp transcript if the script is interrupted or exits in the
# window before the atomic mv, so an operator never finds a half-written
# ${TRANSCRIPT}.tmp left behind. EXIT cleanup returns normally; a trapped signal
# otherwise REPLACES bash's default terminating behavior, so the HUP/INT/TERM
# handler must also terminate non-zero (128+signal) to stay fail-closed.
cleanup_smoke_transcript_tmp() {
  if [ -n "${TRANSCRIPT_TMP:-}" ]; then
    rm -f "$TRANSCRIPT_TMP"
  fi
}
on_signal_cleanup_transcript() {
  cleanup_smoke_transcript_tmp
  trap - EXIT HUP INT TERM
  case "$1" in
    HUP) exit 129 ;;
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 1 ;;
  esac
}
trap cleanup_smoke_transcript_tmp EXIT
trap 'on_signal_cleanup_transcript HUP' HUP
trap 'on_signal_cleanup_transcript INT' INT
trap 'on_signal_cleanup_transcript TERM' TERM
if ! {
  echo "provider/profile: aws-managed"
  echo "timestamp (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "agentops_api_url: ${API_URL}"
  echo "healthz probe: OK — ${API_URL}/healthz responded healthy"
  echo "smoke_test_hints:"
  "$TF" output smoke_test_hints
} >"$TRANSCRIPT_TMP"; then
  rm -f "$TRANSCRIPT_TMP"
  echo "error: failed to build smoke transcript — removed partial ${TRANSCRIPT_TMP}" >&2
  exit 1
fi
mv "$TRANSCRIPT_TMP" "$TRANSCRIPT"
# The final transcript is in place; clear the temp path and disable the cleanup
# trap so the EXIT handler can never remove the operator-facing transcript.
TRANSCRIPT_TMP=""
trap - EXIT HUP INT TERM
echo "Wrote non-secret smoke transcript: ${TRANSCRIPT}"
echo "Attach it to M15 evidence after reviewing it for secrets before sharing."
