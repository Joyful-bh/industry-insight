from sqlalchemy import create_engine

from track_insight.models import Base


def test_stage0_schema_can_be_created() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    Base.metadata.create_all(engine)

    assert {
        "config_versions",
        "sources",
        "pipeline_runs",
        "crawl_runs",
        "source_observations",
        "raw_objects",
        "documents",
        "document_versions",
        "document_relevance_assessments",
        "jobs",
    } <= set(Base.metadata.tables)
