from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from track_insight.core.errors import ModelCallError
from track_insight.discovery.search import SearchService
from track_insight.infrastructure.jobs import complete_job, fail_job
from track_insight.infrastructure.models import (
    Base,
    Job,
    ResearchPlan,
    ResearchWorkPackage,
    SearchTask,
)
from track_insight.settings import load_poc_config


@pytest.fixture
def search_tmp_path():
    with TemporaryDirectory(prefix="search-concurrency-", dir=".") as directory:
        yield Path(directory)


@pytest.mark.parametrize("workers", [1, 4])
def test_search_packages_concurrent_and_resumable(search_tmp_path, monkeypatch, workers):
    engine = create_engine(f"sqlite:///{search_tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    config.bailian.max_concurrency = workers
    barrier = Barrier(workers)
    lock = Lock()
    active = peak = 0
    active_packages = set()
    sessions = set()
    calls = []
    failed_once = False

    def fake_task(session, plan, package, task, *, job, **kwargs):
        nonlocal active, peak, failed_once
        task.status = "running"
        session.commit()
        with lock:
            assert package.id not in active_packages
            active_packages.add(package.id)
            sessions.add(id(session))
            active += 1
            peak = max(peak, active)
            calls.append(task.id)
        if workers > 1 and len(calls) <= 4:
            barrier.wait(timeout=10)
        with lock:
            active -= 1
            active_packages.remove(package.id)
            should_fail = not failed_once and package.sequence_no == 1
            if should_fail:
                failed_once = True
        if should_fail:
            task.status = "failed_retryable"
            fail_job(
                session,
                job,
                error_code="network",
                error_message="test",
                retryable=True,
                retry_delay_seconds=0,
            )
            raise ModelCallError("test", retryable=True)
        task.status = "completed"
        complete_job(session, job)

    service = SearchService(client=object(), config=config)
    monkeypatch.setattr(service, "_run_task", fake_task)
    with Session(engine) as session:
        plan = ResearchPlan(
            config_version="test",
            regions=["北京市"],
            industry_scopes=["制造业"],
            start_date=date(2025, 1, 1),
            end_date=date(2026, 1, 1),
        )
        session.add(plan)
        session.flush()
        for index in range(4):
            package = ResearchWorkPackage(
                research_plan_id=plan.id,
                sequence_no=index + 1,
                objective="测试",
                regions=["北京市"],
                industry_scopes=["制造业"],
                signal_types=["policy"],
                source_classes=["government"],
                max_candidates=10,
            )
            session.add(package)
            session.flush()
            for number in range(2):
                task = SearchTask(
                    research_work_package_id=package.id,
                    query=f"{index}-{number}",
                    purpose="测试",
                    target_regions=["北京市"],
                    target_industries=["制造业"],
                    target_signal_types=["policy"],
                    target_source_classes=["government"],
                    query_fingerprint=f"{index}-{number}",
                )
                session.add(task)
                session.flush()
                session.add(
                    Job(
                        job_type="search",
                        object_type="search_task",
                        object_id=str(task.id),
                        idempotency_key=f"{index}-{number}",
                        input_fingerprint="test",
                        processor_version="test",
                    )
                )
        session.commit()
        result = service.run(session, plan_id=plan.id)
        assert result.processed_searches == 7
        assert result.failed_searches == 1
        assert peak == workers
        if workers > 1:
            assert len(sessions) == 4
        resumed = service.run(session, plan_id=plan.id, resume=True)
        assert resumed.processed_searches == 1
        assert resumed.skipped_searches == 7
        assert resumed.failed_searches == 0
        assert len(calls) == 9
        assert all(t.status == "completed" for t in session.scalars(select(SearchTask)))
    engine.dispose()
