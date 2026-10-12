import pytest

from track_insight.core.errors import ContractError
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput
from track_insight.tracks.support import SupportingRecoveryOutput, apply_support_recovery


def tracks():
    return IdentifiedTrackBuildOutput.model_validate(
        {
            "tracks": [
                {
                    "name": "机器人制造",
                    "offering_family": "机器人",
                    "supplier_role": "MANUFACTURE_PRODUCT",
                    "refs": ["T1"],
                    "supporting_refs": [],
                }
            ],
            "excluded_refs": ["T2", "T3"],
        }
    )


TOPICS = [
    {"topic_ref": "T2", "representative_events": [{"event_id": "E1", "summary": "机器人示范应用"}]}
]


def assignment(track_ref="R0", phrase="机器人"):
    return {
        "topic_ref": "T2",
        "track_ref": track_ref,
        "evidence_event_id": "E1",
        "target_phrase": phrase,
    }


def test_recovers_support_without_creating_or_changing_core():
    output = tracks()
    recovery = SupportingRecoveryOutput.model_validate(
        {
            "assignments": [assignment()],
            "excluded_refs": ["T3"],
        }
    )
    apply_support_recovery(output, recovery, {"T2", "T3"}, TOPICS)
    assert output.tracks[0].refs == ["T1"]
    assert output.tracks[0].supporting_refs == ["T2"]
    assert output.excluded_refs == ["T3"]


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"assignments": [assignment("R99")]}, {"T2"}),
        ({"assignments": [], "excluded_refs": []}, {"T2"}),
        ({"assignments": [], "excluded_refs": ["T2", "T2"]}, {"T2"}),
        ({"assignments": [], "excluded_refs": ["T1"]}, {"T1"}),
    ],
)
def test_rejects_invalid_recovery_atomically(payload, expected):
    output = tracks()
    with pytest.raises(ContractError):
        apply_support_recovery(
            output, SupportingRecoveryOutput.model_validate(payload), expected, TOPICS
        )
    assert output.tracks[0].supporting_refs == []


@pytest.mark.parametrize("phrase", ["制造", "光伏", "机器人的应用"])
def test_unsupported_anchor_is_left_unassigned(phrase):
    output = tracks()
    recovery = SupportingRecoveryOutput(assignments=[assignment(phrase=phrase)])
    apply_support_recovery(output, recovery, {"T2"}, TOPICS)
    assert output.tracks[0].supporting_refs == []
    assert "T2" in output.excluded_refs
