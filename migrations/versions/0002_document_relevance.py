"""document relevance assessments

Revision ID: 0002_relevance
Revises: 0001_stage0
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_relevance"
down_revision: str | None = "0001_stage0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_relevance_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signal_types", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("matched_positive_rules", sa.JSON(), nullable=False),
        sa.Column("matched_negative_rules", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("classifier_type", sa.String(32), nullable=False),
        sa.Column("classifier_version", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_relevance_document_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_relevance_assessments"),
        sa.UniqueConstraint(
            "document_version_id",
            "classifier_version",
            name="uq_relevance_document_classifier",
        ),
    )
    op.create_index(
        "ix_document_relevance_label", "document_relevance_assessments", ["label"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_relevance_label", table_name="document_relevance_assessments")
    op.drop_table("document_relevance_assessments")
