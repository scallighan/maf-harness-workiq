#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
for tool in terraform az docker uv curl; do
  command -v "$tool" >/dev/null || { echo "Required tool not found: $tool" >&2; exit 1; }
done
terraform -chdir=terraform init -input=false
SUBSCRIPTION_ID="$(terraform -chdir=terraform output -raw subscription_id)"
az account show --subscription "$SUBSCRIPTION_ID" --output none
ACR_NAME="$(terraform -chdir=terraform output -raw registry_name)"
LOGIN_SERVER="$(terraform -chdir=terraform output -raw registry_login_server)"
TAG="${TAG:-$(git rev-parse --short=7 HEAD)-$(date -u +%Y%m%d%H%M%S)}"

az acr login --subscription "$SUBSCRIPTION_ID" --name "$ACR_NAME"
docker build --platform linux/amd64 -f ui/Dockerfile -t "$LOGIN_SERVER/workplace-ui:$TAG" .
docker push "$LOGIN_SERVER/workplace-ui:$TAG"
DIGEST="$(az acr repository show --subscription "$SUBSCRIPTION_ID" --name "$ACR_NAME" \
  --image "workplace-ui:$TAG" --query digest --output tsv)"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Invalid UI image digest." >&2; exit 1; }
IMAGE_URI="$LOGIN_SERVER/workplace-ui@$DIGEST"
RELEASE_INPUTS="$(mktemp "$ROOT/terraform/.ui-release.XXXXXX")"
trap 'rm -f "$RELEASE_INPUTS"' EXIT
uv run python - "$IMAGE_URI" >"$RELEASE_INPUTS" <<'PY'
import json
import sys

print(f"ui_image_uri = {json.dumps(sys.argv[1])}")
PY
mv "$RELEASE_INPUTS" "$ROOT/terraform/ui.auto.tfvars"

# Terraform asks for confirmation before provisioning or updating the UI.
terraform -chdir=terraform apply
uv run python -m scripts.setup_ui
URL="$(terraform -chdir=terraform output -raw ui_url)"
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error --max-time 20 "$URL/readyz" >/dev/null; then
    printf '\nUI: %s\nImage: %s\nSign in as an authorized Foundry/Work IQ user.\n' "$URL" "$IMAGE_URI"
    exit 0
  fi
  sleep 10
done
echo "UI readiness did not succeed; inspect Container Apps logs and private Cosmos access." >&2
exit 1
