"""Add signal semantics to events.

Revision ID: 0009_event_signal_semantics
Revises: 0008_track_ecosystem
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_event_signal_semantics"
down_revision: str | None = "0008_track_ecosystem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event",
        sa.Column("event_status", sa.String(length=32), nullable=False, server_default="observed"),
    )
    op.add_column(
        "event",
        sa.Column(
            "industry_objects",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "event",
        sa.Column("topic_hint", sa.String(length=300), nullable=False, server_default=""),
    )
    op.alter_column("event", "event_status", server_default=None)
    op.alter_column("event", "industry_objects", server_default=None)
    op.alter_column("event", "topic_hint", server_default=None)


def downgrade() -> None:
    op.drop_column("event", "topic_hint")
    op.drop_column("event", "industry_objects")
    op.drop_column("event", "event_status")
