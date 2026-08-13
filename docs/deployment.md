# Deployment

## Target environment

- Docker Compose
- GPU server with NVIDIA RTX 3090
- Reverse proxy external to this stack
- Internal-only networking for Ollama, Redis, PostgreSQL, and workers
- NVIDIA Container Toolkit installed on the Docker host

## Deploiement production-like avec reverse proxy externe

This is now the primary recommended path.

Architecture:

- `ai-gateway` in Docker
- `ollama` in Docker
- `whisper-worker` in Docker
- `redis` in Docker
- `postgres` in Docker
- external reverse proxy outside this Compose stack
- only `ai-gateway` published to the host, on `127.0.0.1:8000`

## Configuration .env et overrides Docker Compose

Docker Compose reads `.env` for variable interpolation before merging compose files. A variable is effective only when it is referenced in a compose file and injected through `environment`, `ports`, labels, or another compose field.

Recommended files:

- `.env`: local, ignored by Git, adjusted for the machine currently running the stack
- `.env.example`: complete documented example, production-like by default
- `.env.prod.example`: production-like GPU example for Docker Ollama + faster-whisper CUDA
- `.env.dev.example`: fake/dev example for UX validation only

The production-like GPU path must always include the prod overrides:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml up -d --build
```

The production-like CPU path must include:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.cpu.yml up -d --build
```

The prod overrides intentionally force critical runtime values even if `.env` is stale:

- `LLM_PROVIDER=ollama`
- `OLLAMA_BASE_URL=http://ollama:11434`
- `TRANSCRIPTION_ENGINE=faster_whisper`
- GPU: `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`
- CPU: `WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE_TYPE=int8`

Useful runtime checks:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ai-gateway printenv LLM_PROVIDER
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ai-gateway printenv OLLAMA_BASE_URL
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv TRANSCRIPTION_ENGINE
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv WHISPER_DEVICE
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv WHISPER_COMPUTE_TYPE
```

The easiest complete diagnostic is:

```powershell
.\scripts\prod\check-stack.ps1 -Mode gpu
```

### Volumes modeles

The model caches are deliberately separate from application data:

- Ollama models: Docker volume `ollama-data`, mounted at `/root/.ollama`
- faster-whisper models: Docker volume `whisper-model-cache`, mounted at `/models/whisper`
- PostgreSQL data: Docker volume `postgres-data`
- uploaded audio: Docker volume `audio-storage`

With the default `COMPOSE_PROJECT_NAME=obsidian-local-ai-platform`, the effective Docker volume names are `obsidian-local-ai-platform_ollama-data` and `obsidian-local-ai-platform_whisper-model-cache`.

Never delete `postgres-data`, Redis/runtime data, `audio-storage`, or vault files when refreshing models.

To reset only model caches:

```powershell
.\scripts\prod\reset-model-caches.ps1
```

For unattended advanced use:

```powershell
.\scripts\prod\reset-model-caches.ps1 -Force
```

### 1. Preparer uniquement les modeles necessaires

The runtime Ollama container should not need Internet access. The recommended path is:

1. Pull only the required models on the host Ollama installation.
2. Copy only the requested manifests and referenced blobs into the Docker `ollama-data` volume.
3. Run the production stack with Ollama internal-only.

Required default models:

- LLM: `qwen2.5:7b`
- fallback/general LLM: `mistral:latest`
- RAG embeddings: `nomic-embed-text:latest`
- transcription: faster-whisper `medium`

Prepare the host model store if needed:

```powershell
ollama list
ollama pull qwen2.5:7b
ollama pull mistral:latest
ollama pull nomic-embed-text:latest
```

`ollama list` on the host must show `qwen2.5:7b`, `mistral:latest`, and `nomic-embed-text:latest` before `-Source host` can copy them into Docker.

Copy only those models into Docker Ollama:

```powershell
.\scripts\prod\prepare-ollama-models.ps1 `
  -Mode gpu `
  -Source host `
  -Models "qwen2.5:7b,mistral:latest,nomic-embed-text:latest"
```

This script reads `$env:USERPROFILE\.ollama\models`, copies only the selected model manifests and their referenced `sha256` blobs, restarts Docker Ollama, and verifies `ollama list` inside the container.

