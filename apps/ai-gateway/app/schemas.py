from typing import Literal

from pydantic import BaseModel, Field


OutputLanguage = Literal["same_as_meeting", "fr", "en"]
TranscriptionLanguage = Literal["auto", "fr", "en"]
AssistantMode = Literal["chat", "correct", "rewrite", "summarize"]
AssistantOutputLanguage = Literal["same_as_input", "fr", "en"]
AssistantResponseStyle = Literal["direct", "detailed"]
VaultAnswerLanguage = Literal["same_as_input", "fr", "en"]


class HealthResponse(BaseModel):
    status: str


class ModelsResponse(BaseModel):
    models: list[str]


class NoteSummarizeRequest(BaseModel):
    title: str = Field(default="")
    note_content: str = Field(min_length=1)
    template: str = Field(default="")
    model: str | None = None


class UsageResponse(BaseModel):
    prompt_chars: int
    template_chars: int


class NoteSummarizeResponse(BaseModel):
    model: str
    title: str
    summary_markdown: str
    usage: UsageResponse


class MeetingGenerateRequest(BaseModel):
    title: str = Field(min_length=1)
    transcript: str | None = None
    manual_notes: str | None = None
    participants: list[str] = Field(default_factory=list)
    template: str = Field(min_length=1)
    model: str | None = None
    output_language: OutputLanguage = "same_as_meeting"


class MeetingGenerateFromJobRequest(BaseModel):
    job_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    manual_notes: str | None = None
    participants: list[str] = Field(default_factory=list)
    template: str = Field(min_length=1)
    model: str | None = None
    output_language: OutputLanguage = "same_as_meeting"


class MeetingUsageResponse(BaseModel):
    transcript_chars: int
    manual_notes_chars: int
    template_chars: int
    participants_count: int


class MeetingGenerateResponse(BaseModel):
    model: str
    title: str
    meeting_markdown: str
    usage: MeetingUsageResponse


class MeetingGenerateFromJobResponse(BaseModel):
    job_id: str
    model: str
    title: str
    meeting_markdown: str
    usage: MeetingUsageResponse


class AudioTranscriptionQueuedResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    error: str | None


class TranscriptSegmentResponse(BaseModel):
    start: float
    end: float
    text: str


class TranscriptResponse(BaseModel):
    text: str
    language: str
    duration: float
    segments: list[TranscriptSegmentResponse]


class JobResultResponse(BaseModel):
    job_id: str
    transcript: TranscriptResponse


class AssistantChatRequest(BaseModel):
    message: str = ""
    context: str | None = None
    mode: AssistantMode = "chat"
    output_language: AssistantOutputLanguage = "same_as_input"
    response_style: AssistantResponseStyle | None = None
    model: str | None = None


class AssistantUsageResponse(BaseModel):
    message_chars: int
    context_chars: int


class AssistantChatResponse(BaseModel):
    model: str
    mode: AssistantMode
    answer_markdown: str
    usage: AssistantUsageResponse


class VaultIndexNoteRequest(BaseModel):
    vault_id: str = Field(default="default", min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    path: str = Field(min_length=1)
    title: str | None = None
    content: str = Field(min_length=1)
    modified_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    frontmatter: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class VaultIndexNoteResponse(BaseModel):
    status: Literal["indexed", "skipped"]
    document_id: str
    path: str
    chunks_indexed: int
    content_hash: str


class VaultSearchRequest(BaseModel):
    vault_id: str = Field(default="default", min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    query: str = Field(min_length=1)
    top_k: int | None = None
    path_prefix: str | None = None
    tags: list[str] = Field(default_factory=list)


class VaultSearchResult(BaseModel):
    path: str
    title: str | None
    heading_path: str | None
    snippet: str
    score: float
    chunk_index: int


class VaultSearchResponse(BaseModel):
    query: str
    results: list[VaultSearchResult]


class VaultAskRequest(BaseModel):
    vault_id: str = Field(default="default", min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    question: str = Field(min_length=1)
    model: str | None = None
    top_k: int | None = None
    path_prefix: str | None = None
    tags: list[str] = Field(default_factory=list)
    answer_language: VaultAnswerLanguage = "same_as_input"
    debug: bool = False


class VaultSourceResponse(BaseModel):
    path: str
    title: str | None
    heading_path: str | None
    chunk_index: int
    score: float


class VaultAskResponse(BaseModel):
    model: str
    answer_markdown: str
    sources: list[VaultSourceResponse]
    debug_info: dict[str, object] | None = None


class VaultStatsResponse(BaseModel):
    vault_id: str
    workspace_id: str
    documents: int
    chunks: int
    last_indexed_at: str | None


class VaultDeleteResponse(BaseModel):
    vault_id: str
    workspace_id: str | None = None
    all_users: bool = False
    deleted_documents: int
    deleted_chunks: int
