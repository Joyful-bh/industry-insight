"""Use Bailian for new model runs.

Revision ID: 0003_bailian
Revises: 0002_run_event
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_bailian"
down_revision: str | None = "0002_run_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("model_run", "provider", server_default="bailian")


def downgrade() -> None:
    op.alter_column("model_run", "provider", server_default="zhipu")
