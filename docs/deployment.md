# Deployment

## Target environment

- Docker Compose
- GPU server with NVIDIA RTX 3090
- Reverse proxy in front of `ai-gateway`
- Internal-only networking for Ollama, Redis, PostgreSQL, and workers

## Bootstrap compose behavior

The current `docker-compose.yml` is intentionally minimal:

- builds `ai-gateway`
- builds `whisper-worker`
- starts `postgres`
- starts `redis`
- starts `ollama`
- publishes only `ai-gateway` on `127.0.0.1:8000`

## Start the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

## Health check

```powershell
curl http://127.0.0.1:8000/v1/health
```

## Planned deployment hardening

- Add reverse proxy service and TLS
- Add persistent internal network definitions
- Add GPU reservation/device configuration
- Add secrets management strategy
- Add database migrations
- Add resource limits and observability
