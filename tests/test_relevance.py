from pathlib import Path

from track_insight.enums import RelevanceLabel
from track_insight.relevance import classify_relevance, load_relevance_rules

RULES = load_relevance_rules(Path("config/relevance_rules.yaml"))


def test_relevant_requires_event_signal_and_industry_context() -> None:
    result = classify_relevance(
        "人工智能算力券申报通知",
        "支持企业申报算力补贴，推动大模型产业发展。",
        RULES,
    )

    assert result.label == RelevanceLabel.RELEVANT
    assert "application_program" in result.signal_types
    assert "人工智能" in result.industries
    assert result.evidence


def test_high_confidence_exclusion_without_signal_is_irrelevant() -> None:
    result = classify_relevance(
        "事业单位公开招聘工作人员公告",
        "本次计划招聘工作人员三名。",
        RULES,
    )

    assert result.label == RelevanceLabel.IRRELEVANT


def test_uncertain_document_is_kept_for_semantic_review() -> None:
    result = classify_relevance("区域工作动态", "有关单位召开了工作会议。", RULES)

    assert result.label == RelevanceLabel.POSSIBLY_RELEVANT
