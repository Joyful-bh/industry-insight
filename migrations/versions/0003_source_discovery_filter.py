"""add source discovery filter

Revision ID: 0003_source_discovery_filter
Revises: 0002_relevance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_source_discovery_filter"
down_revision: str | None = "0002_relevance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("discovery_filter", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column("sources", "discovery_filter", server_default=None)


def downgrade() -> None:
    op.drop_column("sources", "discovery_filter")
