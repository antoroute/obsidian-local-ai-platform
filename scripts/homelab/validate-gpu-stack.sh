#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
environment_file="${1:-${repository_root}/.env.gpu}"
compose_file="${repository_root}/infra/docker-compose.gpu-services.yml"

if [[ ! -f "${environment_file}" ]]; then
  echo "Environment file not found: ${environment_file}" >&2
  exit 1
fi

service_token="$(sed -n 's/^DIARIZATION_SERVICE_TOKEN=//p' "${environment_file}" | tail -n 1)"
if [[ -z "${service_token}" || "${service_token}" == replace-with-* ]]; then
  echo "DIARIZATION_SERVICE_TOKEN must be replaced before deployment." >&2
  exit 1
fi

docker compose --env-file "${environment_file}" -f "${compose_file}" config --quiet

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
else
  echo "Warning: nvidia-smi is unavailable; validate the NVIDIA runtime on the GPU VM." >&2
fi

echo "GPU coordinator Compose configuration is valid."
