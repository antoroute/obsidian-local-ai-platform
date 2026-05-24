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

### `POST /v1/assistant/chat`

Protected endpoint for lightweight Obsidian assistant actions: chat, correction, rewriting, and summarization.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `assistant:chat`

Request payload:

```json
{
  "message": "question ou instruction utilisateur",
  "context": "contexte optionnel, par exemple note courante ou selection",
  "mode": "chat",
  "output_language": "same_as_input",
  "response_style": "direct",
  "model": "mistral:latest"
}
```

Accepted values:

- `mode`: `chat`, `correct`, `rewrite`, `summarize`
- `output_language`: `same_as_input`, `fr`, `en`
- `response_style`: optional `direct` or `detailed`; when omitted, editing modes default to `direct` and chat defaults to `detailed`

Behavior:

- uses `DEFAULT_MODEL` when `model` is omitted
- refuses models not present in `ALLOWED_MODELS`
- requires `message` for `chat`
- requires `context` for `correct`, `rewrite`, and `summarize`
- enforces `MAX_ASSISTANT_MESSAGE_CHARS` and `MAX_ASSISTANT_CONTEXT_CHARS`
- returns Markdown only; it is not a generic Ollama proxy

Example response:

```json
{
  "model": "mistral:latest",
  "mode": "rewrite",
  "answer_markdown": "Texte reecrit...",
  "usage": {
    "message_chars": 0,
    "context_chars": 456
  }
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `403 Forbidden` when the token lacks `assistant:chat`
- `403 Forbidden` when the requested model is outside the allowlist
- `413 Content Too Large` when message or context exceeds configured limits
- `422 Unprocessable Entity` when the request is malformed
- `503 Service Unavailable` when Ollama cannot be reached
- `502 Bad Gateway` when Ollama returns an invalid upstream response

## Planned endpoints

## Future realtime endpoint

- `WS /v1/live/transcribe`

## Meeting reports

### `POST /v1/meetings/generate`

Protected endpoint that generates a structured Markdown meeting report by combining transcript text, manual notes, a template, and optional participants.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `meetings:generate`

Request payload:

```json
{
  "title": "Reunion projet",
  "transcript": "Texte transcrit ou transcript brut",
  "manual_notes": "Notes prises manuellement",
  "participants": ["Antonin", "Alice"],
  "template": "Template Markdown ou consignes",
  "model": "qwen2.5:14b",
  "output_language": "same_as_meeting"
}
```

Behavior:

- requires a non-empty `title`
- requires a non-empty `template`
- requires at least one of `transcript` or `manual_notes`
- uses `DEFAULT_MODEL` when `model` is omitted
- uses `same_as_meeting` when `output_language` is omitted
- accepts `output_language` values `same_as_meeting`, `fr`, and `en`
- refuses models not present in `ALLOWED_MODELS`
- enforces `MAX_TRANSCRIPT_CHARS`, `MAX_MANUAL_NOTES_CHARS`, `MAX_TEMPLATE_CHARS`, and `MAX_PARTICIPANTS`
- uses manual notes as the priority source for names, acronyms, dates, decisions, and actions
- uses the transcript as the primary source for chronology and discussion flow
- instructs the model not to invent, to flag uncertainties, and to identify contradictions between notes and transcript

Example response:

```json
{
  "model": "qwen2.5:14b",
  "title": "Reunion projet",
  "meeting_markdown": "## Resume executif\n\n...",
  "usage": {
    "transcript_chars": 123,
    "manual_notes_chars": 456,
    "template_chars": 789,
    "participants_count": 2
  }
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `403 Forbidden` when the token lacks `meetings:generate`
- `403 Forbidden` when the requested model is outside the allowlist
- `413 Content Too Large` when transcript, manual notes, template, or participants exceed configured limits
- `422 Unprocessable Entity` when `title` or `template` is empty, or when neither `transcript` nor `manual_notes` is provided
- `503 Service Unavailable` when Ollama cannot be reached
- `502 Bad Gateway` when Ollama returns an invalid upstream response

### `POST /v1/meetings/generate-from-job`

Protected endpoint that generates a structured meeting report directly from a completed audio transcription job, without requiring the client to fetch the transcript first.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `meetings:generate`

Request payload:

```json
{
  "job_id": "uuid ou identifiant job",
  "title": "Reunion projet",
  "manual_notes": "Notes prises manuellement",
  "participants": ["Antonin", "Alice"],
  "template": "Template Markdown ou consignes",
  "model": "qwen2.5:14b",
  "output_language": "same_as_meeting"
}
```

Behavior:

- loads the transcript from a completed `audio_transcription` job owned by the current token user
- refuses jobs from other users
- refuses queued, processing, or failed jobs
- reuses the same meeting-generation rules as `POST /v1/meetings/generate`
- accepts `output_language` values `same_as_meeting`, `fr`, and `en`
- never exposes internal storage paths in the response

Example response:

```json
{
  "job_id": "1f79508f-8e0d-4c68-b6f8-8f7b891bcb2f",
  "model": "qwen2.5:14b",
  "title": "Reunion projet",
  "meeting_markdown": "## Resume executif\n\n...",
  "usage": {
    "transcript_chars": 123,
    "manual_notes_chars": 456,
    "template_chars": 789,
    "participants_count": 2
  }
}
```

Error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `403 Forbidden` when the token lacks `meetings:generate`
- `404 Not Found` when the job does not exist or is not owned by the current token user
- `409 Conflict` when the job is not completed, failed, or incompatible with meeting generation
- `500 Internal Server Error` when the stored transcript result is invalid
- `403 Forbidden` when the requested model is outside the allowlist
- `503 Service Unavailable` when Ollama cannot be reached
- `502 Bad Gateway` when Ollama returns an invalid upstream response

## Audio jobs

### `POST /v1/audio/transcribe`

Protected endpoint that accepts a multipart audio upload and creates an asynchronous transcription job.

Required authentication:

- `Authorization: Bearer <token>`

Required scope:

- `audio:transcribe`

Accepted upload field:

- multipart field named `file`
- optional multipart field `transcription_language`, one of `auto`, `fr`, or `en`; default is `auto`

Accepted extensions:

- `.wav`
- `.mp3`
- `.m4a`
- `.webm`
- `.ogg`

Behavior:

- stores the uploaded file under `AUDIO_STORAGE_DIR`
- enforces `MAX_AUDIO_UPLOAD_MB`
- stores the requested transcription language in job metadata for the worker
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
- CORS preflight support for Obsidian and Electron clients, including multipart audio upload requests
- Explicit validation and bounded payload sizes
- Stable, predictable JSON error responses
- API tokens use the `obsai_live_<random_secret>` format
- Only token hashes are stored server-side
- Ollama is accessed only through controlled gateway endpoints, never through a generic proxy

## CORS for Obsidian and Electron

Obsidian desktop can trigger browser-style CORS preflight requests, especially for authenticated `POST` calls and multipart uploads such as `POST /v1/audio/transcribe`.

The gateway therefore supports `OPTIONS` preflight requests for the implemented API routes, including:

- `/v1/models`
- `/v1/assistant/chat`
- `/v1/notes/summarize`
- `/v1/audio/transcribe`
- `/v1/jobs/{job_id}`
- `/v1/jobs/{job_id}/result`
- `/v1/meetings/generate`
- `/v1/meetings/generate-from-job`

CORS behavior is configuration-driven:

- `CORS_ENABLED`
- `CORS_ALLOW_ORIGINS`
- `CORS_ALLOW_METHODS`
- `CORS_ALLOW_HEADERS`
- `CORS_ALLOW_CREDENTIALS`

Important:

- CORS support does not make protected endpoints public
- Bearer token authentication remains required on every endpoint except `/v1/health`
- use `CORS_ALLOW_ORIGINS=*` only for development when `CORS_ALLOW_CREDENTIALS=false`

## Typical audio-to-meeting workflow

1. `POST /v1/audio/transcribe`
2. poll `GET /v1/jobs/{job_id}` until `status=completed`
3. `POST /v1/meetings/generate-from-job` with the finished `job_id`, template, and optional manual notes
