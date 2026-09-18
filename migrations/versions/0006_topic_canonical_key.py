"""Make Topic canonical keys unique within a research plan.

Revision ID: 0006_topic_key
Revises: 0005_event_topic
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_topic_key"
down_revision: str | None = "0005_event_topic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    constraints = {
        item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("topic")
    }
    if "uq_topic_plan_canonical_key" not in constraints:
        op.create_unique_constraint(
            "uq_topic_plan_canonical_key",
            "topic",
            ["research_plan_id", "canonical_key"],
        )


def downgrade() -> None:
    constraints = {
        item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("topic")
    }
    if "uq_topic_plan_canonical_key" in constraints:
        op.drop_constraint("uq_topic_plan_canonical_key", "topic", type_="unique")