If you explicitly want Docker Ollama to pull models itself, use `-Source docker`. This is not the default because it requires outbound Internet from the setup container:

```powershell
.\scripts\prod\prepare-ollama-models.ps1 `
  -Mode gpu `
  -Source docker `
  -Models "qwen2.5:7b,mistral:latest,nomic-embed-text:latest"
```

Prepare faster-whisper:

```powershell
.\scripts\prod\prepare-whisper-model.ps1 -Mode gpu -Model medium
```

Production transcription is STT-only by default: faster-whisper runs on GPU and diarization is disabled. This avoids pyannote/Sortformer runtime failures, CUDA OOM, and long blocking steps during meeting generation.

One-command bootstrap for the normal GPU path:

```powershell
.\scripts\prod\bootstrap-stack.ps1 `
  -Mode gpu `
  -OllamaModels "qwen2.5:7b,mistral:latest,nomic-embed-text:latest" `
  -WhisperModel medium
```

Add `-ResetModelCaches` when you intentionally want a clean model cache rebuild. It deletes only `ollama-data` and `whisper-model-cache`.

Clean bootstrap with model cache reset:

```powershell
.\scripts\prod\bootstrap-stack.ps1 `
  -Mode gpu `
  -ResetModelCaches `
  -Force `
  -OllamaModels "qwen2.5:7b,mistral:latest,nomic-embed-text:latest" `
  -WhisperModel medium
```

The bootstrap stops and removes only `ollama` and `whisper-worker` containers before deleting model cache volumes. It does not delete PostgreSQL, Redis data, `audio-storage`, or vault files.

Manual verification after model preparation:

```powershell
docker compose `
  -f docker-compose.yml `
  -f infra/docker-compose.prod.external-proxy.yml `
  -f infra/docker-compose.prod.gpu.yml `
  exec ollama ollama list

.\scripts\prod\check-stack.ps1 -Mode gpu

docker compose `
  -f docker-compose.yml `
  -f infra/docker-compose.prod.external-proxy.yml `
  -f infra/docker-compose.prod.gpu.yml `
  exec ai-gateway python -m app.cli check-rag
```

### RAG vault setup

The RAG backend is prepared in `ai-gateway`, but indexing must come from the Obsidian plugin because LiveSync/CouchDB content is encrypted at rest and must not be read directly by the backend.

Production-like compose uses `pgvector/pgvector:pg16` for PostgreSQL. On startup, the gateway runs `CREATE EXTENSION IF NOT EXISTS vector` and creates the additive RAG tables if they are missing.
PostgreSQL + pgvector is mandatory for production RAG. SQLite/vector JSON behavior is reserved for unit tests only.

Important variables:

- `RAG_ENABLED=true`
- `RAG_VECTOR_BACKEND=pgvector`
- `RAG_EMBEDDING_PROVIDER=ollama`
- `RAG_EMBEDDING_MODEL=nomic-embed-text:latest`
- `RAG_EMBEDDING_DIMENSION=768`
- `RAG_CHUNK_SIZE=900`
- `RAG_CHUNK_OVERLAP=150`
- `RAG_MAX_CHUNKS_PER_QUERY=8`
- `RAG_MAX_CONTEXT_CHARS=24000`
- `RAG_SEARCH_CANDIDATES=30`
- `RAG_MIN_SCORE=0.15`
- `RAG_KEYWORD_BONUS_ENABLED=true`
- `RAG_KEYWORD_BONUS_MAX=0.20`
- `RAG_INDEX_EXCLUDED_DIRS=.obsidian,Templates,Archives,Private`
- `RAG_INDEX_EXCLUDED_TAGS=noai,private`
- `RAG_DEFAULT_VAULT_ID=default`
- `RAG_WORKSPACE_ID=default`

Prepare the embedding model in Docker Ollama through the selective model script:

```powershell
.\scripts\prod\prepare-ollama-models.ps1 -Mode gpu -Source host -Models "nomic-embed-text:latest"
```

Verify the RAG runtime from `ai-gateway`:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ai-gateway python -m app.cli check-rag
```

`check-rag` verifies:

