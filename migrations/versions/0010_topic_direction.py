"""Add Topic-to-Direction build artifacts.

Revision ID: 0010_topic_direction
Revises: 0009_event_signal_semantics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_topic_direction"
down_revision: str | None = "0009_event_signal_semantics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    empty_list = sa.text("'[]'::jsonb")
    empty_object = sa.text("'{}'::jsonb")
    op.create_table(
        "direction_build_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("processor_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column(
            "topic_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=empty_list,
            nullable=False,
        ),
        sa.Column(
            "discarded_topic_ids", postgresql.JSONB(astext_type=sa.Text()),
            server_default=empty_list, nullable=False,
        ),
        sa.Column("topic_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("direction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assigned_topic_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "statistics", postgresql.JSONB(astext_type=sa.Text()), server_default=empty_object,
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_plan_id", "input_fingerprint", "processor_version",
            name="uq_direction_build_run_input",
        ),
    )
    op.create_table(
        "direction",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("direction_build_run_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("direction_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["direction_build_run_id"], ["direction_build_run.id"]),
        sa.ForeignKeyConstraint(["research_plan_id"], ["research_plan.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("direction_build_run_id", "canonical_key", name="uq_direction_run_key"),
    )
    op.create_index(
        "ix_direction_plan_run",
        "direction",
        ["research_plan_id", "direction_build_run_id"],
    )
    op.create_table(
        "direction_topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("direction_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("direction_build_run_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["direction_build_run_id"], ["direction_build_run.id"]),
        sa.ForeignKeyConstraint(["direction_id"], ["direction.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("direction_id", "topic_id", name="uq_direction_topic"),
    )
    op.create_index("ix_direction_topic_build", "direction_topic", ["direction_build_run_id"])


def downgrade() -> None:
    op.drop_index("ix_direction_topic_build", table_name="direction_topic")
    op.drop_table("direction_topic")
    op.drop_index("ix_direction_plan_run", table_name="direction")
    op.drop_table("direction")
    op.drop_table("direction_build_run")
