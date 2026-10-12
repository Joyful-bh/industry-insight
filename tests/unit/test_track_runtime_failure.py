import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from track_insight.infrastructure.models import (
    Base,
    ModelRun,
    PipelineRun,
    ResearchPlan,
    Topic,
    TrackBuildRun,
)
from track_insight.settings import load_poc_config
from track_insight.tracks.service import close_interrupted_build


def test_formal_entry_records_runtime_failure_and_resumes_without_repeating_success(monkeypatch):
    import track_insight.tracks.service as service
    from scripts.run_track_full import monitor_client
    from track_insight.tracks.preparation import Observation

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    config.stage4.generation_batch_topics = 1

    class Client:
        calls = 0

        def embed_texts(self, *args, **kwargs):
            raise AssertionError("No real embeddings in this test")

        def complete_structured(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("unexpected batch failure")
            ref = "O1" if self.calls == 1 else "O2"
            return SimpleNamespace(
                output=kwargs["output_model"].model_validate(
                    {"tracks": [], "excluded_refs": [ref]}
                ),
                request_id="test",
                usage={},
                raw_response={},
                call_count=1,
            )

    def locked(*args, **kwargs):
        raise PermissionError("monitoring locked")

    monkeypatch.setattr("scripts.run_track_full.append_call", locked)
    from unittest.mock import MagicMock

    active = Client()
    state = dict(
        structured_requests=0,
        completed_calls_by_task={},
        structured_usage={k: 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
    )
    monitor_client(active, config, MagicMock(), state, locked)
    with Session(engine) as session:
        plan = ResearchPlan(
            config_version="test",
            regions=["全国"],
            industry_scopes=["制造业"],
            start_date=date(2025, 10, 9),
            end_date=date(2026, 10, 9),
        )
        session.add(plan)
        session.flush()
        topics = [
            Topic(
                research_plan_id=plan.id,
                created_by_run_id=uuid.uuid4(),
                updated_by_run_id=uuid.uuid4(),
                canonical_key=str(i),
                label=f"主题{i}",
                definition="制造产品",
                summary="制造产品",
                confidence=0.8,
                topic_fingerprint=str(i),
                status="candidate",
            )
            for i in range(2)
        ]
        session.add_all(topics)
        session.commit()
        observations = [
            Observation(f"O{i + 1}", [t.id], set(), set(), set(), {"topic_ref": f"O{i + 1}"})
            for i, t in enumerate(topics)
        ]
        monkeypatch.setattr(service, "prepare_observations", lambda *args: observations)
        monkeypatch.setattr(service, "cached_embeddings", lambda *args: ([[1, 0], [0, 1]], {}))
        monkeypatch.setattr(service, "coalesce_compatible_names", lambda o, v: (o, v))
        monkeypatch.setattr(
            service, "semantic_groups", lambda *args, **kwargs: [[o] for o in observations]
        )
        builder = service.TrackService(client=active, config=config)
        monkeypatch.setattr(builder, "_recover_supporting_topics", lambda *args: None)
        monkeypatch.setattr(builder, "_define_tracks", lambda *args: None)
        monkeypatch.setattr(builder, "_normalize_markets", lambda *args: {})
        with pytest.raises(RuntimeError, match="unexpected batch failure"):
            builder.build(session, plan_id=plan.id)
        run = session.scalar(select(TrackBuildRun))
        assert run.status == "failed_retryable"
        assert session.get(PipelineRun, run.pipeline_run_id).status == "failed_retryable"
        children = list(
            session.scalars(
                select(ModelRun)
                .where(ModelRun.task_type == "track_generation_semantic_group")
                .order_by(ModelRun.created_at)
            )
        )
        assert sorted(c.status for c in children) == ["completed", "failed_retryable"]
        result = builder.build(session, plan_id=plan.id)
        assert result["status"] == "completed"
        assert result["track_build_run_id"] == str(run.id)
        assert active.calls == 3  # The first successful batch was not called again.
        session.refresh(run)
        assert run.error_code is None and run.error_message is None
    engine.dispose()


@pytest.mark.parametrize(
    "error", [PermissionError("locked"), RuntimeError("unexpected"), KeyboardInterrupt()]
)
def test_runtime_failure_closes_current_work_preserves_cache_and_rolls_back_unpublished(error):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        pipelines = [PipelineRun(run_type="topic_to_track", status="running") for _ in range(2)]
        session.add_all(pipelines)
        session.flush()
        runs = [
            TrackBuildRun(
                research_plan_id=uuid.uuid4(),
                pipeline_run_id=p.id,
                input_fingerprint=str(i),
                processor_version="test",
                prompt_version="test",
                model="test",
                status="running",
            )
            for i, p in enumerate(pipelines)
        ]
        children = [
            ModelRun(
                pipeline_run_id=pipelines[p].id,
                task_type="track_generation_semantic_group",
                status=status,
                model="test",
                prompt_version="test",
                input_fingerprint=str(i),
                started_at=datetime.now(UTC),
                structured_output={"tracks": []},
            )
            for i, (p, status) in enumerate([(0, "completed"), (0, "running"), (1, "running")])
        ]
        session.add_all([*runs, *children])
        session.commit()
        run_id, pipeline_id = runs[0].id, pipelines[0].id
        # Simulate uncommitted business changes in the failed transaction.
        runs[0].track_count = 999
        children[0].structured_output = {"bad": "uncommitted"}
        assert close_interrupted_build(session, run_id, pipeline_id, error)
        session.commit()
        assert runs[0].status == pipelines[0].status == children[1].status == "failed_retryable"
        assert runs[0].track_count == 0
        assert runs[0].error_code == "track_runtime_error"
        assert runs[0].finished_at is not None
        assert children[0].status == "completed"
        assert children[0].structured_output == {"tracks": []}
        assert runs[1].status == pipelines[1].status == children[2].status == "running"
        assert not close_interrupted_build(session, run_id, pipeline_id, error)
        session.rollback()
        assert len(list(session.scalars(select(ModelRun)))) == 3
    engine.dispose()


def test_cleanup_cannot_close_a_run_now_owned_by_another_pipeline():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = TrackBuildRun(
            research_plan_id=uuid.uuid4(),
            pipeline_run_id=uuid.uuid4(),
            input_fingerprint="test",
            processor_version="test",
            prompt_version="test",
            model="test",
            status="running",
        )
        session.add(run)
        session.commit()
        assert not close_interrupted_build(session, run.id, uuid.uuid4(), PermissionError("locked"))
        assert run.status == "running"
    engine.dispose()
