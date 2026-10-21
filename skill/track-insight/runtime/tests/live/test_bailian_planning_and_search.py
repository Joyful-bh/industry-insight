import os
from datetime import date

import pytest

from track_insight.discovery.search import SearchService
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import session_scope
from track_insight.planning.contracts import ResearchScope
from track_insight.planning.service import PlanningService
from track_insight.settings import get_settings, load_poc_config


@pytest.mark.live
def test_real_bailian_research_slice() -> None:
    if os.getenv("RUN_LIVE_BAILIAN_TESTS") != "1":
        pytest.skip("set RUN_LIVE_BAILIAN_TESTS=1 to run billable Bailian validation")
    settings = get_settings()
    if settings.bailian_api_key is None:
        pytest.skip("DASHSCOPE_API_KEY is required")
    config = load_poc_config()
    client = BailianClient(
        api_key=settings.bailian_api_key.get_secret_value(),
        base_url=config.bailian.base_url,
        timeout_seconds=config.bailian.timeout_seconds,
        max_retries=config.bailian.max_retries,
    )
    with session_scope() as session:
        plan = PlanningService(client=client, config=config).build(
            session,
            ResearchScope(
                regions=["北京市"],
                industry_scopes=["制造业"],
                start_date=date(2024, 9, 16),
                end_date=date(2026, 9, 16),
            ),
        )
        plan_id = plan.id
    with session_scope() as session:
        summary = SearchService(client=client, config=config).run(
            session,
            plan_id=plan_id,
            max_work_packages=1,
            max_searches=1,
        )
    assert summary.processed_searches == 1
    assert summary.audit.raw_result_count > 0
