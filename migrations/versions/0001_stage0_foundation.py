"""stage 0 foundation

Revision ID: 0001_stage0
Revises:
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_stage0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "config_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_config_versions"),
        sa.UniqueConstraint("kind", "fingerprint", name="uq_config_versions_kind"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("publisher", sa.String(300)),
        sa.Column("source_level", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("content_types", sa.JSON(), nullable=False),
        sa.Column("entry_urls", sa.JSON(), nullable=False),
        sa.Column("adapter_key", sa.String(100), nullable=False),
        sa.Column("schedule", sa.String(100)),
        sa.Column("rate_limit", sa.JSON(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("evidence_usage", sa.Text()),
        sa.Column("usage_restrictions", sa.Text()),
        sa.Column("config_version", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("code", name="uq_sources_code"),
    )

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True)),
        sa.Column("input_config", sa.JSON(), nullable=False),
        sa.Column("counters", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_runs"),
    )

    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cursor_before", sa.JSON()),
        sa.Column("cursor_after", sa.JSON()),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_runs.id"],
            name="fk_crawl_runs_pipeline_run_id_pipeline_runs",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name="fk_crawl_runs_source_id_sources"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_runs"),
    )

    op.create_table(
        "source_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("crawl_run_id", sa.Uuid()),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("period_end > period_start", name="positive_period"),
        sa.ForeignKeyConstraint(
            ["crawl_run_id"],
            ["crawl_runs.id"],
            name="fk_source_observations_crawl_run_id_crawl_runs",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name="fk_source_observations_source_id_sources"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_observations"),
        sa.UniqueConstraint(
            "source_id", "period_start", "period_end", name="uq_source_observations_source_id"
        ),
    )

    op.create_table(
        "raw_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(200), nullable=False),
        sa.Column("mime_type", sa.String(200)),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("response_headers", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_objects"),
        sa.UniqueConstraint("sha256", name="uq_raw_objects_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_raw_objects_storage_key"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("external_id", sa.String(300)),
        sa.Column("title", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name="fk_documents_source_id_sources"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("source_id", "canonical_url", name="uq_documents_source_id"),
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("raw_object_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("publisher", sa.String(300)),
        sa.Column("document_number", sa.String(200)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("region_codes", sa.JSON(), nullable=False),
        sa.Column("content_type", sa.String(64)),
        sa.Column("normalized_text", sa.Text()),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64)),
        sa.Column("simhash", sa.String(32)),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("parse_quality", sa.Float()),
        sa.Column("parser_version", sa.String(64)),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_document_versions_document_id_documents"
        ),
        sa.ForeignKeyConstraint(
            ["raw_object_id"],
            ["raw_objects.id"],
            name="fk_document_versions_raw_object_id_raw_objects",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_content"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_version_no"),
    )
    op.create_index("ix_document_versions_published_at", "document_versions", ["published_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(100), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "scheduled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("output", sa.JSON()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"], ["pipeline_runs.id"], name="fk_jobs_pipeline_run_id_pipeline_runs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "scheduled_at", "priority"])


def downgrade() -> None:
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_document_versions_published_at", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("raw_objects")
    op.drop_table("source_observations")
    op.drop_table("crawl_runs")
    op.drop_table("pipeline_runs")
    op.drop_table("sources")
    op.drop_table("config_versions")
