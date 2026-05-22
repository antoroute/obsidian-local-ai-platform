from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "obsidian-local-ai-platform ai-gateway"
    host: str = Field(default="127.0.0.1", alias="AI_GATEWAY_HOST")
    port: int = Field(default=8000, alias="AI_GATEWAY_PORT")
    log_level: str = Field(default="info", alias="AI_GATEWAY_LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
