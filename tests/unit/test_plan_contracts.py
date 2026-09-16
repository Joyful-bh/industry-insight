from datetime import date

import pytest
from pydantic import ValidationError

from track_insight.planning.contracts import ResearchScope, WorkPackageProposal


def test_research_scope_rejects_reverse_date_range() -> None:
    with pytest.raises(ValidationError):
        ResearchScope(
            regions=["北京市"],
            industry_scopes=["制造业"],
            start_date=date(2026, 1, 1),
            end_date=date(2025, 1, 1),
        )


def test_work_package_rejects_duplicate_queries() -> None:
    task = {
        "query": "北京 制造业 投产",
        "purpose": "发现制造业投产事件",
        "target_regions": ["北京市"],
        "target_industries": ["制造业"],
        "target_signal_types": ["投产"],
        "target_source_classes": ["government"],
    }
    with pytest.raises(ValidationError):
        WorkPackageProposal(
            objective="发现北京制造业项目落地",
            regions=["北京市"],
            industry_scopes=["制造业"],
            signal_types=["投产"],
            source_classes=["government"],
            search_tasks=[task, task],
        )
