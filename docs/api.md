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

## Vault RAG

Vault RAG is explicit and separate from `POST /v1/assistant/chat`. The gateway never reads CouchDB or LiveSync directly. The future Obsidian plugin integration will send decrypted note content to these endpoints from the local vault.

### `POST /v1/vault/index-note`

Required scope: `vault:index`

```json
{
  "vault_id": "default",
  "workspace_id": "default",
  "path": "Projects/Note Compagnon.md",
  "title": "Note Compagnon",
  "content": "...markdown...",
  "modified_at": "2026-05-25T12:00:00Z",
  "tags": ["project", "ai"],
  "frontmatter": {},
  "metadata": {}
}
```

Returns `indexed` with `chunks_indexed`, or `skipped` when the content hash is unchanged or the note is excluded.

```bash
curl -X POST "$API_BASE_URL/v1/vault/index-note" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"vault_id":"default","path":"Projects/RAG.md","title":"RAG","content":"# RAG\n\nDecision CouchDB...","tags":["infra"]}'
```

### `POST /v1/vault/search`

Required scope: `vault:search`

```json
{
  "vault_id": "default",
  "workspace_id": "default",
  "query": "ce que j'ai decide pour CouchDB",
  "top_k": 8,
  "path_prefix": "Projects/",
  "tags": ["infra"]
}
```

Uses PostgreSQL + pgvector in production, then applies a small keyword bonus for exact terms in `path`, `title`, `heading_path`, `content`, and tags. The base vector score is derived from cosine distance (`score = 1 - distance`) using pgvector's `<=>` operator. Returns bounded snippets only, never full notes.

Result diagnostics are safe to display in the plugin:

- `score`: final score after vector score and keyword bonus
- `vector_score`: base semantic score
- `keyword_bonus`: exact-match bonus
- `matched_terms`: exact terms found in path, title, heading, tags, or snippet content

```bash
curl -X POST "$API_BASE_URL/v1/vault/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"vault_id":"default","query":"decision CouchDB","top_k":8}'
```

### `POST /v1/vault/ask`

Required scope: `vault:ask`

```json
{
  "vault_id": "default",
  "workspace_id": "default",
  "question": "Quelle solution avais-je retenue pour CouchDB ?",
  "model": "mistral:latest",
  "top_k": 8,
  "path_prefix": null,
  "tags": [],
  "answer_language": "same_as_input",
  "debug": false
}
```

The gateway searches indexed chunks through the same pgvector search layer as `/v1/vault/search`, builds a bounded context, calls the allowlisted LLM, and returns:

```json
{
  "model": "mistral:latest",
  "answer_markdown": "## Reponse\n\n...\n\n## Sources utilisees\n\n- [[Projects/Note Compagnon.md]]",
  "sources": [
    {
      "path": "Projects/Note Compagnon.md",
      "title": "Note Compagnon",
      "heading_path": "Architecture > RAG",
      "chunk_index": 2,
      "score": 0.82,
      "vector_score": 0.72,
      "keyword_bonus": 0.10,
      "matched_terms": ["couchdb", "livesync"]
    }
  ]
}
```

If no source is relevant enough, the answer says that the available notes are insufficient. The endpoint must not claim to have read the whole vault.

Set `debug=true` to return safe search diagnostics without full note content:

```json
{
  "debug_info": {
    "search_candidates_count": 8,
    "selected_sources_count": 3,
    "min_score": 0.15,
    "top_scores": [0.82, 0.61],
    "top_vector_scores": [0.72, 0.58],
    "top_keyword_bonuses": [0.10, 0.03],
    "matched_terms_by_path": {
      "Projects/Note Compagnon.md": ["couchdb", "livesync"]
    },
    "selected_paths": ["Projects/Note Compagnon.md"]
  }
}
```

