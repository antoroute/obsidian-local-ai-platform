# Deployment

## Target environment

- Docker Compose
- GPU server with NVIDIA RTX 3090
- Reverse proxy in front of `ai-gateway`
- Internal-only networking for Ollama, Redis, PostgreSQL, and workers
- NVIDIA Container Toolkit installed on the Docker host

## Compose topology

The hardened `docker-compose.yml` now defines:

- `reverse-proxy` using Traefik as the only public entrypoint
- `ai-gateway` connected to both `proxy` and `ai_internal`
- `ollama`, `redis`, `postgres`, and `whisper-worker` connected only to `ai_internal`
- `proxy` network for public ingress
- `ai_internal` network with `internal: true` for east-west traffic only

Published host ports:

- `80:80`
- `443:443`

No host ports are published for:

- `ai-gateway`
- `ollama`
- `redis`
- `postgres`
- `whisper-worker`

## Required host setup

Install NVIDIA Container Toolkit before starting GPU-backed services.

Reference verification command:

```powershell
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Expected result:

- the container can see the NVIDIA RTX 3090
- `nvidia-smi` reports the installed driver and GPU details

## Start the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

## Optional local dev override

The default stack keeps `ai-gateway` off host ports. If you explicitly want direct local access for development, use the documented override:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml up --build
```

This adds only:

- `127.0.0.1:8000:8000` for `ai-gateway`

## Validation commands

Render the final Compose configuration:

```powershell
docker compose config
```

Start the hardened stack:

```powershell
docker compose up --build -d
```

Check the reverse-proxied health endpoint:

```powershell
curl http://localhost/v1/health
```

Check that only 80 and 443 are published:

```powershell
docker compose ps
```

Verify the gateway can reach Ollama over the internal network:

```powershell
docker compose exec ai-gateway python -c "from urllib.request import urlopen; print(urlopen('http://ollama:11434/api/tags').status)"
```

Verify Ollama is not reachable from the host directly:

```powershell
curl http://127.0.0.1:11434
```

Expected result:

- connection failure or refusal, because port `11434` is not published

Verify Redis is not reachable from the host:

```powershell
Test-NetConnection 127.0.0.1 -Port 6379
```

Verify PostgreSQL is not reachable from the host:

```powershell
Test-NetConnection 127.0.0.1 -Port 5432
```

Verify GPU visibility inside Ollama:

```powershell
docker compose exec ollama ollama ps
```

Verify GPU visibility inside the worker container:

```powershell
docker compose exec whisper-worker python -c "import os; print(os.getenv('NVIDIA_VISIBLE_DEVICES', 'unset'))"
```

## Healthchecks

Configured healthchecks:

- `reverse-proxy`: Traefik ping healthcheck
- `ai-gateway`: `GET /v1/health` on container-local port `8000`
- `postgres`: `pg_isready`
- `redis`: `redis-cli ping`
- `ollama`: `ollama list`

## Operational notes

- `OLLAMA_BASE_URL` must stay `http://ollama:11434` inside Docker
- `AI_GATEWAY_DATABASE_URL` must point to PostgreSQL on the `ai_internal` network
- `AI_GATEWAY_REDIS_URL` must point to Redis on the `ai_internal` network
- TLS certificate management for public Internet exposure is a later step; Traefik is already positioned as the only public entrypoint
