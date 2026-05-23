# Security

## Non-negotiable rules

- Never expose Ollama directly to the Internet
- Never expose Redis, PostgreSQL, or worker ports publicly
- Only `ai-gateway` may be reachable from a reverse proxy
- Do not commit secrets, tokens, or credentials
- Do not store API tokens in plaintext
- Do not log raw tokens, `Authorization` headers, or full audio content

## Bootstrap status

The current gateway bootstrap now includes baseline token authentication:

- `docker-compose.yml` does not publish Redis, PostgreSQL, Ollama, or worker ports
- only the reverse proxy publishes host ports, on `80` and `443`
- `ai-gateway` is no longer published directly on a host port in the default stack
- Placeholder credentials in `.env.example` are intentionally non-secret defaults
- Every API route remains protected except `GET /v1/health`
- Bearer tokens follow the `obsai_live_<random_secret>` format
- Only token hashes are stored in the database
- Revoked or expired tokens are rejected
- Scope checks are enforced per endpoint, starting with `models:list` on `GET /v1/models`
- Ollama is reachable only through explicit gateway actions such as note summarization
- The gateway does not expose generic Ollama passthrough routes or administrative Ollama endpoints
- `ai_internal` is configured as an internal-only Docker network
- only `ai-gateway` is attached to both the ingress and internal networks

## Current token model

Each API token stores only:

- token hash
- token name
- scopes
- creation date
- optional expiration date
- revoked status

The raw token is shown only once by the CLI at creation time and must not be logged or persisted elsewhere.

## Ollama control rules

- Ollama must never be exposed publicly
- Clients must never call Ollama directly
- `ai-gateway` is the only allowed interface to Ollama
- port `11434` must never be published on the Docker host
- Model usage is constrained by an `ALLOWED_MODELS` allowlist
- The gateway applies note and template size limits before calling Ollama
- The gateway must not expose Ollama endpoints for `pull`, `delete`, `create`, or `show`
- `LLM_PROVIDER=fake` is a development-only runtime mode and must not be used for production AI behavior

## Current summarization safeguards

- `POST /v1/notes/summarize` requires the `notes:summarize` scope
- Requested models outside the allowlist are rejected
- Empty notes are rejected before any upstream call
- Oversized notes and templates are rejected before any upstream call
- The gateway should never log full note contents, raw bearer tokens, or `Authorization` headers

## Meeting generation safeguards

- `POST /v1/meetings/generate` requires the `meetings:generate` scope
- `POST /v1/meetings/generate-from-job` also requires the `meetings:generate` scope
- requested models outside the allowlist are rejected before any Ollama call
- transcript, manual notes, template size, and participant count are bounded before upstream processing
- manual notes and transcript are merged with explicit prompt rules to avoid invention
- manual notes are treated as the priority source for names, acronyms, dates, decisions, and action items
- the prompt explicitly requires uncertainties and contradictions to be surfaced
- the gateway should never log full meeting bodies, full transcripts, or manual notes
- job-backed meeting generation is isolated by `user_id`, so one user cannot generate a meeting report from another user's transcription job
- internal `input_path` and `result_path` values must never be exposed in API responses
- fake LLM mode must not bypass authentication, scopes, or job ownership checks

## Audio upload safeguards

- `POST /v1/audio/transcribe` requires the `audio:transcribe` scope
- only `.wav`, `.mp3`, `.m4a`, `.webm`, and `.ogg` uploads are accepted
- audio uploads are bounded by `MAX_AUDIO_UPLOAD_MB`
- uploaded files are stored with generated internal filenames
- original filenames must not be used as storage paths
- absolute filesystem paths are never returned in API responses
- jobs are isolated by token owner through `user_id`
- Redis remains internal-only and must not be exposed publicly
- uploaded audio content must never be logged
- transcription files remain local to the server and are not sent to a third-party cloud service by this pipeline
- worker logs should avoid printing full transcript bodies when they are large
- Docker deployments should keep audio artifacts on a shared internal volume such as `audio-storage`, mounted identically in the gateway and worker

## Required future work

- Per-user authorization and quotas
- Job concurrency controls
- Structured audit logging
- Reverse proxy TLS configuration
- Authentication and authorization test coverage

## Operational guidance

- Use strong secrets in real `.env` files
- Keep `.env` out of version control
- Limit server access to administrators
- Publish only the reverse proxy service publicly
- Verify that `5432`, `6379`, and `11434` are not published on the host
- Keep GPU inference containers on an internal Docker network
- Do not log `Authorization` headers or full bearer tokens
- Rotate and revoke development tokens regularly
