import uuid
from datetime import date

import pytest

from track_insight.core.errors import ContractError
from track_insight.planning.contracts import (
    CompactPlanningOutput,
    ResearchScope,
    expand_planning_output,
)
from track_insight.topics.contracts import CompactTopicGenerationOutput, expand_topic_output


def topic_output(refs):
    return CompactTopicGenerationOutput.model_validate(
        {
            "candidates": [
                {
                    "label": "显示面板",
                    "definition": "面向显示面板生产制造的产业方向",
                    "summary": "多项产业政策支持显示面板制造发展",
                    "event_refs": refs,
                    "confidence": 0.8,
                }
            ]
        }
    )


def test_topic_refs_restore_ids_and_generate_stable_keys():
    event_id = uuid.uuid4()
    compact = topic_output(["E1", "E1"])
    result = expand_topic_output(compact, {"E1": event_id})
    assert result.candidates[0].event_ids == [event_id]
    assert result == expand_topic_output(compact, {"E1": event_id})
    assert result.candidates[0].candidate_key.startswith("topic_")
    assert "candidate_key" not in compact.candidates[0].model_dump()
    assert "event_ids" not in compact.candidates[0].model_dump()


def test_unknown_topic_ref_is_rejected():
    with pytest.raises(ContractError):
        expand_topic_output(topic_output(["E2"]), {"E1": uuid.uuid4()})


def test_empty_topics_and_duplicate_candidates_keep_valid_contract():
    assert expand_topic_output(CompactTopicGenerationOutput(), {}).candidates == []
    compact = topic_output(["E1"])
    compact.candidates.append(compact.candidates[0])
    result = expand_topic_output(compact, {"E1": uuid.uuid4()})
    assert len({c.candidate_key for c in result.candidates}) == 2


def planning_output(refs):
    return CompactPlanningOutput.model_validate(
        {
            "work_packages": [
                {
                    "region_ref": ref,
                    "search_tasks": [
                        {
                            "query": "制造业政策与产业项目",
                            "purpose": "查找制造业政策原文",
                            "target_industries": ["电子制造"],
                            "target_signal_types": ["policy"],
                            "target_source_classes": ["government"],
                        }
                    ],
                }
                for ref in refs
            ]
        }
    )


def scope():
    return ResearchScope(
        regions=["江苏省"],
        industry_scopes=["制造业"],
        start_date=date(2025, 10, 9),
        end_date=date(2026, 10, 9),
    )


def test_planning_fixed_fields_are_filled_without_overwriting_semantics():
    compact = planning_output(["G1"])
    package = expand_planning_output(compact, scope(), {"G1": "江苏省"}).work_packages[0]
    assert package.regions == ["江苏省"]
    assert package.industry_scopes == ["制造业"]
    assert package.signal_types == ["policy"]
    assert package.source_classes == package.search_tasks[0].target_source_classes
    assert package.search_tasks[0].target_regions == ["江苏省"]
    assert package.search_tasks[0].target_industries == ["电子制造"]
    assert "target_regions" not in compact.work_packages[0].search_tasks[0].model_dump()
    assert set(compact.work_packages[0].model_dump()) == {"region_ref", "search_tasks"}


@pytest.mark.parametrize(
    "refs,mapping",
    [
        (["G2"], {"G1": "江苏省"}),
        (["G1", "G1"], {"G1": "江苏省"}),
        (["G1"], {"G1": "青海省"}),
    ],
)
def test_invalid_planning_refs_are_rejected(refs, mapping):
    with pytest.raises(ContractError):
        expand_planning_output(planning_output(refs), scope(), mapping)
