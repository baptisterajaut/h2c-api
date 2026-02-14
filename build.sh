#!/bin/bash
set -euo pipefail

IMAGE="baptisterajaut/h2c-api"
TAG="${1:-latest}"

if command -v nerdctl &> /dev/null; then
    CTR=nerdctl
elif command -v docker &> /dev/null; then
    CTR=docker
else
    echo "Error: neither nerdctl nor docker found" >&2
    exit 1
fi

echo "Using ${CTR}"
echo "Building ${IMAGE}:${TAG}..."

${CTR} build \
    --platform linux/amd64 \
    -t "${IMAGE}:${TAG}" \
    .

echo ""
read -rp "Push ${IMAGE}:${TAG}? [y/N] " answer
if [[ "${answer}" =~ ^[Yy]$ ]]; then
    ${CTR} push "${IMAGE}:${TAG}"
    echo "Pushed ${IMAGE}:${TAG}"
fi
