import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class RegionScope(BaseModel):
    code: str = Field(pattern=r"^CN(?:-\d{2}(?:-\d{2})?)?$")
    name: str


class DiscoveryPolicy(BaseModel):
    search_enabled: bool = False
    search_provider: str | None = None
    enterprise_site_discovery: bool = True
    require_original_source: bool = True

    @model_validator(mode="after")
    def provider_required_when_search_is_enabled(self) -> "DiscoveryPolicy":
        if self.search_enabled and not self.search_provider:
            raise ValueError("search_provider is required when search_enabled is true")
        return self


class RelevancePrefilter(BaseModel):
    mode: Literal["tag_only", "include_if_match"] = "include_if_match"
    title_keywords: list[str] = Field(default_factory=list)


class CollectionScope(BaseModel):
    version: str
    target_regions: list[RegionScope]
    backfill_months: int = Field(gt=0, le=120)
    document_formats: set[Literal["html", "pdf"]]
    publish_time_semantics: Literal["published_at"] = "published_at"
    discovery: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)
    relevance_prefilter: RelevancePrefilter = Field(default_factory=RelevancePrefilter)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_collection_scope(path: Path) -> CollectionScope:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return CollectionScope.model_validate(raw)
