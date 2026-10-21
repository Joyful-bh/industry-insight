import json
import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from track_insight.core.errors import ContractError, ModelCallError
from track_insight.infrastructure.bailian.contracts import ModelRunResult
from track_insight.infrastructure.models import (
    Base,
    Event,
    Job,
    ResearchPlan,
    ResearchWorkPackage,
    Topic,
    TopicBuildRun,
    TopicCandidate,
)
from track_insight.settings import load_poc_config
from track_insight.topics import service as topic_service
from track_insight.topics.contracts import CompactTopicGenerationOutput


class FailsOnceClient:
    def __init__(self):
        self.calls = []
        self.failed = False

    def complete_structured(self, *, messages, model, **kwargs):
        payload = json.loads(messages[1]["content"])
        title = payload["events"][0]["title"]
        self.calls.append(title)
        if title == "second event" and not self.failed:
            self.failed = True
            raise ModelCallError("temporary failure", retryable=True)
        return ModelRunResult(
            model=model,
            raw_response={},
            output=CompactTopicGenerationOutput.model_validate(
                {
                    "candidates": [
                        {
                            "label": title,
                            "definition": (
                                "A specific manufacturing product and its production activities"
                            ),
                            "summary": "A concrete manufacturing event supports this local topic",
                            "event_refs": ["E1"],
                            "confidence": 0.8,
                        }
                    ]
                }
            ),
        )


def test_partial_failure_keeps_snapshot_and_resume_reuses_success(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    config.stage3.event_batch_size = 1
    config.stage3.max_events_per_run = 10
    client = FailsOnceClient()
    service = topic_service.TopicService(client=client, config=config)
    with Session(engine) as session:
        plan = ResearchPlan(
            config_version="test",
            regions=["test"],
            industry_scopes=["manufacturing"],
            start_date=date(2025, 1, 1),
            end_date=date(2026, 1, 1),
        )
        session.add(plan)
        session.flush()
        work_package = ResearchWorkPackage(
            research_plan_id=plan.id,
            sequence_no=1,
            objective="manufacturing",
            regions=["test"],
            industry_scopes=["manufacturing"],
            signal_types=["industry"],
            source_classes=["government"],
            max_candidates=10,
        )
        session.add(work_package)
        events = [
            Event(
                page_analysis_id=uuid.uuid4(),
                sequence_no=1,
                event_type="project_progress",
                event_status="completed",
                title=title,
                summary=title,
                signal_date=None,
                date_precision="unknown",
                regions=["test"],
                industries=["manufacturing"],
                industry_objects=[title],
                topic_hint=title,
                entities=[],
                chain_roles=[],
                smb_relevance="unclear",
                smb_reason="",
                confidence=0.8,
                event_fingerprint=str(uuid.uuid4()).replace("-", ""),
            )
            for title in ("first event", "second event")
        ]
        session.add_all(events)
        previous = TopicBuildRun(
            research_plan_id=plan.id,
            status="completed",
            input_fingerprint="old",
            processor_version="old",
            generation_prompt_version="old",
            reconciliation_prompt_version="old",
            model="old",
        )
        session.add(previous)
        session.flush()
        old_topic = Topic(
            research_plan_id=plan.id,
            created_by_run_id=previous.id,
            updated_by_run_id=previous.id,
            canonical_key="old_topic",
            label="old product",
            definition="Previously published product",
            aliases=[],
            keywords=[],
            regions=["test"],
            industries=["manufacturing"],
            summary="Previously published product",
            event_count=1,
            confidence=0.8,
            status="candidate",
            topic_fingerprint="old_topic",
        )
        session.add(old_topic)
        session.commit()
        plan_id = plan.id
        monkeypatch.setattr(topic_service, "_select_events", lambda *args: events)
        monkeypatch.setattr(
            topic_service, "_events_by_work_package", lambda *args: [(work_package, events)]
        )

        first = service.build(session, plan_id=plan_id)
        assert first["status"] == "failed_retryable"
        assert first["error_code"] == "partial_batch_failure"
        assert session.scalar(select(func.count(Topic.id)).where(Topic.status == "candidate")) == 1
        assert session.get(Topic, old_topic.id).status == "candidate"
        assert session.scalar(select(func.count(Topic.id)).where(Topic.status == "building")) == 1
        assert session.scalar(select(func.count(TopicCandidate.id))) == 1
        assert client.calls == ["first event", "second event"]
        jobs = list(session.scalars(select(Job).where(Job.job_type == "topic_generate")))
        assert sorted(job.status for job in jobs) == ["completed", "failed_retryable"]

        second = service.build(session, plan_id=plan_id, resume=True)
        assert second["run_id"] == first["run_id"]
        assert second["status"] == "completed"
        assert client.calls == ["first event", "second event", "second event"]
        assert session.scalar(select(func.count(Topic.id)).where(Topic.status == "candidate")) == 2
        assert session.get(Topic, old_topic.id).status == "archived"
        assert session.scalar(select(func.count(TopicCandidate.id))) == 2
        assert service.build(session, plan_id=plan_id)["resumed"] is True
        assert len(client.calls) == 3
    engine.dispose()


def test_full_build_rejects_a_silent_event_limit(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    config.stage3.max_events_per_run = 1
    event = Event(id=uuid.uuid4(), confidence=0.8)
    monkeypatch.setattr(topic_service, "_select_events", lambda *args: [event])
    monkeypatch.setattr(topic_service, "_count_eligible_events", lambda *args: 2)
    with Session(engine) as session:
        with pytest.raises(ContractError, match="exceed the configured limit"):
            topic_service.TopicService(client=object(), config=config).build(
                session, plan_id=uuid.uuid4()
            )
        assert session.scalar(select(func.count(TopicBuildRun.id))) == 0
    engine.dispose()
