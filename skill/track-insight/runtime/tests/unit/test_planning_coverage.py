from datetime import date
from types import SimpleNamespace

import pytest

from track_insight.core.errors import ContractError
from track_insight.planning.contracts import PlanningOutput, ResearchScope
from track_insight.planning.coverage import (
    apply_query_scope,
    expand_scope,
    generate_covered_plan,
    missing_regions,
    query_is_regional,
    select_supplement_regions,
    time_windows,
    validate_batch,
)
from track_insight.settings import load_poc_config


def scope(regions=None):
    return ResearchScope(
        regions=regions or ["北京市"],
        industry_scopes=["制造业"],
        start_date=date(2025, 10, 9),
        end_date=date(2026, 10, 9),
    )


def output(s):
    queries = [f"北京制造业政策 {w}" for w in time_windows(s)] + [
        "北京2026制造业项目投产",
        "北京2025装备制造产业化",
    ]
    return PlanningOutput.model_validate(
        {
            "work_packages": [
                {
                    "objective": "调查本地制造业政策与产业化活动",
                    "regions": ["北京市"],
                    "industry_scopes": ["制造业"],
                    "signal_types": ["policy"],
                    "source_classes": ["government"],
                    "search_tasks": [
                        {
                            "query": q,
                            "purpose": "获取制造业政策和产业活动原文",
                            "target_regions": ["北京市"],
                            "target_industries": ["制造业"],
                            "target_signal_types": ["policy"],
                            "target_source_classes": ["government"],
                        }
                        for q in queries
                    ],
                }
            ]
        }
    )


def test_national_scope_expands_to_32_independent_targets():
    assert len(expand_scope(scope(["全国"])).regions) == 32
    assert expand_scope(scope()).regions == ["北京市"]


def test_labels_without_regional_query_do_not_count_as_coverage():
    out = output(scope())
    for task in out.work_packages[0].search_tasks:
        task.query = "2026全国制造业政策"
    assert missing_regions(out, ["北京市"]) == ["北京市"]
    with pytest.raises(ContractError):
        validate_batch(out, ["北京市"], scope(), 6)


def test_each_time_segment_must_be_queried():
    out = output(scope())
    out.work_packages[0].search_tasks[0].query = "北京2026制造业发展规划"
    with pytest.raises(ContractError):
        validate_batch(out, ["北京市"], scope(), 6)


def test_valid_plan_and_aliases():
    validate_batch(output(scope()), ["北京市"], scope(), 6)
    assert query_is_regional("2026广西制造业政策", "广西壮族自治区")


def test_query_scope_removes_conflicting_old_dates():
    out = output(scope())
    out.work_packages[0].search_tasks[1].query = "北京2025年10月制造业项目"
    apply_query_scope(out, scope())
    query = out.work_packages[0].search_tasks[1].query
    assert query.startswith(time_windows(scope())[1])
    assert "2025" not in query


def test_single_bounded_repair():
    calls = []
    config = load_poc_config()

    def complete(name, payload):
        calls.append(name)
        out = output(scope())
        if len(calls) == 1:
            out.work_packages[0].regions = ["上海市"]
        return SimpleNamespace(output=out)

    generate_covered_plan(scope(), config, complete)
    assert calls == ["batch_0", "batch_0_repair"]


def test_supplement_bounded_and_does_not_mask_failures_or_budget_stops():
    rows = {
        "北京": {"supplement_reasons": ["no_retained_content"]},
        "上海": {"supplement_reasons": ["no_retained_content"], "failed_searches": 1},
        "江苏": {"supplement_reasons": ["no_retained_content"], "budget_stopped_searches": 2},
    }
    assert select_supplement_regions(rows, 8) == ["北京"]
    assert select_supplement_regions(rows, 0) == []
