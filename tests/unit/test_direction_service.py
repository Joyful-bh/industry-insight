import json
from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import track_insight.directions.service as direction_service
from track_insight.directions.contracts import DirectionGroupingOutput
from track_insight.directions.service import DirectionService
from track_insight.infrastructure.models import (
    Base,
    Direction,
    DirectionBuildRun,
    DirectionTopic,
    PipelineRun,
    ResearchPlan,
    Topic,
    TopicBuildRun,
)
from track_insight.settings import load_poc_config


class FakeDirectionClient:
    def __init__(self):
        self.calls = 0

    def complete_structured(self, *, messages, output_model, model, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        topic_refs = [topic["topic_ref"] for topic in payload["topics"]]
        output = DirectionGroupingOutput.model_validate(
            {
                "directions": [
                    {
                        "name": "动力电池",
                        "definition": "动力电池电芯、模组和电池系统的研发生产",
                        "topic_refs": topic_refs[:2],
                    }
                ],
            }
        )
        return SimpleNamespace(
            request_id="direction-test",
            usage={},
            raw_response={},
            output=output,
        )


def test_direction_build_persists_one_accounting_decision_per_topic(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    config.directions.deduplication_similarity = 1.0
    client = FakeDirectionClient()

    def fake_embeddings(observations, client, model):
        size = len(observations)
        return [[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)], {
            "calls": 0,
            "input_tokens": 0,
        }

    monkeypatch.setattr(direction_service, "cached_embeddings", fake_embeddings)
    with Session(engine) as session:
        topic_pipeline = PipelineRun(run_type="event_to_topic", status="completed")
        session.add(topic_pipeline)
        session.flush()
        plan = ResearchPlan(
            status="completed",
            config_version="test",
            regions=["全国"],
            industry_scopes=["制造业"],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
        session.add(plan)
        session.flush()
        topic_run = TopicBuildRun(
            research_plan_id=plan.id,
            pipeline_run_id=topic_pipeline.id,
            status="completed",
            input_fingerprint="a" * 64,
            processor_version="test",
            generation_prompt_version="test",
            reconciliation_prompt_version="test",
            model="test",
            event_ids=[],
        )
        session.add(topic_run)
        session.flush()
        topics = [
            Topic(
                research_plan_id=plan.id,
                created_by_run_id=topic_run.id,
                updated_by_run_id=topic_run.id,
                canonical_key=f"topic-{index}",
                label=name,
                definition=f"{name}对应的明确产业活动定义",
                summary=f"{name}产业信号",
                confidence=0.9,
                topic_fingerprint=str(index) * 64,
                event_count=1,
            )
            for index, name in enumerate(["动力电池", "固态电池", "园区奖励"], start=1)
        ]
        session.add_all(topics)
        session.commit()

        result = DirectionService(client=client, config=config).build(session, plan_id=plan.id)

        assert result["direction_count"] == 1
        assert result["assigned_topic_count"] == 2
        assert result["discarded_topic_count"] == 1
        assert session.scalar(select(func.count()).select_from(Direction)) == 1
        assert session.scalar(select(func.count()).select_from(DirectionTopic)) == 2
        run = session.scalar(select(DirectionBuildRun))
        assert len(run.discarded_topic_ids) == 1
        assert client.calls == 1

        resumed = DirectionService(client=client, config=config).build(session, plan_id=plan.id)
        assert resumed["resumed"] is True
        assert client.calls == 1

    engine.dispose()
