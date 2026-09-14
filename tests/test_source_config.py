from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from track_insight.models import Base, ConfigVersion, Source
from track_insight.source_config import (
    SourceDefinition,
    load_source_definitions,
    sync_source_definitions,
)


def test_source_fingerprint_is_stable() -> None:
    definition = SourceDefinition(
        code="source_test",
        name="测试来源",
        source_level="official",
        source_type="government",
        entry_urls=["https://example.com/policies"],
        adapter_key="static_list",
    )

    assert definition.fingerprint() == definition.fingerprint()
    assert len(definition.fingerprint()) == 64


def test_invalid_source_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceDefinition(
            code="Invalid Code",
            name="测试来源",
            source_level="official",
            source_type="government",
            entry_urls=["https://example.com"],
            adapter_key="static_list",
        )


def test_duplicate_source_codes_are_rejected(workspace_tmp_path: Path) -> None:
    content = """
- code: source_same
  name: 来源一
  source_level: official
  source_type: government
  entry_urls: [https://example.com/one]
  adapter_key: static_list
- code: source_same
  name: 来源二
  source_level: official
  source_type: government
  entry_urls: [https://example.com/two]
  adapter_key: static_list
"""
    (workspace_tmp_path / "sources.yaml").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate source code"):
        load_source_definitions(workspace_tmp_path)


def test_source_sync_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    definition = SourceDefinition(
        code="source_test",
        name="测试来源",
        source_level="official",
        source_type="government",
        entry_urls=["https://example.com/policies"],
        adapter_key="static_list",
    )

    with Session(engine) as session:
        assert sync_source_definitions(session, [definition]) == (1, 0)
        assert sync_source_definitions(session, [definition]) == (0, 1)
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        assert session.scalar(select(func.count()).select_from(ConfigVersion)) == 1
