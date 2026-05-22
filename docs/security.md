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

- `docker-compose.yml` does not publish Redis, PostgreSQL, or Ollama ports
- `ai-gateway` is the only service with a host port binding
- The gateway binding is restricted to `127.0.0.1` during bootstrap
- Placeholder credentials in `.env.example` are intentionally non-secret defaults
- Every API route remains protected except `GET /v1/health`
- Bearer tokens follow the `obsai_live_<random_secret>` format
- Only token hashes are stored in the database
- Revoked or expired tokens are rejected
- Scope checks are enforced per endpoint, starting with `models:list` on `GET /v1/models`
- Ollama is reachable only through explicit gateway actions such as note summarization
- The gateway does not expose generic Ollama passthrough routes or administrative Ollama endpoints

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
- Model usage is constrained by an `ALLOWED_MODELS` allowlist
- The gateway applies note and template size limits before calling Ollama
- The gateway must not expose Ollama endpoints for `pull`, `delete`, `create`, or `show`

## Current summarization safeguards

- `POST /v1/notes/summarize` requires the `notes:summarize` scope
- Requested models outside the allowlist are rejected
- Empty notes are rejected before any upstream call
- Oversized notes and templates are rejected before any upstream call
- The gateway should never log full note contents, raw bearer tokens, or `Authorization` headers

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
- Keep GPU inference containers on an internal Docker network
- Do not log `Authorization` headers or full bearer tokens
- Rotate and revoke development tokens regularly
