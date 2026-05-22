# Security

## Non-negotiable rules

- Never expose Ollama directly to the Internet
- Never expose Redis, PostgreSQL, or worker ports publicly
- Only `ai-gateway` may be reachable from a reverse proxy
- Do not commit secrets, tokens, or credentials
- Do not store API tokens in plaintext
- Do not log raw tokens, `Authorization` headers, or full audio content

## Bootstrap status

The current bootstrap focuses on reducing accidental exposure:

- `docker-compose.yml` does not publish Redis, PostgreSQL, or Ollama ports
- `ai-gateway` is the only service with a host port binding
- The gateway binding is restricted to `127.0.0.1` during bootstrap
- Placeholder credentials in `.env.example` are intentionally non-secret defaults

## Required future work

- API token hashing and verification
- Per-user authorization and quotas
- Request size limits
- Job concurrency controls
- Model allowlists
- Structured audit logging
- Reverse proxy TLS configuration
- Authentication and authorization test coverage

## Operational guidance

- Use strong secrets in real `.env` files
- Keep `.env` out of version control
- Limit server access to administrators
- Publish only the reverse proxy service publicly
- Keep GPU inference containers on an internal Docker network
