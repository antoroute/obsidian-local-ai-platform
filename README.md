# obsidian-local-ai-platform

Private AI platform for Obsidian, designed for secure self-hosted note summarization, meeting generation, and audio transcription.

The current state includes:

- `apps/ai-gateway`: authenticated FastAPI API for notes, meetings, audio jobs, assistant actions, and vault RAG
- `apps/obsidian-plugin`: Obsidian plugin for AI actions, recording, meeting reports, and RAG synchronization
- `apps/whisper-worker`: local faster-whisper transcription worker
- `infra/docker-compose.homelab.yml`: deployment target for the Kavalek homelab
- `docs/`: architecture, security, API, and deployment documentation

## Monorepo structure

```text
.
|-- apps/
|   |-- ai-gateway/
|   |-- obsidian-plugin/
|   `-- whisper-worker/
|-- docs/
|-- .env.example
|-- .gitignore
|-- AGENTS.md
`-- docker-compose.yml
```

## Security posture

- Only `ai-gateway` is intended to sit behind a reverse proxy.
- `ollama`, `redis`, `postgres`, and workers must remain internal-only.
- No secrets or plaintext tokens are committed.
- API routes other than health require hashed Bearer tokens and scoped authorization.
- Model access is restricted by an allowlist and Ollama concurrency is bounded.
- Redis-backed daily per-user quotas and active audio-job limits protect local compute.

## Quick start

1. Copy environment variables:

```powershell
Copy-Item .env.example .env
```

2. Start the bootstrap stack:

```powershell
docker compose up --build
```

3. Check gateway health:

```powershell
curl http://127.0.0.1:8000/v1/health
```

## Component commands

### AI Gateway

```powershell
cd apps/ai-gateway
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e .[dev]
pytest
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Obsidian plugin

```powershell
cd apps/obsidian-plugin
npm install
npm run build
npm run check
```

The plugin MVP now supports:

- Obsidian settings for API URL, API token, default model, templates folder, and output folder
- the command `AI Meeting Assistant: Summarize current note`
- summary note creation in the vault after a successful `POST /v1/notes/summarize` call

Plugin-specific setup details are documented in [apps/obsidian-plugin/README.md](apps/obsidian-plugin/README.md).

The dedicated homelab target and the future Portainer/Nginx Proxy Manager procedure
are documented in [docs/homelab-deployment.md](docs/homelab-deployment.md).

### Whisper worker

```powershell
cd apps/whisper-worker
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e .[dev]
python -m whisper_worker
pytest
```

## Current limitations

- schema changes are additive at application startup rather than managed by a migration framework
- public TLS, proxy rate limiting, backups, and disaster recovery remain deployment responsibilities
- transcription intentionally does not perform speaker diarization
