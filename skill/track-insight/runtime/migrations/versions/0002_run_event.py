"""Add structured run events.

Revision ID: 0002_run_event
Revises: 0001_agent_poc
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_run_event"
down_revision: str | None = "0001_agent_poc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100)),
        sa.Column("entity_id", sa.String(200)),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name="fk_run_event_job_id_job"),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["model_run.id"], name="fk_run_event_model_run_id_model_run"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name="fk_run_event_pipeline_run_id_pipeline_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_event"),
    )
    op.create_index(
        "ix_run_event_pipeline_created", "run_event", ["pipeline_run_id", "created_at"]
    )
    op.create_index("ix_run_event_entity", "run_event", ["entity_type", "entity_id"])
    op.create_index("ix_run_event_type_created", "run_event", ["event_type", "created_at"])


def downgrade() -> None:
    op.drop_table("run_event")
