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
- Produces structured transcript results with word timestamps
- Maintains progress and heartbeat state, supports cancellation, and recovers interrupted jobs

### `apps/diarization-service`

- Optional FastAPI service hosted on the GPU VM
- Runs pyannote speaker diarization only when explicitly requested
- Assigns anonymous speaker labels to Whisper words; it never identifies real people
- Serializes diarization and Ollama through one GPU lock and unloads Ollama before diarization
- Loads and releases the pyannote pipeline on demand to protect the RTX 2070's 8 GB VRAM

## Infrastructure

- Docker Compose for local and server deployment bootstrap
- PostgreSQL for persistent app data
- Redis for queues and rate limiting
- Ollama for internal LLM inference
- Nginx Proxy Manager as the only public reverse proxy in the homelab
- Alembic migrations run before the gateway starts

## Trust boundaries

- Internet traffic must terminate at the reverse proxy, then flow only to `ai-gateway`
- `ollama`, `postgres`, `redis`, and worker processes stay on private container networking
- Obsidian clients authenticate against the gateway, never directly against internal services
- Metrics use a separate monitoring token and are not exposed anonymously
- The GPU coordinator is reachable only from trusted private hosts

## Runtime flow

Audio is uploaded to the gateway, queued in Redis, transcribed on CPU, and optionally
sent to the GPU coordinator for speaker turns. The completed job remains available
to the plugin job center, so Obsidian can be restarted without losing the workflow.
Long meetings use bounded chronological pre-digests before final generation. RAG
retrieval limits repeated chunks from a single document to keep source context diverse.