- `RAG_ENABLED`
- `RAG_VECTOR_BACKEND=pgvector`
- `GET http://ollama:11434/api/tags` from inside `ai-gateway`
- the embedding model exists in Docker Ollama
- `/api/embed` works with `RAG_EMBEDDING_MODEL`
- embedding dimension matches `RAG_EMBEDDING_DIMENSION`
- `/api/chat` works separately with `DEFAULT_MODEL`
- PostgreSQL connectivity
- pgvector extension availability
- `vault_chunks.embedding` type, expected `vector(768)`
- vector index presence
- `nomic-embed-text:latest` embedding call and dimension

Migration note:

- if an earlier development build indexed notes with JSON embeddings, those embeddings are not used by the pgvector backend
- delete/recreate the RAG index or reindex notes after migrating to pgvector
- when the plugin indexing workflow is added, run a full reindex from Obsidian
- automatic indexing, when enabled in the plugin, only runs while Obsidian is open
- with several Obsidian devices or regenerated tokens, keep the same `vault_id` and `workspace_id` if they should feed the same backend index
- `/v1/vault/search` uses pgvector first, then a small keyword bonus for exact terms in path, title, heading, tags and chunk text

If old tokens created several RAG spaces for the same vault, reset and rebuild the index:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ai-gateway `
  python -m app.cli create-token --name "rag-admin-reset" --scopes "vault:admin,vault:search"

# Then call DELETE /v1/vault/index?vault_id=default&all_users=true with that token,
# or use "Reinitialiser index" from the plugin dashboard with a full token.
```

After the purge:

1. verify stats are back to 0 for the vault
2. keep `Workspace RAG = default` in the plugin
3. reindex from Obsidian

Automatic RAG sync from Obsidian:

- `create` and `modify` on Markdown notes queue an index/reindex after debounce
- `delete` calls `DELETE /v1/vault/document` for the removed path
- `rename` / move deletes the old path, then queues the new path
- none of these hooks run in manual indexing mode
- the dashboard shows the current progress and the last RAG operations

Create a full Note Compagnon token for future plugin RAG indexing:

```powershell
.\scripts\prod\create-token-full.ps1 -Mode gpu -Name note-compagnon-full
```

Equivalent explicit Docker Compose command:

```powershell
docker compose `
  -f docker-compose.yml `
  -f infra/docker-compose.prod.external-proxy.yml `
  -f infra/docker-compose.prod.gpu.yml `
  exec ai-gateway `
  python -m app.cli create-token --name "note-compagnon-full" --scopes "models:list,notes:summarize,audio:transcribe,meetings:generate,assistant:chat,vault:index,vault:search,vault:ask,vault:admin"
```

For least privilege, issue separate tokens for indexing (`vault:index`) and asking/searching (`vault:search,vault:ask`) when practical.

Scope meaning:

- `models:list`: list allowed models in the plugin
- `notes:summarize`: summarize notes
- `audio:transcribe`: upload audio and create transcription jobs
- `meetings:generate`: generate meeting minutes
- `assistant:chat`: use the simple assistant endpoint
- `vault:index`: index decrypted notes sent by the plugin
- `vault:search`: search indexed vault chunks and read stats
- `vault:ask`: ask questions against retrieved vault sources
- `vault:admin`: delete a user's vault index

### 2. Preparer Whisper

GPU example:

```powershell
.\scripts\prod\prepare-whisper-model.ps1 -Model medium -Mode gpu
```

The GPU worker image is built from:

- `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`

The Dockerfile also installs `libcublas-12-4` explicitly so `faster-whisper` / CTranslate2 can load `libcublas.so.12`.

Do not use `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu24.04`; that tag is not published by NVIDIA.

CPU example:

```powershell
.\scripts\prod\prepare-whisper-model.ps1 -Model small -Mode cpu
```

### 3. Demarrer la stack

GPU:

```powershell
.\scripts\prod\start-stack.ps1 -Mode gpu
```

CPU:

```powershell
.\scripts\prod\start-stack.ps1 -Mode cpu
```

### 4. Verifier la stack

```powershell
.\scripts\prod\check-stack.ps1 -Mode gpu
```

The production-like overrides intentionally replace any stale local `.env` values such as `OLLAMA_BASE_URL=http://host.docker.internal:11434`, `TRANSCRIPTION_ENGINE=fake`, or CPU Whisper settings.

For GPU production-like validation, always use the full compose command:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml up -d --build
```

For CPU production-like validation:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.cpu.yml up -d --build
```

