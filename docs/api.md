# API

## Base path

All API endpoints use the `/v1` prefix.

## Implemented now

### `GET /v1/health`

Returns a basic service status payload.

Example response:

```json
{
  "status": "ok"
}
```

### `GET /v1/models`

Protected endpoint returning the currently allowed model list for the caller.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `models:list`

Example response:

```json
{
  "models": ["qwen2.5:14b", "mistral:7b"]
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing, malformed, invalid, revoked, or expired
- `403 Forbidden` when the token is valid but lacks the `models:list` scope

## Planned endpoints

- `POST /v1/notes/summarize`
- `POST /v1/meetings/generate`
- `POST /v1/audio/transcribe`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/result`

## Future realtime endpoint

- `WS /v1/live/transcribe`

## API principles

- Typed request and response models
- Authentication on every endpoint except `/v1/health`
- Explicit validation and bounded payload sizes
- Stable, predictable JSON error responses
- API tokens use the `obsai_live_<random_secret>` format
- Only token hashes are stored server-side
