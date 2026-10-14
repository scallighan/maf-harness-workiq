#!/usr/bin/env bash
# Build an immutable image and deploy the hosted agent with Foundry Toolbox.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for tool in terraform az docker uv; do
  command -v "$tool" >/dev/null || { echo "Required tool not found: $tool" >&2; exit 1; }
done
az account show --output none
uv sync --frozen

echo "== 1/4 Terraform platform apply (preserves any existing agent release) =="
terraform -chdir=terraform init -input=false
HAS_PLATFORM="$(terraform -chdir=terraform output -json | uv run python -c \
  'import json, sys; print("registry_name" in json.load(sys.stdin))')"
if [[ "$HAS_PLATFORM" == "False" ]]; then
  terraform -chdir=terraform apply
elif [[ "$HAS_PLATFORM" != "True" ]]; then
  echo "Could not determine whether the Terraform platform is provisioned." >&2
  exit 1
fi
PROJECT_ENDPOINT="$(terraform -chdir=terraform output -raw foundry_project_endpoint)"
ACR_NAME="$(terraform -chdir=terraform output -raw registry_name)"
LOGIN_SERVER="$(terraform -chdir=terraform output -raw registry_login_server)"

# Timestamp keeps dirty builds distinct even when the commit SHA is unchanged.
TAG="${TAG:-$(git rev-parse --short=7 HEAD)-$(date -u +%Y%m%d%H%M%S)}"
echo "== 2/4 Build and push image: $TAG =="
az acr login --name "$ACR_NAME"
docker build --platform linux/amd64 -t "$LOGIN_SERVER/workplace-agent:$TAG" .
docker push "$LOGIN_SERVER/workplace-agent:$TAG"
DIGEST="$(az acr repository show --name "$ACR_NAME" \
  --image "workplace-agent:$TAG" --query digest --output tsv)"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Invalid image digest: $DIGEST" >&2; exit 1; }
IMAGE_URI="$LOGIN_SERVER/workplace-agent@$DIGEST"

echo "== 3/4 Persist image and apply hosted agent and identity grants =="
RELEASE_INPUTS="$(mktemp "$ROOT/terraform/.agent-release.XXXXXX")"
trap 'rm -f "$RELEASE_INPUTS"' EXIT
uv run python - "$IMAGE_URI" >"$RELEASE_INPUTS" <<'PY'
import json
import sys

print(f"agent_image_uri = {json.dumps(sys.argv[1])}")
PY
mv "$RELEASE_INPUTS" "$ROOT/terraform/agent.auto.tfvars"
terraform -chdir=terraform apply
uv run python scripts/setup_toolbox.py
AGENT_NAME="$(terraform -chdir=terraform output -raw agent_name)"
AGENT_VERSION="$(terraform -chdir=terraform output -raw agent_version)"

echo "== 4/4 Wait for this exact version and image =="
uv run python scripts/wait_for_agent.py \
  --project-endpoint "$PROJECT_ENDPOINT" --name "$AGENT_NAME" \
  --version "$AGENT_VERSION" --image "$IMAGE_URI"
printf '\nProject: %s\nAgent: %s:%s\nImage: %s\nWork IQ: Foundry Toolbox per-user consent\n' \
  "$PROJECT_ENDPOINT" "$AGENT_NAME" "$AGENT_VERSION" "$IMAGE_URI"