Verify the effective runtime values:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv TRANSCRIPTION_ENGINE
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv WHISPER_DEVICE
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker printenv WHISPER_COMPUTE_TYPE
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ai-gateway printenv OLLAMA_BASE_URL
```

Expected GPU values:

- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_DEVICE=cuda`
- `WHISPER_COMPUTE_TYPE=float16`
- `DIARIZATION_ENABLED=false`
- `OLLAMA_BASE_URL=http://ollama:11434`
- `LLM_PROVIDER=ollama`

### 5. Configurer Obsidian

- `API Base URL = https://ai.kavalek.fr` in production
- `API Base URL = http://127.0.0.1:8000` for local testing
- `Default model = qwen2.5:7b`

Create a token:

```powershell
.\scripts\prod\create-token-full.ps1 -Mode gpu -Name note-compagnon-full
```

### 6. Reverse proxy externe

Typical upstream target:

- `http://127.0.0.1:8000`

Recommended proxy settings:

- `client_max_body_size 1000m`
- `proxy_read_timeout 600s`
- `proxy_send_timeout 600s`

Never proxy:

- Ollama
- Redis
- PostgreSQL
- `whisper-worker`

### 7. Securite

- Ollama not exposed publicly
- Redis not exposed publicly
- PostgreSQL not exposed publicly
- `whisper-worker` not exposed publicly
- `ai-gateway` is the only entrypoint
- Bearer token authentication remains mandatory
- logs must never contain full tokens

## Compose topology

The sections below are secondary workflows for local development and troubleshooting.
They are not the primary recommended deployment path anymore.

The hardened `docker-compose.yml` now defines:

- `reverse-proxy` using Traefik as the only public entrypoint
- `ai-gateway` connected to both `proxy` and `ai_internal`
- `ollama`, `redis`, `postgres`, and `whisper-worker` connected only to `ai_internal`
- `proxy` network for public ingress
- `ai_internal` network with `internal: true` for east-west traffic only
- named volume `audio-storage` shared by `ai-gateway` and `whisper-worker`

Published host ports:

- `80:80`
- `443:443`

No host ports are published for:

- `ai-gateway`
- `ollama`
- `redis`
- `postgres`
- `whisper-worker`

Shared storage:

- `ai-gateway` mounts `audio-storage` at `/data/audio`
- `whisper-worker` mounts the same `audio-storage` volume at `/data/audio`
- uploaded audio and transcript result JSON files therefore remain readable across both containers

## Required host setup

Install NVIDIA Container Toolkit before starting GPU-backed services.

Reference verification command:

```powershell
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Expected result:

- the container can see the NVIDIA RTX 3090
- `nvidia-smi` reports the installed driver and GPU details

The production GPU worker must fail if CUDA is not available. It does not silently fall back to CPU.

After starting the production-like GPU stack, verify GPU access inside the worker:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker nvidia-smi
```

Verify that cuBLAS is present inside the worker:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec whisper-worker sh -lc "ldconfig -p 2>/dev/null | grep libcublas.so.12 || find /usr/local /usr/lib -name 'libcublas.so.12*' -print -quit"
```

The main diagnostic script runs these checks automatically in GPU mode:

```powershell
.\scripts\prod\check-stack.ps1 -Mode gpu
```

## Start the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

## Optional local dev override

These overrides are for local development only. They are useful for diagnostics or UX validation, but they are not the production-like path documented above.

The default stack keeps `ai-gateway` off host ports. If you explicitly want direct local access for development, use the documented override:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml up --build
```

This adds only:

- `127.0.0.1:8000:8000` for `ai-gateway`
- `OLLAMA_BASE_URL=http://host.docker.internal:11434` for `ai-gateway`
- a dedicated non-internal `host_access` network for `ai-gateway`
- `host.docker.internal:host-gateway` inside `ai-gateway`

If you want to use host Ollama and skip the containerized Ollama dependency entirely, use:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.host-ollama.override.yml up --build
```

This keeps PostgreSQL, Redis, and the worker, but prevents `ai-gateway` from waiting on the Docker `ollama` service.

If you want to validate the full Obsidian workflow without Ollama at all, use:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.fake-llm.override.yml up --build
```

This enables:

