from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from track_insight.content.service import PageService
from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.models import (
    Base,
    PageCapture,
    ResearchPlan,
    ResearchWorkPackage,
    SearchResult,
    SearchTask,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.settings import load_poc_config


def test_page_enqueue_batches_new_candidates_without_repeating_the_first_batch() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    with Session(engine) as session:
        plan = ResearchPlan(
            config_version="test",
            regions=["北京市"],
            industry_scopes=["制造业"],
            start_date=date(2024, 1, 1),
            end_date=date(2025, 1, 1),
        )
        session.add(plan)
        session.flush()
        work_package = ResearchWorkPackage(
            research_plan_id=plan.id,
            sequence_no=1,
            objective="制造业政策与产业项目",
            regions=["北京市"],
            industry_scopes=["制造业"],
            signal_types=["policy", "project"],
            source_classes=["government"],
            max_candidates=20,
        )
        session.add(work_package)
        session.flush()
        task = SearchTask(
            research_work_package_id=work_package.id,
            query="北京市制造业政策",
            purpose="发现官方政策",
            target_regions=["北京市"],
            target_industries=["制造业"],
            target_signal_types=["policy"],
            target_source_classes=["government"],
            query_fingerprint="q1",
        )
        session.add(task)
        session.flush()

        for index in range(4):
            result = SearchResult(
                search_task_id=task.id,
                refer=f"ref_{index}",
                url=f"https://example.com/{index}",
                decision="keep",
                url_type="content_page",
                source_class="government",
                reason="测试候选",
                raw_payload={},
            )
            candidate = UrlCandidate(
                canonical_url=f"https://example.com/{index}",
                url_type="content_page",
                source_domain="example.com",
                source_class="government",
                decision="keep",
            )
            session.add_all([result, candidate])
            session.flush()
            session.add(
                UrlDiscovery(
                    url_candidate_id=candidate.id,
                    research_work_package_id=work_package.id,
                    search_task_id=task.id,
                    search_result_id=result.id,
                )
            )
            if index == 0:
                session.add(
                    PageCapture(
                        url_candidate_id=candidate.id,
                        requested_url=candidate.canonical_url,
                        status=TaskStatus.COMPLETED,
                    )
                )
        session.commit()

        service = PageService(config=config)
        first = service.enqueue(session, plan_id=plan.id, limit=2)
        second = service.enqueue(session, plan_id=plan.id, limit=2)

        assert first == {
            "candidate_count": 4,
            "enqueued_count": 2,
            "existing_count": 1,
            "selected_count": 2,
            "recovered_count": 0,
        }
        assert second["candidate_count"] == 4
        assert second["enqueued_count"] == 1
        assert second["existing_count"] == 3
        assert second["selected_count"] == 1
        assert second["recovered_count"] == 0
