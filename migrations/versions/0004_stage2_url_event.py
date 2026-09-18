"""Add page acquisition, analysis, and event evidence tables.

Revision ID: 0004_stage2
Revises: 0003_bailian
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_stage2"
down_revision: str | None = "0003_bailian"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "page_capture",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("acquisition_method", sa.String(40)),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("mime_type", sa.String(200)),
        sa.Column("title", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("content_text", sa.Text()),
        sa.Column("content_chars", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("quality_status", sa.String(40)),
        sa.Column("quality_reasons", sa.JSON(), nullable=False),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["url_candidate_id"], ["url_candidate.id"], name="fk_page_capture_url_candidate"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_page_capture"),
        sa.UniqueConstraint("url_candidate_id", name="uq_page_capture_url_candidate_id"),
    )
    op.create_index("ix_page_capture_status", "page_capture", ["status"])
    op.create_table(
        "page_analysis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_capture_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column("relevance", sa.String(32)),
        sa.Column("document_type", sa.String(50)),
        sa.Column("reason", sa.Text()),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("content_sufficient", sa.Boolean()),
        sa.Column("structured_output", sa.JSON()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["page_capture_id"], ["page_capture.id"], name="fk_page_analysis_page_capture"
        ),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["model_run.id"], name="fk_page_analysis_model_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_page_analysis"),
        sa.UniqueConstraint("page_capture_id", name="uq_page_analysis_page_capture_id"),
    )
    op.create_index("ix_page_analysis_status", "page_analysis", ["status"])
    op.create_table(
        "event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("signal_date", sa.Date()),
        sa.Column("date_precision", sa.String(20), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("chain_roles", sa.JSON(), nullable=False),
        sa.Column("smb_relevance", sa.String(20), nullable=False),
        sa.Column("smb_reason", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("event_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["page_analysis_id"], ["page_analysis.id"], name="fk_event_page_analysis"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event"),
        sa.UniqueConstraint("page_analysis_id", "sequence_no", name="uq_event_analysis_sequence"),
        sa.UniqueConstraint("page_analysis_id", "event_fingerprint", name="uq_event_analysis_fp"),
    )
    op.create_index("ix_event_type_signal_date", "event", ["event_type", "signal_date"])
    op.create_table(
        "event_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("page_capture_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("start_offset", sa.Integer()),
        sa.Column("end_offset", sa.Integer()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], name="fk_event_evidence_event"),
        sa.ForeignKeyConstraint(
            ["page_capture_id"], ["page_capture.id"], name="fk_event_evidence_page_capture"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_evidence"),
        sa.UniqueConstraint("event_id", "evidence_hash", name="uq_event_evidence_hash"),
    )


def downgrade() -> None:
    op.drop_table("event_evidence")
    op.drop_index("ix_event_type_signal_date", table_name="event")
    op.drop_table("event")
    op.drop_index("ix_page_analysis_status", table_name="page_analysis")
    op.drop_table("page_analysis")
    op.drop_index("ix_page_capture_status", table_name="page_capture")
    op.drop_table("page_capture")
