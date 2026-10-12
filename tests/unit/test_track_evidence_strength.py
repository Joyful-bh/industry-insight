from dataclasses import replace

from track_insight.tracks.evidence_strength import EvidenceFact, score_evidence, source_level


def fact(event="1", level="other", document=None, quote=None, confidence=0.9):
    return EvidenceFact(
        event, event, quote or event, document or event, "example.com", level, confidence, True
    )


def test_single_authoritative_document_is_enough():
    assert score_evidence([fact(level="government")])["status"] == "candidate"


def test_single_ordinary_document_is_watchlist():
    assert score_evidence([fact()])["status"] == "watchlist"


def test_distinct_facts_and_documents_promote():
    assert score_evidence([fact("1"), fact("2")])["status"] == "candidate"


def test_duplicate_fact_does_not_promote():
    assert score_evidence([fact(), replace(fact(), document="second")])["status"] == "watchlist"


def test_identical_excerpt_does_not_promote():
    assert (
        score_evidence([fact("1", quote="same"), fact("2", quote="same")])["status"] == "watchlist"
    )


def test_same_document_does_not_promote():
    assert (
        score_evidence([fact("1", document="same"), fact("2", document="same")])["status"]
        == "watchlist"
    )


def test_low_confidence_or_missing_quote_cannot_promote():
    assert score_evidence([fact(level="government", confidence=0.5)])["status"] == "watchlist"
    assert (
        score_evidence([replace(fact(level="government"), has_quote=False)])["status"]
        == "watchlist"
    )


def test_weak_member_does_not_demote_strong_evidence():
    assert (
        score_evidence([fact(level="government"), fact("2", confidence=0.5)])["status"]
        == "candidate"
    )


def test_no_evidence_is_watchlist():
    assert score_evidence([])["status"] == "watchlist"


def test_domain_suffix_is_not_substring_matching():
    assert source_level("https://www.miit.gov.cn/a")[1] == "government"
    assert source_level("https://gov.cn.example.com/a")[1] == "other"
