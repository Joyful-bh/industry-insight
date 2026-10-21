"""Agent driven POC foundation.

Revision ID: 0001_agent_poc
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_agent_poc"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("counters", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_run"),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(100), nullable=False),
        sa.Column("object_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "scheduled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("output", sa.JSON()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"], ["pipeline_run.id"], name="fk_job_pipeline_run_id_pipeline_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
        sa.UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
    )
    op.create_index("ix_job_claim", "job", ["status", "scheduled_at", "priority"])
    op.create_table(
        "model_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid()),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("request_id", sa.String(200)),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("tool_config", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("raw_response", sa.JSON()),
        sa.Column("structured_output", sa.JSON()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name="fk_model_run_pipeline_run_id_pipeline_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_run"),
    )
    op.create_table(
        "research_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("config_version", sa.String(100), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industry_scopes", sa.JSON(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("coverage_audit", sa.JSON()),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["model_run.id"], name="fk_research_plan_model_run_id_model_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_plan"),
    )
    op.create_table(
        "research_work_package",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_plan_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("industry_scopes", sa.JSON(), nullable=False),
        sa.Column("signal_types", sa.JSON(), nullable=False),
        sa.Column("source_classes", sa.JSON(), nullable=False),
        sa.Column("max_candidates", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_plan_id"],
            ["research_plan.id"],
            name="fk_research_work_package_research_plan_id_research_plan",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_work_package"),
        sa.UniqueConstraint(
            "research_plan_id", "sequence_no", name="uq_research_work_package_research_plan_id"
        ),
    )
    op.create_table(
        "search_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("research_work_package_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("target_regions", sa.JSON(), nullable=False),
        sa.Column("target_industries", sa.JSON(), nullable=False),
        sa.Column("target_signal_types", sa.JSON(), nullable=False),
        sa.Column("target_source_classes", sa.JSON(), nullable=False),
        sa.Column("query_fingerprint", sa.String(64), nullable=False),
        sa.Column("model_run_id", sa.Uuid()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("completion_reason", sa.String(100)),
        sa.ForeignKeyConstraint(
            ["model_run_id"], ["model_run.id"], name="fk_search_task_model_run_id_model_run"
        ),
        sa.ForeignKeyConstraint(
            ["research_work_package_id"],
            ["research_work_package.id"],
            name="fk_search_task_research_work_package_id_research_work_package",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_search_task"),
        sa.UniqueConstraint(
            "research_work_package_id",
            "query_fingerprint",
            name="uq_search_task_research_work_package_id",
        ),
    )
    op.create_table(
        "search_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("search_task_id", sa.Uuid(), nullable=False),
        sa.Column("refer", sa.String(200), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text()),
        sa.Column("media", sa.String(300)),
        sa.Column("publish_date", sa.String(100)),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("url_type", sa.String(32), nullable=False),
        sa.Column("source_class", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_task_id"],
            ["search_task.id"],
            name="fk_search_result_search_task_id_search_task",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_search_result"),
        sa.UniqueConstraint("search_task_id", "refer", name="uq_search_result_search_task_id"),
    )
    op.create_table(
        "url_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_type", sa.String(32), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("snippet", sa.Text()),
        sa.Column("source_domain", sa.String(300), nullable=False),
        sa.Column("source_class", sa.String(50), nullable=False),
        sa.Column("possible_published_at", sa.String(100)),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_url_candidate"),
        sa.UniqueConstraint("canonical_url", name="uq_url_candidate_canonical_url"),
    )
    op.create_table(
        "url_discovery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("research_work_package_id", sa.Uuid(), nullable=False),
        sa.Column("search_task_id", sa.Uuid(), nullable=False),
        sa.Column("search_result_id", sa.Uuid(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["research_work_package_id"],
            ["research_work_package.id"],
            name="fk_url_discovery_research_work_package_id_research_work_package",
        ),
        sa.ForeignKeyConstraint(
            ["search_result_id"],
            ["search_result.id"],
            name="fk_url_discovery_search_result_id_search_result",
        ),
        sa.ForeignKeyConstraint(
            ["search_task_id"],
            ["search_task.id"],
            name="fk_url_discovery_search_task_id_search_task",
        ),
        sa.ForeignKeyConstraint(
            ["url_candidate_id"],
            ["url_candidate.id"],
            name="fk_url_discovery_url_candidate_id_url_candidate",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_url_discovery"),
        sa.UniqueConstraint(
            "url_candidate_id",
            "research_work_package_id",
            "search_task_id",
            name="uq_url_discovery_url_candidate_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("url_discovery")
    op.drop_table("url_candidate")
    op.drop_table("search_result")
    op.drop_table("search_task")
    op.drop_table("research_work_package")
    op.drop_table("research_plan")
    op.drop_table("model_run")
    op.drop_index("ix_job_claim", table_name="job")
    op.drop_table("job")
    op.drop_table("pipeline_run")
