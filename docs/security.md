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
Production scripts may invoke the token CLI, but they must not write the raw token to a file.

## Ollama control rules

- Ollama must never be exposed publicly
- Clients must never call Ollama directly
- `ai-gateway` is the only allowed interface to Ollama
- port `11434` must never be published on the Docker host
- the production runtime Ollama container can remain without outbound Internet access
- model preparation is explicit and should copy or pull only requested models such as `mistral:latest` and `nomic-embed-text:latest`
- do not copy a full host Ollama model store into Docker unless you intentionally want every model there
- model cache volumes are not expected to contain secrets, but they can be large and should be treated as runtime artifacts
- Model usage is constrained by an `ALLOWED_MODELS` allowlist
- The gateway applies note and template size limits before calling Ollama
- The gateway must not expose Ollama endpoints for `pull`, `delete`, `create`, or `show`
- `LLM_PROVIDER=fake` is only for development UX checks or CI and must not be used as the primary runtime path

## Current summarization safeguards

- `POST /v1/notes/summarize` requires the `notes:summarize` scope
- Requested models outside the allowlist are rejected
- Empty notes are rejected before any upstream call
- Oversized notes and templates are rejected before any upstream call
- The gateway should never log full note contents, raw bearer tokens, or `Authorization` headers

## Assistant safeguards

- `POST /v1/assistant/chat` requires the `assistant:chat` scope
- requested models outside the allowlist are rejected before any Ollama call
- `message` and `context` are bounded by `MAX_ASSISTANT_MESSAGE_CHARS` and `MAX_ASSISTANT_CONTEXT_CHARS`
- correction and rewriting prompts instruct the model to preserve meaning and avoid unsupported additions
- assistant requests must not log full selected text, note context, raw bearer tokens, or `Authorization` headers
- the assistant endpoint is a controlled task endpoint, not a generic Ollama proxy

## Vault RAG safeguards

- Vault RAG is explicit: `/v1/assistant/chat` must never search the vault implicitly
- `POST /v1/vault/index-note` requires `vault:index`
- `POST /v1/vault/search` requires `vault:search`
- `POST /v1/vault/ask` requires `vault:ask`
- `DELETE /v1/vault/document` requires `vault:index` and deletes only the indexed backend copy for the authenticated user
- `DELETE /v1/vault/index` requires `vault:admin`
- `GET /v1/vault/stats` requires `vault:search` or `vault:admin`
- the backend must not read CouchDB or LiveSync directly; LiveSync E2EE means decrypted note content is available only inside Obsidian
- indexing is expected to come from the Obsidian plugin, which can see decrypted notes locally
- automatic indexing, if enabled, still runs only from the Obsidian plugin and only while Obsidian is open
- automatic delete/rename cleanup removes only RAG index rows for the selected workspace/vault/path, never local files
- RAG data is isolated by `workspace_id` and `vault_id`; when no workspace is provided, the backend falls back to the token `user_id`
- for personal deployments, use a stable `RAG_WORKSPACE_ID` / plugin Workspace RAG value so regenerated tokens share the same vault index
- multiplying tokens without a shared workspace can create separate historical RAG spaces
- excluded directories and tags should be configured with `RAG_INDEX_EXCLUDED_DIRS` and `RAG_INDEX_EXCLUDED_TAGS`
- private notes should be tagged with an excluded tag such as `noai` or `private`
- search responses return bounded snippets, not full notes
- `debug=true` on `/v1/vault/ask` returns only safe metadata such as counts, scores, and paths, never complete note content
- `/v1/vault/ask` must answer only from retrieved sources and must return the sources used
- note contents, embeddings input text, and full retrieved context must not be logged
- `RAG_ENABLED=false` disables RAG endpoints cleanly
- `vault:admin` tokens can delete the user's vault index and should be issued sparingly
- `DELETE /v1/vault/index?all_users=true` requires `vault:admin` and purges the selected vault index across every workspace/user; it must be used only for cleanup and never deletes Obsidian notes
- PostgreSQL + pgvector is the production RAG backend; this does not change the token, user isolation, or logging rules
- indexed note chunks and embeddings are stored in PostgreSQL, so do not index notes that should remain outside the AI index
- after deleting an index with `vault:admin`, the plugin must reindex notes before vault questions can use them again

## Meeting generation safeguards

- `POST /v1/meetings/generate` requires the `meetings:generate` scope
- `POST /v1/meetings/generate-from-job` also requires the `meetings:generate` scope
- requested models outside the allowlist are rejected before any Ollama call
- transcript, manual notes, template size, and participant count are bounded before upstream processing
- manual notes and transcript are merged with explicit prompt rules to avoid invention
- manual notes are treated as the priority source for names, acronyms, dates, decisions, and action items
- the prompt explicitly requires uncertainties and contradictions to be surfaced
- the optional `output_language` field only changes the generation instruction (`same_as_meeting`, `fr`, or `en`)
- language selection must not weaken confidentiality rules: full transcripts and manual notes still must not be logged
- the gateway should never log full meeting bodies, full transcripts, or manual notes
- job-backed meeting generation is isolated by `user_id`, so one user cannot generate a meeting report from another user's transcription job
- internal `input_path` and `result_path` values must never be exposed in API responses
- fake LLM mode must not bypass authentication, scopes, or job ownership checks

## Audio upload safeguards

- `POST /v1/audio/transcribe` requires the `audio:transcribe` scope
- only `.wav`, `.mp3`, `.m4a`, `.webm`, and `.ogg` uploads are accepted
- audio uploads are bounded by `MAX_AUDIO_UPLOAD_MB`
- `transcription_language` is strictly limited to `auto`, `fr`, or `en`
- the requested transcription language is stored as job metadata and consumed by the worker
- uploaded files are stored with generated internal filenames
- original filenames must not be used as storage paths
- absolute filesystem paths are never returned in API responses
- jobs are isolated by token owner through `user_id`
- Redis remains internal-only and must not be exposed publicly
- uploaded audio content must never be logged
- transcription files remain local to the server and are not sent to a third-party cloud service by this pipeline
- worker logs should avoid printing full transcript bodies when they are large
- Docker deployments should keep audio artifacts on a shared internal volume such as `audio-storage`, mounted identically in the gateway and worker
- production transcription is STT-only; speaker diarization dependencies were removed to avoid gated models, heavy GPU memory use, and long-running speaker-labeling failures

## Usage and concurrency safeguards

- Redis enforces separate daily per-user quotas for LLM, embedding, and audio requests
- quota keys contain a truncated hash of the user ID rather than the raw identifier
- exhausted quotas return `429` with a `Retry-After` header and reset at midnight UTC
- `MAX_ACTIVE_AUDIO_JOBS_PER_USER` bounds queued plus processing audio jobs before an upload is saved
- `OLLAMA_MAX_CONCURRENT_REQUESTS` bounds all gateway LLM and embedding calls; the homelab value is `1`
- the homelab defaults are 100 LLM requests, 5,000 embedding requests, and 20 audio jobs per user and UTC day
- Redis failures make quota-protected routes fail closed with `503`

## Required future work

- Structured audit logging

## Operational guidance

- Use strong secrets in real `.env` files
- Keep `.env` out of version control
- Limit server access to administrators
- In the production-like Docker mode, publish only `ai-gateway` on `127.0.0.1:8000` for the external reverse proxy
- Verify that `5432`, `6379`, and `11434` are not published on the host
- Keep GPU inference containers on an internal Docker network
- Do not log `Authorization` headers or full bearer tokens
- Rotate and revoke development tokens regularly
