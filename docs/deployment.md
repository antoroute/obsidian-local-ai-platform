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

- `OLLAMA_BASE_URL` must stay `http://ollama:11434` inside Docker
- `AI_GATEWAY_DATABASE_URL` must point to PostgreSQL on the `ai_internal` network
- `AI_GATEWAY_REDIS_URL` must point to Redis on the `ai_internal` network
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

## Audio storage hygiene

- uploaded audio files and transcript JSON files must not be committed to git
- the shared Docker volume is intended for runtime data only
- future work should add retention, purge, or rotation policies for old audio and transcript artifacts
