from pathlib import Path

from track_insight.enums import RelevanceLabel
from track_insight.relevance import classify_relevance, load_relevance_rules

RULES = load_relevance_rules(Path("config/relevance_rules.yaml"))


def test_relevant_requires_event_signal_and_industry_context() -> None:
    result = classify_relevance(
        "智能制造项目申报通知",
        "支持制造业企业申报补贴，推动工业互联网改造生产线。",
        RULES,
    )

    assert result.label == RelevanceLabel.RELEVANT
    assert "application_program" in result.signal_types
    assert "智能制造与装备" in result.industries
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


def test_multiple_event_words_without_manufacturing_context_need_semantic_review() -> None:
    result = classify_relevance(
        "职业教育项目申报和认定通知",
        "学校可以申报教学改革项目，验收后予以认定。",
        RULES,
    )

    assert result.label == RelevanceLabel.POSSIBLY_RELEVANT
