from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import Settings

ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".webm", ".ogg"}
CHUNK_SIZE = 1024 * 1024


def validate_audio_filename(filename: str | None) -> str:
    if not filename:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Audio filename is required.")

    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported audio file extension.")
    return extension


async def save_uploaded_audio(upload: UploadFile, settings: Settings) -> str:
    extension = validate_audio_filename(upload.filename)
    base_dir = Path(settings.audio_storage_dir).resolve()
    input_dir = base_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    internal_name = f"{uuid.uuid4()}{extension}"
    destination = (input_dir / internal_name).resolve()
    if destination.parent != input_dir.resolve():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio destination path.")

    max_bytes = settings.max_audio_upload_mb * 1024 * 1024
    total_bytes = 0

    try:
        with destination.open("wb") as output:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"Audio upload exceeds the maximum of {settings.max_audio_upload_mb} MB.",
                    )
                output.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    finally:
        await upload.close()

    return str(destination)
