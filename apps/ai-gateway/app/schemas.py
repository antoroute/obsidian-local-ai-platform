from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ModelsResponse(BaseModel):
    models: list[str]
