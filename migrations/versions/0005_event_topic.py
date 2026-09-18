"""Add Event-to-Topic processing tables.

Revision ID: 0005_event_topic
Revises: 0004_stage2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_event_topic"
down_revision: str | None = "0004_stage2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_build_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("generation_prompt_version", sa.String(100), nullable=False),
        sa.Column("reconciliation_prompt_version", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("topic_count", sa.Integer(), nullable=False),
        sa.Column("unassigned_event_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_plan_id",
            "input_fingerprint",
            "processor_version",
            name="uq_topic_build_run_input",
        ),
    )
    op.create_table(
        "topic_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_build_run_id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("batch_no", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(100), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("proposed_memberships", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["topic_build_run_id"], ["topic_build_run.id"]),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "topic_build_run_id", "batch_no", "candidate_key", name="uq_topic_candidate_key"
        ),
    )
    op.create_index(
        "ix_topic_candidate_run_batch", "topic_candidate", ["topic_build_run_id", "batch_no"]
    )
    op.create_table(
        "topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_run_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_run_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(100), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.Date()),
        sa.Column("latest_seen_at", sa.Date()),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("topic_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.ForeignKeyConstraint(["created_by_run_id"], ["topic_build_run.id"]),
        sa.ForeignKeyConstraint(["updated_by_run_id"], ["topic_build_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_plan_id", "topic_fingerprint", name="uq_topic_plan_fingerprint"
        ),
    )
    op.create_index("ix_topic_plan_status", "topic", ["research_plan_id", "status"])
    op.create_table(
        "topic_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("topic_build_run_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(32), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("assignment_reason", sa.Text(), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"]),
        sa.ForeignKeyConstraint(["topic_build_run_id"], ["topic_build_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_id", "event_id", name="uq_topic_event"),
    )
    op.create_table(
        "topic_merge_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_build_run_id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_topic_id", sa.Uuid()),
        sa.Column("canonical_key", sa.String(100)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["topic_build_run_id"], ["topic_build_run.id"]),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_run.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["topic_candidate.id"]),
        sa.ForeignKeyConstraint(["target_topic_id"], ["topic.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_build_run_id", "candidate_id", name="uq_topic_merge_candidate"),
    )


def downgrade() -> None:
    op.drop_table("topic_merge_decision")
    op.drop_table("topic_event")
    op.drop_index("ix_topic_plan_status", table_name="topic")
    op.drop_table("topic")
    op.drop_index("ix_topic_candidate_run_batch", table_name="topic_candidate")
    op.drop_table("topic_candidate")
    op.drop_table("topic_build_run")
