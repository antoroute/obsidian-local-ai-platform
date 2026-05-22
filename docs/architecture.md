# Architecture

## Goal

`obsidian-local-ai-platform` is a private AI platform for Obsidian focused on local or self-hosted inference with a strong security posture.

## Main components

### `apps/obsidian-plugin`

- Obsidian community plugin written in TypeScript
- Reads the active note or a selected range
- Sends requests to `ai-gateway`
- Writes generated Markdown back into the vault

### `apps/ai-gateway`

- FastAPI backend
- Single public API entrypoint, expected behind HTTPS reverse proxy
- Owns authentication, validation, quotas, audit logging, and model allowlists
- Talks to Ollama and background workers over private internal networking only

### `apps/whisper-worker`

- Python background worker
- Consumes audio transcription jobs from Redis
- Produces structured transcript results with timestamps

## Infrastructure

- Docker Compose for local and server deployment bootstrap
- PostgreSQL for persistent app data
- Redis for queues and rate limiting
- Ollama for internal LLM inference
- Reverse proxy later via Traefik or Caddy

## Trust boundaries

- Internet traffic must terminate at the reverse proxy, then flow only to `ai-gateway`
- `ollama`, `postgres`, `redis`, and worker processes stay on private container networking
- Obsidian clients authenticate against the gateway, never directly against internal services

## Current bootstrap scope

This repository currently provides only a minimal skeleton. Authentication, job orchestration, persistence, and inference flows are planned for later incremental work.