```bash
curl -X POST "$API_BASE_URL/v1/vault/ask" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"vault_id":"default","question":"Quelle solution avais-je retenue pour CouchDB ?","model":"mistral:latest","answer_language":"same_as_input"}'
```

### `GET /v1/vault/stats`

Required scope: `vault:search` or `vault:admin`

### `DELETE /v1/vault/index`

Required scope: `vault:admin`

Deletes the selected workspace index for the selected `vault_id`.

Optional query params:

- `workspace_id=default` selects a stable RAG workspace shared by regenerated tokens
- `all_users=true` deletes the full RAG index for the `vault_id`, across every `workspace_id` and historical `user_id`

`all_users=true` is an administrative cleanup action for duplicate token/user spaces. It deletes only RAG index rows, never Obsidian notes or other backend tables.

```bash
curl -X DELETE "$API_BASE_URL/v1/vault/index?vault_id=default&all_users=true" \
  -H "Authorization: Bearer $TOKEN"
```

### `DELETE /v1/vault/document`

Required scope: `vault:index`

Deletes one indexed document for the selected workspace and vault. This is intended for Obsidian rename/delete cleanup and never deletes a local Obsidian note.
If the document does not exist, the endpoint returns `200 OK` with `document_deleted=false`.

```bash
curl -X DELETE "$API_BASE_URL/v1/vault/document?vault_id=default&path=Projects%2FRAG.md" \
  -H "Authorization: Bearer $TOKEN"
```

Response fields include `path`, `workspace_id`, `document_deleted`, `chunks_deleted`, `deleted_documents`, and `deleted_chunks`.

RAG error behavior:

- `401 Unauthorized` when the bearer token is missing or invalid
- `403 Forbidden` when the required vault scope is missing or the LLM model is outside `ALLOWED_MODELS`
- `503 Service Unavailable` when `RAG_ENABLED=false` or embeddings are unavailable
- `502 Bad Gateway` when the embedding or LLM backend returns an invalid response

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
  "output_language": "same_as_meeting",
  "generation_mode": "standard"
}
```

Behavior:

- requires a non-empty `title`
- requires a non-empty `template`
- requires at least one of `transcript` or `manual_notes`
- uses `DEFAULT_MODEL` when `model` is omitted
- uses `same_as_meeting` when `output_language` is omitted
- accepts `output_language` values `same_as_meeting`, `fr`, and `en`
- accepts `generation_mode` values `standard` and `deep_think`; omitted defaults to `standard`
- `deep_think` generates the report section by section and is slower but better suited to long or information-rich meetings
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
  "generation_mode": "standard",
  "generation_stages": null,
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
  "output_language": "same_as_meeting",
  "generation_mode": "deep_think"
}
```

Behavior:

- loads the transcript from a completed `audio_transcription` job owned by the current token user
- refuses jobs from other users
- refuses queued, processing, or failed jobs
- reuses the same meeting-generation rules as `POST /v1/meetings/generate`
- accepts `output_language` values `same_as_meeting`, `fr`, and `en`
- accepts `generation_mode=deep_think` for slower section-by-section detailed reports
- never exposes internal storage paths in the response

Example response:

```json
{
  "job_id": "1f79508f-8e0d-4c68-b6f8-8f7b891bcb2f",
  "model": "qwen2.5:14b",
  "title": "Reunion projet",
  "meeting_markdown": "## Resume executif\n\n...",
  "generation_mode": "deep_think",
  "generation_stages": 6,
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
    "diarization_enabled": true,
    "diarization_status": "completed",
    "segments": [
      {
        "start": 0,
        "end": 1,
        "text": "Fake transcript for testing.",
        "speaker": "Speaker 1"
      }
    ]
  }
}
```

`segments[].speaker`, `diarization_enabled`, and `diarization_status` are optional/non-breaking diarization fields. When diarization is disabled or fails, clients should keep using timestamps and text normally. Speaker labels are anonymous (`Speaker 1`, `Speaker 2`) and must not be treated as real participant names.

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
