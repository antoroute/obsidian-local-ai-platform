# GPU diarization coordinator

Private service for the GPU VM. It has two responsibilities:

- run optional pyannote speaker diarization and return anonymous time ranges;
- proxy Ollama behind the same asynchronous lock so both workloads cannot occupy
  the RTX 2070 at once.

Every GPU consumer (Obsidian AI, Open WebUI, Hermes, and health probes) must use
`http://10.0.70.10:18080/ollama`. Direct external access to port 11434 must then be
blocked. `GET /v1/ollama-health` checks Ollama's real `/api/version` endpoint
without loading a model.

`POST /v1/diarize` requires `Authorization: Bearer <SERVICE_TOKEN>`. The Ollama
proxy is not an Internet API and must be restricted by the host firewall. The
gateway continues to expose only its allowlisted application endpoints.

The default is `pyannote/speaker-diarization-3.1` with pyannote.audio 3.3.2. The
pipeline is loaded on demand and released after each request. The feature remains
disabled by default in Note Compagnon because an 8 GB GPU needs a real workload
benchmark before routine use.

The Hugging Face model is gated. Accept its conditions and provide a read-only
`HF_TOKEN` only during model preparation/runtime download. Never commit `.env.gpu`.
On VM120, Ollama listens on `10.0.70.10:11434`, so keep
`GPU_OLLAMA_BASE_URL=http://10.0.70.10:11434`.

```bash
docker compose --env-file .env.gpu \
  -f infra/docker-compose.gpu-services.yml up -d --build
curl http://127.0.0.1:18080/v1/health
```

Speaker labels are chronological aliases such as `Speaker 1`; they are not voice
identity or participant recognition.
