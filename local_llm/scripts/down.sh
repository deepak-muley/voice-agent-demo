#!/usr/bin/env bash
# Delete the Kind cluster. Model files on the cluster disk go with it.
set -euo pipefail

CLUSTER="llm"

if ! command -v kind >/dev/null 2>&1; then
  echo "missing kind" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind delete cluster --name "$CLUSTER"
else
  echo "kind cluster ${CLUSTER} is already gone"
fi