- `LLM_PROVIDER=fake`
- `TRANSCRIPTION_ENGINE=fake`
- `DEFAULT_MODEL=fake-local-model`
- `ALLOWED_MODELS=fake-local-model,mistral:latest,qwen2.5:14b`

In this mode:

- `POST /v1/notes/summarize` returns deterministic fake Markdown
- `POST /v1/meetings/generate` returns deterministic fake Markdown
- `POST /v1/meetings/generate-from-job` still loads the completed transcription job, then returns deterministic fake Markdown
- authentication, scopes, and per-user job isolation remain enforced
- this mode validates the end-to-end Obsidian workflow, but it does not perform real LLM generation

## Dev reel local Windows : faster-whisper + Ollama hote

Use this mode when you want to validate the real end-to-end local workflow from Obsidian:

- microphone recording in Obsidian
- real audio transcription with `faster-whisper`
- real meeting generation through host Ollama
- final note creation in `AI Summaries`

This mode uses:

- PostgreSQL in Docker
- Redis in Docker
- `ai-gateway` in Docker
- `whisper-worker` in Docker with `TRANSCRIPTION_ENGINE=faster_whisper`
- Ollama on the Windows host through `http://host.docker.internal:11434`
- no fake LLM provider
- no fake transcription engine

Start everything with:

```powershell
.\scripts\dev\start-real-local-windows.ps1
```

Prepare the faster-whisper model once before starting the real worker if it is not already cached:

```powershell
.\scripts\dev\prepare-whisper-model-windows.ps1
```

Run the diagnostic with:

```powershell
.\scripts\dev\check-real-local-windows.ps1
```

Direct compose command used by this mode:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml up --build
```

Recommended local models:

- LLM: `qwen2.5:7b` for more faithful meeting reports on 8 GB GPUs
- fallback/general LLM: `mistral:latest`
- Whisper on RTX 3070: `medium`
- Whisper on RTX 3090: `large-v3` is possible, `medium` is a good lower-latency default

Important variables in this mode:

- `LLM_PROVIDER=ollama`
- `OLLAMA_BASE_URL=http://host.docker.internal:11434`
- `DEFAULT_MODEL=qwen2.5:7b`
- `ALLOWED_MODELS=qwen2.5:7b,mistral:latest,qwen2.5:14b,llama3:latest`
- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_MODEL_SIZE=medium`
- `WHISPER_DEVICE=cuda`
- `WHISPER_COMPUTE_TYPE=float16`
- `WHISPER_LANGUAGE=auto`
- `WHISPER_MODEL_CACHE_DIR=/models/whisper`

Create a development token:

```powershell
cd apps/ai-gateway
.\.venv\Scripts\python -m app.cli create-token --name "obsidian-real-dev" --scopes "models:list,notes:summarize,audio:transcribe,meetings:generate,assistant:chat,vault:index,vault:search,vault:ask,vault:admin"
```

Configure Obsidian with:

- `API Base URL = http://127.0.0.1:8000`
- `Default model = qwen2.5:7b`

Expected path:

1. `POST /v1/audio/transcribe` returns a job
2. `whisper-worker` transcribes the audio for real
3. `POST /v1/meetings/generate-from-job` returns a real meeting report
4. Obsidian creates the final note in `AI Summaries`

## Preparer le modele faster-whisper

`faster-whisper` needs a local model snapshot before real transcription can start reliably. In the real-local mode, the model is stored in the persistent Docker volume mounted at `/models/whisper`.

Download or refresh the configured model with:

```powershell
.\scripts\dev\prepare-whisper-model-windows.ps1
```

Why this step exists:

- it avoids a fragile implicit model download during worker startup
- it keeps the downloaded model in a Docker volume for reuse across restarts
- it makes worker startup failures much easier to diagnose

Recommendations:

- RTX 3070: `WHISPER_MODEL_SIZE=medium`
- RTX 3090: `WHISPER_MODEL_SIZE=large-v3` is possible, `medium` remains a good default

CPU fallback for diagnostics:

- `WHISPER_DEVICE=cpu`
- `WHISPER_COMPUTE_TYPE=int8`

## Dev Windows avec Ollama hote

For local Windows development, you can keep Ollama installed on the host and point the gateway container to it through `host.docker.internal`.

