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

## Planned endpoints

- `GET /v1/models`
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
