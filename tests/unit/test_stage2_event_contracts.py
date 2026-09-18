import pytest
from pydantic import ValidationError

from track_insight.events.contracts import PageEventOutput
from track_insight.events.service import _sanitize_evidence


def test_event_contract_rejects_events_without_page_sufficiency() -> None:
    with pytest.raises(ValidationError):
        PageEventOutput.model_validate(
            {
                "page_review": {
                    "relevance": "relevant",
                    "document_type": "policy",
                    "content_sufficient": False,
                    "reason": "正文不完整",
                    "regions": [],
                    "industries": [],
                },
                "events": [
                    {
                        "event_type": "policy_release",
                        "title": "制造业政策发布",
                        "summary": "主管部门发布支持制造业发展的新政策措施。",
                        "signal_date": None,
                        "date_precision": "unknown",
                        "regions": [],
                        "industries": ["制造业"],
                        "entities": [],
                        "chain_roles": [],
                        "smb_relevance": "unclear",
                        "smb_reason": "正文信息不足",
                        "confidence": 0.5,
                        "evidence": ["这是一段长度大于二十个字符的原文证据文本。"],
                        "evidence_coverage": {
                            "subject": [0],
                            "action": [0],
                            "date": [],
                            "numbers": [],
                        },
                    }
                ],
            }
        )


def test_event_contract_accepts_empty_event_list_for_irrelevant_page() -> None:
    result = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "irrelevant",
                "document_type": "other",
                "content_sufficient": True,
                "reason": "页面只介绍机构职能",
                "regions": [],
                "industries": [],
            },
            "events": [],
        }
    )
    assert result.events == []


def test_event_contract_normalizes_date_precision_and_accepts_short_auxiliary_evidence() -> None:
    result = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "relevant",
                "document_type": "policy",
                "content_sufficient": True,
                "reason": "正文包含明确的政策发布事实",
                "regions": ["北京市"],
                "industries": ["制造业"],
            },
            "events": [
                {
                    "event_type": "policy_release",
                    "title": "北京市发布制造业支持政策",
                    "summary": "北京市有关部门发布支持制造业发展的政策文件。",
                    "signal_date": "2026-09-16",
                    "date_precision": "unknown",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                    "entities": [{"name": "北京市财政局", "type": "government"}],
                    "chain_roles": [],
                    "smb_relevance": "high",
                    "smb_reason": "政策直接支持制造业企业发展",
                    "confidence": 0.9,
                    "evidence": ["北京市财政局", "2026年9月16日发布制造业支持政策。"],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [1],
                        "date": [1],
                        "numbers": [],
                    },
                }
            ],
        }
    )

    assert result.events[0].date_precision.value == "day"
    assert result.events[0].evidence[0] == "北京市财政局"


def test_evidence_sanitizer_keeps_verbatim_support_and_drops_unsupported_events() -> None:
    body = (
        "北京市经济和信息化局印发制造业数字化转型实施方案，"
        "推动北京市制造业率先实现数字化转型。^[1]^"
    )
    output = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "relevant",
                "document_type": "policy",
                "content_sufficient": True,
                "reason": "正文包含明确政策发布事实",
                "regions": ["北京市"],
                "industries": ["制造业"],
            },
            "events": [
                {
                    "event_type": "policy_release",
                    "title": "北京市印发制造业数字化转型实施方案",
                    "summary": "北京市经济和信息化局印发制造业数字化转型实施方案。",
                    "signal_date": None,
                    "date_precision": "unknown",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                    "entities": [],
                    "chain_roles": [],
                    "smb_relevance": "high",
                    "smb_reason": "政策涉及制造业数字化改造",
                    "confidence": 0.9,
                    "evidence": [
                        "北京市经济和信息化局印发制造业数字化转型实施方案，推动北京市制造业率先实现数字化转型。"
                    ],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [0],
                        "date": [],
                        "numbers": [],
                    },
                },
                {
                    "event_type": "application_or_funding",
                    "title": "北京市提供专项资金支持",
                    "summary": "页面声称北京市提供专项资金支持制造业数字化转型。",
                    "signal_date": None,
                    "date_precision": "unknown",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                    "entities": [],
                    "chain_roles": [],
                    "smb_relevance": "high",
                    "smb_reason": "可能形成企业资金支持",
                    "confidence": 0.8,
                    "evidence": ["页面明确设立制造业数字化转型专项资金。"],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [0],
                        "date": [],
                        "numbers": [],
                    },
                },
            ],
        }
    )

    _sanitize_evidence(output, body)

    assert len(output.events) == 1
    assert output.events[0].evidence == [
        "北京市经济和信息化局印发制造业数字化转型实施方案，推动北京市制造业率先实现数字化转型。"
    ]


