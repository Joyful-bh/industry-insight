"""Add meso ecosystem fields to candidate tracks.

Revision ID: 0008_track_ecosystem
Revises: 0007_candidate_track
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_track_ecosystem"
down_revision: str | None = "0007_candidate_track"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    empty_json = sa.text("'[]'::jsonb")
    empty_object = sa.text("'{}'::jsonb")
    for name in (
        "core_products_services",
        "core_company_types",
        "supporting_company_types",
        "shared_demand_drivers",
        "supporting_event_ids",
    ):
        op.add_column(
            "candidate_track",
            sa.Column(
                name,
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=empty_json,
            ),
        )
        op.alter_column("candidate_track", name, server_default=None)
    op.add_column(
        "candidate_track",
        sa.Column(
            "granularity_assessment",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_object,
        ),
    )
    op.alter_column("candidate_track", "granularity_assessment", server_default=None)


def downgrade() -> None:
    op.drop_column("candidate_track", "granularity_assessment")
    op.drop_column("candidate_track", "supporting_event_ids")
    op.drop_column("candidate_track", "shared_demand_drivers")
    op.drop_column("candidate_track", "supporting_company_types")
    op.drop_column("candidate_track", "core_company_types")
    op.drop_column("candidate_track", "core_products_services")
