from track_insight.core.fingerprints import fingerprint


def test_fingerprint_is_stable_across_mapping_order() -> None:
    left = fingerprint({"region": "北京", "signals": ["投产", "扩产"]}, "v1")
    right = fingerprint({"signals": ["投产", "扩产"], "region": "北京"}, "v1")
    assert left == right
    assert left != fingerprint({"region": "北京", "signals": ["投产"]}, "v1")