def test_event_contract_discards_invalid_evidence_coverage_index() -> None:
    result = PageEventOutput.model_validate(
            {
                "page_review": {
                    "relevance": "relevant",
                    "document_type": "project",
                    "content_sufficient": True,
                    "reason": "正文包含项目投产事实",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                },
                "events": [
                    {
                        "event_type": "project_progress",
                        "title": "制造项目投产",
                        "summary": "某企业在北京市启动制造项目投产。",
                        "signal_date": None,
                        "date_precision": "unknown",
                        "regions": ["北京市"],
                        "industries": ["制造业"],
                        "entities": [],
                        "chain_roles": [],
                        "smb_relevance": "medium",
                        "smb_reason": "可能带来产业链需求",
                        "confidence": 0.8,
                        "evidence": ["某企业在北京市启动制造项目并正式投产。"],
                        "evidence_coverage": {
                            "subject": [0],
                            "action": [1],
                            "date": [],
                            "numbers": [],
                        },
                    }
                ],
            }
        )

    assert result.events[0].evidence_coverage.subject == [0]
    assert result.events[0].evidence_coverage.action == []


def test_evidence_sanitizer_requires_numeric_and_date_support() -> None:
    body = "2026年9月16日，北京制造公司宣布项目投产，总投资10亿元。"
    output = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "relevant",
                "document_type": "project",
                "content_sufficient": True,
                "reason": "正文包含明确项目投产事实",
                "regions": ["北京市"],
                "industries": ["制造业"],
            },
            "events": [
                {
                    "event_type": "project_progress",
                    "title": "北京制造公司项目投产",
                    "summary": "北京制造公司项目投产，总投资10亿元。",
                    "signal_date": "2026-09-16",
                    "date_precision": "day",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                    "entities": [{"name": "北京制造公司", "type": "company"}],
                    "chain_roles": [],
                    "smb_relevance": "medium",
                    "smb_reason": "可能形成配套需求",
                    "confidence": 0.9,
                    "evidence": [body],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [0],
                        "date": [0],
                        "numbers": [0],
                    },
                }
            ],
        }
    )

    _sanitize_evidence(output, body)

    assert len(output.events) == 1


def test_evidence_sanitizer_drops_event_when_number_is_not_in_covered_quote() -> None:
    body = "北京制造公司宣布项目正式投产，项目位于北京市。"
    output = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "relevant",
                "document_type": "project",
                "content_sufficient": True,
                "reason": "正文包含项目投产事实",
                "regions": ["北京市"],
                "industries": ["制造业"],
            },
            "events": [
                {
                    "event_type": "project_progress",
                    "title": "北京制造公司项目投产",
                    "summary": "北京制造公司项目投产，总投资10亿元。",
                    "signal_date": None,
                    "date_precision": "unknown",
                    "regions": ["北京市"],
                    "industries": ["制造业"],
                    "entities": [{"name": "北京制造公司", "type": "company"}],
                    "chain_roles": [],
                    "smb_relevance": "medium",
                    "smb_reason": "可能形成配套需求",
                    "confidence": 0.7,
                    "evidence": [body],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [0],
                        "date": [],
                        "numbers": [0],
                    },
                }
            ],
        }
    )

    _sanitize_evidence(output, body)

    assert output.events == []


def test_evidence_sanitizer_splits_abbreviated_quotes_into_verbatim_fragments() -> None:
    body = "主管部门发布资金指南。单个企业最高补贴100万元，申报截止到9月30日。"
    output = PageEventOutput.model_validate(
        {
            "page_review": {
                "relevance": "relevant",
                "document_type": "application_or_funding",
                "content_sufficient": True,
                "reason": "正文包含资金申报事项",
                "regions": [],
                "industries": ["制造业"],
            },
            "events": [
                {
                    "event_type": "application_or_funding",
                    "title": "主管部门发布资金指南",
                    "summary": "主管部门发布资金指南，单个企业最高补贴100万元。",
                    "signal_date": None,
                    "date_precision": "unknown",
                    "regions": [],
                    "industries": ["制造业"],
                    "entities": [{"name": "主管部门", "type": "government"}],
                    "chain_roles": [],
                    "smb_relevance": "high",
                    "smb_reason": "企业可申请资金支持",
                    "confidence": 0.9,
                    "evidence": [
                        "主管部门发布资金指南...单个企业最高补贴100万元，申报截止到9月30日。"
                    ],
                    "evidence_coverage": {
                        "subject": [0],
                        "action": [0],
                        "date": [],
                        "numbers": [0],
                    },
                }
            ],
        }
    )

    _sanitize_evidence(output, body)

    assert len(output.events) == 1
    assert output.events[0].evidence == [
        "主管部门发布资金指南",
        "单个企业最高补贴100万元，申报截止到9月30日。",
    ]
