# AGENTS.md

## Project

This repository builds a private AI meeting assistant for Obsidian.

The system has three main components:

1. `apps/obsidian-plugin`
   - Obsidian community plugin written in TypeScript.
   - Reads the current note or selected text.
   - Lets the user choose a Markdown template.
   - Sends requests to a secured remote/local AI Gateway.
   - Writes generated summaries, meeting minutes, transcripts, and action items back into the Obsidian vault.

2. `apps/ai-gateway`
   - FastAPI backend.
   - Public-facing API behind a reverse proxy.
   - Must authenticate every request using API tokens.
   - Must never expose Ollama directly.
   - Handles note summarization, meeting report generation, audio transcription jobs, job status, quotas, and audit logs.

3. `apps/whisper-worker`
   - Python worker for audio transcription.
   - Consumes jobs from Redis.
   - Uses faster-whisper or whisper.cpp-compatible abstraction.
   - Stores transcripts and returns structured segments with timestamps.

## Non-negotiable security rules

- Never expose Ollama directly to the Internet.
- Never expose Redis, PostgreSQL, or worker ports publicly.
- The only public service is `ai-gateway`, routed through HTTPS reverse proxy.
- Every API request except `/v1/health` must require authentication.
- API tokens must never be stored in plaintext.
- Store only token hashes.
- Do not log secrets, raw API tokens, Authorization headers, or full audio contents.
- Enforce request size limits.
- Enforce per-user quotas.
- Enforce model allowlists.
- Enforce job concurrency limits.
- Add tests for authentication and authorization.
- Do not add cloud AI dependencies unless explicitly requested.

## Target deployment

- Docker Compose.
- GPU server with NVIDIA RTX 3090.
- Ollama runs internally on Docker with GPU access.
- Ollama API is reachable only from `ai-gateway`.
- Whisper worker uses GPU when available.
- Redis handles queues and rate limiting.
- PostgreSQL stores users, API tokens, jobs, quotas, and audit logs.
- Traefik or Caddy handles HTTPS and reverse proxy.

## API design

Base path: `/v1`.

Required endpoints:

- `GET /v1/health`
- `GET /v1/models`
- `POST /v1/notes/summarize`
- `POST /v1/meetings/generate`
- `POST /v1/audio/transcribe`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/result`

Future endpoint:

- `WS /v1/live/transcribe`

## Data model

Minimum entities:

- User
- ApiToken
- Job
- AuditLog
- UsageQuota

## Coding style

- Keep code simple and explicit.
- Prefer small modules.
- Use typed request/response models.
- Add tests for every endpoint.
- Add clear README instructions.
- Add `.env.example`.
- Never commit secrets.
- Use Docker healthchecks where useful.
- Use structured logs.

## Expected workflow

Do not implement everything at once.

Work in small pull requests:

1. Repo bootstrap.
2. AI Gateway skeleton.
3. API token authentication.
4. Ollama integration.
5. Docker Compose infrastructure.
6. Obsidian plugin MVP.
7. Whisper worker.
8. Meeting report generation.
9. Quotas and rate limiting.
10. Security hardening.
11. Documentation.

## Testing expectations

For backend:

- Use pytest.
- Test unauthenticated requests fail.
- Test invalid tokens fail.
- Test valid tokens work.
- Test unauthorized model names fail.
- Test request size validation.
- Test job creation and status flow.

For plugin:

- Use TypeScript strict mode.
- Build must pass.
- Do not hardcode secrets.
- Settings must store API URL and API token.