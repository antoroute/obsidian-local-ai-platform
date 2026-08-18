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
    llm_provider: str = Field(default="ollama", alias="LLM_PROVIDER")
    ollama_base_url: str = Field(default="http://ollama:11434", alias="OLLAMA_BASE_URL")
    default_model: str = Field(default="qwen3:8b", alias="DEFAULT_MODEL")
    allowed_models_raw: str = Field(default="qwen3:8b", alias="ALLOWED_MODELS")
    ollama_timeout_seconds: int = Field(default=120, alias="OLLAMA_TIMEOUT_SECONDS")
    ollama_num_ctx: int = Field(default=8192, alias="OLLAMA_NUM_CTX")
    ollama_keep_alive: str = Field(default="5m", alias="OLLAMA_KEEP_ALIVE")
    ollama_max_concurrent_requests: int = Field(default=1, ge=1, le=8, alias="OLLAMA_MAX_CONCURRENT_REQUESTS")
    max_note_chars: int = Field(default=200000, alias="MAX_NOTE_CHARS")
    max_template_chars: int = Field(default=50000, alias="MAX_TEMPLATE_CHARS")
    max_transcript_chars: int = Field(default=300000, alias="MAX_TRANSCRIPT_CHARS")
    max_manual_notes_chars: int = Field(default=100000, alias="MAX_MANUAL_NOTES_CHARS")
    max_participants: int = Field(default=100, alias="MAX_PARTICIPANTS")
    meeting_transcript_cleanup_enabled: bool = Field(default=True, alias="MEETING_TRANSCRIPT_CLEANUP_ENABLED")
    meeting_predigest_enabled: bool = Field(default=True, alias="MEETING_PREDIGEST_ENABLED")
    meeting_predigest_min_chars: int = Field(default=12000, alias="MEETING_PREDIGEST_MIN_CHARS")
    meeting_predigest_chunk_chars: int = Field(default=24000, ge=4000, le=40000, alias="MEETING_PREDIGEST_CHUNK_CHARS")
    meeting_predigest_max_chunks: int = Field(default=10, ge=1, le=16, alias="MEETING_PREDIGEST_MAX_CHUNKS")
    meeting_predigest_manual_notes_max_chars: int = Field(default=6000, ge=1000, le=20000, alias="MEETING_PREDIGEST_MANUAL_NOTES_MAX_CHARS")
    meeting_deep_think_enabled: bool = Field(default=True, alias="MEETING_DEEP_THINK_ENABLED")
    meeting_deep_think_max_sections: int = Field(default=6, alias="MEETING_DEEP_THINK_MAX_SECTIONS")
    meeting_deep_think_excerpt_chars_per_section: int = Field(default=2000, alias="MEETING_DEEP_THINK_EXCERPT_CHARS_PER_SECTION")
    meeting_deep_think_final_cleanup: bool = Field(default=True, alias="MEETING_DEEP_THINK_FINAL_CLEANUP")
    max_assistant_message_chars: int = Field(default=20000, alias="MAX_ASSISTANT_MESSAGE_CHARS")
    max_assistant_context_chars: int = Field(default=100000, alias="MAX_ASSISTANT_CONTEXT_CHARS")
    redis_url: str = Field(default="redis://redis:6379/0", alias="AI_GATEWAY_REDIS_URL")
    audio_storage_dir: str = Field(default="./data/audio", alias="AUDIO_STORAGE_DIR")
    max_audio_upload_mb: int = Field(default=500, alias="MAX_AUDIO_UPLOAD_MB")
    audio_queue_name: str = Field(default="audio_transcription_jobs", alias="AUDIO_QUEUE_NAME")
    usage_quotas_enabled: bool = Field(default=True, alias="USAGE_QUOTAS_ENABLED")
    daily_llm_requests_per_user: int = Field(default=100, ge=1, alias="DAILY_LLM_REQUESTS_PER_USER")
    daily_embedding_requests_per_user: int = Field(default=5000, ge=1, alias="DAILY_EMBEDDING_REQUESTS_PER_USER")
    daily_audio_jobs_per_user: int = Field(default=20, ge=1, alias="DAILY_AUDIO_JOBS_PER_USER")
    max_active_audio_jobs_per_user: int = Field(default=1, ge=1, le=10, alias="MAX_ACTIVE_AUDIO_JOBS_PER_USER")
    job_stalled_after_seconds: int = Field(default=900, ge=60, alias="JOB_STALLED_AFTER_SECONDS")
    job_history_limit: int = Field(default=50, ge=1, le=200, alias="JOB_HISTORY_LIMIT")
    metrics_token: str = Field(default="", alias="METRICS_TOKEN")
    worker_heartbeat_key: str = Field(default="obsidian_ai:whisper_worker:heartbeat", alias="WORKER_HEARTBEAT_KEY")
    health_dependency_timeout_seconds: float = Field(default=3.0, ge=0.5, le=15, alias="HEALTH_DEPENDENCY_TIMEOUT_SECONDS")
    diarization_service_url: str = Field(default="", alias="DIARIZATION_SERVICE_URL")
    cors_enabled: bool = Field(default=True, alias="CORS_ENABLED")
    cors_allow_origins_raw: str = Field(default="*", alias="CORS_ALLOW_ORIGINS")
    cors_allow_methods_raw: str = Field(default="GET,POST,DELETE,OPTIONS", alias="CORS_ALLOW_METHODS")
    cors_allow_headers_raw: str = Field(default="Authorization,Content-Type", alias="CORS_ALLOW_HEADERS")
    cors_allow_credentials: bool = Field(default=False, alias="CORS_ALLOW_CREDENTIALS")
    rag_enabled: bool = Field(default=True, alias="RAG_ENABLED")
    rag_vector_backend: str = Field(default="pgvector", alias="RAG_VECTOR_BACKEND")
    rag_embedding_provider: str = Field(default="ollama", alias="RAG_EMBEDDING_PROVIDER")
    rag_embedding_model: str = Field(default="nomic-embed-text", alias="RAG_EMBEDDING_MODEL")
    rag_embedding_dimension: int = Field(default=768, alias="RAG_EMBEDDING_DIMENSION")
    rag_chunk_size: int = Field(default=900, alias="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=150, alias="RAG_CHUNK_OVERLAP")
    rag_max_chunks_per_query: int = Field(default=8, alias="RAG_MAX_CHUNKS_PER_QUERY")
    rag_max_chunks_per_document: int = Field(default=2, ge=1, le=8, alias="RAG_MAX_CHUNKS_PER_DOCUMENT")
    rag_search_candidates: int = Field(default=30, alias="RAG_SEARCH_CANDIDATES")
    rag_max_context_chars: int = Field(default=24000, alias="RAG_MAX_CONTEXT_CHARS")
    rag_min_score: float = Field(default=0.15, alias="RAG_MIN_SCORE")
    rag_keyword_bonus_enabled: bool = Field(default=True, alias="RAG_KEYWORD_BONUS_ENABLED")
    rag_keyword_bonus_max: float = Field(default=0.20, alias="RAG_KEYWORD_BONUS_MAX")
    rag_index_excluded_dirs_raw: str = Field(default=".obsidian,Templates,Archives,Private", alias="RAG_INDEX_EXCLUDED_DIRS")
    rag_index_excluded_tags_raw: str = Field(default="noai,private", alias="RAG_INDEX_EXCLUDED_TAGS")
    rag_default_vault_id: str = Field(default="default", alias="RAG_DEFAULT_VAULT_ID")
    rag_workspace_id: str = Field(default="", alias="RAG_WORKSPACE_ID")

    @property
    def allowed_models(self) -> list[str]:
        return [model.strip() for model in self.allowed_models_raw.split(",") if model.strip()]

    @property
    def cors_allow_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins_raw.split(",") if origin.strip()]

    @property
    def cors_allow_methods(self) -> list[str]:
        return [method.strip().upper() for method in self.cors_allow_methods_raw.split(",") if method.strip()]

    @property
    def cors_allow_headers(self) -> list[str]:
        return [header.strip() for header in self.cors_allow_headers_raw.split(",") if header.strip()]

    @property
    def rag_index_excluded_dirs(self) -> list[str]:
        return [item.strip() for item in self.rag_index_excluded_dirs_raw.split(",") if item.strip()]

    @property
    def rag_index_excluded_tags(self) -> list[str]:
        return [item.strip() for item in self.rag_index_excluded_tags_raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
