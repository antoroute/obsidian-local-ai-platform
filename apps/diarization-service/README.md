# GPU diarization coordinator

Private service for the GPU VM. It has two responsibilities:

- run optional pyannote speaker diarization and return anonymous time ranges;
- proxy Ollama behind the same asynchronous lock so both workloads cannot occupy
  the RTX 2070 at once.

Every GPU consumer (Obsidian AI, Open WebUI, Hermes, and health probes) keeps using
`http://10.0.70.10:11434`. The coordinator owns that address and transparently
proxies `/api/*` and `/v1/*`; native Ollama listens only on
`http://127.0.0.1:11435`. `GET /v1/ollama-health` checks Ollama's real API without
loading a model.

`POST /v1/diarize` requires `Authorization: Bearer <SERVICE_TOKEN>`. The Ollama
proxy is not an Internet API and must be restricted by the host firewall. The
gateway continues to expose only its allowlisted application endpoints.

The default is `pyannote/speaker-diarization-3.1` with pyannote.audio 3.3.2. The
pipeline is loaded on demand and released after each request. The feature remains
disabled by default in Note Compagnon because an 8 GB GPU needs a real workload
benchmark before routine use.

The Hugging Face model is gated. Accept its conditions and provide a read-only
`HF_TOKEN` only during model preparation/runtime download. Never commit `.env.gpu`.
On VM120, keep `GPU_OLLAMA_BASE_URL=http://127.0.0.1:11435`.

```bash
docker compose --env-file .env.gpu \
  -f infra/docker-compose.gpu-services.yml up -d --build
curl http://10.0.70.10:11434/v1/health
```

Speaker labels are chronological aliases such as `Speaker 1`; they are not voice
identity or participant recognition.
