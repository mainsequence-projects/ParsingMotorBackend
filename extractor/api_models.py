"""Infrastructure response models; document contracts live in document_models."""
from typing import Any, List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorResponse(BaseModel):
    detail: Union[str, List[Any]] = Field(..., description="Human-readable reason (422 from FastAPI validation is a list)")


class Limits(_Open):
    max_files: int
    max_pages: int
    max_file_mb: float


class HealthResponse(_Open):
    status: Literal["ok"]
    version: str = Field(..., examples=["0.3.0"])
    region: str = Field(..., examples=["eu-central-1"])
    git_commit: str
    limits: Limits


class SessionResponse(_Open):
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str
    expiration: str
    region: str
    functionUrl: str
    service: Literal["lambda"]
