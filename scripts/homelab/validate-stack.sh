#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
environment_file="${1:-${repository_root}/.env.homelab}"
compose_file="${repository_root}/infra/docker-compose.homelab.yml"

if [[ ! -f "${environment_file}" ]]; then
  echo "Environment file not found: ${environment_file}" >&2
  echo "Create it from .env.homelab.example or export the variables in Portainer." >&2
  exit 1
fi

postgres_password="$(sed -n 's/^POSTGRES_PASSWORD=//p' "${environment_file}" | tail -n 1)"
if [[ -z "${postgres_password}" || "${postgres_password}" == replace-with-* ]]; then
  echo "POSTGRES_PASSWORD must be replaced before deployment." >&2
  exit 1
fi

bind_address="$(sed -n 's/^GATEWAY_BIND_ADDRESS=//p' "${environment_file}" | tail -n 1)"
if [[ "${bind_address}" == "0.0.0.0" ]]; then
  echo "GATEWAY_BIND_ADDRESS must target the Docker host address, not 0.0.0.0." >&2
  exit 1
fi

docker compose --env-file "${environment_file}" -f "${compose_file}" config --quiet
docker compose --env-file "${environment_file}" -f "${compose_file}" --profile prepare-model config --quiet

echo "Homelab Compose configuration is valid."
