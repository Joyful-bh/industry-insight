import uuid

from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import (
    CompactTrackBuildOutput,
    TrackAnalysisOutput,
    TrackBuildOutput,
)
from track_insight.tracks.service import (
    _compact_text,
    _event_ids_for_topic_ids,
    _expand_topic_refs,
    _normalize_build_output,
    _validate_build,
)


def test_compact_track_refs_expand_to_topic_ids() -> None:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    compact = CompactTrackBuildOutput.model_validate(
        {
            "tracks": [
                {
                    "name": "工业视觉质检",
                    "refs": ["T1", "T2", "T1", "UNKNOWN"],
                }
            ]
        }
    )

    topics = {
        first_id: type("Topic", (), {"definition": "工业视觉检测设备企业集合。"})(),
        second_id: type("Topic", (), {"definition": "工业视觉检测软件企业集合。"})(),
    }
    session = _CapturingSession([uuid.uuid4(), uuid.uuid4()])
    expanded = _expand_topic_refs(
        session, compact, {"T1": first_id, "T2": second_id}, topics
    )

    assert expanded.tracks[0].topic_ids == [first_id, second_id]
    assert expanded.tracks[0].status == "candidate"
    assert expanded.tracks[0].definition.startswith("围绕工业视觉质检")


def test_track_input_text_is_compact_and_readable() -> None:
    assert _compact_text("  智能\n  算力平台  ", 20) == "智能 算力平台"
    assert _compact_text("甲乙丙丁戊", 4) == "甲乙丙…"


class _Topic:
    def __init__(self, topic_id: uuid.UUID, regions: list[str] | None = None) -> None:
        self.id = topic_id
        self.regions = regions or []


class _CapturingSession:
    def __init__(self, event_ids: list[uuid.UUID]) -> None:
        self.event_ids = event_ids
        self.statement = None

    def scalars(self, statement):
        self.statement = statement
        return self.event_ids


def _track(topic_id: uuid.UUID) -> dict:
    return {
        "name": "工业视觉质检设备与解决方案",
        "definition": "从事工业视觉检测设备、软件及整体解决方案研发和交付的企业集合。",
        "topic_ids": [str(topic_id)],
        "status": "candidate",
        "confidence": 0.9,
    }


def test_track_event_lookup_deduplicates_only_event_ids() -> None:
    event_id = uuid.uuid4()
    session = _CapturingSession([event_id, event_id])
    result = _event_ids_for_topic_ids(session, [uuid.uuid4(), uuid.uuid4()])
    assert result == {event_id}
    assert list(session.statement.selected_columns)[0].name == "event_id"
    assert session.statement._distinct is True


def test_track_build_contract_uses_topic_ids() -> None:
    topic_id = uuid.uuid4()
    output = TrackBuildOutput.model_validate({"tracks": [_track(topic_id)]})
    assert output.tracks[0].topic_ids == [topic_id]


def test_track_analysis_contract_preserves_business_to_it_chain() -> None:
    event_id = uuid.uuid4()
    output = TrackAnalysisOutput.model_validate(
        {
            "enterprise_archetype": "工业视觉设备制造商、质检软件企业和系统解决方案商。",
            "core_products_services": ["工业视觉质检设备与软件"],
            "core_company_types": ["工业视觉设备与解决方案企业"],
            "supporting_company_types": ["视觉算法与光学组件企业"],
            "shared_demand_drivers": ["制造企业自动化质量检测需求"],
            "included_activities": ["视觉检测设备研发制造"],
            "excluded_activities": ["仅使用视觉质检的制造企业"],
            "chain_roles": ["设备供应商", "系统集成商"],
            "observable_company_features": ["存在产品目录", "公开工业质检项目案例"],
            "possible_it_needs": ["AI训练与推理算力"],
            "summary": "候选赛道围绕工业视觉设备和解决方案企业形成。",
            "why_now": "近期出现产业集群认定和园区启用等不同类型信号。",
            "activity_assessment": {
                "label": "recently_active",
                "historical_comparability": False,
                "basis": "存在近期信号，但缺少可比历史基线。",
            },
            "industry_chain_analysis": "企业主要位于设备、软件和系统交付环节。",
            "smb_value_analysis": [
                {
                    "business_activity": "视觉模型研发和现场交付",
                    "workload": "图像训练、推理和项目数据管理",
                    "it_need": "AI工作站、边缘计算和存储",
                    "coverage_path": "按研发及项目团队提供标准化配置",
                }
            ],
            "evidence_event_ids": [str(event_id)],
            "uncertainties": ["缺少企业规模分布"],
        }
    )
    assert output.smb_value_analysis[0].it_need.startswith("AI工作站")


def test_track_build_allows_topics_to_remain_unassigned_implicitly() -> None:
    topic_id = uuid.uuid4()
    output = TrackBuildOutput.model_validate({"tracks": []})
    _validate_build(None, output, [_Topic(topic_id)], load_poc_config())


def test_track_build_accepts_one_supporting_event() -> None:
    topic_id = uuid.uuid4()
    output = TrackBuildOutput.model_validate({"tracks": [_track(topic_id)]})
    session = _CapturingSession([uuid.uuid4()])

    _validate_build(session, output, [_Topic(topic_id)], load_poc_config())


def test_track_build_normalizes_region_identity_without_failing_batch() -> None:
    topic_id = uuid.uuid4()
    item = _track(topic_id)
    item["name"] = "北京市工业视觉质检"
    output = TrackBuildOutput.model_validate({"tracks": [item]})
    session = _CapturingSession([uuid.uuid4()])

    adjustments = _normalize_build_output(
        session, output, [_Topic(topic_id, ["北京市"])]
    )

    assert output.tracks[0].name == "工业视觉质检"
    assert output.tracks[0].status == "watchlist"
    assert adjustments[0]["action"] == "region_removed_and_marked_watchlist"


def test_track_build_drops_policy_instrument_identity() -> None:
    topic_id = uuid.uuid4()
    item = _track(topic_id)
    item["name"] = "先进制造业税收优惠"
    output = TrackBuildOutput.model_validate({"tracks": [item]})
    session = _CapturingSession([uuid.uuid4()])

    adjustments = _normalize_build_output(session, output, [_Topic(topic_id)])

    assert output.tracks == []
    assert adjustments[0]["reason"] == "identity describes a policy instrument or transient signal"
