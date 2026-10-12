import pytest

from experiments.track_quality.candidate_interface_pilot import market_schema, repair_schema


def unit(scope, anchor):
    return dict(
        product_scope=scope,
        supplier_role="MANUFACTURE_PRODUCT",
        anchors=[anchor],
    )


def test_same_mixed_event_can_support_different_explicit_deliverables():
    schema = repair_schema(
        [
            dict(
                candidate_ref="C0",
                events=[dict(event_ref="E0", summary="企业生产铝板，也生产电缆。")],
            )
        ]
    )
    result = schema.model_validate(
        dict(
            candidates=[
                dict(
                    candidate_ref="C0",
                    units=[unit("铝板", "E0S0"), unit("电缆", "E0S0")],
                    background_event_refs=[],
                )
            ]
        )
    )
    assert len(result.candidates[0].units) == 2
    with pytest.raises(ValueError, match="own source fact"):
        schema.model_validate(
            dict(
                candidates=[
                    dict(
                        candidate_ref="C0",
                        units=[unit("电缆", "invented")],
                        background_event_refs=[],
                    )
                ]
            )
        )


def test_background_material_is_preserved_without_forcing_product_identity():
    schema = repair_schema(
        [dict(candidate_ref="C0", events=[dict(event_ref="E0", summary="建设产业基地")])]
    )
    result = schema.model_validate(
        dict(candidates=[dict(candidate_ref="C0", units=[], background_event_refs=["E0"])])
    )
    assert not result.candidates[0].units
    incomplete = schema.model_validate(
        dict(candidates=[dict(candidate_ref="C0", units=[], background_event_refs=[])])
    )
    assert incomplete.candidates[0].background_event_refs == ["E0"]
    normalized = schema.model_validate(
        dict(candidates=[dict(candidate_ref="C0", units=[], background_event_refs=["invented"])])
    )
    assert normalized.candidates[0].background_event_refs == ["E0"]


def test_market_input_must_account_for_each_repaired_unit():
    schema = market_schema(["C0U0", "C0U1"])
    result = schema.model_validate(dict(markets=[], pending=["C0U0", "C0U1"]))
    assert len(result.pending) == 2
    with pytest.raises(ValueError, match="exactly one"):
        schema.model_validate(dict(markets=[], pending=["C0U0"]))
