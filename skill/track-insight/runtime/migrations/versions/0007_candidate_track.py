"""Add candidate Track generation and analysis tables.

Revision ID: 0007_candidate_track
Revises: 0006_topic_key
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_candidate_track"
down_revision: str | None = "0006_topic_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "track_build_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("topic_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("topic_count", sa.Integer(), nullable=False),
        sa.Column("track_count", sa.Integer(), nullable=False),
        sa.Column("unassigned_topic_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_plan_id",
            "input_fingerprint",
            "processor_version",
            name="uq_track_build_run_input",
        ),
    )
    op.create_table(
        "candidate_track",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_run_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_run_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("enterprise_archetype", sa.Text(), nullable=False),
        sa.Column("included_activities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded_activities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chain_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observable_company_features", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("possible_it_needs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("regions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("track_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.ForeignKeyConstraint(["created_by_run_id"], ["track_build_run.id"]),
        sa.ForeignKeyConstraint(["updated_by_run_id"], ["track_build_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_plan_id", "canonical_key", name="uq_candidate_track_plan_key"
        ),
    )
    op.create_index(
        "ix_candidate_track_plan_status", "candidate_track", ["research_plan_id", "status"]
    )
    op.create_table(
        "candidate_track_topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_track_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("track_build_run_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_track_id"], ["candidate_track.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.ForeignKeyConstraint(["track_build_run_id"], ["track_build_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_track_id", "topic_id", name="uq_candidate_track_topic"),
    )
    op.create_table(
        "candidate_track_analysis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_track_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("why_now", sa.Text()),
        sa.Column("signal_statistics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("activity_assessment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("industry_chain_analysis", sa.Text()),
        sa.Column("smb_value_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["candidate_track_id"], ["candidate_track.id"]),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_track_id",
            "input_fingerprint",
            "processor_version",
            name="uq_track_analysis_input",
        ),
    )


def downgrade() -> None:
    op.drop_table("candidate_track_analysis")
    op.drop_table("candidate_track_topic")
    op.drop_index("ix_candidate_track_plan_status", table_name="candidate_track")
    op.drop_table("candidate_track")
    op.drop_table("track_build_run")