With the dev override:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml up --build
```

The `ai-gateway` container is explicitly configured with:

- `OLLAMA_BASE_URL=http://host.docker.internal:11434`
- `DEFAULT_MODEL=qwen2.5:7b`
- `ALLOWED_MODELS=qwen2.5:7b,mistral:latest,qwen2.5:14b,llama3:latest`
- a dedicated `host_access` bridge network
- `extra_hosts: host.docker.internal:host-gateway`

Verify the host Ollama service first:

```powershell
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

If Ollama is not already listening beyond loopback on Windows, start it explicitly:

```powershell
$env:OLLAMA_HOST="0.0.0.0:11434"
ollama serve
```

Verify the effective environment inside `ai-gateway`:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml exec ai-gateway printenv OLLAMA_BASE_URL
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml exec ai-gateway python -m app.cli check-ollama --model mistral:latest
```

Verify host name resolution from a container:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml exec ai-gateway python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"
```

Expected result:

- `OLLAMA_BASE_URL` prints `http://host.docker.internal:11434`
- `python -m app.cli check-ollama` reports `Ollama connectivity OK`
- `host.docker.internal` resolves to a reachable host IP

If `host.docker.internal` still does not work in your Docker setup, a fallback host IP can be discovered with:

```powershell
docker run --rm alpine getent hosts host.docker.internal
```

You can then temporarily test connectivity to that IP from `ai-gateway`.

The diagnostic command prints:

- the effective `OLLAMA_BASE_URL`
- the tested model
- `/api/tags` connectivity status
- whether the requested model is present
- `/api/chat` connectivity status
- a clear probable cause and recommended action on failure

## Dev fake LLM pour valider Obsidian

This mode is now only for UX validation and CI-style checks. It is not the primary runtime path and must never be treated as a production configuration.

Use the fake LLM override when you want to validate:

- audio recording from Obsidian
- vault file creation
- audio upload to the gateway
- fake transcription job flow
- final meeting note creation in Obsidian

without depending on Ollama connectivity.

Start the stack:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.fake-llm.override.yml up --build
```

Expected behavior:

- the gateway accepts the normal authenticated API requests
- the public JSON contract stays unchanged
- Markdown payloads are deterministic and clearly marked fake
- this mode must be disabled in production

## Validation commands

Render the final Compose configuration:

```powershell
docker compose config
```

Start the hardened stack:

```powershell
docker compose up --build -d
```

Check the reverse-proxied health endpoint:

```powershell
curl http://localhost/v1/health
```

Check that only 80 and 443 are published:

```powershell
docker compose ps
```

Verify the shared audio volume mounts:

```powershell
docker compose config
```

Expected result:

- both `ai-gateway` and `whisper-worker` mount `audio-storage:/data/audio`

Verify the gateway can reach Ollama over the internal network:

```powershell
docker compose exec ai-gateway python -c "from urllib.request import urlopen; print(urlopen('http://ollama:11434/api/tags').status)"
```

Verify host Ollama from `ai-gateway` in Windows development:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml exec ai-gateway python -m app.cli check-ollama --model mistral:latest
docker compose -f docker-compose.yml -f infra/docker-compose.dev.host-ollama.override.yml exec ai-gateway python -m app.cli check-ollama --model mistral:latest
docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml exec ai-gateway python -m app.cli check-ollama --model mistral:latest
```

Verify Ollama is not reachable from the host directly:

```powershell
curl http://127.0.0.1:11434
```

Expected result:

- connection failure or refusal, because port `11434` is not published

Verify Redis is not reachable from the host:

```powershell
Test-NetConnection 127.0.0.1 -Port 6379
```

Verify PostgreSQL is not reachable from the host:

```powershell
Test-NetConnection 127.0.0.1 -Port 5432
```

Verify GPU visibility inside Ollama:

```powershell
docker compose exec ollama ollama ps
```

Verify GPU visibility inside the worker container:

```powershell
docker compose exec whisper-worker python -c "import os; print(os.getenv('NVIDIA_VISIBLE_DEVICES', 'unset'))"
```

Verify `faster-whisper` import inside the worker container:

```powershell
docker compose exec whisper-worker python -c "import faster_whisper; print('faster-whisper ok')"
```

