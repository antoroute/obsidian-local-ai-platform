FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

ARG INSTALL_DIARIZATION=false
RUN pip install --no-cache-dir .[faster-whisper] \
    && if [ "$INSTALL_DIARIZATION" = "true" ]; then \
        pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.3.1 torchaudio==2.3.1; \
        pip install --no-cache-dir .[diarization]; \
    fi

CMD ["python", "-m", "whisper_worker"]
