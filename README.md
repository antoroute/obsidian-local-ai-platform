# obsidian-local-ai-platform

Private AI platform for Obsidian, designed for secure self-hosted note summarization, meeting generation, and audio transcription.

The current state includes:

- `apps/ai-gateway`: authenticated FastAPI API for notes, meetings, audio jobs, assistant actions, and vault RAG
- `apps/obsidian-plugin`: Obsidian plugin for AI actions, recording, meeting reports, and RAG synchronization
- `apps/whisper-worker`: local faster-whisper transcription worker
- `apps/diarization-service`: optional GPU speaker diarization and Ollama GPU coordinator
- `infra/docker-compose.homelab.yml`: deployment target for the Kavalek homelab
- `docs/`: architecture, security, API, and deployment documentation

## Monorepo structure

```text
.
|-- apps/
|   |-- ai-gateway/
|   |-- diarization-service/
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

- an operational dashboard with real dependency health and a persistent job center
- recording/upload, progress, cancellation, recovery, transcripts, and meeting reports
- assistant actions, explicit vault RAG, and versioned meeting templates
- optional anonymous speaker labels through the GPU diarization service

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

- speaker labels are anonymous and approximate; they are not participant identification
- GPU diarization is opt-in and serialized with Ollama on 8 GB GPUs
- public TLS, proxy rate limiting, backups, monitoring, and disaster recovery remain deployment responsibilities
