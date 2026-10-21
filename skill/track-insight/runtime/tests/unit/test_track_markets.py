import uuid
from types import SimpleNamespace

import pytest

from track_insight.tracks.contracts import CandidateTrackOutput, TrackBuildOutput
from track_insight.tracks.markets import normalize_markets
from track_insight.tracks.service import TrackService


def candidates(second_role="MANUFACTURE_PRODUCT"):
    return [
        CandidateTrackOutput(
            name=name,
            offering_family=name,
            supplier_role=role,
            definition="从事显示面板产品研发生产的制造企业。",
            topic_ids=[uuid.uuid4()],
            supporting_topic_ids=[uuid.uuid4()],
            status="candidate",
            confidence=0.8,
        )
        for name, role in [("OLED显示面板", "MANUFACTURE_PRODUCT"), ("LCD显示面板", second_role)]
    ]


def client(decision="ACCEPT", destination="M1", omit=False):
    def complete(task, payload, schema):
        if task == "catalog":
            if destination in {"KEEP", "EXCLUDE"}:
                return schema.model_validate({"markets": []})
            return schema.model_validate(
                {
                    "markets": [
                        {
                            "market_ref": "M1",
                            "name": "显示面板制造",
                            "offering_family": "显示面板",
                            "supplier_role": "MANUFACTURE_PRODUCT",
                            "boundary": "覆盖不同显示技术路线的显示面板制造企业。",
                            "member_refs": ["C0"] if omit else ["C0", "C1"],
                        }
                    ]
                }
            )
        if task == "audit":
            return schema.model_validate({"decisions": [{"group_ref": "M1", "decision": decision}]})
        return schema.model_validate(
            {
                "assignments": [
                    {"track_ref": ref, "destination": destination}
                    for ref in (["C0"] if omit else ["C0", "C1"])
                ]
            }
        )

    return complete


def test_merges_and_preserves_core_and_support():
    tracks = candidates()
    for track in tracks:
        track.identity_event_ids = [uuid.uuid4()]
    result, stats = normalize_markets(tracks, {}, client())
    assert len(result) == 1
    assert result[0].topic_ids == [t.topic_ids[0] for t in tracks]
    assert result[0].supporting_topic_ids == [t.supporting_topic_ids[0] for t in tracks]
    assert result[0].identity_event_ids == [t.identity_event_ids[0] for t in tracks]
    assert stats["merged_groups"] == 1


@pytest.mark.parametrize(
    "decision,destination", [("REJECT", "M1"), ("ACCEPT", "KEEP"), ("ACCEPT", "EXCLUDE")]
)
def test_rejected_or_unassigned_candidates_are_kept(decision, destination):
    tracks = candidates()
    result, _ = normalize_markets(tracks, {}, client(decision, destination))
    assert result == tracks


def test_cross_role_assignment_cannot_merge():
    tracks = candidates("DESIGN_PRODUCT")
    result, stats = normalize_markets(tracks, {}, client())
    assert result == tracks
    assert stats["cross_role_assignments_blocked"] == 1


def test_missing_assignment_fails_instead_of_losing_a_candidate():
    with pytest.raises(ValueError):
        normalize_markets(candidates(), {}, client(omit=True))


def test_service_runs_normalizer_and_records_model_calls():
    recorded = []
    response = client()

    class Client:
        def complete_structured(self, *, output_model, **kwargs):
            task = {
                "MarketCatalog": "catalog",
                "MarketAssignments": "assignment_0",
                "MarketDecisions": "audit",
            }[output_model.__name__]
            return SimpleNamespace(
                output=response(task, {}, output_model),
                request_id="test",
                usage={},
                raw_response={},
            )

    session = SimpleNamespace(add=recorded.append, commit=lambda: None, scalar=lambda query: None)
    config = SimpleNamespace(
        stage4=SimpleNamespace(model="test", generation_max_output_tokens=8192)
    )
    service = TrackService(client=Client(), config=config)
    output = TrackBuildOutput(tracks=candidates())
    stats = service._normalize_markets(
        session,
        SimpleNamespace(id=uuid.uuid4()),
        output,
        {},
        {"catalog": "catalog", "assignment": "assignment", "audit": "audit"},
    )
    assert len(output.tracks) == 1
    assert stats["merged_groups"] == 1
    assert len(recorded) == 2
    assert all(run.structured_output for run in recorded)
