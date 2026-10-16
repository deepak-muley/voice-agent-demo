#!/usr/bin/env bash
# Create the Kind cluster and install Ollama and Open WebUI with Helm.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="llm"
NAMESPACE="llm"
# Pinned so a later default StorageClass change does not surprise this script.
LOCAL_PATH_VERSION="v0.0.37"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing $1" >&2
    exit 1
  fi
}

need docker
need kind
need kubectl
need helm

if ! docker info >/dev/null 2>&1; then
  echo "docker is not running" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "kind cluster ${CLUSTER} already exists"
else
  echo "creating kind cluster ${CLUSTER}"
  kind create cluster --name "$CLUSTER" --config "$ROOT/kind-config.yaml"
fi

kubectl config use-context "kind-${CLUSTER}"

echo "installing local-path-provisioner ${LOCAL_PATH_VERSION}"
kubectl apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_VERSION}/deploy/local-path-storage.yaml"
kubectl -n local-path-storage rollout status deploy/local-path-provisioner --timeout=180s

helm repo add otwld https://helm.otwld.com/ --force-update
helm repo add open-webui https://helm.openwebui.com/ --force-update
helm repo update

echo "installing ollama (the pod stays unready while llama3.2:3b downloads)"
helm upgrade --install ollama otwld/ollama \
  --namespace "$NAMESPACE" \
  --create-namespace \
  --values "$ROOT/values/ollama.yaml" \
  --wait \
  --timeout 30m

echo "installing open-webui"
helm upgrade --install open-webui open-webui/open-webui \
  --namespace "$NAMESPACE" \
  --values "$ROOT/values/open-webui.yaml" \
  --wait \
  --timeout 15m

echo
echo "Open WebUI  http://localhost:8080"
echo "Ollama API   http://localhost:11434"
echo
echo "Voice agent:"
echo "  OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \\"
echo "    OPENAI_MODEL=llama3.2:3b python3 single_agent/voice_agent.py --live"
