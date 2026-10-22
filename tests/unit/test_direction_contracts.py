import pytest
from pydantic import ValidationError

from track_insight.core.errors import ContractError
from track_insight.directions.contracts import DirectionDedupOutput, DirectionGroupingOutput
from track_insight.directions.service import _validate_group_output


def test_group_output_allows_reuse_and_unreferenced_topics():
    output = DirectionGroupingOutput.model_validate(
        {
            "directions": [
                {
                    "name": "动力电池",
                    "definition": "动力电池电芯、模组和电池系统的研发生产",
                    "topic_refs": ["T1", "T2"],
                },
                {
                    "name": "电池隔膜",
                    "definition": "用于电池正负极隔离的功能膜材料",
                    "topic_refs": ["T2"],
                },
            ],
        }
    )
    _validate_group_output(output, {"T1", "T2", "T3", "T4"})

    with pytest.raises(ContractError, match="unknown Topics"):
        _validate_group_output(output, {"T1"})


def test_dedup_contract_rejects_reused_direction_refs():
    with pytest.raises(ValidationError, match="cannot appear in multiple"):
        DirectionDedupOutput.model_validate(
            {
                "merge_groups": [
                    {
                        "direction_refs": ["D1", "D2"],
                        "name": "动力电池",
                        "definition": "动力电池产品的研发生产",
                    },
                    {
                        "direction_refs": ["D2", "D3"],
                        "name": "电池材料",
                        "definition": "电池关键材料的研发生产",
                    },
                ]
            }
        )
