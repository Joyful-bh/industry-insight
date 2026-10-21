import pytest

from experiments.track_quality.object_first_pilot import (
    Relations,
    SupplyObject,
    checked_schema,
    combine_objects,
    components,
    extraction_schema,
    merged_pairs,
    neighbor_pairs,
)


def supply(name, refs, role="MANUFACTURE_PRODUCT"):
    return SupplyObject(object=name, role=role, event_refs=refs)


def test_identity_references_must_exist_in_input():
    schema = extraction_schema({"E1"})
    schema.model_validate({"objects": [supply("product", ["E1"]).model_dump()]})
    with pytest.raises(ValueError, match="supplied events"):
        schema.model_validate({"objects": [supply("product", ["invented"]).model_dump()]})


def test_exact_objects_combine_evidence_without_crossing_roles():
    result = combine_objects(
        [
            supply("product", ["E1"]),
            supply("product", ["E2"]),
            supply("product", ["E3"], "DESIGN_PRODUCT"),
        ]
    )
    assert len(result) == 2
    assert {tuple(row.event_refs) for row in result} == {("E1", "E2"), ("E3",)}


def test_neighbor_recall_is_bounded_and_does_not_cross_roles():
    rows = [
        supply("product", ["E1"]),
        supply("variant", ["E2"]),
        supply("design", ["E3"], "DESIGN_PRODUCT"),
    ]
    assert neighbor_pairs(rows, [[1, 0], [1, 0], [1, 0]]) == [(0, 1)]


def test_containment_does_not_become_a_merge():
    pairs = {"P0": (0, 1), "P1": (1, 2)}
    result = components(4, pairs, {"P0": "LEFT_CONTAINS", "P1": "SAME_MARKET"})
    assert result == [[0], [1, 2], [3]]
    assert sorted(i for group in result for i in group) == list(range(4))


def test_relation_batch_must_account_for_every_pair():
    schema = checked_schema(Relations, "decisions", "pair_ref", {"P0", "P1"})
    with pytest.raises(ValueError, match="exactly"):
        schema.model_validate({"decisions": [{"pair_ref": "P0", "relation": "DISTINCT"}]})


def test_stability_counts_merge_pairs_not_singleton_agreement():
    assert merged_pairs([[0, 1, 2], [3]]) == {(0, 1), (0, 2), (1, 2)}
    assert merged_pairs([[3], [2, 0, 1]]) == merged_pairs([[0, 1, 2], [3]])


def test_single_character_material_is_a_valid_object():
    assert supply("铝", ["E1"]).object == "铝"
