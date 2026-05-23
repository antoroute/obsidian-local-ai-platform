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

### `POST /v1/notes/summarize`

Protected endpoint that summarizes a Markdown note through the gateway-managed Ollama integration.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `notes:summarize`

Request payload:

```json
{
  "title": "Titre de la note",
  "note_content": "Contenu markdown de la note",
  "template": "Template markdown ou consignes",
  "model": "qwen2.5:14b"
}
```

Behavior:

- uses `DEFAULT_MODEL` when `model` is omitted
- refuses models not present in `ALLOWED_MODELS`
- requires non-empty `note_content`
- enforces `MAX_NOTE_CHARS` and `MAX_TEMPLATE_CHARS`
- returns a structured Markdown summary only, not a generic Ollama proxy response

Example response:

```json
{
  "model": "qwen2.5:14b",
  "title": "Titre de la note",
  "summary_markdown": "## Summary\n\n...",
  "usage": {
    "prompt_chars": 123,
    "template_chars": 456
  }
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing, malformed, invalid, revoked, or expired
- `403 Forbidden` when the token is valid but lacks the `notes:summarize` scope
- `403 Forbidden` when the requested model is outside the allowlist
- `413 Request Entity Too Large` when `note_content` or `template` exceeds configured limits
- `422 Unprocessable Entity` when the payload is malformed or `note_content` is empty
- `503 Service Unavailable` when Ollama cannot be reached
- `502 Bad Gateway` when Ollama returns an invalid upstream response

## Planned endpoints

- `POST /v1/meetings/generate`

## Future realtime endpoint

- `WS /v1/live/transcribe`

## Audio jobs

### `POST /v1/audio/transcribe`

Protected endpoint that accepts a multipart audio upload and creates an asynchronous transcription job.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `audio:transcribe`

Accepted upload field:

- multipart field named `file`

Accepted extensions:

- `.wav`
- `.mp3`
- `.m4a`
- `.webm`
- `.ogg`

Behavior:

- stores the uploaded file under `AUDIO_STORAGE_DIR`
- enforces `MAX_AUDIO_UPLOAD_MB`
- creates a queued job in the database
- pushes the job id into Redis queue `audio_transcription_jobs`

Example response:

```json
{
  "job_id": "1f79508f-8e0d-4c68-b6f8-8f7b891bcb2f",
  "status": "queued"
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `403 Forbidden` when the token lacks `audio:transcribe`
- `413 Content Too Large` when the upload exceeds the configured size limit
- `422 Unprocessable Entity` when the extension is not allowed or the multipart payload is invalid

### `GET /v1/jobs/{job_id}`

Protected endpoint returning the status of a job owned by the current token user.

Example response:

```json
{
  "job_id": "1f79508f-8e0d-4c68-b6f8-8f7b891bcb2f",
  "status": "queued",
  "created_at": "2026-05-23T12:00:00+00:00",
  "updated_at": "2026-05-23T12:00:00+00:00",
  "error": null
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `404 Not Found` when the job does not exist or is not owned by the current token user

### `GET /v1/jobs/{job_id}/result`

Protected endpoint returning the transcript result of a completed job owned by the current token user.

If the job is not completed yet, the endpoint returns `409 Conflict`.

Example response:

```json
{
  "job_id": "1f79508f-8e0d-4c68-b6f8-8f7b891bcb2f",
  "transcript": {
    "text": "Fake transcript for testing.",
    "language": "fr",
    "duration": 0,
    "segments": [
      {
        "start": 0,
        "end": 1,
        "text": "Fake transcript for testing."
      }
    ]
  }
}
```

## API principles

- Typed request and response models
- Authentication on every endpoint except `/v1/health`
- Explicit validation and bounded payload sizes
- Stable, predictable JSON error responses
- API tokens use the `obsai_live_<random_secret>` format
- Only token hashes are stored server-side
- Ollama is accessed only through controlled gateway endpoints, never through a generic proxy
