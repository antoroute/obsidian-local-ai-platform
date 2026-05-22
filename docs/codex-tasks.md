# Codex Tasks

## Recommended implementation order

1. Repo bootstrap
2. AI Gateway skeleton
3. API token authentication
4. Ollama integration
5. Docker Compose infrastructure hardening
6. Obsidian plugin MVP
7. Whisper worker queue integration
8. Meeting report generation
9. Quotas and rate limiting
10. Security hardening
11. Documentation expansion

## Immediate next tasks after bootstrap

1. Add gateway settings validation and structured logging
2. Add authentication middleware or dependency for all protected routes
3. Add pytest coverage for auth failures and allowed route exceptions
4. Define database models for `User`, `ApiToken`, `Job`, `AuditLog`, and `UsageQuota`
5. Add Redis-backed job creation flow for transcription

## Notes for future contributors

- Work in small, reviewable pull requests
- Keep internal services private by default
- Prefer explicit configuration over hidden magic
- Add tests with each new endpoint or security-sensitive feature
