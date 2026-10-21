import pytest

from experiments.track_quality.model_capacity_diagnostic import SCOPES, Output, pairs


def test_diagnostic_accounts_for_all_sources_without_forcing_merges():
    output = Output.model_validate(dict(markets=[], pending=[f"C{i}" for i in SCOPES]))
    assert pairs(output) == set()
    with pytest.raises(ValueError, match="Exactly one"):
        Output.model_validate(dict(markets=[], pending=[]))


def test_diagnostic_rejects_duplicate_membership_and_reports_actual_merge_pairs():
    market = dict(
        name="铝加工",
        supplier_role="MANUFACTURE_PRODUCT",
        members=["C14", "C32"],
        boundary="铝加工产品",
    )
    pending = [f"C{i}" for i in SCOPES if i not in (14, 32)]
    output = Output.model_validate(dict(markets=[market], pending=pending))
    assert pairs(output) == {("C14", "C32")}
    with pytest.raises(ValueError, match="Exactly one"):
        Output.model_validate(dict(markets=[market], pending=pending + ["C14"]))
