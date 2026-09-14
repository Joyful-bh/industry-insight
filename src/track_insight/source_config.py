import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.models import ConfigVersion, Source


class SourceDefinition(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,99}$")
    name: str
    publisher: str | None = None
    source_level: str
    source_type: str
    regions: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    entry_urls: list[HttpUrl]
    adapter_key: str
    schedule: str | None = None
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    discovery_filter: dict[str, Any] = Field(default_factory=dict)
    evidence_usage: str | None = None
    usage_restrictions: str | None = None
    enabled: bool = True

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_source_definitions(directory: Path) -> list[SourceDefinition]:
    definitions: list[SourceDefinition] = []
    seen_codes: set[str] = set()
    for path in sorted(directory.glob("*.y*ml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            definition = SourceDefinition.model_validate(item)
            if definition.code in seen_codes:
                raise ValueError(f"duplicate source code: {definition.code}")
            seen_codes.add(definition.code)
            definitions.append(definition)
    return definitions


def sync_source_definitions(
    session: Session, definitions: list[SourceDefinition]
) -> tuple[int, int]:
    created = 0
    updated = 0
    for definition in definitions:
        payload = definition.model_dump(mode="json", exclude_none=True)
        fingerprint = definition.fingerprint()
        config_version = session.scalar(
            select(ConfigVersion).where(
                ConfigVersion.kind == "source",
                ConfigVersion.fingerprint == fingerprint,
            )
        )
        if config_version is None:
            session.add(
                ConfigVersion(
                    kind="source",
                    version=fingerprint[:12],
                    fingerprint=fingerprint,
                    payload=payload,
                )
            )

        source = session.scalar(select(Source).where(Source.code == definition.code))
        values = {
            "name": definition.name,
            "publisher": definition.publisher,
            "source_level": definition.source_level,
            "source_type": definition.source_type,
            "regions": definition.regions,
            "content_types": definition.content_types,
            "entry_urls": [str(url) for url in definition.entry_urls],
            "adapter_key": definition.adapter_key,
            "schedule": definition.schedule,
            "rate_limit": definition.rate_limit,
            "retry_policy": definition.retry_policy,
            "discovery_filter": definition.discovery_filter,
            "evidence_usage": definition.evidence_usage,
            "usage_restrictions": definition.usage_restrictions,
            "config_version": fingerprint,
            "enabled": definition.enabled,
        }
        if source is None:
            session.add(Source(code=definition.code, **values))
            created += 1
        else:
            for key, value in values.items():
                setattr(source, key, value)
            updated += 1
    session.flush()
    return created, updated
