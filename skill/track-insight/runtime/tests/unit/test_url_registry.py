from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from track_insight.discovery.url_registry import canonicalize_url, register_candidate
from track_insight.infrastructure.models import (
    Base,
    ResearchPlan,
    ResearchWorkPackage,
    SearchResult,
    SearchTask,
    UrlCandidate,
    UrlDiscovery,
)


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    value = canonicalize_url("HTTPS://Example.COM:443/a?id=2&utm_source=test&b=1#section")
    assert value == "https://example.com/a?b=1&id=2"


def test_register_candidate_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        plan = ResearchPlan(
            status="completed",
            config_version="test",
            regions=["北京市"],
            industry_scopes=["制造业"],
            start_date=date(2024, 1, 1),
            end_date=date(2026, 1, 1),
        )
        session.add(plan)
        session.flush()
        package = ResearchWorkPackage(
            research_plan_id=plan.id,
            sequence_no=1,
            status="pending",
            objective="测试制造业项目发现",
            regions=["北京市"],
            industry_scopes=["制造业"],
            signal_types=["投产"],
            source_classes=["government"],
            max_candidates=10,
        )
        session.add(package)
        session.flush()
        task = SearchTask(
            research_work_package_id=package.id,
            status="completed",
            query="北京 制造业 投产",
            purpose="发现制造业投产事件",
            target_regions=["北京市"],
            target_industries=["制造业"],
            target_signal_types=["投产"],
            target_source_classes=["government"],
            query_fingerprint="a" * 64,
        )
        session.add(task)
        session.flush()
        result = SearchResult(
            search_task_id=task.id,
            refer="ref_1",
            title="项目投产",
            url="https://EXAMPLE.com/a?utm_source=x&id=1#top",
            snippet="某项目投产",
            decision="keep",
            url_type="content_page",
            source_class="government",
            reason="包含项目投产信息",
            raw_payload={},
        )
        session.add(result)
        session.flush()
        first = register_candidate(
            session,
            work_package_id=package.id,
            search_task_id=task.id,
            search_result=result,
        )
        second = register_candidate(
            session,
            work_package_id=package.id,
            search_task_id=task.id,
            search_result=result,
        )
        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(UrlCandidate)) == 1
        assert session.scalar(select(func.count()).select_from(UrlDiscovery)) == 1
