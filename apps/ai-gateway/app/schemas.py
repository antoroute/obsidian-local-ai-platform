from pydantic import BaseModel, Field


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