Verify the worker engine can load the configured model:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml exec whisper-worker python -m whisper_worker check-engine
```

## Healthchecks

Configured healthchecks:

- `reverse-proxy`: Traefik ping healthcheck
- `ai-gateway`: `GET /v1/health` on container-local port `8000`
- `postgres`: `pg_isready`
- `redis`: `redis-cli ping`
- `ollama`: `ollama list`

## Operational notes

- in the default internal Docker deployment, `OLLAMA_BASE_URL` should stay `http://ollama:11434`
- `LLM_PROVIDER=ollama` is the normal production mode
- `LLM_PROVIDER=fake` and `TRANSCRIPTION_ENGINE=fake` are development-only runtime modes for UX validation or CI-style tests; never use them for production-like validation
- for local Windows development with host Ollama, override `OLLAMA_BASE_URL` to `http://host.docker.internal:11434`
- in that Windows dev mode, attach only `ai-gateway` to a non-internal `host_access` bridge network
- `python -m app.cli check-ollama` is the quickest end-to-end diagnostic from inside `ai-gateway`
- `python -m whisper_worker check-engine` is the quickest worker-side diagnostic for real transcription
- `AI_GATEWAY_DATABASE_URL` must point to PostgreSQL on the `ai_internal` network
- `AI_GATEWAY_REDIS_URL` must point to Redis on the `ai_internal` network
- `CORS_ENABLED` controls FastAPI CORS support for Obsidian and Electron clients
- `CORS_ALLOW_ORIGINS` should be restricted to trusted origins in production
- `CORS_ALLOW_METHODS` should include at least `GET`, `POST`, and `OPTIONS`
- `CORS_ALLOW_HEADERS` should include at least `Authorization` and `Content-Type`
- `CORS_ALLOW_CREDENTIALS` should stay `false` unless you intentionally need browser credentials
- `AUDIO_STORAGE_DIR` controls where uploaded audio and result JSON files are stored
- `AUDIO_STORAGE_DIR` should stay `/data/audio` in Docker so the gateway and worker see the same files
- `MAX_AUDIO_UPLOAD_MB` controls the maximum accepted audio file size
- `MEETING_TRANSCRIPT_CLEANUP_ENABLED=true` normalizes transcripts before meeting generation by removing empty lines, exact repeated lines, and tiny filler-only lines
- `MEETING_PREDIGEST_ENABLED=true` enables the controlled hybrid meeting pipeline for long transcripts
- `MEETING_PREDIGEST_MIN_CHARS=12000` keeps short and medium meetings on a single LLM call, and uses one compact pre-digest call only beyond that threshold
- `MEETING_DEEP_THINK_ENABLED=true` allows clients to request the optional slower `generation_mode=deep_think` pipeline
- `MEETING_DEEP_THINK_MAX_SECTIONS=10` bounds section-by-section generation for detailed reports
- `MEETING_DEEP_THINK_EXCERPT_CHARS_PER_SECTION=3000` bounds transcript excerpts sent to each section prompt
- `MEETING_DEEP_THINK_FINAL_CLEANUP=true` enables deterministic cleanup of global code fences and leaked prompt labels
- `MAX_ASSISTANT_MESSAGE_CHARS` controls the maximum assistant chat instruction size
- `MAX_ASSISTANT_CONTEXT_CHARS` controls the maximum selected text or note context size sent to the assistant endpoint
- `TRANSCRIPTION_ENGINE` selects `fake` or `faster_whisper`; production-like stacks must use `faster_whisper`
- `WHISPER_MODEL_SIZE` controls the faster-whisper model size such as `medium` or `large-v3`
- `WHISPER_DEVICE` controls CPU or CUDA execution
- `WHISPER_COMPUTE_TYPE` controls inference precision such as `int8`, `float16`, or `int8_float16`
- `WHISPER_LANGUAGE` is an optional global fallback for direct worker checks; normal audio jobs carry per-job language metadata
- audio clients can now request per-job transcription language with `transcription_language=auto|fr|en`; `auto` does not force a faster-whisper language
- meeting generation clients can request `output_language=same_as_meeting|fr|en`; this changes the prompt instruction only
- meeting generation clients can request `generation_mode=standard|deep_think`; `standard` is faster and default, `deep_think` is slower but better for long or important meetings
- meeting prompts are optimized for local models: direct useful output, no empty template sections, no generic filler, and action items in the simple form `Action | Owner | Due date`
- `WHISPER_BEAM_SIZE` controls beam search width
- `WHISPER_MODEL_CACHE_DIR`, `HF_HOME`, and `HUGGINGFACE_HUB_CACHE` should point to the persistent model cache volume in Docker
- `DIARIZATION_ENABLED=false` is the recommended production value. The meeting pipeline currently prioritizes reliable GPU STT over speaker diarization.
- TLS certificate management for public Internet exposure is a later step; Traefik is already positioned as the only public entrypoint

