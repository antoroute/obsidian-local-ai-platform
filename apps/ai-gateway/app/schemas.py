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
