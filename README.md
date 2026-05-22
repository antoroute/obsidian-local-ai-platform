# obsidian-local-ai-platform

Private AI platform for Obsidian, designed for secure self-hosted note summarization, meeting generation, and audio transcription.

This repository is intentionally bootstrapped in small steps. The current state includes:

- `apps/ai-gateway`: minimal FastAPI service with `GET /v1/health`
- `apps/obsidian-plugin`: minimal compilable Obsidian plugin in TypeScript
- `apps/whisper-worker`: minimal Python worker process
- `docs/`: initial architecture, security, API, deployment, and task planning docs

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
- This bootstrap does not yet implement authentication, quotas, or model access control.

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

- No authentication yet
- No database migrations yet
- No real queue consumption yet
- No Ollama integration yet
- No audio transcription yet

These are expected future steps and should be added incrementally.
