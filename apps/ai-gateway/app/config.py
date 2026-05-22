from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "obsidian-local-ai-platform ai-gateway"
    host: str = Field(default="127.0.0.1", alias="AI_GATEWAY_HOST")
    port: int = Field(default=8000, alias="AI_GATEWAY_PORT")
    log_level: str = Field(default="info", alias="AI_GATEWAY_LOG_LEVEL")
    database_url: str = Field(default="sqlite:///./ai_gateway.db", alias="AI_GATEWAY_DATABASE_URL")
    ollama_base_url: str = Field(default="http://ollama:11434", alias="OLLAMA_BASE_URL")
    default_model: str = Field(default="qwen2.5:14b", alias="DEFAULT_MODEL")
    allowed_models_raw: str = Field(default="qwen2.5:14b,mistral:7b", alias="ALLOWED_MODELS")
    ollama_timeout_seconds: int = Field(default=120, alias="OLLAMA_TIMEOUT_SECONDS")
    max_note_chars: int = Field(default=200000, alias="MAX_NOTE_CHARS")
    max_template_chars: int = Field(default=50000, alias="MAX_TEMPLATE_CHARS")

    @property
    def allowed_models(self) -> list[str]:
        return [model.strip() for model in self.allowed_models_raw.split(",") if model.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
