import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from track_insight.core.errors import ContractError
from track_insight.infrastructure.models import (
    Job,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchTask,
)
from track_insight.infrastructure.models.base import Base
from track_insight.planning.contracts import PlanningOutput, ResearchScope
from track_insight.planning.service import PlanningService, validate_plan
from track_insight.settings import load_poc_config


def test_saved_plan_import_is_valid_and_idempotent_without_model_client():
    root = Path("data/planning_quality/national_coverage/run_2")
    if not (root / "plan.json").exists():
        pytest.skip("historical local plan is not available")
    output = PlanningOutput.model_validate_json((root / "plan.json").read_text(encoding="utf-8"))
    scope = ResearchScope.model_validate(
        json.loads((root / "metrics.json").read_text(encoding="utf-8"))["scope"]
    )
    config = load_poc_config(Path("config/national.yaml"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        service = PlanningService(config=config)
        plan = service.import_plan(
            session,
            scope,
            output,
            source_path=root / "plan.json",
            metadata_path=root / "metrics.json",
        )
        session.commit()
        assert validate_plan(session, plan.id, config).valid
        again = service.import_plan(
            session,
            scope,
            output,
            source_path=root / "plan.json",
            metadata_path=root / "metrics.json",
        )
        assert again.id == plan.id
        for model, count in [
            (ResearchPlan, 1),
            (ResearchWorkPackage, 32),
            (SearchTask, 187),
            (Job, 187),
            (PipelineRun, 1),
        ]:
            assert session.scalar(select(func.count()).select_from(model)) == count
        assert plan.model_run_id is None
        assert all(status == "pending" for status in session.scalars(select(SearchTask.status)))
        invalid = scope.model_copy(update={"regions": scope.regions[1:2]})
        with pytest.raises(ContractError):
            service.import_plan(
                session,
                invalid,
                output,
                source_path=root / "plan.json",
                metadata_path=root / "metrics.json",
            )
