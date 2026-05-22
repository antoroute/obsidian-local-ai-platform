from fastapi import FastAPI

from app.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/v1/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
