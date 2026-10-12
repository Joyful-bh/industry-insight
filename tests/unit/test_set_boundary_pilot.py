import pytest

from experiments.track_quality.set_boundary_pilot import merge_pairs, partition_schema


def candidate(ref, events):
    return dict(candidate_ref=ref, events=[dict(event_ref=e) for e in events])


def market(members, events):
    return dict(
        name="市场", supplier_role="MANUFACTURE_PRODUCT", members=members, core_event_refs=events
    )


def test_partition_requires_complete_candidate_accounting():
    schema = partition_schema([candidate("C0", ["E0"]), candidate("C1", ["E1"])])
    with pytest.raises(ValueError, match="Account for all"):
        schema.model_validate(dict(markets=[market(["C0"], ["E0"])], pending=[]))
    output = schema.model_validate(dict(markets=[market(["C0"], ["E0"])], pending=["C1"]))
    assert output.pending == ["C1"]


def test_each_member_requires_its_own_available_identity_fact():
    schema = partition_schema([candidate("C0", ["E0"]), candidate("C1", ["E1"])])
    with pytest.raises(ValueError, match="Every core member"):
        schema.model_validate(dict(markets=[market(["C0", "C1"], ["E0"])], pending=[]))
    with pytest.raises(ValueError, match="come from"):
        schema.model_validate(dict(markets=[market(["C0", "C1"], ["invented"])], pending=[]))


def test_split_requires_disjoint_facts_and_pending_is_program_normalized():
    schema = partition_schema([candidate("C0", ["E0", "E1"])])
    output = schema.model_validate(
        dict(markets=[market(["C0"], ["E0"]), market(["C0"], ["E1"])], pending=["C0"])
    )
    assert output.pending == []
    assert merge_pairs(output.markets) == set()
    with pytest.raises(ValueError, match="disjoint"):
        schema.model_validate(
            dict(markets=[market(["C0"], ["E0"]), market(["C0"], ["E0"])], pending=[])
        )
