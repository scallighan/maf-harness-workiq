#!/usr/bin/env bash
# Run the interactive local demo against the Terraform-managed Foundry project.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for tool in terraform az uv; do
  command -v "$tool" >/dev/null || { echo "Required tool not found: $tool" >&2; exit 1; }
done
if ! az account show --output none; then
  echo "Sign in with az login and select your Azure subscription before running this script." >&2
  exit 1
fi

FOUNDRY_PROJECT_ENDPOINT="$(terraform -chdir=terraform output -raw foundry_project_endpoint)"
AZURE_AI_PROJECT_ENDPOINT="$FOUNDRY_PROJECT_ENDPOINT"
AZURE_AI_MODEL_DEPLOYMENT_NAME="$(terraform -chdir=terraform output -raw chat_model_deployment)"
AZURE_TENANT_ID="$(terraform -chdir=terraform output -raw tenant_id)"
TOOLBOX_ENDPOINT="$(terraform -chdir=terraform output -raw workiq_toolbox_endpoint)"
export FOUNDRY_PROJECT_ENDPOINT AZURE_AI_PROJECT_ENDPOINT AZURE_AI_MODEL_DEPLOYMENT_NAME AZURE_TENANT_ID TOOLBOX_ENDPOINT
export ENABLE_SENSITIVE_DATA=false

uv sync --frozen
exec uv run workplace-demo "$@"
