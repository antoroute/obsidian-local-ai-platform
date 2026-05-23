# Deployment

## Target environment

- Docker Compose
- GPU server with NVIDIA RTX 3090
- Reverse proxy in front of `ai-gateway`
- Internal-only networking for Ollama, Redis, PostgreSQL, and workers
- NVIDIA Container Toolkit installed on the Docker host

## Compose topology

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

## Start the stack

```powershell
Copy-Item .env.example .env
docker compose up --build
```

## Optional local dev override

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

## Dev Windows avec Ollama hote

For local Windows development, you can keep Ollama installed on the host and point the gateway container to it through `host.docker.internal`.

With the dev override:

```powershell
docker compose -f docker-compose.yml -f infra/docker-compose.dev.override.yml up --build
```

The `ai-gateway` container is explicitly configured with:

- `OLLAMA_BASE_URL=http://host.docker.internal:11434`
- `DEFAULT_MODEL=mistral:latest`
- `ALLOWED_MODELS=qwen2.5:14b,mistral:latest,llama3:latest`
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
- `LLM_PROVIDER=fake` is a development-only runtime mode for validating the surrounding workflow without a live LLM backend
- for local Windows development with host Ollama, override `OLLAMA_BASE_URL` to `http://host.docker.internal:11434`
- in that Windows dev mode, attach only `ai-gateway` to a non-internal `host_access` bridge network
- `python -m app.cli check-ollama` is the quickest end-to-end diagnostic from inside `ai-gateway`
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
- `TRANSCRIPTION_ENGINE` selects `fake` or `faster_whisper`
- `WHISPER_MODEL_SIZE` controls the faster-whisper model size such as `medium` or `large-v3`
- `WHISPER_DEVICE` controls CPU or CUDA execution
- `WHISPER_COMPUTE_TYPE` controls inference precision such as `int8`, `float16`, or `int8_float16`
- `WHISPER_LANGUAGE` can pin the expected language, for example `fr`
- `WHISPER_BEAM_SIZE` controls beam search width
- TLS certificate management for public Internet exposure is a later step; Traefik is already positioned as the only public entrypoint

## Recommended worker settings

For CI and fast local tests:

- `TRANSCRIPTION_ENGINE=fake`

For CPU-only local testing with real transcription:

- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_DEVICE=cpu`
- `WHISPER_MODEL_SIZE=medium`
- `WHISPER_COMPUTE_TYPE=int8`
- `WHISPER_LANGUAGE=fr`

For an NVIDIA RTX 3090:

- `TRANSCRIPTION_ENGINE=faster_whisper`
- `WHISPER_DEVICE=cuda`
- `WHISPER_COMPUTE_TYPE=float16`
- `WHISPER_MODEL_SIZE=large-v3` for best quality
- `WHISPER_MODEL_SIZE=medium` for lower latency and lower VRAM usage

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