## Recommended worker settings

For CI and fast local tests:

- `TRANSCRIPTION_ENGINE=fake`

For CPU-only local testing with real transcription:

- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_DEVICE=cpu`
- `WHISPER_MODEL_SIZE=medium`
- `WHISPER_COMPUTE_TYPE=int8`
- `WHISPER_LANGUAGE=auto`

For an NVIDIA RTX 3090:

- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_DEVICE=cuda`
- `WHISPER_COMPUTE_TYPE=float16`
- `WHISPER_MODEL_SIZE=large-v3` for best quality
- `WHISPER_MODEL_SIZE=medium` for lower latency and lower VRAM usage
- `WHISPER_LANGUAGE=auto` for bilingual FR/EN meetings

Audio files and transcript results remain local to your self-hosted storage under `AUDIO_STORAGE_DIR`.

## Ollama troubleshooting

If `POST /v1/meetings/generate-from-job` returns `502` or `503` during local Windows development and the Docker `ollama` container shows `total blobs: 0`, the gateway is probably still using the empty Docker Ollama instance instead of the host Ollama service.

Checks:

- confirm `OLLAMA_BASE_URL` inside `ai-gateway`
- confirm `host.docker.internal` resolves inside `ai-gateway`
- run `python -m app.cli check-ollama`
- confirm the required models are actually installed in the Ollama instance you are targeting

Important:

- `DEFAULT_MODEL` and `ALLOWED_MODELS` do not install models
- the Docker Ollama container can be empty even if the Windows host Ollama already has models
- if you use host Ollama, that host instance must contain the models
- if you use containerized Ollama, pull the models into the container volume
- if you use `LLM_PROVIDER=fake`, no Ollama call is made at runtime
- Ollama must still never be exposed publicly
- Redis, PostgreSQL, the worker, and Docker Ollama must still not publish host ports in this dev mode

## Real local troubleshooting

- `check-ollama` shows `Connection refused`:
  Ollama is not listening on `0.0.0.0:11434`, or Windows Firewall is blocking the connection from Docker.
- `check-ollama` shows `Model not found`:
  run `ollama pull mistral:latest`.
- `faster-whisper` CUDA error:
  switch temporarily to `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` in the real-local override for diagnosis.
- worker fails immediately with a missing model message:
  run `.\scripts\dev\prepare-whisper-model-windows.ps1` to populate the persistent model cache volume.
- `whisper-worker` fails:
  inspect `docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml logs whisper-worker`.
- `AI Summaries` note is missing:
  confirm that `POST /v1/meetings/generate-from-job` returns `200` and inspect `ai-gateway` logs.

To restart host Ollama for Docker reachability:

```powershell
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
$env:OLLAMA_HOST="0.0.0.0:11434"
ollama serve
```

## CORS notes for Obsidian

Obsidian desktop and Electron-based clients can send `OPTIONS` preflight requests before authenticated API calls, especially for multipart audio upload.

Recommended defaults for local development:

- `CORS_ENABLED=true`
- `CORS_ALLOW_ORIGINS=*`
- `CORS_ALLOW_METHODS=GET,POST,OPTIONS`
- `CORS_ALLOW_HEADERS=Authorization,Content-Type`
- `CORS_ALLOW_CREDENTIALS=false`

For production:

- replace `*` with an explicit allowlist of trusted origins
- keep Bearer token authentication enabled on all protected endpoints
- remember that CORS is a browser access control feature, not an authentication or authorization mechanism

## Audio storage hygiene

- uploaded audio files and transcript JSON files must not be committed to git
- the shared Docker volume is intended for runtime data only
- future work should add retention, purge, or rotation policies for old audio and transcript artifacts
