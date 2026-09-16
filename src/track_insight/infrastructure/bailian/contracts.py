from typing import Any

from pydantic import BaseModel, Field


class WebSearchSource(BaseModel):
    refer: str
    title: str | None = None
    link: str
    content: str | None = None
    media: str | None = None
    publish_date: str | None = None


class ModelRunResult[OutputT: BaseModel](BaseModel):
    request_id: str | None = None
    model: str
    usage: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any]
    output: OutputT
    web_search: list[WebSearchSource] = Field(default_factory=list)
    call_count: int = 1
    recovery_used: bool = False
